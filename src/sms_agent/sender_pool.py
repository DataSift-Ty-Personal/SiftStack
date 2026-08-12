"""Sending-number pool, pacing, and recipient-local quiet hours.

Three jobs:
  1. Pick which of the ~18 registered numbers a message leaves from, and keep
     that choice STICKY per conversation. A reply arriving from a different
     number than the one the owner has been talking to reads as a spam farm
     and breaks the thread on their phone.
  2. Spread the seeding load across the pool so no single number carries the
     volume that gets it filtered.
  3. Refuse to send outside 8am-9pm in the RECIPIENT's timezone, derived from
     their area code, not ours.
"""
from __future__ import annotations

import logging
import random
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from . import config, store

log = logging.getLogger(__name__)

# Area code -> timezone. Grouped by zone because the flat map is 300+ rows and
# the grouping is what a human actually verifies. Anything unlisted defaults to
# Eastern, which is the conservative choice: it opens latest and closes
# earliest relative to the zones west of it.
_CENTRAL = {
    # AL, AR
    "205", "251", "256", "334", "659", "938", "479", "501", "870",
    # IL
    "217", "224", "309", "312", "331", "447", "464", "618", "630", "708",
    "730", "773", "779", "815", "847", "861", "872",
    # IA, KS
    "319", "515", "563", "641", "712", "316", "620", "785", "913",
    # KY (western), LA
    "270", "364", "225", "318", "337", "504", "985",
    # MN, MS
    "218", "320", "507", "612", "651", "763", "952", "228", "601", "662", "769",
    # MO, NE, ND, OK, SD
    "314", "417", "557", "573", "636", "660", "816", "975", "402", "531",
    "701", "405", "539", "572", "580", "918", "605",
    # TN (west + middle). 423 and 865 are EASTERN and deliberately absent.
    "615", "629", "731", "901", "931",
    # TX
    "210", "214", "254", "281", "325", "346", "361", "409", "430", "432",
    "469", "512", "682", "713", "726", "737", "806", "817", "830", "832",
    "903", "936", "940", "945", "956", "972", "979",
    # WI
    "262", "274", "414", "534", "608", "715", "920",
}
_MOUNTAIN = {
    "303", "719", "720", "970",          # CO
    "208", "986",                        # ID
    "406", "307",                        # MT, WY
    "505", "575",                        # NM
    "385", "435", "801",                 # UT
    "915", "308",                        # El Paso TX, western NE
}
_ARIZONA = {"480", "520", "602", "623", "928"}  # no DST
_PACIFIC = {
    "209", "213", "279", "310", "323", "341", "350", "408", "415", "424",
    "442", "510", "530", "559", "562", "619", "626", "628", "650", "657",
    "661", "669", "707", "714", "747", "760", "805", "818", "820", "831",
    "840", "858", "909", "916", "925", "935", "949", "951",
    "458", "503", "541", "971",          # OR
    "206", "253", "360", "425", "509", "564",  # WA
    "702", "725", "775",                 # NV
}
_ALASKA = {"907"}
_HAWAII = {"808"}


def timezone_for(phone: str) -> ZoneInfo:
    ac = store.clean_phone(phone)[:3]
    if ac in _CENTRAL:
        return ZoneInfo("America/Chicago")
    if ac in _MOUNTAIN:
        return ZoneInfo("America/Denver")
    if ac in _ARIZONA:
        return ZoneInfo("America/Phoenix")
    if ac in _PACIFIC:
        return ZoneInfo("America/Los_Angeles")
    if ac in _ALASKA:
        return ZoneInfo("America/Anchorage")
    if ac in _HAWAII:
        return ZoneInfo("Pacific/Honolulu")
    return ZoneInfo("America/New_York")


def within_quiet_hours(phone: str, at: Optional[datetime] = None) -> bool:
    """True when it is an acceptable local time to text this recipient."""
    tz = timezone_for(phone)
    local = (at or datetime.now(timezone.utc)).astimezone(tz)
    return config.QUIET_START_HOUR <= local.hour < config.QUIET_END_HOUR


def next_send_window(phone: str, at: Optional[datetime] = None) -> datetime:
    """The earliest UTC instant this recipient may be texted."""
    tz = timezone_for(phone)
    now_utc = at or datetime.now(timezone.utc)
    local = now_utc.astimezone(tz)
    if local.hour < config.QUIET_START_HOUR:
        target = local.replace(hour=config.QUIET_START_HOUR, minute=0, second=0, microsecond=0)
    elif local.hour >= config.QUIET_END_HOUR:
        target = (local + timedelta(days=1)).replace(
            hour=config.QUIET_START_HOUR, minute=0, second=0, microsecond=0
        )
    else:
        return now_utc
    # Jitter so a night's worth of queued replies does not fire as one burst at
    # 08:00:00 sharp, which is the most machine-looking thing we could do.
    return target.astimezone(timezone.utc) + timedelta(seconds=random.randint(0, 1800))


def pool(owner: str = "") -> list[str]:
    """The numbers available to this caller, or everything when unbound."""
    pools = config.number_pools()
    if owner:
        for name, numbers in pools.items():
            if name and name.strip().lower() == owner.strip().lower():
                return list(numbers)
        log.warning("no number pool for %r; falling back to the full pool", owner)
    return config.numbers()


def available(from_number: str) -> tuple[bool, str]:
    """Whether this number may send right now: daily cap plus a pacing gap."""
    if config.DAILY_CAP_PER_NUMBER and store.sends_today(from_number) >= config.DAILY_CAP_PER_NUMBER:
        return False, f"daily cap {config.DAILY_CAP_PER_NUMBER} reached"
    last = store.last_send_at(from_number)
    if last:
        try:
            elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds()
        except ValueError:
            elapsed = config.MIN_SEND_GAP_SECONDS
        if elapsed < config.MIN_SEND_GAP_SECONDS:
            return False, f"pacing: {int(config.MIN_SEND_GAP_SECONDS - elapsed)}s to go"
    return True, ""


def assign(phone: str, owner: str = "") -> Optional[str]:
    """The number this conversation sends from.

    Sticky: once a thread has a number it keeps it even when that number is at
    its cap, because switching mid-conversation is worse than waiting. Only a
    brand-new conversation picks from the pool, least-loaded first.

    `owner` is the caller who signs the thread. Their numbers are preferred so
    the name on the text and the flow behind a callback are the same person.
    A sticky number is honored even if it belongs to someone else, because
    changing the number mid-thread is the worse of the two problems.
    """
    numbers = pool(owner)
    if not numbers:
        return None

    conv = store.get_conversation(phone)
    if conv and conv.get("from_number"):
        if conv["from_number"] in numbers or conv["from_number"] in config.numbers():
            return conv["from_number"]

    load = store.number_load()
    ranked = sorted(numbers, key=lambda n: (load.get(n, 0), n))
    for n in ranked:
        ok, _ = available(n)
        if ok:
            return n
    return ranked[0]  # everything capped: hand back the lightest and let the worker hold it


def capacity_today(owner: str = "") -> dict:
    """What the pool can still push today. Surfaced by `status` and `seed`."""
    numbers = pool(owner)
    load = store.number_load()
    used = sum(load.get(n, 0) for n in numbers)
    total = len(numbers) * config.DAILY_CAP_PER_NUMBER
    by_owner = {}
    for name, nums in config.number_pools().items():
        if not name:
            continue
        spent = sum(load.get(n, 0) for n in nums)
        by_owner[name] = {
            "numbers": len(nums),
            "sent_today": spent,
            "capacity": len(nums) * config.DAILY_CAP_PER_NUMBER,
            "remaining": max(0, len(nums) * config.DAILY_CAP_PER_NUMBER - spent),
        }
    return {
        "numbers": len(numbers),
        "cap_per_number": config.DAILY_CAP_PER_NUMBER,
        "sent_today": used,
        "capacity": total,
        "remaining": max(0, total - used),
        "by_owner": by_owner,
        "per_number": {n: load.get(n, 0) for n in numbers},
    }
