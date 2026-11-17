"""Revocation (build plan 0.17): a no stops what is already in motion.

The gate checks a record when it is picked up and again when a batch is released. Between those
two moments an owner can opt out, die, sell or become a lead, and work already queued for them
keeps going unless something pulls it. This stage is that something. It reads the day's status and
tag changes from the shared log, finds the owners who just became untouchable, and takes them out
of what is still waiting:

  * staged mail batches (the autopilot's own): the owner's records are removed before anyone
    releases the batch;
  * Smarter Contact, when this box holds its credentials: every phone on the owner's record is
    marked do-not-contact there, so a campaign that is still sending skips them. A new LEAD is not
    marked do-not-contact (the lead manager still texts them one to one); the stage only says
    which leads still have numbers in a campaign.

It works by OWNER, not by phone. Five of nine measured replies came from a different number than
the one that was texted, so pulling only the number that answered leaves the others queued.

The texting app's own queue lives in another app and is pulled there, by the same gate, right
before each send.
"""
from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime, timedelta

from .. import store
from . import activity, gate, ledger
from .stages import Ctx, addr, tag_titles

log = logging.getLogger(__name__)

# Two high-water marks, on purpose. A plan run reports each change once and moves only the PLAN
# mark. The LIVE mark moves only when something was really pulled, so the first live run starts
# from its own window and does not skip the owners that plan runs merely listed.
KV = "autopilot:revoke:through"
KV_PLAN = "autopilot:revoke:plan_through"


def _suppressing(e: dict, cfg: gate.GateConfig) -> tuple[str, str]:
    """(why, kind) when this logged change makes a record untouchable, else ("", "")."""
    et, d = e["event_type"], e["data"]
    if et == "property.status.updated":
        to = str(d.get("to") or "").strip().lower()
        code, _ = gate._status_rule(to, cfg)
        if to in ("dnc", "opt_out"):
            return "the owner opted out", "stop"
        if code == "sold":
            return "the house sold", "stop"
        if code == "lead":
            return "became a lead (%s)" % to, "lead"
        if code == "finished":
            return "status %s" % to, "stop"
        return "", ""
    if et == "property.tags.added":
        tags = {str(t).lower() for t in d.get("tags") or []}
        if tags & {t.lower() for t in cfg.do_not_market_tags}:
            return "tagged Do Not Market", "stop"
        if tags & {t.lower() for t in cfg.litigator_tags}:
            return "a litigator's number", "stop"
        if tags & {t.lower() for t in cfg.sold_tags}:
            return "the house sold", "stop"
        if tags & {t.lower() for t in cfg.deceased_tags}:
            return "the owner may have died", "stop"
    return "", ""


def pull_from_batches(ctx: Ctx, record_uuids: set[str], why: dict) -> int:
    """Take these records out of every staged batch: mail pieces, texts and lead drip steps. Returns
    how many were pulled. Until 2026-11-13 only mail was pulled, so a record that became untouchable
    stayed in a staged text batch, held back only by the gate at release and missing from this count."""
    pulled = 0
    for b in ledger.batches(status="staged"):
        full = ledger.batch(b["id"])
        if not full or full["kind"] not in ("mail", "text", "drip", "heir_mail", "heir_text", "obituary_mail"):
            continue
        hit = [it for it in full["items"] if it.get("record_uuid") in record_uuids]
        if not hit:
            continue
        if full["kind"] == "text":
            from . import textlane
            gone, what = textlane.pull(b["id"], record_uuids, commit=ctx.live), "text(s)"
        else:
            keep = [it for it in full["items"] if it.get("record_uuid") not in record_uuids]
            gone = len(hit)
            what = {"mail": "piece(s)", "drip": "drip step(s)", "heir_mail": "heir letter(s)",
                    "heir_text": "heir text(s)", "obituary_mail": "obituary letter(s)"}[full["kind"]]
            if ctx.live:
                if full["kind"] == "mail":
                    ledger.batch_items_set(b["id"], keep, sum(float(it.get("price") or 0) for it in keep))
                else:
                    ledger.batch_items_set(b["id"], keep)
                if full["kind"] == "heir_text":
                    from . import limits
                    for u in {it.get("record_uuid") for it in hit}:
                        limits.drop_text(b["id"], u)
        pulled += gone
        ctx.out.line("batch %d: %d %s pulled before release" % (b["id"], gone, what))
        if ctx.live:
            for it in hit:
                ledger.event(ctx.run_id, ctx.stage.id, it["record_uuid"], "revoke",
                             {"from": "%s batch %d" % (full["kind"], b["id"]), "why": why.get(it["record_uuid"], "")},
                             now=ctx.now, tz=ctx.policy.tz)
    return pulled


def run_revoke(ctx: Ctx) -> None:
    st = ctx.stage
    cfg = getattr(ctx.policy, "gate", None) or gate.GateConfig()
    sync = activity.sync(ctx.crm, "state", now=ctx.now, floor_days=int(st.get("floor_days", 3)),
                         max_calls=int(st.get("max_log_calls", 400)))
    if not sync["complete"]:
        ctx.out.problem("the change log is still catching up (read through %s)" % (sync["through"] or "nothing yet"))
    mark = KV if ctx.live else KV_PLAN
    last = store.kv_get(mark)
    since = datetime.fromisoformat(last) if last else ctx.now - timedelta(hours=float(st.get("first_window_hours", 24)))
    why: dict[str, str] = {}
    kinds: dict[str, str] = {}
    for e in activity.events(("property.status.updated", "property.tags.added"), since=since):
        reason, kind = _suppressing(e, cfg)
        if reason and e["resource"]:
            why[e["resource"]] = reason
            kinds[e["resource"]] = "stop" if "stop" in (kind, kinds.get(e["resource"])) else kind
    ctx.out.found = len(why)
    by = Counter(why.values())
    for reason, n in by.most_common(6):
        ctx.out.tally["just became untouchable: %s" % reason] = n
    pulled = pull_from_batches(ctx, set(why), why) if why else 0
    sc_done = sc_leads = 0
    use_sc = bool(st.get("smarter_contact", True))
    sc = None
    if use_sc and why:
        try:
            from sms_agent import sc_client as sc
            if not sc.configured():
                sc = None
        except Exception:  # noqa: BLE001
            sc = None
        if sc is None:
            ctx.out.tally["Smarter Contact not reachable from this box (its campaigns were not checked)"] = 1
    if sc is not None:
        for u in list(why)[: int(st.get("max_records", 80))]:
            rec = ctx.crm.record(u)
            numbers = [gate.digits(p.get("number")) for p in ((rec.get("owner") or {}).get("phones") or [])]
            for n in [x for x in numbers if x]:
                try:
                    cid = sc.contact_id_for(n)
                except Exception as exc:  # noqa: BLE001
                    ctx.out.problem("Smarter Contact lookup failed (%s); stopped checking it this run" % str(exc)[:100])
                    sc = None
                    break
                if not cid:
                    continue
                if kinds.get(u) == "lead":
                    sc_leads += 1
                    continue
                ctx.write("smarter contact dnc", sc.add_contact_dnc, cid)
                sc_done += 1
                if ctx.live:
                    ledger.event(ctx.run_id, st.id, u, "revoke", {"from": "smarter contact", "why": why[u]},
                                 now=ctx.now, tz=ctx.policy.tz)
            if sc is None:
                break
            if kinds.get(u) != "lead":
                ctx.out.line("%s: %s" % (addr(rec), why[u]))
    if sc_leads:
        ctx.out.line("%d number(s) of new leads are still in Smarter Contact; take them out of their campaign by hand" % sc_leads)
    store.kv_set(mark, ctx.now.isoformat(timespec="seconds"))
    ctx.out.acted = pulled + sc_done
    verb = "Pulled" if ctx.live else "Would pull"
    if not why:
        ctx.out.head = "Nobody became untouchable since the last check."
    else:
        ctx.out.head = "%d record(s) just became untouchable. %s %d staged mail piece(s) and %d Smarter Contact number(s)." % (
            len(why), verb, pulled, sc_done)
