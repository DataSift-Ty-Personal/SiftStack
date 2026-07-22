"""NoticeData → DataSift Open API upload (the autonomous-pipeline spine).

Maps an enriched NoticeData record to the validated property-create payload and
uploads it via the REST API ([[datasift_api]]), already tagged `Courthouse Data`
+ notice type. Replaces the manual "download every Amplify DP PDF and hand-
upload" step.

HARD BOUNDARY ([[no_auto_mail_rule]]): this ONLY creates clean, verified, tagged
records in DataSift. It NEVER fires mail/SMS/outreach — Mike deploys all mail.
The niche presets are just the queue he works from.

Reuses datasift_formatter's proven mapping (_build_tags, _get_contact_info) so
the API path and the legacy CSV path stay identical in field/tag logic.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def _collect_phones(notice) -> list[dict]:
    phones = []
    for f in ("mobile_1", "mobile_2", "mobile_3", "mobile_4", "mobile_5"):
        v = (getattr(notice, f, "") or "").strip()
        if v:
            phones.append({"number": v, "type": "MOBILE"})
    for f in ("landline_1", "landline_2", "landline_3"):
        v = (getattr(notice, f, "") or "").strip()
        if v:
            phones.append({"number": v, "type": "LANDLINE"})
    if not phones:
        pp = (getattr(notice, "primary_phone", "") or "").strip()
        if pp:
            phones.append({"number": pp, "type": "UNKNOWN"})
    return phones


def _collect_emails(notice) -> list[str]:
    return [e for f in ("email_1", "email_2", "email_3", "email_4", "email_5")
            if (e := (getattr(notice, f, "") or "").strip())]


def validate_notice_for_upload(notice) -> list[str]:
    """Data-quality gate — the core fields Aaron requires correct BEFORE upload.
    Returns a list of problems (empty = OK to upload)."""
    from datasift_formatter import _get_contact_info
    problems = []
    prop = (getattr(notice, "address", "") or "").strip()
    if not prop:
        problems.append("no property address")
    elif not prop[0].isdigit():
        problems.append(f"property address has no house number ('{prop[:30]}')")
    if not (getattr(notice, "notice_type", "") or "").strip():
        problems.append("no notice_type (can't tag foreclosure/probate/etc.)")

    contact = _get_contact_info(notice)
    if not (contact["street"] or "").strip():
        problems.append("no mailing address")
    # Deceased owner → we must have the executor/DM name to mail the right person
    if (getattr(notice, "owner_deceased", "") or "") == "yes":
        if not (contact["first"] or contact["last"]):
            problems.append("deceased owner but no executor/decision-maker name")
    elif not (contact["first"] or contact["last"]):
        problems.append("no contact name (owner/executor)")
    return problems


def notice_to_api_payload(notice) -> dict:
    """Map an enriched NoticeData to the /property/ CREATE payload.

    Create carries only what the endpoint reliably stores: cleaned property +
    owner address and the rich Notes. Tags and Lists are applied AFTER create
    via the array add-tags/add-lists endpoints (the create endpoint stores a
    comma-string as ONE literal tag/list — that's the "FTM,Probate" bug Mike
    flagged). Custom-field values (Date Added + enrichment) are written after
    create too, via update_custom_field_values(). Property address is cleaned
    (house-number ranges collapsed, "<City> City" normalized).
    """
    from datasift_formatter import _get_contact_info, clean_city, clean_street
    from datasift_formatter import _notes_for_record
    contact = _get_contact_info(notice)

    def _addr(street, city, state, zc):
        return {"street": clean_street(street or ""), "city": clean_city(city or ""),
                "state": (state or "").strip(), "postal_code": (zc or "").strip(),
                "country": "US"}

    # Same rich Notes the CSV path builds (deceased header + DM + heir map +
    # property/source), NOT the old one-line stub.
    notes = (_notes_for_record(notice) or "").strip() or \
        f"{notice.notice_type} — {notice.county} courthouse (SiftStack API upload)"

    return {
        "address": _addr(notice.address, notice.city, notice.state, notice.zip),
        "owner": {
            "first_name": contact["first"],
            "last_name": contact["last"],
            "address": _addr(contact["street"], contact["city"], contact["state"], contact["zip"]),
            "phones": _collect_phones(notice),
            "emails": _collect_emails(notice),
        },
        "notes": notes,
    }


def _custom_field_values(notice, title_map: dict) -> dict:
    """Resolve our per-record custom-field data to {field_id: value} using the
    account's EXISTING custom-field definitions (Mike's manual uploads already
    created "Notice Type", "County", "Date Added", "Decision Maker", …). Reuses
    _build_row so the API path and CSV path are byte-identical in field logic.

    Any column whose title isn't a real custom field in the account (core
    address, phones, Tags/Lists/Notes, DataSift-enrich-owned built-ins like
    Estimated Value) is silently skipped — it simply won't be in title_map.
    """
    if not title_map:
        return {}
    from datasift_formatter import _build_row, _notes_for_record
    row = _build_row(notice, notes_override=_notes_for_record(notice))
    out: dict[str, str] = {}
    for title, value in row.items():
        fid = title_map.get(title.strip().lower())
        if fid and value not in (None, ""):
            out[fid] = str(value)
    return out


def upload_notices(api, notices, verify=True) -> dict:
    """Create verified, tagged records in DataSift via the API. Skips records
    failing the data-quality gate. NEVER fires mail. Returns a summary.

    Per record: create (clean addr + owner + rich notes) → add-lists(array) →
    add-tags(array) → write custom-field values. Each post-create step is
    non-fatal: a record still lands clean even if tag/list/field writes fail,
    and the failure is counted in result["partial"].

    api: a datasift_api.DataSiftAPI instance (from_config()).
    """
    from datasift_formatter import build_list_names, build_tag_names

    result = {"created": [], "errors": [], "skipped": [], "partial": [],
              "before": None, "after": None}
    if verify:
        try:
            result["before"] = api.count_properties()
        except Exception as e:
            logger.warning("count_properties (before) failed: %s", e)

    # Resolve custom-field titles → ids ONCE (definitions are account-wide).
    title_map: dict = {}
    try:
        title_map = api.custom_field_title_map()
        logger.info("Resolved %d custom-field definitions for value writes", len(title_map))
    except Exception as e:
        logger.warning("Could not load custom-field definitions (%s) — "
                       "records will still land with tags/lists/notes", e)

    for n in notices:
        problems = validate_notice_for_upload(n)
        if problems:
            result["skipped"].append({"address": getattr(n, "address", ""), "problems": problems})
            continue
        try:
            rec = api.create_property(notice_to_api_payload(n))
        except Exception as e:
            result["errors"].append({"address": getattr(n, "address", ""), "error": str(e)})
            continue

        uuid = rec.get("uuid")
        result["created"].append({"address": n.address, "uuid": uuid, "type": rec.get("type")})

        # ── post-create writes (non-fatal) ──
        issues = []
        if uuid:
            lists = build_list_names(n)
            if lists:
                try:
                    api.add_lists_to_property(uuid, lists)
                except Exception as e:
                    issues.append(f"lists: {e}")
            tags = build_tag_names(n)
            if tags:
                try:
                    api.add_tags_to_property(uuid, tags)
                except Exception as e:
                    issues.append(f"tags: {e}")
            cf = _custom_field_values(n, title_map)
            if cf:
                try:
                    api.update_custom_field_values(uuid, cf)
                except Exception as e:
                    issues.append(f"custom-fields: {e}")
        if issues:
            result["partial"].append({"address": n.address, "uuid": uuid, "issues": issues})

    if verify:
        try:
            result["after"] = api.count_properties()
        except Exception as e:
            logger.warning("count_properties (after) failed: %s", e)

    logger.info(
        "API upload: %d created, %d skipped (quality gate), %d errors, %d with partial post-writes",
        len(result["created"]), len(result["skipped"]), len(result["errors"]),
        len(result["partial"]),
    )
    if result["partial"]:
        logger.warning("Partial post-create writes (record exists, some tags/lists/fields failed): %s",
                       [p["address"] for p in result["partial"][:10]])
    return result
