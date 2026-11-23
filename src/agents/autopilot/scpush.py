"""Hand a released text batch to the texting app, which loads it into Smarter Contact (2026-11-18).

The Smarter Contact login lives on the texting app and nowhere else, so the brain never talks to
Smarter Contact: it posts the load (textlane.build_load) to the texting app's load route and reads
back what was created. AGENTS_SC_LOAD_URL is the route (https://<texting app>/hooks/scload) and
AGENTS_SC_LOAD_TOKEN the bearer token it checks, sent in a header so no access log ever prints it;
the token is never written to a log, a problem line or a post. Either unset, nothing is handed over
and the stage says so.

The revocation stage uses the same route for its Smarter Contact half (revoke, job_status): a job
with "action": "revoke" asks the texting app to put an untouchable owner's numbers on Smarter
Contact's do-not-contact list, and the answer comes back number by number.
"""
from __future__ import annotations

import os
import time

import requests

WAIT_S = 300            # how long a stage run waits for Smarter Contact to finish building
POLL_S = 10
REVOKE_WAIT_S = 120     # a do-not-contact job still running after this is read on the stage's next run


def url() -> str:
    return (os.environ.get("AGENTS_SC_LOAD_URL") or "").strip().rstrip("/")


def token() -> str:
    return (os.environ.get("AGENTS_SC_LOAD_TOKEN") or "").strip()


def ready() -> bool:
    """True when this box can hand work to the texting app (both settings are present)."""
    return bool(url() and token())


def _clean(text: str) -> str:
    out = str(text)
    t = token()
    return out.replace(t, "<the token>") if t else out


def hand_over(load: dict, *, dry: bool = False, wait_s: float = WAIT_S, poll_s: float = POLL_S,
              session=None, sleep=time.sleep) -> dict:
    """Post the load and wait for the texting app's verdict. Returns its status document
    ({status: done|failed|loading, result: {...}}) or {status: "not handed over", why}."""
    return _send(load, dry=dry, wait_s=wait_s, poll_s=poll_s, session=session, sleep=sleep)


def revoke(job: dict, *, dry: bool = False, wait_s: float = REVOKE_WAIT_S, poll_s: float = POLL_S,
           session=None, sleep=time.sleep) -> dict:
    """Post a do-not-contact job ({"action": "revoke", "batch", "job", "owners"}) and wait for the
    texting app's verdict. Returns its status document ({status, action, job, result: {numbers: [{record,
    number, outcome, detail}], counts, problems}}) or {status: "not handed over", why}. `dry` asks
    the texting app to look the numbers up and mark nothing."""
    return _send(job, dry=dry, wait_s=wait_s, poll_s=poll_s, session=session, sleep=sleep)


def job_status(batch: int, *, dry: bool = False, session=None) -> dict:
    """One read of a job's status document, for a job an earlier run stopped waiting on. A read that
    fails comes back as {status: "loading", why} so the caller keeps waiting instead of giving up."""
    target = url()
    if not target or not token():
        return {"status": "not handed over", "why": "AGENTS_SC_LOAD_URL or AGENTS_SC_LOAD_TOKEN is not set on this box"}
    http = session or requests
    try:
        g = http.get("%s/%d%s" % (target, int(batch), "?dry=1" if dry else ""),
                     headers={"authorization": "Bearer " + token()}, timeout=30)
        if g.status_code == 200:
            return g.json()
        return {"status": "loading", "why": "the texting app answered HTTP %d" % g.status_code}
    except Exception as exc:  # noqa: BLE001
        return {"status": "loading", "why": _clean(str(exc)[:120])}


def _send(load: dict, *, dry: bool, wait_s: float, poll_s: float, session, sleep) -> dict:
    target = url()
    if not target or not token():
        return {"status": "not handed over", "why": "AGENTS_SC_LOAD_URL or AGENTS_SC_LOAD_TOKEN is not set on this box"}
    http = session or requests
    q = "?dry=1" if dry else ""
    head = {"authorization": "Bearer " + token()}
    try:
        r = http.post(target + q, json=load, headers=head, timeout=30)
    except Exception as exc:  # noqa: BLE001
        return {"status": "not handed over", "why": _clean("the texting app did not answer (%s)" % str(exc)[:120])}
    if r.status_code != 200:
        return {"status": "not handed over", "why": "the texting app answered HTTP %d (%s)" % (r.status_code, _clean(r.text[:160]))}
    batch = int(load.get("batch") or 0)
    deadline = time.monotonic() + wait_s
    last: dict = {"status": "loading"}
    while True:
        try:
            g = http.get("%s/%d%s" % (target, batch, q), headers=head, timeout=30)
            if g.status_code == 200:
                last = g.json()
        except Exception as exc:  # noqa: BLE001
            last = {"status": "loading", "why": _clean(str(exc)[:120])}
        if last.get("status") in ("done", "failed"):
            return last
        if time.monotonic() >= deadline:
            last.setdefault("why", "still loading after %d seconds; the texting app keeps going and the result is "
                                   "read on the next run" % int(wait_s))
            return last
        sleep(poll_s)


def words(doc: dict) -> tuple[str, list[str]]:
    """(one line for the stage's head, problem lines) from the texting app's status document."""
    st = doc.get("status")
    res = doc.get("result") or {}
    if st == "not handed over":
        return "Released, but not handed to Smarter Contact: %s." % doc.get("why"), [str(doc.get("why"))]
    if res.get("refused"):
        return "Smarter Contact refused the load: %s." % res["refused"], [str(res["refused"])]
    if st == "loading":
        return "Handed to Smarter Contact; it was still building when this run stopped waiting.", []
    groups = res.get("groups") or []
    started = [g for g in groups if g.get("campaign")]
    texts = sum(int(g.get("contacts") or 0) for g in started)
    skipped = res.get("skipped") or {}
    line = "In Smarter Contact: %d campaign(s) for %d text(s), %d new contact(s)%s." % (
        len(started), texts, int(res.get("contacts_new") or 0),
        ("; left out " + ", ".join("%d %s" % (n, w) for w, n in skipped.items())) if skipped else "")
    return line, [str(p) for p in res.get("problems") or []]
