"""12-preset niche sequential marketing system with 3-day cycle orchestration.

Implements the DataSift niche sequential marketing workflow:
  Day 1: Text → Call → Trigger mailer
  Day 2: Call (different script) → Text variation
  Day 3: Final call → Final text → Mailer arrives

Channels escalate by cost: SMS ($0.01) → Call ($0.03-0.06) → Mail ($0.50-2.00) → Deep Prospecting ($1.50-4.00)

12 filter presets in "00 Niche Sequential Marketing" folder:
  00. Needs Skip Traced
  01. Ready to Text
  02-04. Needs Called Day 1/2/3
  05. Needs Mailed
  06. Needs Deep Prospecting
  07. Callback Scheduled
  08. Hot Lead
  09. Not Interested
  10. Bad Data
  11. Completed Cycle

Usage:
  python src/main.py niche-sequential --list-name "Foreclosure" --channel sms --day 1
  python src/main.py niche-sequential --action setup-presets
"""

import csv
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import config

logger = logging.getLogger(__name__)

# ── Preset definitions ────────────────────────────────────────────────

PRESET_FOLDER = "00 Niche Sequential Marketing"

PRESETS = [
    {
        "number": "00",
        "name": "00. Needs Skip Traced",
        "description": "New records without phone data — route to skip trace",
        "filter": {"has_phone": False, "has_tag": "Courthouse Data"},
        "action": "Run Tracerfy batch skip trace → phone validate via Trestle",
    },
    {
        "number": "01",
        "name": "01. Ready to Text",
        "description": "Has phone (Dial First/Second tier), not yet texted",
        "filter": {"has_phone": True, "phone_tier": ["Dial First", "Dial Second"],
                   "not_tag": "sms_sent"},
        "action": "Send Day 1 SMS via Launch Control / REISimpli",
    },
    {
        "number": "02",
        "name": "02. Needs Called Day 1",
        "description": "Texted, not called yet — first call attempt",
        "filter": {"has_tag": "sms_sent", "not_tag": "called_day1"},
        "action": "Call all numbers, leave voicemail, log disposition",
    },
    {
        "number": "03",
        "name": "03. Needs Called Day 2",
        "description": "Called once, no answer — second attempt with different script",
        "filter": {"has_tag": "called_day1", "not_tag": "called_day2"},
        "action": "Call with alternate script, leave new voicemail",
    },
    {
        "number": "04",
        "name": "04. Needs Called Day 3",
        "description": "Called twice, final attempt — urgency-focused",
        "filter": {"has_tag": "called_day2", "not_tag": "called_day3"},
        "action": "Final call pass, urgency voicemail, final text",
    },
    {
        "number": "05",
        "name": "05. Needs Mailed",
        "description": "Exhausted calls, ready for direct mail piece",
        "filter": {"has_tag": "called_day3", "not_tag": "mailed"},
        "action": "Export mail-ready CSV, send handwritten letter ($1.75)",
    },
    {
        "number": "06",
        "name": "06. Needs Deep Prospecting",
        "description": "Mail returned / no response after full cycle",
        "filter": {"has_tag": "cycle_complete", "not_tag": "dp_complete",
                   "status_not": "Sold"},
        "action": "Route to deep_prospector.py for Level 1-3 research",
    },
    {
        "number": "07",
        "name": "07. Callback Scheduled",
        "description": "Appointment set during a call — follow up on schedule",
        "filter": {"has_tag": "callback_scheduled"},
        "action": "Call at scheduled time, update disposition",
    },
    {
        "number": "08",
        "name": "08. Hot Lead",
        "description": "Expressed interest during contact — route to closer",
        "filter": {"has_tag": "hot"},
        "action": "Immediate closer assignment, schedule appointment",
    },
    {
        "number": "09",
        "name": "09. Not Interested",
        "description": "Declined — schedule 90-day recycle",
        "filter": {"has_tag": "not_interested"},
        "action": "Tag for 90-day follow-up, rotate to different mailer type",
    },
    {
        "number": "10",
        "name": "10. Bad Data",
        "description": "Wrong number/address — route to re-skip",
        "filter": {"has_tag": "bad_data"},
        "action": "Remove bad phone/address, re-run skip trace",
    },
    {
        "number": "11",
        "name": "11. Completed Cycle",
        "description": "Full 3-day cycle done, move to nurture",
        "filter": {"has_tag": "cycle_complete", "not_tag": "hot"},
        "action": "Move to nurture list, schedule monthly touch",
    },
]


@dataclass
class CycleRecord:
    """A record being processed through the niche sequential cycle."""
    address: str = ""
    owner_name: str = ""
    phone: str = ""
    email: str = ""
    current_preset: str = ""
    cycle_day: int = 0
    cycle_count: int = 0
    tags: list = field(default_factory=list)


# ── Channel execution ─────────────────────────────────────────────────

def export_sms_list(records: list[dict], day: int = 1,
                    output_path: str = "") -> str:
    """Export records for SMS sending via Launch Control / REISimpli.

    Returns path to CSV with: name, phone, message template.
    """
    if not output_path:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = str(config.OUTPUT_DIR / f"sms_list_day{day}_{timestamp}.csv")

    sms_templates = {
        1: "Hi {name}, I noticed your property at {address} and wanted to reach out. "
           "Are you or anyone in the family considering selling? — [Your Name]",
        2: "Hey {name}, just following up on your property at {address}. "
           "If you're interested in a quick, fair cash offer, I'd love to chat. — [Your Name]",
        3: "Last message — {name}, I have a cash offer ready for {address}. "
           "If the timing isn't right, no worries. Let me know! — [Your Name]",
    }
    template = sms_templates.get(day, sms_templates[1])

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["name", "phone", "message", "address"])
        writer.writeheader()
        for rec in records:
            name = rec.get("owner_name") or rec.get("full_name") or "there"
            phone = rec.get("primary_phone") or rec.get("mobile_1") or rec.get("Phone 1") or ""
            address = rec.get("address") or rec.get("Property Street") or ""
            if phone:
                writer.writerow({
                    "name": name,
                    "phone": phone,
                    "message": template.format(name=name.split()[0] if name else "there",
                                               address=address),
                    "address": address,
                })

    logger.info("Exported SMS list (Day %d): %s", day, output_path)
    return output_path


def export_call_list(records: list[dict], day: int = 1,
                     output_path: str = "") -> str:
    """Export records for cold calling with dial priority ordering.

    Returns path to CSV ordered by phone tier (Dial First → Second → Third).
    """
    if not output_path:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = str(config.OUTPUT_DIR / f"call_list_day{day}_{timestamp}.csv")

    tier_order = {"Dial First": 1, "Dial Second": 2, "Dial Third": 3,
                  "Dial Fourth": 4, "Drop": 5}

    call_records = []
    for rec in records:
        phone = rec.get("primary_phone") or rec.get("mobile_1") or rec.get("Phone 1") or ""
        if not phone:
            continue
        tier = rec.get("phone_tier_tag") or rec.get("Phone Tag") or "Unscored"
        call_records.append({
            "name": rec.get("owner_name") or rec.get("full_name") or "",
            "phone": phone,
            "phone_2": rec.get("mobile_2") or rec.get("Phone 2") or "",
            "phone_3": rec.get("landline_1") or rec.get("Phone 3") or "",
            "tier": tier,
            "tier_order": tier_order.get(tier, 3),
            "address": rec.get("address") or rec.get("Property Street") or "",
            "notice_type": rec.get("notice_type") or "",
            "notes": rec.get("notes") or "",
        })

    # Sort by tier (best first)
    call_records.sort(key=lambda r: r["tier_order"])

    fields = ["name", "phone", "phone_2", "phone_3", "tier", "address", "notice_type", "notes"]
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(call_records)

    logger.info("Exported call list (Day %d): %d records, %s", day, len(call_records), output_path)
    return output_path


def export_mail_list(records: list[dict], output_path: str = "") -> str:
    """Export records for direct mail piece.

    Returns path to CSV with mailing-ready address formatting.
    """
    if not output_path:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = str(config.OUTPUT_DIR / f"mail_list_{timestamp}.csv")

    fields = ["first_name", "last_name", "address_line_1", "address_line_2",
              "city", "state", "zip", "property_address", "notice_type"]

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for rec in records:
            name = rec.get("owner_name") or rec.get("full_name") or ""
            parts = name.split(None, 1)
            first = parts[0] if parts else ""
            last = parts[1] if len(parts) > 1 else ""

            # Use mailing address if available, fall back to property
            mail_street = (rec.get("owner_street") or rec.get("decision_maker_street") or
                           rec.get("address") or rec.get("Property Street") or "")
            mail_city = (rec.get("owner_city") or rec.get("decision_maker_city") or
                         rec.get("city") or rec.get("Property City") or "")
            mail_state = (rec.get("owner_state") or rec.get("decision_maker_state") or
                          rec.get("state") or "TN")
            mail_zip = (rec.get("owner_zip") or rec.get("decision_maker_zip") or
                        rec.get("zip") or rec.get("Property ZIP") or "")

            if mail_street:
                writer.writerow({
                    "first_name": first,
                    "last_name": last,
                    "address_line_1": mail_street,
                    "address_line_2": "",
                    "city": mail_city,
                    "state": mail_state,
                    "zip": mail_zip,
                    "property_address": rec.get("address") or "",
                    "notice_type": rec.get("notice_type") or "",
                })

    logger.info("Exported mail list: %s", output_path)
    return output_path


# ── Cycle orchestration ───────────────────────────────────────────────

def run_niche_sequential(list_name: str = "", channel: str = "sms",
                         day: int = 1, csv_path: str = "",
                         action: str = "execute") -> dict:
    """Run niche sequential marketing for a list/channel/day combination.

    Args:
        list_name: DataSift list to filter (e.g., "Foreclosure")
        channel: "sms", "call", "mail", "dp"
        day: 1, 2, or 3 of the 3-day cycle
        csv_path: Direct CSV path (bypasses DataSift filter)
        action: "execute" (run channel), "setup-presets" (create in DataSift),
                "status" (show cycle progress)
    """
    if action == "setup-presets":
        logger.info("Preset creation requires Playwright — use: "
                     "python src/main.py manage-presets --discover")
        return {"presets": PRESETS, "folder": PRESET_FOLDER,
                "note": "Use manage-presets CLI to create/modify in DataSift"}

    if action == "status":
        return {"presets": PRESETS,
                "message": "Preset status requires DataSift connection — "
                           "use manage-presets --discover"}

    # Load records
    records = []
    if csv_path:
        with open(csv_path, "r", encoding="utf-8-sig") as f:
            records = list(csv.DictReader(f))
    else:
        # Find most recent CSV for the list name
        for p in sorted(config.OUTPUT_DIR.glob("*.csv"),
                        key=lambda x: x.stat().st_mtime, reverse=True):
            if list_name.lower().replace(" ", "_") in p.name.lower():
                with open(p, "r", encoding="utf-8-sig") as f:
                    records = list(csv.DictReader(f))
                logger.info("Loaded %d records from %s", len(records), p)
                break

    if not records:
        return {"error": f"No records found for list '{list_name}'"}

    # Execute channel
    result = {"channel": channel, "day": day, "records": len(records)}

    if channel == "sms":
        result["output"] = export_sms_list(records, day)
    elif channel == "call":
        result["output"] = export_call_list(records, day)
    elif channel == "mail":
        result["output"] = export_mail_list(records)
    elif channel == "dp":
        result["note"] = "Route to: python src/main.py deep-prospect --csv-path <path> --depth 3"
    else:
        return {"error": f"Unknown channel: {channel}"}

    logger.info("Niche sequential: %s channel, Day %d, %d records", channel, day, len(records))
    return result
