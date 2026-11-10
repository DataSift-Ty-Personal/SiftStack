#!/usr/bin/env python3
"""Launch the Priority 1 empathy campaign in Smarter Contact, one wave at a time.

The textable group (DataSift push "Priority 1- Attempt 1", ~10K contacts, one
per phone) is too big for the three-day burst to run in one go: Smarter Contact
only builds a follow-up campaign once its source campaign has COMPLETED, so
day 1 and day 2 need a day-0 campaign that finishes in an afternoon. Waves of
about 1,000 contacts do, at roughly 100 texts per number across 10 numbers.

    python src/smarter_contact_launch.py --phase plan              # pull + split, writes waves.json
    python src/smarter_contact_launch.py --phase groups --wave 1   # dry; --commit creates "P1 Wave 1"
    python src/smarter_contact_launch.py --phase groups --wave 0   # the no-name group
    python src/smarter_contact_launch.py --phase campaign --wave 1 --start "2026-10-28 12:00"
                                                                   # dry; --commit creates the draft,
                                                                   # --schedule also starts it

Rules each phase keeps:
  * Waves split by PROPERTY, not by phone. One owner's three phones are three
    contacts, and splitting them across waves would text the same person the
    same sequence on different days.
  * The no-name split uses the same rule as the DataSift tagging
    (smarter_contact_templates.no_name_reason): Smarter Contact prints the raw
    first-name field and cannot branch on it.
  * Every payload is validated by the server before any write, every write is
    read back, and creating a campaign never starts it unless --schedule.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from playwright.async_api import async_playwright  # noqa: E402

import smarter_contact_setup as scs  # noqa: E402
import smarter_contact_templates as T  # noqa: E402

SOURCE_GROUP = "Priority 1- Attempt 1"
PLAN = ROOT / "output" / "smarter_contact" / "waves.json"
TZ = ZoneInfo("America/New_York")
WAVE_SIZE = 1000
# Pacing: 5 texts a minute per campaign is 300 an hour, so a wave's East half
# (~830) finishes by early afternoon and replies arrive at a pace one person can
# answer. Smarter Contact's default is 100 a minute.
PER_BATCH, INTERVAL, FREQUENCY = 5, 1, "MINUTELY"
WEEKDAYS = [1, 2, 3, 4, 5]
# Smarter Contact's delivery window runs on the ACCOUNT's timezone (Eastern), and
# Ty's rule is 9am-6pm in the RECIPIENT's time too. So each wave splits by the
# phone's area code: Eastern phones start at 9am, everyone else at noon Eastern
# (11am Central, 9am Pacific). One window for all would text Pacific at 6am.
TZ_SPLITS = {"east": ("East", 9), "other": ("Other TZ", 12)}
WINDOW_END = 18

Q_CONTACTS = ("query P($filter: ContactFilter, $pagination: Pagination) { findContacts(filter: $filter, "
              "pagination: $pagination) { hasNext nextId nextCreatedAt items { id phone firstName lastName "
              "attrs { fieldId value } } } }")
M_GROUP = "mutation CreateGroup($input: CreateGroupInput!) { createGroup(input: $input) { id name } }"
M_ADD = "mutation AddGroupToContacts($input: AddGroupToContactsInput!) { addGroupToContacts(input: $input) { success } }"
M_CAMPAIGN = "mutation CreateCampaign($input: CreateCampaignInput!) { createCampaign(input: $input) { id name } }"
M_START = "mutation StartCampaign($id: String!) { startCampaign(id: $id) { success } }"


def _group_name(wave: int, tz: str = "") -> str:
    base = "P1 No Name" if wave == 0 else f"P1 Wave {wave}"
    return f"{base} {TZ_SPLITS[tz][0]}" if tz else base


def _is_eastern(phone: str) -> bool:
    """Same area-code map the SMS agent uses for recipient quiet hours."""
    from sms_agent.sender_pool import timezone_for
    probe = datetime(2026, 10, 28, 15, 0, tzinfo=ZoneInfo("UTC"))
    return timezone_for(str(phone or "")).utcoffset(probe) == TZ.utcoffset(probe)


def _address_key(row: dict, field_ids: dict[str, str]) -> str:
    """The property this contact belongs to.

    Smarter Contact keeps the pushed DataSift fields in `attrs` by field id. The
    integration writes the DataSift RECORD UUID into the APN field, which is the
    cleanest per-property key; the property address is the fallback.
    """
    attrs = {a.get("fieldId"): (a.get("value") or "").strip() for a in row.get("attrs") or []}
    apn = attrs.get(field_ids.get("ASSESSOR_PARCEL_NUMBER", ""), "")
    if apn:
        return f"apn:{apn.lower()}"
    raw = f"{attrs.get(field_ids.get('ADDRESS', ''), '')}|{attrs.get(field_ids.get('CITY', ''), '')}"
    key = "".join(c for c in raw.lower() if c.isalnum() or c == "|").strip("|")
    return f"addr:{key}" if key else f"phone:{row.get('phone')}"  # nothing: the phone stands alone


async def _session(pw, route: str = "/contacts/all-contacts"):
    ctx = await scs._context(pw, headless=True)
    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
    s = scs.Session(ctx, page)
    await page.goto(scs.APP + route, wait_until="domcontentloaded")
    await page.wait_for_timeout(4000)
    if not scs._signed_in(page.url):
        await ctx.close()
        raise SystemExit("NOT SIGNED IN: run smarter_contact_setup.py --phase login first")
    return ctx, s


async def _groups(s, ops) -> dict[str, dict]:
    return {g["name"]: g for g in await scs._list(s, ops, "FindGroupsList")}


async def plan(size: int) -> int:
    ops = json.loads((scs.DISCOVER_DIR / "gql_ops.json").read_text(encoding="utf-8"))
    async with async_playwright() as pw:
        ctx, s = await _session(pw)
        fr = await s.call(ops["FindFields"]["query"], ops["FindFields"]["variables"], "FindFields")
        fields = next(iter(fr["data"].values()))
        fields = fields.get("items", fields) if isinstance(fields, dict) else fields
        field_ids = {f.get("key"): f.get("id") for f in fields if f.get("key")}
        src = (await _groups(s, ops)).get(SOURCE_GROUP)
        if not src:
            raise SystemExit(f"no group named {SOURCE_GROUP!r}")
        rows, pg = [], {"limit": 200}
        while True:
            r = await s.call(Q_CONTACTS, {"filter": {"status": "ACTIVE", "groups": [src["id"]]}, "pagination": pg})
            if r.get("errors"):
                raise SystemExit(json.dumps(r["errors"])[:300])
            d = r["data"]["findContacts"]
            rows += d["items"]
            if not d["hasNext"]:
                break
            pg = {"limit": 200, "nextId": d["nextId"], "nextCreatedAt": d["nextCreatedAt"]}
        await ctx.close()

    no_name, by_addr = [], {}
    for row in rows:
        if T.no_name_reason({"first_name": row.get("firstName") or "", "last_name": row.get("lastName") or ""}):
            no_name.append(row["id"])
        else:
            by_addr.setdefault(_address_key(row, field_ids), []).append(row["id"])
    # Stable shuffle by address hash, so waves are representative of the whole
    # list rather than whatever order the push arrived in, and a rerun deals
    # the same addresses into the same waves.
    waves, cur = [], []
    for key in sorted(by_addr, key=lambda k: hashlib.sha256(k.encode()).hexdigest()):
        if cur and len(cur) + len(by_addr[key]) > size:
            waves.append(cur)
            cur = []
        cur += by_addr[key]
    if cur:
        waves.append(cur)
    PLAN.write_text(json.dumps({"made": datetime.now(TZ).isoformat(timespec="seconds"),
                                "source_group": src["id"], "contacts": len(rows),
                                "no_name": no_name, "waves": waves}), encoding="utf-8")
    print(f"{len(rows)} contacts in {SOURCE_GROUP!r}: {len(no_name)} no-name, "
          f"{sum(map(len, waves))} named across {len(by_addr)} properties")
    print(f"{len(waves)} waves of <= {size}: sizes {[len(w) for w in waves]}")
    print(f"wrote {PLAN.relative_to(ROOT)}")
    return 0


def _wave_ids(wave: int) -> list[str]:
    p = json.loads(PLAN.read_text(encoding="utf-8"))
    return p["no_name"] if wave == 0 else p["waves"][wave - 1]


async def groups(wave: int, commit: bool, tz: str = "") -> int:
    ops = json.loads((scs.DISCOVER_DIR / "gql_ops.json").read_text(encoding="utf-8"))
    ids, name = _wave_ids(wave), _group_name(wave, tz)
    async with async_playwright() as pw:
        ctx, s = await _session(pw)
        if tz:  # split the wave's master group by the phone's timezone
            master = (await _groups(s, ops)).get(_group_name(wave))
            if not master:
                raise SystemExit(f"group {_group_name(wave)!r} missing: run --phase groups without --tz first")
            rows, pg = [], {"limit": 200}
            while True:
                r = await s.call(Q_CONTACTS, {"filter": {"status": "ACTIVE", "groups": [master["id"]]},
                                              "pagination": pg})
                d = r["data"]["findContacts"]
                rows += d["items"]
                if not d["hasNext"]:
                    break
                pg = {"limit": 200, "nextId": d["nextId"], "nextCreatedAt": d["nextCreatedAt"]}
            ids = [x["id"] for x in rows if _is_eastern(x.get("phone")) == (tz == "east")]
            print(f"{_group_name(wave)}: {len(rows)} contacts, {len(ids)} in the {tz} split")
            if not ids:
                await ctx.close()
                return 0
        existing = (await _groups(s, ops)).get(name)
        if existing:
            print(f"group {name!r} exists with {existing.get('contacts')} contacts (plan: {len(ids)})")
            await ctx.close()
            return 0 if existing.get("contacts") == len(ids) else 1
        bad = await scs._server_validates(s, M_GROUP, {"input": {"name": name}})
        if bad:
            raise SystemExit(f"server rejects the group payload: {bad}")
        if not commit:
            print(f"DRY RUN: would create {name!r} and add {len(ids)} contacts. Rerun with --commit.")
            await ctx.close()
            return 0
        gid = (await s.call(M_GROUP, {"input": {"name": name}}))["data"]["createGroup"]["id"]
        for i in range(0, len(ids), 200):
            res = await s.call(M_ADD, {"input": {"groupId": gid, "ids": ids[i:i + 200]}})
            if res.get("errors") or not (res.get("data") or {}).get("addGroupToContacts", {}).get("success"):
                raise SystemExit(f"adding contacts failed at {i}: {json.dumps(res)[:300]}")
        await asyncio.sleep(3)
        back = (await _groups(s, ops)).get(name) or {}
        await ctx.close()
    print(f"created {name!r}: {back.get('contacts')} contacts read back (plan {len(ids)})")
    return 0 if back.get("contacts") == len(ids) else 1


async def campaign(wave: int, start: str, commit: bool, schedule: bool, tz: str = "", group_name: str = "") -> int:
    ops = json.loads((scs.DISCOVER_DIR / "gql_ops.json").read_text(encoding="utf-8"))
    start_local = datetime.strptime(start, "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
    if start_local <= datetime.now(TZ):
        raise SystemExit(f"start {start_local:%Y-%m-%d %H:%M %Z} is not in the future")
    aud = "noname" if wave == 0 else "named"
    pool = T.TOUCH1_NONAME if wave == 0 else T.TOUCH1
    titles = [T.title(1, "", aud, i) for i in range(1, len(pool) + 1)]
    from_hour = TZ_SPLITS[tz][1] if tz else 12
    if start_local.hour < from_hour:
        raise SystemExit(f"{tz or 'this'} campaign cannot start before {from_hour}:00 Eastern")
    window = {"from": {"hour": from_hour, "minute": 0}, "to": {"hour": WINDOW_END, "minute": 0}}
    group_name = group_name or _group_name(wave, tz)
    name = f"{group_name} T1 {start_local:%Y-%m-%d}"
    async with async_playwright() as pw:
        ctx, s = await _session(pw, "/campaigns")
        group = (await _groups(s, ops)).get(group_name)
        if not group:
            raise SystemExit(f"group {group_name!r} does not exist yet: run --phase groups first")
        tpl = {t["name"]: t["id"] for t in await scs._list(s, ops, "FindMessageTemplates")}
        missing = [t for t in titles if t not in tpl]
        if missing:
            raise SystemExit(f"templates missing in Smarter Contact: {missing}")
        op = ops["FindOwnPhones"]
        v = json.loads(json.dumps(op["variables"] or {}))
        v.setdefault("pagination", {})["limit"] = 100
        r = await s.call(op["query"], v, "FindOwnPhones")
        phones = next(iter(r["data"].values()))
        phones = phones.get("items", phones) if isinstance(phones, dict) else phones
        phones = [p for p in phones if p.get("status") == "APPROVED" and not p.get("isBlocked")]
        need = min(-(-group["contacts"] // 50), 8)  # the app's own rule: 1 per 50, capped at 8
        if len(titles) < need:
            raise SystemExit(f"{group['contacts']} contacts need {need} templates, have {len(titles)}")
        payload = {"input": {
            "name": name, "type": "STANDARD", "groups": [group["id"]],
            "ownPhones": [p["id"] for p in phones],
            "sequences": [{"type": "SMS", "templates": [tpl[t] for t in titles],
                           "hasCustomTimeRestrictions": True, "timeRange": window, "days": WEEKDAYS}],
            "scheduleOptions": {"startAt": start_local.astimezone(ZoneInfo("UTC")).isoformat().replace("+00:00", "Z"),
                                "hasCustomStartDate": True, "hasCustomBatchSize": True,
                                "perBatch": PER_BATCH, "interval": INTERVAL, "frequency": FREQUENCY},
        }}
        bad = await scs._server_validates(s, M_CAMPAIGN, payload)
        print(f"{name}: {group['contacts']} contacts, {len(titles)} templates, {len(phones)} numbers "
              f"({', '.join(p['number'][-4:] for p in phones)}), starts {start_local:%a %b %d %I:%M %p %Z}, "
              f"{PER_BATCH}/min, window {from_hour}:00-{WINDOW_END}:00 Eastern Mon-Fri")
        if bad:
            raise SystemExit(f"server rejects the campaign payload: {bad}")
        if not commit:
            print("DRY RUN: payload accepted by the server. Rerun with --commit (draft) or --commit --schedule.")
            await ctx.close()
            return 0
        res = await s.call(M_CAMPAIGN, payload)
        if res.get("errors"):
            raise SystemExit(f"create failed: {json.dumps(res['errors'])[:400]}")
        cid = res["data"]["createCampaign"]["id"]
        print(f"created draft campaign {cid}")
        if schedule:
            res = await s.call(M_START, {"id": cid})
            print("start:", json.dumps(res.get("errors") or res.get("data"))[:300])
        await ctx.close()
    return 0


M_FOLLOW = ("mutation CreateFollowUpCampaign($input: CampaignConversationsFilterInput!) { "
            "createFollowUpCampaign(input: $input) { id name contacts state { status } } }")
M_UPDATE = ("mutation UpdateCampaign($input: UpdateCampaignInput!, $id: String!) { "
            "updateCampaign(input: $input, id: $id) { id } }")
Q_CAMPAIGN = ("query GetCampaign($id: String!) { getCampaign(id: $id) { id name contacts state { status } "
              "ownPhones { id } sequences { templates } scheduleOptions { startAt perBatch } "
              "parentCampaign { ids } } }")
Q_CAMPAIGNS = ("query F($p: Pagination) { findCampaigns(pagination: $p) { items { id name contacts "
               "state { status } } } }")


def _touch_pool(touch: int, wave: int) -> list:
    return getattr(T, f"TOUCH{touch}_NONAME" if wave == 0 else f"TOUCH{touch}")


async def followup(wave: int, touch: int, source_date: str, start: str, commit: bool, schedule: bool,
                   tz: str = "") -> int:
    """Touch N to everyone in touch N-1 who never replied.

    Built off the finished source campaign with Smarter Contact's own
    follow-up (mode NO_RESPONSE), so anyone who answered, opted out or was
    dispositioned in the inbox is left out by the platform itself. Created as a
    DRAFT; only --schedule starts it.
    """
    start_local = datetime.strptime(start, "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
    if start_local <= datetime.now(TZ):
        raise SystemExit(f"start {start_local:%Y-%m-%d %H:%M %Z} is not in the future")
    from_hour = TZ_SPLITS[tz][1] if tz else 12
    if start_local.hour < from_hour:
        raise SystemExit(f"{tz or 'this'} campaign cannot start before {from_hour}:00 Eastern")
    aud = "noname" if wave == 0 else "named"
    titles = [T.title(touch, "", aud, i) for i in range(1, len(_touch_pool(touch, wave)) + 1)]
    source_name = f"{_group_name(wave, tz)} T{touch - 1} {source_date}"
    name = f"{_group_name(wave, tz)} T{touch} {start_local:%Y-%m-%d}"
    window = {"from": {"hour": from_hour, "minute": 0}, "to": {"hour": WINDOW_END, "minute": 0}}
    ops = json.loads((scs.DISCOVER_DIR / "gql_ops.json").read_text(encoding="utf-8"))
    async with async_playwright() as pw:
        ctx, s = await _session(pw, "/campaigns")
        found = (await s.call(Q_CAMPAIGNS, {"p": {"limit": 100}}))["data"]["findCampaigns"]["items"]
        by_name = {c["name"]: c for c in found if c["state"]["status"] != "DELETED"}
        if name in by_name:
            print(f"{name!r} already exists ({by_name[name]['state']['status']}, {by_name[name]['contacts']} contacts)")
            await ctx.close()
            return 0
        src = by_name.get(source_name)
        if not src:
            raise SystemExit(f"source campaign {source_name!r} not found")
        if src["state"]["status"] not in ("ENDED", "COMPLETED"):
            raise SystemExit(f"{source_name!r} is {src['state']['status']}; a follow-up needs it finished")
        tpl = {t["name"]: t["id"] for t in await scs._list(s, ops, "FindMessageTemplates")}
        missing = [t for t in titles if t not in tpl]
        if missing:
            raise SystemExit(f"templates missing in Smarter Contact: {missing}")
        op = ops["FindOwnPhones"]
        v = json.loads(json.dumps(op["variables"] or {}))
        v.setdefault("pagination", {})["limit"] = 100
        r = await s.call(op["query"], v, "FindOwnPhones")
        phones = next(iter(r["data"].values()))
        phones = phones.get("items", phones) if isinstance(phones, dict) else phones
        phones = [p for p in phones if p.get("status") == "APPROVED" and not p.get("isBlocked")]
        follow = {"input": {"campaigns": [src["id"]], "mode": "NO_RESPONSE"}}
        update = {"input": {
            "name": name, "ownPhones": [p["id"] for p in phones],
            "sequences": [{"type": "SMS", "templates": [tpl[t] for t in titles],
                           "hasCustomTimeRestrictions": True, "timeRange": window, "days": WEEKDAYS}],
            "scheduleOptions": {"startAt": start_local.astimezone(ZoneInfo("UTC")).isoformat().replace("+00:00", "Z"),
                                "hasCustomStartDate": True, "hasCustomBatchSize": True,
                                "perBatch": PER_BATCH, "interval": INTERVAL, "frequency": FREQUENCY},
        }, "id": "00000000-0000-0000-0000-000000000000"}
        for doc, payload, what in ((M_FOLLOW, follow, "follow-up"), (M_UPDATE, update, "update")):
            bad = await scs._server_validates(s, doc, payload)
            if bad:
                raise SystemExit(f"server rejects the {what} payload: {bad}")
        print(f"{name}: from {source_name} ({src['contacts']} contacts) to non-responders, {len(titles)} templates, "
              f"{len(phones)} numbers, starts {start_local:%a %b %d %I:%M %p %Z}, window {from_hour}:00-{WINDOW_END}:00")
        if not commit:
            print("DRY RUN: both payloads accepted by the server. Rerun with --commit (draft) or --commit --schedule.")
            await ctx.close()
            return 0
        res = await s.call(M_FOLLOW, follow)
        if res.get("errors"):
            raise SystemExit(f"follow-up create failed: {json.dumps(res['errors'])[:400]}")
        made = res["data"]["createFollowUpCampaign"]
        need = min(-(-int(made["contacts"] or 0) // 50), 8)
        if len(titles) < need:
            raise SystemExit(f"{made['contacts']} contacts need {need} templates, have {len(titles)}; "
                             f"draft {made['id']} left for review")
        update["id"] = made["id"]
        res = await s.call(M_UPDATE, update)
        if res.get("errors"):
            raise SystemExit(f"update failed on draft {made['id']}: {json.dumps(res['errors'])[:400]}")
        back = (await s.call(Q_CAMPAIGN, {"id": made["id"]}))["data"]["getCampaign"]
        seq = (back.get("sequences") or [{}])[0]
        print(f"draft {back['id']}: {back['name']!r}, {back['contacts']} contacts, {back['state']['status']}, "
              f"{len(seq.get('templates') or [])} templates, {len(back.get('ownPhones') or [])} numbers, "
              f"starts {(back.get('scheduleOptions') or {}).get('startAt')}, parent {back.get('parentCampaign')}")
        if schedule:
            res = await s.call(M_START, {"id": made["id"]})
            print("start:", json.dumps(res.get("errors") or res.get("data"))[:300])
        await ctx.close()
    return 0


M_REMOVE_CAMPAIGN = "mutation R($input: RemoveCampaignInput!) { removeCampaign(input: $input) { success } }"
Q_MSGS = ("query M($c: String!, $p: Pagination) { findConversationMessages(contactId: $c, pagination: $p) "
          "{ items { direction } } }")


async def touch(wave: int, touch_n: int, source_date: str, start: str, commit: bool, schedule: bool,
                tz: str = "") -> int:
    """Touch N as a STANDARD campaign to an audited group, not a follow-up.

    Smarter Contact's follow-up snapshots its recipients into a hidden group
    that cannot be edited ("Group not found" on removal, 2026-10-28), so a
    follow-up can never drop a sold, not-interested or DNC record. This builds
    the group itself: Touch N-1's group, minus everyone who has replied (text
    or call, re-read now), minus everyone `smarter_contact_audit.py` flagged
    for that touch. Run the audit first; its newest file for this campaign is
    the exclusion list. A same-named follow-up DRAFT that never sent is
    deleted so the name is free.
    """
    start_local = datetime.strptime(start, "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
    if start_local <= datetime.now(TZ):
        raise SystemExit(f"start {start_local:%Y-%m-%d %H:%M %Z} is not in the future")
    from_hour = TZ_SPLITS[tz][1] if tz else 12
    if start_local.hour < from_hour:
        raise SystemExit(f"{tz or 'this'} campaign cannot start before {from_hour}:00 Eastern")
    aud = "noname" if wave == 0 else "named"
    titles = [T.title(touch_n, "", aud, i) for i in range(1, len(_touch_pool(touch_n, wave)) + 1)]
    source_name = f"{_group_name(wave, tz)} T{touch_n - 1} {source_date}"
    name = f"{_group_name(wave, tz)} T{touch_n} {start_local:%Y-%m-%d}"
    group_name = f"{_group_name(wave, tz)} T{touch_n}"
    audits = sorted((ROOT / "output" / "smarter_contact" / "audits").glob("followup_audit_*.json"))
    flagged, audited = {}, False
    for f in audits:   # newest audit per campaign name wins
        for d in json.loads(f.read_text(encoding="utf-8"))["drafts"]:
            if d["draft"] == name:
                flagged, audited = {x["contact_id"]: x["why"] for x in d["flagged"]}, True
    if not audited:
        raise SystemExit(f"no audit found for {name!r}: run smarter_contact_audit.py first")
    window = {"from": {"hour": from_hour, "minute": 0}, "to": {"hour": WINDOW_END, "minute": 0}}
    ops = json.loads((scs.DISCOVER_DIR / "gql_ops.json").read_text(encoding="utf-8"))
    async with async_playwright() as pw:
        ctx, s = await _session(pw, "/campaigns")
        found = (await s.call(Q_CAMPAIGNS, {"p": {"limit": 100}}))["data"]["findCampaigns"]["items"]
        live = {c["name"]: c for c in found if c["state"]["status"] != "DELETED"}
        src = live.get(source_name)
        if not src or src["state"]["status"] not in ("ENDED", "COMPLETED"):
            raise SystemExit(f"source {source_name!r} missing or not finished")
        old = live.get(name)
        if old and old["state"]["status"] != "DRAFT":
            raise SystemExit(f"{name!r} already exists and is {old['state']['status']}")
        src_groups = (await s.call(Q_CAMP_GROUPS, {"id": src["id"]}))["data"]["getCampaign"]["groups"]
        contacts = []
        for g in src_groups:
            page = {"limit": 100}
            while True:
                r = (await s.call(Q_GROUP_CONTACTS, {"f": {"status": "ACTIVE", "groups": [g["id"]]}, "p": page}))
                r = r["data"]["findContacts"]
                contacts += [c["id"] for c in r["items"]]
                if not r["hasNext"]:
                    break
                page = {"limit": 100, "nextId": r["nextId"], "nextCreatedAt": r["nextCreatedAt"]}
        replied = set()
        for cid in contacts:
            msgs = (await s.call(Q_MSGS, {"c": cid, "p": {"limit": 40}}))["data"]["findConversationMessages"]["items"]
            if any(str(m.get("direction") or "").upper().startswith("IN") for m in msgs):
                replied.add(cid)
        keep = [c for c in contacts if c not in replied and c not in flagged]
        tpl = {t["name"]: t["id"] for t in await scs._list(s, ops, "FindMessageTemplates")}
        missing = [t for t in titles if t not in tpl]
        if missing:
            raise SystemExit(f"templates missing in Smarter Contact: {missing}")
        need = min(-(-len(keep) // 50), 8)
        if len(titles) < need:
            raise SystemExit(f"{len(keep)} contacts need {need} templates, have {len(titles)}")
        print(f"{name}: {len(contacts)} in {source_name} - {len(replied)} replied - "
              f"{len([c for c in contacts if c in flagged and c not in replied])} flagged = {len(keep)} to text, "
              f"{len(titles)} templates, starts {start_local:%a %b %d %I:%M %p %Z}")
        if not commit:
            print("DRY RUN. Rerun with --commit (group + draft) or --commit --schedule.")
            await ctx.close()
            return 0
        if old:
            res = await s.call(M_REMOVE_CAMPAIGN, {"input": {"ids": [old["id"]]}})
            print("deleted the unsendable follow-up draft:", json.dumps(res.get("errors") or res.get("data"))[:120])
        groups = await _groups(s, ops)
        if group_name in groups:
            raise SystemExit(f"group {group_name!r} already exists; delete it or pick another name")
        gid = (await s.call(M_GROUP, {"input": {"name": group_name}}))["data"]["createGroup"]["id"]
        for i in range(0, len(keep), 200):
            res = await s.call(M_ADD, {"input": {"groupId": gid, "ids": keep[i:i + 200]}})
            if res.get("errors") or not (res.get("data") or {}).get("addGroupToContacts", {}).get("success"):
                raise SystemExit(f"adding contacts failed at {i}: {json.dumps(res)[:300]}")
        await asyncio.sleep(3)
        back = (await _groups(s, ops)).get(group_name) or {}
        if back.get("contacts") != len(keep):
            raise SystemExit(f"group read back {back.get('contacts')} of {len(keep)}; not creating the campaign")
        op = ops["FindOwnPhones"]
        v = json.loads(json.dumps(op["variables"] or {}))
        v.setdefault("pagination", {})["limit"] = 100
        r = await s.call(op["query"], v, "FindOwnPhones")
        phones = next(iter(r["data"].values()))
        phones = phones.get("items", phones) if isinstance(phones, dict) else phones
        phones = [p for p in phones if p.get("status") == "APPROVED" and not p.get("isBlocked")]
        payload = {"input": {
            "name": name, "type": "STANDARD", "groups": [gid], "ownPhones": [p["id"] for p in phones],
            "sequences": [{"type": "SMS", "templates": [tpl[t] for t in titles],
                           "hasCustomTimeRestrictions": True, "timeRange": window, "days": WEEKDAYS}],
            "scheduleOptions": {"startAt": start_local.astimezone(ZoneInfo("UTC")).isoformat().replace("+00:00", "Z"),
                                "hasCustomStartDate": True, "hasCustomBatchSize": True,
                                "perBatch": PER_BATCH, "interval": INTERVAL, "frequency": FREQUENCY},
        }}
        res = await s.call(M_CAMPAIGN, payload)
        if res.get("errors"):
            raise SystemExit(f"create failed: {json.dumps(res['errors'])[:400]}")
        cid = res["data"]["createCampaign"]["id"]
        made = (await s.call(Q_CAMPAIGN, {"id": cid}))["data"]["getCampaign"]
        print(f"draft {cid}: {made['contacts']} contacts, starts {(made.get('scheduleOptions') or {}).get('startAt')}")
        if schedule:
            res = await s.call(M_START, {"id": cid})
            state = (await s.call(Q_CAMPAIGN, {"id": cid}))["data"]["getCampaign"]["state"]["status"]
            print("start:", json.dumps(res.get("errors") or res.get("data"))[:200], "| state now", state)
        await ctx.close()
    return 0


Q_CAMP_GROUPS = "query G($id: String!) { getCampaign(id: $id) { groups { id name } } }"
Q_GROUP_CONTACTS = ("query F($f: ContactFilter, $p: Pagination) { findContacts(filter: $f, pagination: $p) "
                    "{ hasNext nextId nextCreatedAt items { id } } }")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--phase", choices=["plan", "groups", "campaign", "followup", "touch"], required=True)
    ap.add_argument("--touch", type=int, default=2, help="followup: the touch to send (2 = day 1)")
    ap.add_argument("--source-date", default="", help="followup: date in the source campaign's name, YYYY-MM-DD")
    ap.add_argument("--wave", type=int, default=1, help="wave number; 0 = the no-name group")
    ap.add_argument("--size", type=int, default=WAVE_SIZE)
    ap.add_argument("--start", default="", help='local Eastern time, "YYYY-MM-DD HH:MM"')
    ap.add_argument("--tz", choices=["", "east", "other"], default="",
                    help="split by recipient timezone: east starts 9am, other noon")
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--schedule", action="store_true", help="campaign: also start it (sends at --start)")
    ap.add_argument("--group", default="", help="campaign: send to this group instead of the wave's own "
                    "(a make-up Touch 1 for contacts pulled out of a campaign already sending)")
    ap.add_argument("--per-min", type=int, default=0,
                    help="texts per minute for this campaign (default %d; the A2P approval shows maxTpm 6)" % PER_BATCH)
    a = ap.parse_args()
    if a.per_min:
        if a.per_min > 6:
            raise SystemExit("--per-min above 6 exceeds the account's approved A2P throughput (maxTpm 6)")
        globals()["PER_BATCH"] = a.per_min
    if a.phase == "plan":
        return asyncio.run(plan(a.size))
    if a.phase == "groups":
        return asyncio.run(groups(a.wave, a.commit, a.tz))
    if not a.start:
        raise SystemExit("--start is required for --phase campaign and followup")
    if a.phase in ("followup", "touch") and not a.source_date:
        raise SystemExit("--source-date is required for --phase followup and touch")
    if a.phase == "touch":
        return asyncio.run(touch(a.wave, a.touch, a.source_date, a.start, a.commit, a.schedule, a.tz))
    if a.phase == "followup":
        return asyncio.run(followup(a.wave, a.touch, a.source_date, a.start, a.commit, a.schedule, a.tz))
    return asyncio.run(campaign(a.wave, a.start, a.commit, a.schedule, a.tz, a.group))


if __name__ == "__main__":
    sys.exit(main())
