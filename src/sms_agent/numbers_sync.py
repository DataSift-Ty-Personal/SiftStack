"""Re-pull the sending pool from smrtPhone Admin > Phone Numbers.

There is no public API for this. `/phoneNumbers` and `/callerIds` return the
SPA shell to an API key, so the route is the saved web session plus the
FOSJsRouting route map (`GET /js/routing?callback=fos.Router.setData` dumps
~1,187 routes), which surfaces `POST /phoneNumbers/filtered`, a DataTables
endpoint of the same shape as `/logs/calls/filtered`. Fields come back as HTML
fragments and have to be parsed.

Grouping is by FLOW, not by the friendly name, and that distinction is the
whole point. The friendly name is a label; the **flow** is what actually routes
an inbound call. Renaming "Tinaa - 4" to "Adriana - 11" changes nothing about
where a callback rings. Reassign the flow in smrtPhone, then run this.

    python src/sms_agent/cli.py numbers            # show the current pool
    python src/sms_agent/cli.py numbers --refresh  # re-pull and rewrite the file
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Optional

import requests

from . import config

log = logging.getLogger(__name__)

BASE = "https://phone.smrt.studio"
GRID = "/phoneNumbers/filtered"

# Numbers that must never enter the pool, matched on the friendly name or flow.
# A test line in a production pool is worse than one fewer number.
EXCLUDE_PATTERNS = [
    (re.compile(r"\bwebsite\b", re.I), "website number"),
    (re.compile(r"\btest\b", re.I), "test number"),
    (re.compile(r"\bdispo\b", re.I), "dispo number"),
    (re.compile(r"\binbound\b", re.I), "inbound call flow"),
]


def _session():
    state_path = Path(config.SMRTPHONE_STATE_FILE)
    if not state_path.exists():
        raise RuntimeError(
            f"no smrtPhone session at {state_path}; run _api/smrtphone_login.py to capture one"
        )
    state = json.loads(state_path.read_text(encoding="utf-8"))
    jar = {c["name"]: c["value"] for c in state.get("cookies", []) if "smrt" in c.get("domain", "")}
    if not jar:
        raise RuntimeError("session file has no smrtPhone cookies")
    s = requests.Session()
    s.cookies.update(jar)
    s.headers.update(
        {
            "User-Agent": "Mozilla/5.0",
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Referer": f"{BASE}/phoneNumbers",
        }
    )
    return s


def _text(html) -> str:
    from bs4 import BeautifulSoup

    return " ".join(BeautifulSoup(str(html or ""), "html.parser").get_text(" ").split())


def fetch() -> list[dict]:
    """Every number on the account, with its friendly name and routing flow."""
    s = _session()
    form = {
        "draw": "1", "start": "0", "length": "500",
        "search[value]": "", "search[regex]": "false",
        "order[0][column]": "0", "order[0][dir]": "asc",
    }
    for i, col in enumerate(["number", "friendlyName", "flow", "user", "status"]):
        form[f"columns[{i}][data]"] = col
        form[f"columns[{i}][name]"] = col
        form[f"columns[{i}][searchable]"] = "true"
        form[f"columns[{i}][orderable]"] = "true"
        form[f"columns[{i}][search][value]"] = ""
        form[f"columns[{i}][search][regex]"] = "false"

    resp = s.post(BASE + GRID, data=form, timeout=60)
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code} from {GRID}")
    try:
        payload = resp.json()
    except ValueError:
        raise RuntimeError("session expired; re-run _api/smrtphone_login.py") from None

    out = []
    for row in payload.get("data") or []:
        digits = re.sub(r"\D", "", row.get("strippedPhone") or _text(row.get("phoneNumber")))[-10:]
        if len(digits) != 10:
            continue
        # The friendly-name cell carries the inline edit controls too.
        name = _text(row.get("friendlyName")).split(" Save")[0].strip()
        out.append(
            {
                "e164": "+1" + digits,
                "digits": digits,
                "name": name,
                "flow": _text(row.get("flow")).strip(),
            }
        )
    out.sort(key=lambda r: r["digits"])
    return out


def excluded_reason(row: dict) -> Optional[str]:
    for pattern, why in EXCLUDE_PATTERNS:
        if pattern.search(row["name"]) or pattern.search(row["flow"]):
            return why
    return None


def build_pools(rows: list[dict]) -> tuple[dict, dict]:
    """Group into {owner: [numbers]} by FLOW, and report what was excluded."""
    pools: dict[str, list[str]] = {}
    excluded: dict[str, str] = {}
    for row in rows:
        why = excluded_reason(row)
        if why:
            excluded[f"{row['digits']} ({row['name']})"] = why
            continue
        owner = re.sub(r"\s*(numbers|flow)\s*$", "", row["flow"], flags=re.I).strip() or "Unassigned"
        pools.setdefault(owner, []).append(row["e164"])
    for owner in pools:
        pools[owner].sort()
    return pools, excluded


def refresh(write: bool = True) -> dict:
    rows = fetch()
    pools, excluded = build_pools(rows)
    doc = {
        "_comment": (
            "Sending pool for src/sms_agent, synced from smrtPhone Admin > Phone Numbers by "
            "`cli.py numbers --refresh`. Grouped by the ROUTING FLOW, not the friendly name: "
            "the flow is what a callback actually rings, so renaming a number changes nothing "
            "here. Every number must be on the approved A2P 10DLC campaign."
        ),
        "_synced_at": __import__("datetime").datetime.now().astimezone().isoformat(timespec="seconds"),
        "_excluded": excluded,
        "pools": pools,
    }
    if write:
        path = Path(config.NUMBERS_FILE)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    return doc


def report(doc: dict) -> str:
    lines = []
    total = 0
    for owner, nums in sorted(doc["pools"].items()):
        total += len(nums)
        lines.append(f"  {owner:16} {len(nums):3} numbers")
    lines.append(f"  {'TOTAL':16} {total:3} numbers"
                 f"  x {config.DAILY_CAP_PER_NUMBER}/day = {total * config.DAILY_CAP_PER_NUMBER}/day")
    if doc.get("_excluded"):
        lines.append("")
        lines.append("  excluded:")
        for number, why in doc["_excluded"].items():
            lines.append(f"    {number:34} {why}")
    return "\n".join(lines)
