"""Pull property ids into a DataSift account so its own unlimited skip trace runs, then read the phones back.

Input is any JSON list whose rows carry a `dataflik_id`: a pool from ai_pools.py, the `properties` index
behind the opensearch MCP, a SiftMap export, a saved cohort. Nothing outside DataSift is called (no
Tracerfy, no Trestle, no vendor spend). The route is d4d_call_list's, verified 2026-09-11 on 1,714 records
and 2026-09-30 on 30,000: add-properties-by-query with an ids-typed address list lands the records (async,
about 100-250 a minute on data+staging), attaches the list to ids that were already records, and the add
skip traces new records on its own on the unlimited plan at $0.

  python src/pull_by_ids.py --ids <pool.json> --list "<name>"                  # dry, ty+2
  python src/pull_by_ids.py --ids <pool.json> --list "<name>" --commit         # probe one, send the rest, wait
  python src/pull_by_ids.py --ids <pool.json> --list "<name>" --phase qa       # list count, untraced count
  python src/pull_by_ids.py --ids <pool.json> --list "<name>" --phase phones   # read DataSift's phones back

Which account
  default          ty+2 (the .env login or REISIFT_API_KEY)
  --key-env NAME   another account by its Open API key (.env NAME) plus --expect-email <a user on it>; the
                   search and the add route both take the key (verified 2026-11-05)
  --target EMAIL   an allowlisted account by a pasted bearer token in REISIFT_TARGET_JWT_FILE; the token's
                   email AND account id must match before anything is written

pull    probe one new id with a read-back, then send the rest in chunks. Ids that were already records go
        through the add route too (--existing add, the Sep 2026 route) or get the list one record at a
        time (--existing attach). Resumable: an id is never sent twice.
qa      how many records the list holds and how many the skip trace has not reached yet. Read-only.
skiptrace  trace the list's untraced records (an existing record that only gets the list is never traced).
        Guarded: unlimited plan only, a free estimate no wider than the list's own untraced count, and the
        account's traced counter read before and after. Dry by default.
phones  read each record's owner phones, which is what DataSift's skip trace wrote. Read-only and about
        two records a second, so a 20,000 pool takes hours.

The add's own skip trace lags: 2 of 7 new records still sat at owner.skiptraced false with 0 attempts 10
minutes after landing (2026-11-05) and were traced within the hour. An untraced record reads like a no-hit
one, so qa and phones both count them. The skip-trace route ignores a uuid scope and once traced a whole account
(datasift_skiptrace.py); only a list-query scope confirmed by an estimate is safe there.

State lives in output/pull_by_ids/[<account>/]<ids folder>_<ids file stem>/. The folder is part of the
name because two pools both called investor.json once shared one state file: the second pool's probe read
the first pool's and was skipped (2026-11-05).
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from datasift_api_upload import Api, attach_to_record, env  # noqa: E402
import d4d_call_list as d4d  # noqa: E402
import siftmap_pull as sp  # noqa: E402

# Same allowlist as franklin_top30k.TARGETS: a pasted token may only ever write to one of these.
TARGETS = {"ty+1@dataflik.com": "e2415c08-d4f1-4c70-9b6c-5dcc70d4a23d",
           "data+staging@dataflik.com": "478569d5-d3e6-41ca-bb34-c3c5885aee1c"}


class _SessionApi:
    """reisift_session.Session shaped like datasift_api_upload.Api, so d4d._call, attach_to_record and
    siftmap_pull.Map run on a pasted token. Session.call takes method/body as keywords and the method
    override as its own argument, and raises SessionError ("HTTP 429 on ...") where d4d._call's backoff
    expects ApiError; calling it the Api way raised a TypeError before the first write (caught 2026-11-05)."""

    def __init__(self, session):
        self.s, self.api_key = session, ""

    @property
    def token(self):
        return self.s.token

    def _mint(self):
        self.s._mint()

    def call(self, path, method="GET", body=None, _retry=True, headers=None):
        from datasift_api_upload import ApiError
        from reisift_session import SessionError
        try:
            return self.s.call(path, method=method, body=body,
                               method_override=(headers or {}).get("x-http-method-override"))
        except SessionError as e:
            hit = re.search(r"HTTP (\d{3})", str(e))
            if not hit:
                raise
            raise ApiError(int(hit.group(1)), method, path, str(e)) from e


class _MapSearch:
    """search_by_ids / poll_saved call `_request`; siftmap_pull.Map speaks a key and a pasted JWT alike."""

    def __init__(self, api):
        self.map = sp.Map(api)

    def _request(self, path, method="POST", body=None):
        return self.map._post(path, body or {})


def clients(args) -> tuple:
    """(CRM client, SiftMap client) for the target: ty+2's default login, a --key-env key, or a --target token."""
    if args.target:
        if args.target not in TARGETS:
            raise SystemExit("REFUSING: %s is not an allowed target %s" % (args.target, sorted(TARGETS)))
        f = os.environ.get("REISIFT_TARGET_JWT_FILE", "").strip()
        if f and not os.environ.get("REISIFT_TARGET_JWT"):
            os.environ["REISIFT_TARGET_JWT"] = Path(f).read_text(encoding="utf-8").strip()
        from reisift_session import Session
        s = Session(target_email=args.target)
        ident = s.verify_target()
        d4d.gate(ident.get("account") == TARGETS[args.target],
                 "token account %s is not %s (%s)" % (ident.get("account"), args.target, TARGETS[args.target]))
        api = _SessionApi(s)
        return api, _MapSearch(api)
    if not args.key_env:
        return Api(), d4d._siftmap()
    key = (os.environ.get(args.key_env) or env().get(args.key_env) or "").strip()
    d4d.gate(bool(key), "%s is not set; put the account's Open API key in .env" % args.key_env)
    from siftmap_standalone import SiftMapClient
    return Api(api_key=key), SiftMapClient(key, min_interval=0.5)


def verify_account(api, expect: str, commit: bool) -> None:
    """The key decides which account gets written, so a named user must be on it before anything is."""
    r = d4d._call(api, "/api/internal/account/user/")
    rows = r.get("results") if isinstance(r, dict) else r
    emails = {str(u.get("email") or "").strip().lower() for u in rows or [] if isinstance(u, dict)}
    ok = bool(expect) and expect.strip().lower() in emails
    print("target account: %d users; %s %s" % (len(emails), expect or "no --expect-email given",
                                                "is a user" if ok else "is NOT a user"))
    if commit:
        d4d.gate(ok, "refusing to write: --expect-email must be a user on the key's account")


def load_ids(path: Path) -> list:
    rows = json.loads(path.read_text(encoding="utf-8"))
    ids = []
    for r in rows:
        i = r.get("dataflik_id") if isinstance(r, dict) else r
        if i not in (None, "") and str(i) not in ids:
            ids.append(str(i))
    d4d.gate(bool(ids), "no dataflik_id in %s" % path)
    return ids


def saved_uuids(m, ids: list) -> tuple:
    """(id -> saved_uuid for ids already in the account, ids SiftMap does not know at all)."""
    saved, seen = {}, set()
    for i in range(0, len(ids), 100):
        for h in d4d.search_by_ids(m, ids[i:i + 100]):
            seen.add(str(h.get("id")))
            if h.get("saved") and h.get("saved_uuid"):
                saved[str(h.get("id"))] = h["saved_uuid"]
        if i and i % 5000 == 0:
            print("  checked %d of %d ids" % (i, len(ids)), flush=True)
    return saved, [i for i in ids if i not in seen]


def list_uuid(api, title: str) -> str:
    r = d4d._call(api, "/api/internal/list/?limit=999")
    rows = r.get("results") if isinstance(r, dict) else r
    return next((x.get("uuid") for x in rows or [] if (x.get("title") or "").strip().casefold()
                 == title.strip().casefold()), "")


def list_count(api, uuid: str, **must) -> int:
    r = d4d._call(api, "/api/internal/property/", "POST", headers={"x-http-method-override": "GET"},
                  body={"limit": 1, "offset": 0, "query": {"must": {"any_lists": [uuid], **must}}})
    return int(r.get("count", 0))


def phase_pull(args, run: Path, tag: str) -> None:
    ids = load_ids(Path(args.ids))
    api, m = clients(args)
    if args.key_env:
        verify_account(api, args.expect_email, args.commit)
    st = d4d._load(run / "state.json", {})
    new = dict(st.get("new_uuids") or {})
    sent = set(st.get("sent") or [])
    saved, unknown = saved_uuids(m, ids)
    if st.get("sent") and st.get("existing") is not None:
        # Who was already a record is decided once, before the first send. Recomputed after landing it
        # would relabel every new record as existing.
        existing = dict(st["existing"])
        new.update({i: u for i, u in saved.items() if i not in existing and i not in new})
    else:
        existing = {i: u for i, u in saved.items() if i not in new}
    todo = [i for i in ids if i not in saved and i not in unknown and i not in sent]
    used, limit = sp.usage(api)
    print("pull: %d ids, %d already records, %d to pull, %d unknown to SiftMap, %d sent before; allowance %s / %s"
          % (len(ids), len(existing), len(todo), len(unknown), len(sent), used, limit), flush=True)
    d4d.gate(used + len(todo) <= limit, "the pull would cross the record allowance")
    if not args.commit:
        print("DRY RUN. Would probe one id, pull %d, then put %d existing records on list %r + tag %r (%s). "
              "Re-run with --commit." % (len(todo), len(existing), args.list_name, tag, args.existing))
        return

    def save(**more):
        d4d._save(run / "state.json", {**st, "new_uuids": new, "sent": sorted(sent), "existing": existing,
                                       "unknown": unknown, "list": args.list_name, "tag": tag,
                                       "when": datetime.now().isoformat(timespec="seconds"), **more})

    d4d._ensure_title(api, "list", args.list_name, True)
    d4d._ensure_title(api, "tag", tag, True)
    if todo and not st.get("probed"):
        probe = todo.pop(0)
        resp = d4d.add_by_ids(api, [probe], args.list_name, tag, "A")
        sent.add(probe)
        print("  probe %s -> %s" % (probe, str(resp)[:120]), flush=True)
        landed = d4d.poll_saved(m, [probe], wait_min=args.probe_wait)
        d4d.gate(bool(landed), "probe id %s did not land in %s min; nothing batched" % (probe, args.probe_wait))
        rec = d4d._call(api, "/api/internal/property/%s/" % landed[probe])
        lists = [(x.get("title") if isinstance(x, dict) else x) for x in rec.get("lists") or []]
        ok = str(rec.get("dataflik_id")) == probe
        print("  read-back: dataflik_id match %s, on list %s" % (ok, args.list_name in lists), flush=True)
        d4d.gate(ok, "probe landed with a different dataflik_id")
        if args.list_name not in lists:
            attach_to_record(api, landed[probe], {"lists": [args.list_name], "tags": [tag]})
        new[probe] = landed[probe]
        st["probed"] = True
        save()
    if args.probe_only:
        # Probing every pool before any batch keeps each probe off the back of another pool's queue.
        print("probe only: %s; re-run --commit to send the other %d ids" % (
            "probed" if st.get("probed") else "no new id to probe", len(todo)), flush=True)
        return

    batch = list(todo)
    if args.existing == "add":
        batch += [i for i in existing if i not in sent]
    for n, i in enumerate(range(0, len(batch), args.chunk), 1):
        chunk = batch[i:i + args.chunk]
        resp = d4d.add_by_ids(api, chunk, args.list_name, tag, "A")
        sent.update(chunk)
        save()
        if n == 1 or n % 10 == 0 or i + args.chunk >= len(batch):
            print("  chunk %d: %d ids -> %s (%d sent)" % (n, len(chunk), str(resp)[:60], len(sent)), flush=True)
        time.sleep(2)

    if args.existing == "attach":
        attached = set(st.get("attached") or [])
        for u in existing.values():
            if u not in attached:
                attach_to_record(api, u, {"lists": [args.list_name], "tags": [tag]})
                attached.add(u)
                time.sleep(0.3)
        st["attached"] = sorted(attached)

    pending = [i for i in sent if i not in new and i not in existing]
    if pending:
        new.update(d4d.poll_saved(m, pending, wait_min=args.wait_min, every=60.0))
    still = [i for i in sent if i not in new and i not in existing]
    save(pending=still)
    print("pull: %d new records landed, %d existing sent through %s, %d new still landing (re-run --commit "
          "to keep waiting; nothing is re-sent)" % (len(new), len(existing), args.existing, len(still)), flush=True)


def phase_qa(args, run: Path) -> None:
    st = d4d._load(run / "state.json", {})
    api = clients(args)[0]
    uuid = list_uuid(api, args.list_name)
    d4d.gate(bool(uuid), "list %r does not exist on this account" % args.list_name)
    held, untraced = list_count(api, uuid), list_count(api, uuid, skiptraced=0)
    with_phone = list_count(api, uuid, phone=1)
    ids = len(load_ids(Path(args.ids)))
    print("qa: list %r holds %d of %d ids; %d with a phone; %d not skip traced yet; state: %d new landed, "
          "%d existing, %d still landing" % (args.list_name, held, ids, with_phone, untraced,
                                             len(st.get("new_uuids") or {}), len(st.get("existing") or {}),
                                             len(st.get("pending") or [])))
    d4d._save(run / "qa.json", {"list": args.list_name, "list_uuid": uuid, "held": held, "ids": ids,
                                "with_phone": with_phone, "untraced": untraced,
                                "when": datetime.now().isoformat(timespec="seconds")})


SKIP_TRACE = "/api/internal/property/skip-trace/"
SKIP_STATS = "/api/internal/activity/skiptrace/stats/"


def phase_skiptrace(args, run: Path) -> None:
    """Skip trace the list's records nobody has traced. Attaching a list to a record that already existed
    never triggers a trace, and on 2026-11-05 most of a 6,251-record list sat untraced for that reason.
    The route ignores a uuid scope and once traced a whole account (datasift_skiptrace.py), so the
    list-query scope must be confirmed by a free estimate no wider than an independent count first."""
    api = clients(args)[0]
    uuid = list_uuid(api, args.list_name)
    d4d.gate(bool(uuid), "list %r does not exist on this account" % args.list_name)
    stats = d4d._call(api, SKIP_STATS)
    spent, saved = float(stats.get("value_spent") or 0), float(stats.get("saved_by_unlimited_skiptracing") or 0)
    d4d.gate(spent == 0 and saved > 0, "refusing: not the unlimited plan (value_spent %s, saved_by_unlimited %s)"
             % (spent, saved))
    body = {"query": {"must": {"any_lists": [uuid], "skiptraced": 0}}}
    want = list_count(api, uuid, skiptraced=0)
    est = d4d._call(api, SKIP_TRACE, "POST", dict(body, estimate=1))
    d4d.gate(isinstance(est, dict) and "number_of_records" in est, "unexpected estimate response: %s" % str(est)[:200])
    n, cost = int(est.get("number_of_records") or 0), float(est.get("cost") or 0)
    print("skiptrace: %d untraced on %r; the estimate covers %d at $%.2f" % (want, args.list_name, n, cost), flush=True)
    d4d.gate(cost == 0, "refusing: the estimate is not free")
    d4d.gate(n <= max(int(want * 1.02), want + 25),
             "refusing: the estimate (%d) is wider than the list's untraced records (%d), so the scope is not held" % (n, want))
    if not args.commit or n == 0:
        print("%s nothing sent" % ("DRY RUN." if n else "Nothing to trace:"))
        return
    before = int(stats.get("total_properties") or 0)
    resp = d4d._call(api, SKIP_TRACE, "POST", body)
    time.sleep(10)
    after = int(d4d._call(api, SKIP_STATS).get("total_properties") or 0)
    print("skiptrace: sent; response %s; account traced count %d -> %d (+%d)" % (
        str(resp)[:120], before, after, after - before), flush=True)
    d4d._save(run / ("skiptrace_%s.json" % datetime.now().strftime("%Y%m%d_%H%M%S")),
              {"list": args.list_name, "untraced": want, "estimate": n, "response": resp,
               "traced_before": before, "traced_after": after})


def phase_phones(args, run: Path) -> None:
    st = d4d._load(run / "state.json", {})
    d4d.gate(bool(st.get("new_uuids") or st.get("existing")), "pull --commit has not landed anything for %s" % args.ids)
    new = st.get("new_uuids") or {}
    uuids = {**(st.get("existing") or {}), **new}
    api = clients(args)[0]
    rows = []
    for i, u in uuids.items():
        try:
            rec = d4d._call(api, "/api/internal/property/%s/" % u)
        except Exception as e:  # noqa: BLE001
            print("  read failed %s: %s" % (u, str(e)[:100]))
            continue
        x = d4d._slim(rec, i, i in new)
        live = [p for p in x["phones"] if (p.get("status") or "").upper() not in d4d.BAD_STATUSES]
        ow = rec.get("owner") or {}
        x["skiptraced"], x["skiptrace_attempts"] = bool(ow.get("skiptraced")), ow.get("skiptrace_attempts") or 0
        x["live_phones"] = [d4d.fmt_phone(p.get("number") or "") for p in live]
        rows.append(x)
    d4d.gate(bool(rows), "no records read")
    d4d._save(run / "records.json", rows)
    with (run / "phones.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["dataflik_id", "new_record", "skiptraced", "owner", "street", "city", "zip", "live_phones",
                    "all_phones", "crm_url"])
        for x in rows:
            owner = " ".join(s for s in (x["owner_first"], x["owner_last"]) if s) or x["owner_company"]
            w.writerow([x["dataflik_id"], x["pulled"], x["skiptraced"], owner, x["street"], x["city"], x["zip"],
                        "; ".join(x["live_phones"]), len(x["phones"]), d4d.CRM_URL % x["uuid"]])
    for label, grp in (("new", [x for x in rows if x["pulled"]]), ("existing", [x for x in rows if not x["pulled"]])):
        if grp:
            hit = sum(1 for x in grp if x["live_phones"])
            untraced = sum(1 for x in grp if not x["skiptraced"])
            print("phones: %d %s records, %d with a live phone, %d phones total, %d not skip traced yet" % (
                len(grp), label, hit, sum(len(x["phones"]) for x in grp), untraced))
    print("wrote %s" % (run / "phones.csv"))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--ids", required=True, help="JSON list of rows carrying dataflik_id")
    ap.add_argument("--list", dest="list_name", required=True, help="DataSift list the records go on")
    ap.add_argument("--tag", default="", help="tag to add (default: the list name)")
    ap.add_argument("--target", default="", help="allowlisted account email, token in REISIFT_TARGET_JWT_FILE")
    ap.add_argument("--key-env", default="", help="env var holding another account's Open API key (default: ty+2)")
    ap.add_argument("--expect-email", default="", help="with --key-env: a user on that account; writes refuse otherwise")
    ap.add_argument("--phase", default="pull", choices=["pull", "qa", "skiptrace", "phones"])
    ap.add_argument("--commit", action="store_true", help="pull: write to the account (dry by default)")
    ap.add_argument("--existing", default="add", choices=["add", "attach"],
                    help="how ids that are already records get the list")
    ap.add_argument("--probe-only", action="store_true", help="pull: land and read back one new id, then stop")
    ap.add_argument("--chunk", type=int, default=250, help="ids per add call")
    ap.add_argument("--probe-wait", type=float, default=5.0, help="minutes to wait for the probe id")
    ap.add_argument("--wait-min", type=float, default=15.0, help="minutes to wait for the batch to land")
    args = ap.parse_args()
    d4d.gate(not (args.target and args.key_env), "pass --target or --key-env, not both")
    account = args.key_env.lower() or args.target.split("@")[0].replace("+", "_")
    ids_path = Path(args.ids).resolve()
    run = Path("output/pull_by_ids") / account / ("%s_%s" % (ids_path.parent.name, ids_path.stem))
    if args.phase == "pull":
        phase_pull(args, run, args.tag or args.list_name)
    elif args.phase == "qa":
        phase_qa(args, run)
    elif args.phase == "skiptrace":
        phase_skiptrace(args, run)
    else:
        phase_phones(args, run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
