"""Command line for the SMS agent.

    python src/sms_agent/cli.py doctor                 # is anything actually wired up
    python src/sms_agent/cli.py serve                  # run the webhook receiver
    python src/sms_agent/cli.py work --loop            # run the worker
    python src/sms_agent/cli.py map --csv output/mms_send_queue.csv
    python src/sms_agent/cli.py simulate 8652548712 "how much are you offering"
    python src/sms_agent/cli.py thread 8652548712
    python src/sms_agent/cli.py approve 8652548712
    python src/sms_agent/cli.py status
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from pathlib import Path

if __package__ in (None, ""):  # allow `python src/sms_agent/cli.py`
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "sms_agent"

from . import (backfill, campaign, classify, config, crm, engine, escalate, respond,
               seed, sender_pool, smrtphone, store, transport, worker)


def cmd_doctor(args) -> int:
    print(f"phase          {config.PHASE}   (1 classify, 2 +escalate, 3 +draft, 4 +auto-send)")
    print(f"dry run        {config.DRY_RUN}")
    print(f"database       {store.init()}")
    print(f"model          {config.MODEL}")

    gaps = config.missing()
    if gaps:
        print("\nconfig gaps:")
        for g in gaps:
            print(f"  - {g}")
    else:
        print("\nconfig: complete for this phase")

    t = transport.available()
    print(f"\ntransport      {t['configured']}")
    print(f"  API          {'ok' if t['api']['ok'] else 'FAIL: ' + t['api']['detail']}")
    print(f"  session      {'ok' if t['session']['ok'] else 'FAIL: ' + t['session']['detail']}")
    if not transport.supports_number_pool():
        print("  NOTE: the session transport sends from the account default caller ID only,")
        print("        so the sticky sender and per-number caps do not apply.")

    pool = sender_pool.pool()
    print(f"\nsending pool   {len(pool)} number(s)")
    for n in pool[:25]:
        print(f"  {n}  {store.sends_today(n)} sent today")

    reason = crm.unavailable_reason()
    print(f"\nreisift CRM    {'ok (' + config.SIFT_ACCOUNT + ')' if not reason else 'FAIL: ' + reason}")

    senders = config.senders()
    print(f"sender names   {len(senders)} mapped in {config.SENDERS_FILE.name}")
    for uuid, name in list(senders.items())[:10]:
        print(f"  {uuid[:12]}...  {name}")
    if not senders and not config.SENDER_NAME:
        print("  WARNING: no sender names. Threads will go out unsigned rather than")
        print("           risk the model inventing a name.")

    print(f"slack          {'configured' if config.SLACK_WEBHOOK_URL else 'NOT configured'}")
    print(f"playbook       {len(__import__('sms_agent.knowledge', fromlist=['x']).playbook())} chars")

    if config.WEBHOOK_SECRET:
        print("\npoint these at your public host:")
        print(f"  smrtPhone -> https://YOUR_HOST/hooks/{config.WEBHOOK_SECRET}/smrtphone")
        print(f"  DataSift  -> https://YOUR_HOST/hooks/{config.WEBHOOK_SECRET}/datasift")
        print("\nsmrtPhone Admin > Webhooks, subscribe to:")
        print("  smsIncoming, smsOutgoing, smsDeliveryCallback, addNumberToDNT, addNumberToDNC")
    return 0 if not gaps else 1


def cmd_serve(args) -> int:
    try:
        import uvicorn
    except ImportError:
        print("uvicorn is not installed. pip install -r requirements.txt")
        return 1
    uvicorn.run(
        "sms_agent.receiver:app",
        host=args.host or config.RECEIVER_HOST,
        port=args.port or config.RECEIVER_PORT,
        log_level="info",
    )
    return 0


def cmd_work(args) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    store.init()
    if args.loop:
        worker.run_forever(interval=args.interval)
        return 0
    print(json.dumps(worker.run_once(), indent=2))
    return 0


def cmd_map(args) -> int:
    """Load phone -> record mappings.

    This is what lets an inbound reply find its record: the smsIncoming payload
    carries only a phone number. Sends register the mapping automatically; this
    backfills from an existing queue CSV so replies to the current campaign
    resolve from day one.
    """
    path = Path(args.csv)
    if not path.exists():
        print(f"no such file: {path}")
        return 1
    phone_cols = ("to_number", "phone", "best_dial_phone", "number")
    n = extra = 0
    with path.open(encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            phone = next((row[c] for c in phone_cols if row.get(c)), "")
            uuid = row.get("record_uuid") or row.get("uuid") or ""
            if not phone or not uuid:
                continue
            store.map_phone(
                phone,
                record_uuid=uuid,
                first_name=row.get("first_name") or row.get("owner_name") or "",
                address=row.get("address") or "",
            )
            n += 1
            if args.all_phones:
                # Real replies frequently arrive from another line on the same
                # record, so map every phone the record carries.
                extra += crm.map_all_phones(uuid)
    print(f"mapped {n} phone(s) from {path.name}")
    if args.all_phones:
        print(f"plus {extra} phone(s) pulled from the records themselves")
    return 0


def cmd_simulate(args) -> int:
    """Push a fake smsIncoming through the real pipeline. No webhook needed."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    store.init()
    payload = {
        "event": "smsIncoming",
        "smsId": f"sim-{store.now()}",
        "from": args.phone,
        "to": args.to or (sender_pool.pool()[0] if sender_pool.pool() else "+10000000000"),
        "message": args.message,
        "source": "simulation",
    }
    print(json.dumps(engine.process("smrtphone", payload), indent=2, default=str))
    return 0


def cmd_classify(args) -> int:
    """Classify text without touching anything. Use it to sanity-check the rules."""
    result = classify.classify(args.message)
    print(json.dumps(result.to_dict(), indent=2))
    return 0


def cmd_draft(args) -> int:
    """Draft a reply for an existing thread without queueing it."""
    store.init()
    phone = store.clean_phone(args.phone)
    thread = store.thread(phone)
    if not thread:
        print(f"no thread for {phone}")
        return 1
    mapped = store.lookup_phone(phone) or {}
    ctx = mapped.get("context") or crm.deal_context(mapped.get("record_uuid", ""))
    last = next((m["body"] for m in reversed(thread) if m["direction"] == "in"), "")
    intent = classify.classify(last, thread[:-1])
    reply = respond.draft(thread, ctx, intent.intent, intent.rationale)
    print(json.dumps({"intent": intent.to_dict(), "reply": reply.to_dict()}, indent=2))
    return 0


def cmd_thread(args) -> int:
    store.init()
    phone = store.clean_phone(args.phone)
    conv = store.get_conversation(phone)
    if not conv:
        print(f"no conversation for {phone}")
        return 1
    print(f"{phone}  state={conv['state']}  turns={conv['ai_turns']}  from={conv['from_number']}")
    if conv.get("paused_reason"):
        print(f"  paused: {conv['paused_reason']}")
    print()
    for m in store.thread(phone, limit=100):
        who = "them" if m["direction"] == "in" else f"us ({m.get('author') or '?'})"
        print(f"  [{m['created_at'][:16]}] {who:>12}: {m['body']}")
    return 0


def cmd_approve(args) -> int:
    """Release the held draft for a conversation into the send queue."""
    store.init()
    phone = store.clean_phone(args.phone)
    rows = [
        r
        for r in store._conn().execute(
            "SELECT * FROM outbox WHERE phone=? AND status='held' ORDER BY id DESC", (phone,)
        )
    ]
    if not rows:
        print(f"nothing held for {phone}")
        return 1
    row = dict(rows[0])
    print(f"releasing: {row['body']}")
    with store.tx() as c:
        c.execute("UPDATE outbox SET status='queued' WHERE id=?", (row["id"],))
        # Anything older than the newest held draft is stale by definition.
        c.execute(
            "UPDATE outbox SET status='cancelled', error='superseded'"
            " WHERE phone=? AND status='held' AND id<>?",
            (phone, row["id"]),
        )
    store.bump_ai_turns(phone)
    print("queued. run `work` to send.")
    return 0


def cmd_pause(args) -> int:
    store.init()
    phone = store.clean_phone(args.phone)
    store.ensure_conversation(phone)
    store.pause_conversation(phone, args.reason or "manual")
    n = store.cancel_queued(phone, "paused manually")
    print(f"{phone} paused; cancelled {n} queued message(s)")
    return 0


def cmd_numbers(args) -> int:
    """Show the sending pool, or re-sync it from smrtPhone."""
    from . import numbers_sync

    if args.refresh:
        try:
            doc = numbers_sync.refresh(write=not args.dry_run)
        except Exception as exc:  # noqa: BLE001 - session expiry is the common case
            print(f"refresh failed: {exc}")
            return 1
        print(f"synced from smrtPhone{' (dry run, file not written)' if args.dry_run else ''}:")
        print(numbers_sync.report(doc))
        if not args.dry_run:
            print()
            print("Push to the live box with:")
            print("  python src/sms_agent/cli.py numbers --push")
        return 0

    if args.sync:
        # refresh + push in one step, since those always happen together after
        # a flow change in smrtPhone.
        try:
            doc = numbers_sync.refresh(write=True)
        except Exception as exc:  # noqa: BLE001
            print(f"refresh failed: {exc}")
            return 1
        print(numbers_sync.report(doc))
        import json as _json, subprocess, os, tempfile
        line = "SMRTPHONE_NUMBERS=" + _json.dumps(doc["pools"], separators=(",", ":"))
        fd, path = tempfile.mkstemp(suffix=".env")
        # LF only, no trailing CR: a stray carriage return in a secret is what
        # made the Anthropic key an illegal HTTP header on the live box.
        with os.fdopen(fd, "wb") as fh:
            fh.write(line.encode() + b"\n")
        print()
        print("push it live with:")
        print(f"  flyctl secrets import -a siftstack < {path}")
        return 0

    if args.push:
        import json as _json
        pools = config.number_pools()
        print("SMRTPHONE_NUMBERS=" + _json.dumps(pools, separators=(",", ":")))
        print()
        print("Pipe that into: fly secrets import -a siftstack")
        return 0

    pools = config.number_pools()
    total = sum(len(v) for v in pools.values())
    for owner, nums in sorted(pools.items()):
        print(f"  {owner or '(unbound)':16} {len(nums):3} numbers")
    print(f"  {'TOTAL':16} {total:3} numbers x {config.DAILY_CAP_PER_NUMBER}/day"
          f" = {total * config.DAILY_CAP_PER_NUMBER}/day")
    return 0


def cmd_presets(args) -> int:
    """List the filter presets the agent can seed from."""
    titles = crm.list_presets()
    if not titles:
        print("no presets readable:", crm.unavailable_reason() or "empty list")
        return 1
    needle = (args.filter or "").lower()
    for t in titles:
        if not needle or needle in t.lower():
            print(" ", t)
    return 0


def cmd_backfill(args) -> int:
    """Replay real replies from the CRM activity log through the classifier."""
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    store.init()
    path = Path(args.queue)
    if not path.exists():
        print(f"no such file: {path}")
        return 1

    targets = backfill.load_targets(path)
    print(f"{len(targets)} record(s) from {path.name}; reading the CRM activity log...")
    result = backfill.classify_all(backfill.collect(targets, limit=args.limit))
    print()
    print(backfill.report(result))

    if args.json:
        out = Path(args.json)
        backfill.save(result, out)
        print()
        print(f"structured -> {out}")

    if args.apply:
        print()
        label = "COMMIT" if args.commit else "DRY preview"
        print(f"applying terminal intents ({label}):")
        for line in backfill.apply_terminal(result, commit=args.commit) or ["  nothing to apply"]:
            print(f"  {line}")
    return 0


def cmd_campaign(args) -> int:
    """Touch-aware run across the whole Hottest cadence."""
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    store.init()
    plan = campaign.build(sender_fallback=args.sender or "", log_pages=args.log_pages)
    print(campaign.summary(plan))
    if args.show:
        print()
        print("samples:")
        for c in plan.candidates[: args.show]:
            print(f"  t{getattr(c, 'touch', '?')}  {c.phone}  {c.message[:96]}")
    if not args.queue:
        print()
        print("preview only. Pass --queue to stage these as HELD.")
        return 0
    by_touch = {}
    for c in plan.candidates:
        by_touch.setdefault(getattr(c, "touch", 1), []).append(c)
    total = 0
    for touch, cands in sorted(by_touch.items()):
        total += seed.queue(cands, touch=touch)["queued"]
    print()
    print(f"staged {total} as HELD. Release per touch with:")
    for touch in sorted(by_touch):
        print(f"  python src/sms_agent/cli.py release --touch {touch}")
    return 0


def cmd_reschedule(args) -> int:
    """Re-time the whole staged batch as one properly spaced sequence."""
    store.init()
    print(json.dumps(seed.reschedule_held(), indent=2))
    return 0


def cmd_cancel_held(args) -> int:
    """Drop every staged-but-unsent message. Used when a batch is superseded."""
    store.init()
    n = store.cancel_queued_all(args.reason or "superseded")
    print(f"cancelled {n} staged message(s)")
    return 0


def cmd_reconcile(args) -> int:
    """Poll the smrtPhone SMS log for replies the webhook never delivered."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    store.init()
    from . import reconcile

    result = reconcile.run(pages=args.pages, apply=not args.dry_run)
    print(json.dumps({k: v for k, v in result.items() if k != "results"}, indent=2))
    for r in result.get("results", [])[:20]:
        print(f"  {r['phone']}  -> {r['action']}")
    return 0


def cmd_digest(args) -> int:
    """The daily readout: funnel on top, work queue underneath."""
    from . import digest

    store.init()
    print(digest.run(days=args.days, post=args.post))
    if args.post:
        print()
        print("posted to the escalation channel")
    return 0


def cmd_selftest(args) -> int:
    """Exercise every phase offline. Sends nothing, writes nothing."""
    from . import selftest

    return selftest.run(live_model=args.live_model)


def cmd_seed(args) -> int:
    """Build (and optionally queue) outreach touches from a CSV export."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    store.init()
    if args.preset:
        rows, matched = seed.from_preset(args.preset, limit=args.limit or 0)
        if not matched:
            print(f"no preset matching {args.preset!r}. Try: cli.py presets")
            return 1
        print(f"preset: {matched} -> {len(rows)} mobile record(s)")
    else:
        path = Path(args.csv or "")
        if not path.exists():
            print(f"no such file: {path}")
            return 1
        rows = seed.from_csv(path)
    cands = seed.build(rows, touch=args.touch, sender_fallback=args.sender or "")
    info = seed.summary(cands)

    print(f"touch {args.touch}: {info['ready']} ready, {info['held']} held, of {info['total']}")
    for reason, n in info["held_reasons"].items():
        print(f"  held: {n:4}  {reason}")
    cap = info["capacity"]
    print(f"pool capacity today: {cap['remaining']} of {cap['capacity']}"
          f" across {cap['numbers']} number(s)")
    if info["ready"] > cap["remaining"]:
        print(f"  NOTE: {info['ready'] - cap['remaining']} would wait for tomorrow's capacity.")

    print()
    print("samples:")
    for cand in [c for c in cands if c.status == "ready"][: args.show]:
        print(f"  {cand.phone}  {cand.message}")

    if not args.queue:
        print()
        print("preview only. Pass --queue to stage these as HELD outbox rows.")
        return 0
    result = seed.queue(cands, touch=args.touch)
    print()
    print(f"staged {result['queued']} as HELD. Nothing sends until you run:")
    print(f"  python src/sms_agent/cli.py release --touch {args.touch}")
    return 0


def cmd_release(args) -> int:
    """Move held seed touches into the send queue. The go/no-go."""
    store.init()
    n = seed.release(args.touch, limit=args.limit or 0)
    print(f"released {n} touch-{args.touch} message(s) into the send queue")
    if config.DRY_RUN:
        print("SMS_AGENT_DRY_RUN is on, so the worker will log instead of send.")
    return 0


def cmd_senders(args) -> int:
    """Show which assignees resolve to a name, and which need mapping.

    The thread is signed by the person assigned to the record, so an unmapped
    uuid means that record's texts go out unsigned. Better unsigned than signed
    with a guess.
    """
    store.init()
    directory = crm._user_directory()
    print(f"{len(directory)} sender name(s) known ({config.SENDERS_FILE})")
    for uuid, name in directory.items():
        print(f"  {uuid}  ->  {name}")

    if args.record:
        rec = crm.get_record(args.record) or {}
        assigned = rec.get("assigned_to")
        if isinstance(assigned, dict):
            assigned = assigned.get("uuid") or assigned.get("id") or ""
        resolved = crm.sender_name_for(assigned or "")
        print(f"\nrecord {args.record}")
        print(f"  assigned_to  {assigned or '(unassigned)'}")
        print(f"  signs as     {resolved or '(unsigned - add this uuid to the senders file)'}")
    if not directory:
        print("\nAdd entries to the senders file as: {\"<assigned_to uuid>\": \"Adriana\"}")
        print("Get a uuid with: python src/sms_agent/cli.py senders --record <record_uuid>")
    return 0


def cmd_status(args) -> int:
    store.init()
    print(json.dumps({"stats": store.stats(), "capacity": sender_pool.capacity_today()}, indent=2))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="sms-agent", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("doctor", help="check wiring and print webhook URLs").set_defaults(fn=cmd_doctor)

    p = sub.add_parser("serve", help="run the webhook receiver")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p.set_defaults(fn=cmd_serve)

    p = sub.add_parser("work", help="drain events and the outbox")
    p.add_argument("--loop", action="store_true")
    p.add_argument("--interval", type=int, default=20)
    p.set_defaults(fn=cmd_work)

    p = sub.add_parser("map", help="backfill phone -> record mappings from a CSV")
    p.add_argument("--csv", required=True)
    p.add_argument("--all-phones", action="store_true",
                   help="also map every OTHER phone on each record (replies often "
                        "come from a different line)")
    p.set_defaults(fn=cmd_map)

    p = sub.add_parser("simulate", help="push a fake inbound through the real pipeline")
    p.add_argument("phone")
    p.add_argument("message")
    p.add_argument("--to", help="which of our numbers it came in on")
    p.set_defaults(fn=cmd_simulate)

    p = sub.add_parser("classify", help="classify a message, touch nothing")
    p.add_argument("message")
    p.set_defaults(fn=cmd_classify)

    p = sub.add_parser("draft", help="draft a reply for an existing thread")
    p.add_argument("phone")
    p.set_defaults(fn=cmd_draft)

    p = sub.add_parser("thread", help="print a conversation")
    p.add_argument("phone")
    p.set_defaults(fn=cmd_thread)

    p = sub.add_parser("approve", help="release a held draft into the send queue")
    p.add_argument("phone")
    p.set_defaults(fn=cmd_approve)

    p = sub.add_parser("pause", help="stop the agent on one conversation")
    p.add_argument("phone")
    p.add_argument("--reason")
    p.set_defaults(fn=cmd_pause)

    p = sub.add_parser("numbers", help="show or re-sync the sending pool from smrtPhone")
    p.add_argument("--refresh", action="store_true", help="re-pull from smrtPhone and rewrite the file")
    p.add_argument("--push", action="store_true", help="print the secret line for the live box")
    p.add_argument("--sync", action="store_true", help="refresh from smrtPhone and stage the live push")
    p.add_argument("--dry-run", action="store_true", help="with --refresh, do not write the file")
    p.set_defaults(fn=cmd_numbers)

    p = sub.add_parser("presets", help="list DataSift filter presets")
    p.add_argument("--filter", help="substring to narrow the list")
    p.set_defaults(fn=cmd_presets)

    p = sub.add_parser("backfill", help="classify real replies already in the CRM; sends nothing")
    p.add_argument("--queue", default="output/mms_send_queue.csv",
                   help="CSV of records we texted (record_uuid + phone)")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--json", help="write the structured result here")
    p.add_argument("--apply", action="store_true",
                   help="write opt-out / wrong-number consequences (dry unless --commit)")
    p.add_argument("--commit", action="store_true")
    p.set_defaults(fn=cmd_backfill)

    p = sub.add_parser("campaign", help="touch-aware run across the whole Hottest cadence")
    p.add_argument("--sender", help="fallback caller first name")
    p.add_argument("--show", type=int, default=6)
    p.add_argument("--log-pages", type=int, default=6, help="pages of SMS history to read")
    p.add_argument("--queue", action="store_true", help="stage as HELD outbox rows")
    p.set_defaults(fn=cmd_campaign)

    sub.add_parser("reschedule", help="re-time the staged batch as one sequence").set_defaults(fn=cmd_reschedule)

    p = sub.add_parser("cancel-held", help="drop every staged-but-unsent message")
    p.add_argument("--reason")
    p.set_defaults(fn=cmd_cancel_held)

    p = sub.add_parser("reconcile", help="catch inbound replies the webhook missed")
    p.add_argument("--pages", type=int, default=2, help="pages of SMS log to scan (200 each)")
    p.add_argument("--dry-run", action="store_true", help="find them but do not process")
    p.set_defaults(fn=cmd_reconcile)

    p = sub.add_parser("digest", help="daily readout of the funnel and the work queue")
    p.add_argument("--days", type=int, default=1)
    p.add_argument("--post", action="store_true", help="also post it to Slack")
    p.set_defaults(fn=cmd_digest)

    p = sub.add_parser("selftest", help="exercise every phase offline; sends nothing")
    p.add_argument("--live-model", action="store_true",
                   help="also exercise the real Claude calls (costs a few cents)")
    p.set_defaults(fn=cmd_selftest)

    p = sub.add_parser("seed", help="build outreach touches from a preset or a CSV")
    p.add_argument("--preset", help="DataSift filter preset title, e.g. 'Hottest - 02 Ready to Call'")
    p.add_argument("--csv", help="CSV export (alternative to --preset)")
    p.add_argument("--limit", type=int, default=0, help="cap how many records to pull")
    p.add_argument("--touch", type=int, default=1, choices=[1, 2, 3, 4])
    p.add_argument("--sender", help="fallback caller first name")
    p.add_argument("--show", type=int, default=5)
    p.add_argument("--queue", action="store_true", help="stage as HELD outbox rows")
    p.set_defaults(fn=cmd_seed)

    p = sub.add_parser("release", help="release held seed touches into the send queue")
    p.add_argument("--touch", type=int, required=True, choices=[1, 2, 3, 4])
    p.add_argument("--limit", type=int, default=0)
    p.set_defaults(fn=cmd_release)

    p = sub.add_parser("senders", help="show assignee -> caller first name mapping")
    p.add_argument("--record", help="also show which name this record would sign as")
    p.set_defaults(fn=cmd_senders)

    sub.add_parser("status", help="counters and pool capacity").set_defaults(fn=cmd_status)

    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
