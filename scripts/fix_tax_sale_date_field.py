"""Create the 'Tax Sale Date' custom field as a DATE type.

The original (id 14423) was created as `text` during the 2026-07-30 manual
upload — a text field can't be date-range filtered. Nick deleted it in the UI;
this recreates it as `date` in the same group so auction urgency is sortable.

Run from the SiftStack repo root:  python scripts/fix_tax_sale_date_field.py
"""
import json
import sys
import urllib.error
import urllib.request

GROUP_ID = 4321  # Property Debts & Encumbrances
LABEL = "Tax Sale Date"
BASE = "https://apiv2.reisift.io/"

tok = None
with open(".env") as fh:
    for line in fh:
        if line.startswith("REISIFT_TOKEN="):
            tok = line.split("=", 1)[1].strip().strip('"').strip("'")
if not tok:
    sys.exit("REISIFT_TOKEN not found in .env — run from the SiftStack repo root")

HEADERS = {
    "Authorization": f"Bearer {tok}",
    "X-REISIFT-UI-VERSION": "2022.02.01.7",
    "Accept": "application/json",
    "Content-Type": "application/json",
}


def req(method: str, path: str, payload: dict | None = None) -> tuple[int, str]:
    r = urllib.request.Request(
        BASE + path,
        headers=HEADERS,
        method=method,
        data=json.dumps(payload).encode() if payload is not None else None,
    )
    try:
        resp = urllib.request.urlopen(r, timeout=30)
        return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:500]


def find_active() -> dict | None:
    st, body = req("GET", "api/internal/custom-fields?limit=300")
    if st != 200:
        sys.exit(f"GET custom-fields failed: {st} {body}")
    for f in json.loads(body)["results"]:
        if f["label"] == LABEL and f["is_active"]:
            return f
    return None


existing = find_active()
if existing:
    if existing["field_type"] == "date":
        print(f"Already correct: '{LABEL}' id {existing['id']} is a date field — nothing to do")
        sys.exit(0)
    sys.exit(
        f"'{LABEL}' still exists as {existing['field_type']} (id {existing['id']}) — "
        "delete it in the UI first, then re-run"
    )

st, body = req(
    "POST",
    "api/internal/custom-fields/",
    {
        "label": LABEL,
        "field_type": "date",
        "entity_type": "property",
        "group_id": GROUP_ID,
        "required": False,
        "placeholder": "Scheduled tax auction / sheriff sale date",
    },
)
print(f"POST create -> {st}")
if st not in (200, 201):
    sys.exit(f"CREATE failed: {st} {body}")

f = find_active()
if f and f["field_type"] == "date":
    print(f"OK: '{LABEL}' created as date field, id {f['id']}, group {f['group']['label']}")
else:
    sys.exit(f"Created but verification failed: {f}")
