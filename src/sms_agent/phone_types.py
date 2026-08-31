"""One Trestle pass that answers the two questions blocking the FTM book.

    python src/sms_agent/cli.py phone-types --preset "FTM - 02 Ready to Call"
    python src/sms_agent/cli.py phone-types --preset "FTM - 02 Ready to Call" --commit

**Litigator risk.** Ty, 2026-08-31: "DNC is okay, but the litigation list here
on sellers is what we'd want to suppress throughout the entire process, and
then we can deploy them out." A TCPA serial plaintiff is the one number on this
list that can cost real money, and unlike the do-not-call scrub it is knowable.
A hit is written into the local suppression table, which `seed.build` and
`worker.drain_outbox` already consult before every single send, so one row
blocks outreach, replies and any program written later. It is also tagged on
the phone in DataSift so the dialer and the mail lanes can see it, because "the
entire process" is more than SMS.

**Line type.** Every phone on the FTM book is type UNKNOWN (measured: 151
phones across 25 records, zero MOBILE), which is why the campaign's mobile-only
rule dropped all 604 records. `seed.textable_line` now defers to the dial tier
when the type is unknown, and this pass removes the need for that deference by
writing the real value back.

Both come from ONE Trestle call per number (`add_ons=litigator_checks` rides
along with `line_type`), so checking litigators costs nothing extra once the
line-type pass is being run anyway.

Scope is the cost control: only phones that are UNKNOWN **and** carry a Dial
First/Second tag, because those are the only ones the campaign will ever text.
About 44% of the phones on the sample, so roughly 1,600 numbers for the FTM
book. State is keyed by NUMBER, not record, so a number appearing on three
records costs one call.
"""
from __future__ import annotations

import argparse
import json
import logging
import time
from pathlib import Path

from . import config, crm, store

log = logging.getLogger(__name__)

STATE_PATH = Path(config.DATA_DIR) / "phone_types.json"

# Trestle's line types. Only the two we are sure map onto DataSift's enum get
# written; a VOIP number is genuinely textable but promoting it to MOBILE is
# the same guess in the other direction, so it stays UNKNOWN unless asked.
SURE_TYPES = {"mobile": "MOBILE", "landline": "LANDLINE"}
VOIP_TYPES = {"fixedvoip", "nonfixedvoip", "voip"}

LITIGATOR_TAG = "Litigator"


def _load() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except ValueError:
            log.warning("state file unreadable; starting fresh")
    return {}


def _save(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=1), encoding="utf-8")


TRESTLE_ENDPOINT = "https://api.trestleiq.com/3.1/phone_intel"


def _trestle_key() -> str:
    key = (config._env("TRESTLE_PAID_API_KEY")
           or config._env("TRESTLE_API_KEY")
           or config._env("TRESTLE_FREE_API_KEY"))
    if not key:
        raise RuntimeError("no TRESTLE_PAID_API_KEY / TRESTLE_API_KEY in the environment")
    return key


def call_trestle(number: str, key: str) -> dict:
    """One phone_intel lookup, asking for the litigator add-on.

    `src/phone_validator.py` already has this call and would be the obvious
    thing to import, but it imports the project-wide `src/config.py` and the
    agent's container deliberately ships only the `sms_agent` package (see
    deploy/Dockerfile). Importing it when present and falling back when absent
    would mean this behaves differently on the workstation than in production,
    which is a worse failure than fifteen duplicated lines.

    line_type and add_ons.litigator_checks come back together, so the litigator
    answer costs nothing on top of the line-type pass.
    """
    import requests

    for attempt in range(4):
        try:
            resp = requests.get(
                TRESTLE_ENDPOINT,
                params={"phone": number, "add_ons": "litigator_checks"},
                headers={"x-api-key": key, "Accept": "application/json"},
                timeout=20,
            )
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code in (429, 500, 502, 503, 504):
                time.sleep(2 ** attempt)
                continue
            return {"error": f"HTTP {resp.status_code}: {resp.text[:120]}"}
        except Exception as exc:  # noqa: BLE001
            if attempt == 3:
                return {"error": str(exc)[:120]}
            time.sleep(2 ** attempt)
    return {"error": "exhausted retries"}


def targets(preset: str, limit: int = 0) -> list[dict]:
    """Phones worth spending a Trestle call on, with their record and owner."""
    must, matched = crm.resolve_preset(preset)
    if not must:
        raise RuntimeError(f"preset {preset!r} not found")
    log.info("reading %r", matched)

    out: list[dict] = []
    seen_numbers: set = set()
    for rec_row in crm.fetch_cohort(must, limit=limit):
        uuid = rec_row.get("uuid")
        if not uuid:
            continue
        rec = crm.get_record(uuid)
        if not rec:
            continue
        owner = rec.get("owner") if isinstance(rec.get("owner"), dict) else {}
        for p in owner.get("phones") or []:
            if not isinstance(p, dict):
                continue
            number = store.clean_phone(p.get("number"))
            if len(number) != 10:
                continue
            tier = ""
            for tag in (p.get("tags") or []):
                name = (tag.get("title") or tag.get("name") or tag.get("tag")
                        if isinstance(tag, dict) else str(tag))
                if name in seed_tiers():
                    tier = name
                    break
            if not tier:
                continue
            if (p.get("type") or "UNKNOWN").upper() != "UNKNOWN":
                continue
            out.append({"number": number, "record_uuid": uuid,
                        "owner_uuid": owner.get("uuid") or "", "tier": tier})
            seen_numbers.add(number)
    log.info("%d phone(s) worth checking, %d distinct numbers", len(out), len(seen_numbers))
    return out


def seed_tiers() -> set:
    from . import seed

    return seed.ALLOWED_DIAL_TIERS


def run(preset: str, limit: int = 0, commit: bool = False,
        voip_as: str = "unknown") -> dict:
    """Score the pool, suppress litigators, write line types back."""
    key = _trestle_key()
    state = _load()
    rows = targets(preset, limit=limit)

    checked = cached = litigators = typed = errors = 0
    by_type: dict = {}

    for i, row in enumerate(rows, 1):
        number = row["number"]
        known = state.get(number)
        if known is None:
            if not commit:
                # A dry run must not spend money. Report what it would check.
                checked += 1
                continue
            data = call_trestle(number, key)
            if "error" in data and not data.get("is_valid"):
                errors += 1
                log.warning("  %s: %s", number, str(data.get("error"))[:80])
                continue
            addons = data.get("add_ons") or {}
            lit = None
            if isinstance(addons, dict):
                checks = addons.get("litigator_checks") or {}
                lit = checks.get("phone.is_litigator_risk", checks.get("is_litigator"))
            known = {
                "trestle_line_type": data.get("line_type"),
                "litigator": bool(lit),
                "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "written_to": [],
            }
            state[number] = known
            checked += 1
            if i % 25 == 0:
                _save(state)
        else:
            cached += 1

        raw = str(known.get("trestle_line_type") or "").lower().replace(" ", "")
        by_type[raw or "unknown"] = by_type.get(raw or "unknown", 0) + 1

        # 1. Litigator suppression. This is the part Ty asked for, and it is
        #    deliberately written to the LOCAL suppression table rather than a
        #    campaign-specific filter: every send path already checks it, so
        #    one row covers outreach, replies and anything built later.
        if known.get("litigator"):
            litigators += 1
            if commit and not store.is_suppressed(number):
                store.suppress(number, config.LITIGATOR_SUPPRESSION_REASON)
                log.info("  SUPPRESSED litigator %s", number)
            if commit and row.get("record_uuid"):
                # Visible to the dialer and the mail lanes too, since "the
                # entire process" is more than this program.
                crm.add_phone_tag(row["record_uuid"], number, LITIGATOR_TAG)
            continue

        # 2. Line type, only where Trestle was unambiguous.
        sift_type = SURE_TYPES.get(raw)
        if sift_type is None and raw in VOIP_TYPES and voip_as == "mobile":
            sift_type = "MOBILE"
        if not sift_type:
            continue
        if row["record_uuid"] in known.get("written_to", []):
            continue
        if commit:
            res = crm.set_phone_type(row["record_uuid"], number, sift_type)
            if res.get("verified"):
                typed += 1
                known.setdefault("written_to", []).append(row["record_uuid"])
            elif res.get("error"):
                errors += 1
                log.warning("  type write failed on %s: %s", number, res["error"])
        else:
            typed += 1

    if commit:
        _save(state)

    return {
        "phones_considered": len(rows),
        "trestle_calls": checked,
        "already_known": cached,
        "litigators_suppressed": litigators,
        "line_types_written": typed,
        "errors": errors,
        "line_type_mix": dict(sorted(by_type.items(), key=lambda kv: -kv[1])),
        "committed": commit,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--preset", default="FTM - 02 Ready to Call")
    ap.add_argument("--limit", type=int, default=0, help="cap the records read")
    ap.add_argument("--commit", action="store_true",
                    help="call Trestle and write. Without it nothing is spent.")
    ap.add_argument("--voip-as", choices=("mobile", "unknown"), default="unknown",
                    help="what to write for a VOIP line (default: leave it unknown)")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    store.init()
    out = run(args.preset, limit=args.limit, commit=args.commit, voip_as=args.voip_as)
    print(json.dumps(out, indent=2))
    if not args.commit:
        print("\ndry run: no Trestle calls made, nothing written. Add --commit.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
