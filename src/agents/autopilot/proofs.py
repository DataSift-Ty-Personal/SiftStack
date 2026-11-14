"""One-record proofs (build plan 0.11): every write that has never been proven, tried on ONE record,
read back, and put back.

A route that "should work" has cost this project a morning more than once: a scope that was
silently ignored traced a whole account, a counter write that did not stick left mail presets
frozen. So before a stage goes live, the writes it depends on are proven here, one record at a
time, on a STAGING account:

    python src/agents/autopilot/proofs.py list
    python src/agents/autopilot/proofs.py run --key-env STAGING_DATASIFT_KEY --record <uuid>            # dry: says what it would do
    python src/agents/autopilot/proofs.py run --key-env STAGING_DATASIFT_KEY --record <uuid> --commit

The account is named by the ENVIRONMENT VARIABLE that holds its Open API key, never by a key on
the command line. The production key (REISIFT_API_KEY) is refused unless --allow-production is
given, and that switch exists only for a record created for the purpose.

Each proof writes one value, reads it back, restores what was there and reads again. The result
(sent, read back, restored) is appended to output/autopilot/proofs/<date>.jsonl. A proof that fails
names the stage it blocks.

Not scripted here, because each needs a human choice or spends money: a SiftMail launch (it mails
a real piece), a card move between two named columns (to see whether an API move fires the board's
sequences), a task from a preset, a document upload, a one-off text in a Smarter Contact thread.
`list` prints them with what to check.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

SRC = Path(__file__).resolve().parents[2]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

PROOF_TAG = "Proof Test"

BY_HAND = [
    ("siftmail_launch", "ftm_mail", "Quote then launch one piece to your own address; check the record's mail counter and last-mailed date moved."),
    ("card_move", "the lead clock (plan 2.5)", "Move one card between two columns over the API; check whether the board's card.moved sequence fired (a task appears)."),
    ("task_from_preset", "next-action checks (plan 2.7)", "Create one task from a preset over the API; check it shows on the record with the right assignee and due date."),
    ("document_upload", "research write-back (plan 3.4)", "Upload one PDF to the record's Documents tab through the presigned route."),
    ("thread_text", "the afternoon text (plan 1.3)", "Send one text into an existing Smarter Contact thread; check it lands in that thread and not a new one."),
    ("do_not_mail", "the gate (plan 0.1)", "Set do-not-mail-ever on a throwaway record; there is no documented way to clear it, so use a record made for it."),
]


def _log(row: dict) -> None:
    root = Path(os.environ.get("AGENTS_DATA_DIR") or SRC.parent / "output" / "autopilot") / "proofs"
    root.mkdir(parents=True, exist_ok=True)
    with (root / (datetime.now(timezone.utc).strftime("%Y-%m-%d") + ".jsonl")).open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, default=str) + "\n")


def _counter(crm, uuid: str, field: str, write, commit: bool) -> dict:
    rec = crm.record(uuid)
    before = int(rec.get(field) or 0)
    out = {"before": before, "sent": before + 1}
    if not commit:
        return dict(out, ok=None, note="dry: would write %d, read it back, then restore %d" % (before + 1, before))
    write(uuid, before + 1)
    out["read_back"] = int(crm.record(uuid).get(field) or 0)
    write(uuid, before)
    out["restored"] = int(crm.record(uuid).get(field) or 0)
    out["ok"] = out["read_back"] == before + 1 and out["restored"] == before
    return out


def proof_call_counter(crm, uuid: str, commit: bool) -> dict:
    return _counter(crm, uuid, "predictivecall_attempts", crm.call_attempts, commit)


def proof_sms_counter(crm, uuid: str, commit: bool) -> dict:
    return _counter(crm, uuid, "sms_attempts", crm.sms_attempts, commit)


def proof_skiptrace_counter(crm, uuid: str, commit: bool) -> dict:
    ow = crm.record(uuid).get("owner") or {}
    owner = ow.get("uuid") or ""
    before = int(ow.get("skiptrace_attempts") or 0)
    out = {"before": before, "sent": before + 1}
    if not owner:
        return dict(out, ok=False, note="the record has no owner")
    if not commit:
        return dict(out, ok=None, note="dry: would write %d on the owner, read it back, then restore %d" % (before + 1, before))
    crm.skiptrace_attempts(owner, before + 1)
    out["read_back"] = int((crm.record(uuid).get("owner") or {}).get("skiptrace_attempts") or 0)
    crm.skiptrace_attempts(owner, before)
    out["restored"] = int((crm.record(uuid).get("owner") or {}).get("skiptrace_attempts") or 0)
    out["ok"] = out["read_back"] == before + 1 and out["restored"] == before
    return out


def proof_tag_round_trip(crm, uuid: str, commit: bool) -> dict:
    def tags():
        return [(t.get("title") if isinstance(t, dict) else t) for t in (crm.record(uuid).get("tags") or [])]
    before = tags()
    out = {"before": PROOF_TAG in before, "sent": "add then remove %r" % PROOF_TAG}
    if not commit:
        return dict(out, ok=None, note="dry: would add the tag, read it back, remove it, read again")
    crm.add_tags(uuid, [PROOF_TAG])
    out["read_back"] = PROOF_TAG in tags()
    crm.remove_tags(uuid, [PROOF_TAG])
    out["restored"] = PROOF_TAG not in tags()
    out["ok"] = bool(out["read_back"] and out["restored"])
    return out


def proof_status_round_trip(crm, uuid: str, commit: bool) -> dict:
    def status():
        s = crm.record(uuid).get("status")
        return (s.get("value") if isinstance(s, dict) else s) or None
    before = status()
    out = {"before": before, "sent": "prospecting"}
    if before not in (None, "prospecting"):
        return dict(out, ok=None, note="skipped: the record holds status %r and a status change fires sequences; use a record with no status" % before)
    if not commit:
        return dict(out, ok=None, note="dry: would set prospecting, read it back, then clear it")
    crm.set_status(uuid, "prospecting")
    out["read_back"] = status()
    crm.set_status(uuid, before)
    out["restored"] = status()
    out["ok"] = out["read_back"] == "prospecting" and out["restored"] == before
    return out


def proof_scoped_trace_estimate(crm, uuid: str, commit: bool) -> dict:
    """The scoped skip trace's DRY RUN only: it traces nothing. It proves the scope takes (the
    estimate must cover exactly one record, not the whole account)."""
    est = crm.skiptrace_scoped([uuid], dry_run=True)
    n = est.get("number_of_records", est.get("count", est.get("expected_count"))) if isinstance(est, dict) else None
    return {"sent": "dry_run on 1 record", "read_back": n, "ok": n == 1,
            "note": "DataSift's estimate covers %r record(s); anything but 1 means the scope did not take" % n}


PROOFS = [
    ("call_counter", "ftm_attempts", proof_call_counter),
    ("sms_counter", "the text lane (plan 1.4)", proof_sms_counter),
    ("skiptrace_counter", "ftm_retrace", proof_skiptrace_counter),
    ("tag_round_trip", "every stage that tags", proof_tag_round_trip),
    ("status_round_trip", "the hot auction lane (plan 1.8)", proof_status_round_trip),
    ("scoped_trace_estimate", "ftm_native_trace", proof_scoped_trace_estimate),
]


def run(crm, uuid: str, commit: bool = False, only: list[str] | None = None) -> list[dict]:
    results = []
    for name, blocks, fn in PROOFS:
        if only and name not in only:
            continue
        try:
            r = fn(crm, uuid, commit)
        except Exception as exc:  # noqa: BLE001  (a refused route is the finding, not a crash)
            r = {"ok": False, "note": "%s: %s" % (type(exc).__name__, str(exc)[:300])}
        r.update(proof=name, blocks=blocks, record=uuid, commit=commit, at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        results.append(r)
        _log(r)
    return results


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="One-record proofs for the autopilot's writes")
    ap.add_argument("action", choices=["list", "run"])
    ap.add_argument("--key-env", default="", help="the environment variable that holds the TARGET account's Open API key")
    ap.add_argument("--record", default="", help="the record uuid to prove on")
    ap.add_argument("--only", action="append", default=[])
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--allow-production", action="store_true", help="permit the production key, for a record made for this")
    a = ap.parse_args(argv)
    if a.action == "list":
        print("Scripted (each writes one value, reads it back, restores it):")
        for name, blocks, fn in PROOFS:
            print("  %-22s proves a write that %s depends on" % (name, blocks))
        print("By hand (a human choice or real money):")
        for name, blocks, what in BY_HAND:
            print("  %-22s for %s: %s" % (name, blocks, what))
        return 0
    from agents import config  # noqa: F401  (loads .env)
    key = os.environ.get(a.key_env or "", "")
    if not a.key_env or not key:
        print("name the environment variable that holds the staging account's Open API key: --key-env STAGING_DATASIFT_KEY")
        return 2
    if key == os.environ.get("REISIFT_API_KEY") and not a.allow_production:
        print("that is the production key. Proofs run on staging; pass --allow-production only for a record made for this.")
        return 3
    if not a.record:
        print("name the record: --record <uuid>")
        return 2
    from datasift_api_upload import Api
    from agents.autopilot.crm import CRM
    crm = CRM(api=Api(api_key=key))
    results = run(crm, a.record, commit=a.commit, only=a.only)
    bad = 0
    for r in results:
        mark = "ok  " if r.get("ok") else ("--  " if r.get("ok") is None else "FAIL")
        bad += r.get("ok") is False
        print("%s %-22s %s" % (mark, r["proof"], r.get("note") or "wrote %s, read %s, restored %s" % (r.get("sent"), r.get("read_back"), r.get("restored"))))
        if r.get("ok") is False:
            print("     blocks: %s" % r["blocks"])
    if not a.commit:
        print("DRY RUN: nothing was written. Re-run with --commit.")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
