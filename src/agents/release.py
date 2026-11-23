"""One way to release a staged batch, used by the command line and by Slack.

A batch is work a stage worked out and a person must approve before it happens: mail pieces, the
attempt keeper's first correction, a day's texts. Until 2026-11-13 the only way to release one was
the command line on a machine holding the repo, so the person the plan gives that job (the
assistant who watches the AI, decision D37) had no way to do it. They do it in the team's Slack
channel now, in two steps:

    release batch 12      the brain says what batch 12 holds and what it costs
    confirm 12            within 15 minutes, by the same person: the batch is released

Four rules keep this from becoming a way around a person's judgment:
  * Only Slack users named in AGENTS_RELEASERS can release. Everyone else is told so, and nothing
    is staged for them to confirm.
  * A batch is released only where the stage that staged it would act on its own (rule(), the
    stages' own config.stage_live, 2026-11-19): every batch on a live box; on a free box a batch
    whose stage is live in the playbook and whose kind spends nothing, or a paid or outreach one
    whose stage AGENTS_LIVE_STAGES names; nothing on a dry box. A script command has no stage and
    waits for a live box. The release functions take a plain commit switch and do not look at the
    box, so this module checks it every time: when the person confirms, when the worker runs the
    release, and on the command line (`cli.py autopilot release N --commit`).
  * A model never releases. The command is read by a pattern in intake before any employee sees
    the message, and release_batch is not on the roster, so no employee has it as a tool.
  * Each release re-runs the gate on every item (the release functions already do), so a record
    that became a lead, died or opted out after the batch was staged is held back at release.

A new batch kind registers its release function in RELEASE, one line.
"""
from __future__ import annotations

import importlib
import json
import re
from datetime import datetime, timedelta, timezone

from . import config, store

# batch kind -> (module under agents, function). Each takes (batch_id, commit=..., **kw) and returns a dict.
RELEASE: dict[str, tuple[str, str]] = {
    "mail": ("autopilot.mail", "release"),
    "attempts": ("autopilot.attempts", "release"),
    "text": ("autopilot.textlane", "release"),
    "leadclock": ("autopilot.leadclock", "release"),
    "drip": ("autopilot.drips", "release"),
    "heir_mail": ("autopilot.heirlane", "release"),
    "heir_text": ("autopilot.heirlane", "release"),
    "obituary_mail": ("autopilot.obituary", "release"),
    "probate": ("autopilot.probate", "release"),
    "command": ("workflows.run_script", "release"),     # a script command line that writes or pays (run_script)
    "agency": ("autopilot.agencyexport", "release"),   # the monthly file for the calling agency
    "newhire": ("autopilot.newhire", "release"),       # a new hire's day of coaching notes, after the owner reads them
    "closer_text": ("autopilot.handoff", "release"),   # the closer's "I'll call you in a few minutes" text at the handoff
}
# Kinds whose release has two steps because a person uploads a file between them: the second step
# (after the upload) is the confirm with "uploaded".
FILE_KINDS = ("heir_mail", "heir_text", "obituary_mail", "agency")
LABELS = {"mail": "mail piece(s)", "attempts": "call counter correction(s)", "text": "owner text(s)",
          "leadclock": "card move(s)", "drip": "text(s) and email(s)", "heir_mail": "heir letter(s)",
          "heir_text": "heir text(s)", "obituary_mail": "obituary letter(s)", "probate": "probate change(s)",
          "command": "command(s)", "agency": "owner(s) for the calling agency",
          "newhire": "day(s) of new hire notes", "closer_text": "handoff text(s)"}


def label(kind: str) -> str:
    return LABELS.get(kind, "item(s)")
CONFIRM_MINUTES = 15
WORKFLOW = "release_batch"

_RELEASE_RX = re.compile(r"^\s*release\s+(?:batch\s+)?#?(\d+)\s*[.!]?\s*$", re.I)
_CONFIRM_RX = re.compile(r"^\s*confirm\s+(?:batch\s+)?#?(\d+)(\s+uploaded)?\s*[.!]?\s*$", re.I)


def releasers() -> set[str]:
    raw = getattr(config, "RELEASERS", "") or ""
    return {u.strip() for u in str(raw).replace(";", ",").split(",") if u.strip()}


def parse(text: str) -> tuple[str, int, bool] | None:
    """("release" | "confirm", batch id, uploaded) for a release command, else None."""
    t = (text or "").strip()
    m = _RELEASE_RX.match(t)
    if m:
        return "release", int(m.group(1)), False
    m = _CONFIRM_RX.match(t)
    if m:
        return "confirm", int(m.group(1)), bool(m.group(2))
    return None


def release_fn(kind: str):
    module, fn = RELEASE.get(kind) or (None, None)
    if not module:
        return None
    return getattr(importlib.import_module("%s.%s" % (__package__, module)), fn)


def describe(b: dict) -> str:
    """One plain line about a staged batch: what it is, how many items, what it costs."""
    n = len(b.get("items") or [])
    what = label(b.get("kind") or "")
    cost = " for about $%.2f" % float(b["usd"]) if float(b.get("usd") or 0) else ""
    return "batch %d from %s: %d %s%s, staged %s" % (b["id"], (b.get("stage") or "").replace("_", " "), n, what, cost,
                                                     str(b.get("created") or "")[:16].replace("T", " "))


def _policy():
    """The playbook rule() reads (the selftest puts its own here)."""
    from .autopilot import policy as policy_mod
    return policy_mod.load()


def stage_may_release(st) -> bool:
    """May a batch this stage staged be released on this box? On a live box, yes. On a free box, when
    the stage acts live on its own there: its playbook mode is live and its kind spends nothing, or
    AGENTS_LIVE_STAGES names it (config.stage_live). agencyexport.drain asks the same before it marks
    the rest of a released file."""
    if config.live():
        return True
    return st is not None and getattr(st, "mode", "") == "live" and config.stage_live(st.spend, st.id)


def rule(b: dict, pol=None) -> tuple[bool, str, str]:
    """(may this box release the batch, a short code for the intake log, why not in words)."""
    if config.live():
        return True, "", ""
    if config.MODE != "free":
        return False, "dry box", "the brain is in dry mode"
    b = b or {}
    if b.get("kind") == "command":
        return False, "live box only", "the brain is in free mode, and a script command is released on a live box only"
    try:
        pol = pol if pol is not None else _policy()
    except Exception as exc:  # noqa: BLE001  (a playbook that does not load releases nothing on a free box)
        return False, "no playbook", "the brain is in free mode and the playbook did not load (%s)" % str(exc)[:160]
    sid = str(b.get("stage") or "")
    st = (getattr(pol, "stages", None) or {}).get(sid)
    if st is None:
        return False, "no stage", "the brain is in free mode and the batch's stage %s is not in the playbook" % (sid or "(none)")
    if st.mode != "live":
        return False, "stage not live", "the brain is in free mode and stage %s is in %s mode in the playbook" % (sid, st.mode)
    if not stage_may_release(st):
        return False, "stage not live", ("the brain is in free mode and stage %s spends on %s without AGENTS_LIVE_STAGES "
                                         "naming it" % (sid, st.spend))
    return True, "", ""


def run(batch_id: int, *, commit: bool = False, uploaded: bool = False, **kw) -> dict:
    """Release one batch through its kind's own function. Dry unless commit. A commit this box may not
    make (rule) is refused before the kind's function runs, from the command line and from the worker."""
    from .autopilot import ledger
    b = ledger.batch(batch_id)
    if not b:
        raise SystemExit("no batch %d" % batch_id)
    fn = release_fn(b["kind"])
    if fn is None:
        raise SystemExit("batch %d is a %s batch, and nothing is registered to release that kind" % (batch_id, b["kind"]))
    if commit:
        ok, _, why = rule(b)
        if not ok:
            raise SystemExit("batch %d was not released: %s. Nothing changed; it is still staged." % (batch_id, why))
    if b["kind"] == "mail" or b["kind"] in FILE_KINDS:
        kw["uploaded"] = uploaded
    return fn(batch_id, commit=commit, **kw)


def _key(user: str, batch_id: int) -> str:
    return "release:pending:%s:%d" % (user, batch_id)


def slack_command(cmd: tuple[str, int, bool], user: str, channel: str, thread: str, now: datetime | None = None) -> dict:
    """Handle a release or a confirm typed in Slack. Returns what it did, for the intake log; every
    answer is posted in the thread of the command."""
    from . import slack_app
    from .autopilot import ledger
    action, batch_id, uploaded = cmd
    now = now or datetime.now(timezone.utc)

    def say(text: str) -> None:
        slack_app.post(channel, text, thread_ts=thread)

    if user not in releasers():
        say("Only a person named as a releaser can release a batch. The owner adds a person by their Slack id "
            "(AGENTS_RELEASERS).")
        return {"release": "refused", "why": "not a releaser"}
    b = ledger.batch(batch_id)
    if not b:
        say("There is no batch %d." % batch_id)
        return {"release": "refused", "why": "no batch"}
    if b["status"] != "staged":
        say("Batch %d is %s, so there is nothing to release." % (batch_id, b["status"]))
        return {"release": "refused", "why": "not staged"}
    if b["kind"] not in RELEASE:
        say("Batch %d is a %s batch, and nothing can release that kind yet." % (batch_id, b["kind"]))
        return {"release": "refused", "why": "unknown kind"}

    if action == "release":
        store.kv_set(_key(user, batch_id), now.isoformat(timespec="seconds"))
        extra = ""
        if b["kind"] == "command" and b.get("items"):
            extra = " Command: `%s`." % (b["items"][0].get("command") or "")
        if b["kind"] == "mail" and _export_only(b):
            extra = (" It is a file for the mail house: upload it there first, then reply `confirm %d uploaded`."
                     % batch_id)
        if b["kind"] == "agency":
            from .autopilot import agencyexport
            extra = " " + agencyexport.release_note(b)
        if b["kind"] == "newhire":
            from .autopilot import newhire
            extra = " " + newhire.release_note(b)
        ok, code, why = rule(b)
        mode = "" if ok else (" The brain is in dry mode, so a confirm will not send anything yet." if code == "dry box"
                              else " %s%s, so a confirm will not release it yet." % (why[:1].upper(), why[1:]))
        say("%s. Reply `confirm %d` within %d minutes to release it.%s%s" % (
            describe(b)[0].upper() + describe(b)[1:], batch_id, CONFIRM_MINUTES, extra, mode))
        return {"release": "asked", "batch": batch_id}

    asked = store.kv_get(_key(user, batch_id))
    try:
        asked_at = datetime.fromisoformat(asked) if asked else None
    except ValueError:
        asked_at = None
    if not asked_at:
        say("Ask first with `release batch %d`, so you see what it holds before it goes." % batch_id)
        return {"release": "refused", "why": "not asked"}
    if now - asked_at > timedelta(minutes=CONFIRM_MINUTES):
        store.kv_set(_key(user, batch_id), "")
        say("That was more than %d minutes ago. Ask again with `release batch %d`." % (CONFIRM_MINUTES, batch_id))
        return {"release": "refused", "why": "expired"}
    store.kv_set(_key(user, batch_id), "")          # one confirm, one release
    ok, code, why = rule(b)
    if not ok:
        if code == "dry box":
            say("The brain is in dry mode, so nothing was released. Batch %d is still staged." % batch_id)
        else:
            say("Nothing was released: %s. Batch %d is still staged." % (why, batch_id))
        return {"release": "refused", "why": code}
    tid, _ = store.enqueue("ops", WORKFLOW, "", subject="release batch %d" % batch_id,
                           payload={"batch": batch_id, "uploaded": uploaded, "by": user},
                           mode="direct", source="slack", slack_channel=channel, slack_thread=thread)
    say("Releasing batch %d now (task %d). I will post the result here." % (batch_id, tid))
    return {"release": "queued", "batch": batch_id, "task": tid}


def _export_only(b: dict) -> bool:
    if b.get("kind") in FILE_KINDS:
        return True
    try:
        from .autopilot import mail
        from .autopilot import policy as policy_mod
        return bool(mail.vendor_for(policy_mod.load().stage(b["stage"])).export_only)
    except Exception:  # noqa: BLE001  (a stage that no longer exists: the release itself will say so)
        return False


# ------------------------------------------------------------------ the worker's side (workflows.REGISTRY)

def task_run(task: dict) -> dict:
    """The queued release. The rule is checked again here: the box may have been put back in dry mode,
    or the stage taken off AGENTS_LIVE_STAGES or out of live, between the confirm and this run."""
    from .autopilot import ledger
    p = store.payload_of(task)
    try:
        b = ledger.batch(int(p.get("batch") or 0)) or {"id": p.get("batch")}
    except (TypeError, ValueError):
        b = {"id": p.get("batch")}
    ok, _, why = rule(b)
    if not ok:
        return {"batch": p.get("batch"), "released": False, "why": why}
    out = run(int(p["batch"]), commit=True, uploaded=bool(p.get("uploaded")))
    return {"batch": p.get("batch"), "released": True, "result": out, "by": p.get("by", "")}


def task_render(task: dict, result: dict) -> str:
    if not result.get("released"):
        return "Batch %s was not released: %s." % (result.get("batch"), result.get("why") or "see the log")
    r = result.get("result") or {}
    bits = []
    if "sent" in r:
        bits.append("%s of %s sent" % (r.get("sent"), r.get("of", "?")))
    if r.get("usd"):
        bits.append("$%.2f" % float(r["usd"]))
    if r.get("results"):
        bits.append(", ".join("%s %s" % (v, k) for k, v in r["results"].items()))
    probs = r.get("problems") or []
    line = "Batch %s released: %s." % (result.get("batch"), "; ".join(bits) or json.dumps(r, default=str)[:200])
    if r.get("next"):
        line += "\nNext: %s.%s" % (r["next"], (" File: %s" % r["file"]) if r.get("file") else "")
    if probs:
        line += "\n" + "\n".join(":small_orange_diamond: %s" % x for x in probs[:3])
    return line
