"""The Instantly campaign: create it paused, load a built list, preview, and launch only on a yes.

    python src/cold_email/campaign.py create --name "Agency queue email 1"          # paused, no leads
    python src/cold_email/campaign.py load --name "Agency queue email 1" --leads output/cold_email/lists/<slug>/leads.json
    python src/cold_email/campaign.py preview --name "Agency queue email 1" --leads <same file> [--n 5]
    python src/cold_email/campaign.py launch --name "Agency queue email 1" --yes      # the only step that sends
    python src/cold_email/campaign.py pause --name "Agency queue email 1"
    python src/cold_email/campaign.py status

Campaign settings follow the course: Monday to Friday 9 to 5 Eastern, every sending inbox, stop on
reply, open and link tracking off, plain text only, new leads first, 10 minutes between sends, one
step with three versions (emailcopy.py), no unsubscribe link (the opt-out is by reply). The postal
address line comes from .env. Loading writes output/cold_email/ledger.json so no owner or address
is ever loaded twice; Instantly's own skip_if_in_workspace backs that up.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import emailcopy  # noqa: E402
from instantly import ROOT, Instantly, InstantlyError  # noqa: E402
from setup_accounts import env  # noqa: E402

LEDGER = ROOT / "output" / "cold_email" / "ledger.json"
# Instantly's schedule takes 102 fixed zones and America/New_York is not one of them (HTTP 400,
# read off api.instantly.ai/openapi/api_v2.json 2026-11-16). America/Detroit keeps Eastern time.
TZ = "America/Detroit"
DAILY = 250


def address() -> str:
    """The postal address line under the signature, or '' (Ty, 2026-11-16: not his own address)."""
    a = emailcopy.footer(env("COLD_EMAIL_ADDRESS"))
    if not a:
        print("note: no postal address in the footer (COLD_EMAIL_ADDRESS is empty); CAN-SPAM wants one,"
              " a PO box or virtual mailbox works")
    return a


def find(api: Instantly, name: str) -> dict | None:
    return next((c for c in api.campaigns() if c.get("name") == name), None)


def body_for(api: Instantly, name: str) -> dict:
    addr = address()
    probs = emailcopy.audit(addr)
    if probs:
        raise SystemExit("copy audit failed: " + "; ".join(probs[:5]))
    inboxes = sorted(a["email"] for a in api.accounts())
    return {
        "name": name,
        "campaign_schedule": {"schedules": [{
            "name": "Weekdays 9 to 5", "timing": {"from": "09:00", "to": "17:00"},
            "days": {"0": False, "1": True, "2": True, "3": True, "4": True, "5": True, "6": False},
            "timezone": TZ}]},
        "sequences": [{"steps": [{"type": "email", "delay": 0, "variants": [
            {"subject": v["subject"], "body": emailcopy.html_of(v, addr)} for v in emailcopy.VERSIONS]}]}],
        "email_list": inboxes,
        "daily_limit": DAILY,
        "stop_on_reply": True,
        "stop_on_auto_reply": False,
        "link_tracking": False,
        "open_tracking": False,
        "text_only": True,
        "prioritize_new_leads": True,
        "email_gap": 10,
        "insert_unsubscribe_header": False,
    }


CHECK = ("stop_on_reply", "link_tracking", "open_tracking", "text_only", "prioritize_new_leads", "daily_limit", "email_gap")


def copy_problems(got: dict) -> list[str]:
    """The stored bodies must still hold the words: every version's greeting and opt-out, and the
    postal address. Instantly strips content it does not like without an error."""
    out = []
    steps = ((got.get("sequences") or [{}])[0].get("steps") or [])
    variants = (steps[0] if steps else {}).get("variants") or []
    if len(variants) != len(emailcopy.VERSIONS):
        out.append(f"{len(variants)} versions stored, {len(emailcopy.VERSIONS)} sent")
    addr = emailcopy.footer(env("COLD_EMAIL_ADDRESS"))
    for n, v in enumerate(variants, 1):
        b = v.get("body") or ""
        for need in ("Hey {{firstName}}", "{{RANDOM|", "{{accountSignature}}") + ((addr.split(",")[0],) if addr else ()):
            if need not in b:
                out.append(f"version {n} body is missing {need!r}")
        if not addr and not b.rstrip().endswith("{{accountSignature}}</div>"):
            out.append(f"version {n} has text after the signature, but no postal address is set")
        if "{{RANDOM|" not in (v.get("subject") or ""):
            out.append(f"version {n} subject lost its spintext")
    return out


def create(api: Instantly, name: str) -> dict:
    c = find(api, name)
    body = body_for(api, name)
    if c:
        print(f"already exists, status {c.get('status')}: {c['id']}; updating its copy")
        if c.get("status") not in (0, 2):
            raise SystemExit("the campaign is not paused or draft: pause it before changing the copy")
        api.call("PATCH", f"/campaigns/{c['id']}", json={"sequences": body["sequences"]})
    else:
        try:
            c = api.create_campaign(body)
        except InstantlyError as exc:
            raise SystemExit(str(exc))
    got = api.call("GET", f"/campaigns/{c['id']}")
    off = {k: (got.get(k), body[k]) for k in CHECK if got.get(k) != body[k] and not (body[k] is False and got.get(k) is None)}
    steps = ((got.get("sequences") or [{}])[0].get("steps") or [])
    print(f"campaign {c['id']} | inboxes {len(got.get('email_list') or [])} | versions "
          f"{len((steps[0] if steps else {}).get('variants') or [])} | status {got.get('status')} (0 = draft, not sending)")
    if off:
        print("SETTINGS THAT DID NOT STICK:", json.dumps(off))
    probs = copy_problems(got)
    print("copy read-back:", "OK" if not probs else "PROBLEMS " + "; ".join(probs))
    return got


def load_ledger() -> dict:
    try:
        return json.loads(LEDGER.read_text(encoding="utf-8"))
    except Exception:
        return {"owners": {}, "emails": {}}


def load(api: Instantly, name: str, leads_path: Path) -> None:
    c = find(api, name)
    if not c:
        raise SystemExit("no campaign named %r: run create first" % name)
    leads = json.loads(leads_path.read_text(encoding="utf-8"))
    ledger = load_ledger()
    fresh = [l for l in leads if l["owner"] not in ledger["owners"] and l["email"] not in ledger["emails"]]
    print(f"{len(leads)} in the file, {len(leads) - len(fresh)} already loaded before, {len(fresh)} to add")
    day = time.strftime("%Y-%m-%d")
    for i in range(0, len(fresh), 1000):
        chunk = fresh[i:i + 1000]
        payload = [{
            "email": l["email"], "first_name": l["first_name"] or "there", "last_name": l.get("last_name") or "",
            "custom_variables": {"street": l["street"], "city": l["city"], "zip": l.get("zip", ""),
                                 "sift_record": l["record"], "sift_owner": l["owner"]},
        } for l in chunk]
        r = api.add_leads(payload, campaign_id=c["id"], skip_if_in_workspace=True,
                          idempotency_key=f"{c['id']}-{leads_path.parent.name}-{i}")
        print("instantly:", json.dumps({k: r.get(k) for k in ("status", "total_sent", "leads_uploaded", "in_blocklist",
                                                                "duplicated_leads", "skipped_count", "invalid_email_count")}))
        for l in chunk:
            ledger["owners"][l["owner"]] = {"email": l["email"], "record": l["record"], "campaign": c["id"], "day": day}
            ledger["emails"][l["email"]] = l["owner"]
        LEDGER.parent.mkdir(parents=True, exist_ok=True)
        LEDGER.write_text(json.dumps(ledger, indent=1), encoding="utf-8")


def preview(name: str, leads_path: Path, n: int) -> None:
    leads = json.loads(leads_path.read_text(encoding="utf-8"))
    rng = random.Random(int(time.time()))
    addr = address()
    for lead in rng.sample(leads, min(n, len(leads))):
        v = rng.choice(emailcopy.VERSIONS)
        print("=" * 60)
        print("To:", lead["first_name"] or "(no first name, greets as 'there')", "|", lead["street"], lead["city"])
        print("Subject:", emailcopy.render(v["subject"], lead, rng))
        print()
        print(emailcopy.render(emailcopy.text_of(v, addr), lead, rng))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["create", "load", "preview", "launch", "pause", "status"])
    ap.add_argument("--name", default="Agency queue email 1")
    ap.add_argument("--leads", type=Path)
    ap.add_argument("--n", type=int, default=5)
    ap.add_argument("--yes", action="store_true", help="required to launch")
    a = ap.parse_args()
    api = Instantly()
    if a.action == "create":
        create(api, a.name)
    elif a.action == "load":
        load(api, a.name, a.leads)
    elif a.action == "preview":
        preview(a.name, a.leads, a.n)
    elif a.action in ("launch", "pause"):
        c = find(api, a.name)
        if not c:
            raise SystemExit("no campaign named %r" % a.name)
        if a.action == "launch" and not a.yes:
            raise SystemExit("launch sends email to real people: re-run with --yes once Ty has said go")
        (api.activate if a.action == "launch" else api.pause)(c["id"])
        hold = ROOT / "output" / "cold_email" / "hold.json"
        if a.action == "launch" and hold.exists():
            hold.unlink()                       # a pause on purpose ends when the campaign runs again
        print(a.action, "done:", c["id"], "status now", (api.call("GET", f"/campaigns/{c['id']}") or {}).get("status"))
    elif a.action == "status":
        for c in api.campaigns():
            print(c.get("status"), c["id"], c.get("name"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
