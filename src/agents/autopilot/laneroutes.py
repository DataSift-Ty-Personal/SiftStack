"""Route helpers the lane stages share: tasks and a preset's scheduled export.

Every route here is in DataSift's published reference (developers.datasift.ai, DataSift API 2.0,
checked against the cached spec on 2026-11-13):

  * create a task      POST  /api/internal/task/
                       {title, all_day, due_date, notes, assigned_to_users: [user uuid],
                        assigned_to_property: record uuid, event_type: "task", source}
  * read a task        GET   /api/internal/task/{uuid}/        `completed` holds the time it was done
  * complete a task    POST  /api/internal/task/{uuid}/complete/
  * a preset's export  GET, POST, PATCH  /api/internal/filter-preset/{uuid}/scheduled-export/
                       {export_config: {columns: {phones: {must: {any_phone_tags, phone_status}}},
                        filename, export_messages}, start_date, recurrence, enabled}

They are plain functions that take the crm object and go through crm.call, so every request is
counted against this app's share of the account's rate limit and the selftest's fake stands in
for all of them. Nothing here decides anything. A stage decides, then calls one of these through
ctx.write, so a plan run never reaches them.

Two habits from the routes this project already learned the hard way:
  * A key that has no value is left OUT of the body. Sending an empty string or an empty list is
    not the same as not sending the key: the task-preset route refuses an empty assignee list.
  * A task is given exactly one kind of assignee, a user. Role and round-robin assignment are for
    presets a person builds in the app.

None of these writes has been proven on a live record yet. Each stage that uses them ships in plan
mode, and one task created and read back on the staging account is the proof to take first
(proofs.py lists it under task_from_preset).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

TASKS = "/api/internal/task/"
EXPORT_RECURRENCE = ("weekly", "bi-weekly", "monthly")
PHONE_STATUSES = ("CORRECT", "WRONG", "DEAD", "NO_ANSWER", "CORRECT_DNC", "WRONG_DNC", "DNC", "UNKNOWN")


# ---------------------------------------------------------------------- dates
def end_of_day(day: date, tz: str) -> str:
    """The last moment of a business-local day in UTC, which is how DataSift stores an all-day due
    date (the app sends 23:59:59.999 of the day in the user's zone)."""
    local = datetime(day.year, day.month, day.day, 23, 59, 59, 999000, tzinfo=ZoneInfo(tz))
    return local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def business_day(day: date, plus: int = 0) -> date:
    """`day` moved forward by `plus` weekdays, and onto a weekday if it falls on a weekend. A call
    task due on a Saturday is a task nobody sees until it is already late."""
    d = day
    while d.weekday() >= 5:
        d += timedelta(days=1)
    for _ in range(max(0, int(plus))):
        d += timedelta(days=1)
        while d.weekday() >= 5:
            d += timedelta(days=1)
    return d


def week_of(now: datetime, tz: str) -> str:
    """The business-local ISO week, for example 2026-W46: what "once a week" is counted by."""
    iso = now.astimezone(ZoneInfo(tz)).isocalendar()
    return "%d-W%02d" % (iso[0], iso[1])


def nice_day(day: date) -> str:
    return day.strftime("%b %d").replace(" 0", " ")


# ---------------------------------------------------------------------- tasks
def task_body(title: str, due: str = "", notes: str = "", user_uuid: str = "", property_uuid: str = "",
              source: str = "autopilot") -> dict:
    """The documented body for one task. Keys without a value are left out, never sent empty."""
    body: dict = {"title": " ".join(str(title).split())[:256], "all_day": True, "event_type": "task"}
    if due:
        body["due_date"] = due
    if notes:
        body["notes"] = str(notes)[:4000]
    if user_uuid:
        body["assigned_to_users"] = [user_uuid]
    if property_uuid:
        body["assigned_to_property"] = property_uuid
    if source:
        body["source"] = str(source)[:256]
    return body


def create_task(crm, title: str, due: str = "", notes: str = "", user_uuid: str = "", property_uuid: str = "",
                source: str = "autopilot") -> dict:
    """Create one task and return what DataSift answered (its uuid is what the stage remembers)."""
    r = crm.call(TASKS, "POST", task_body(title, due, notes, user_uuid, property_uuid, source))
    return r if isinstance(r, dict) else {}


def get_task(crm, task_uuid: str) -> dict | None:
    """The task as it stands now, or None when it no longer exists (a person deleted it)."""
    try:
        r = crm.call("/api/internal/task/%s/" % task_uuid)
    except Exception as exc:  # noqa: BLE001  (only a 404 is an answer; anything else is a failed read)
        if getattr(exc, "code", None) == 404:
            return None
        raise
    return r if isinstance(r, dict) else {}


def completed_at(task: dict | None) -> str:
    """When the task was completed, as DataSift wrote it, or '' while it is still open."""
    return str((task or {}).get("completed") or "")


def complete_task(crm, task_uuid: str) -> None:
    """Mark a task done. Used to close out a call task whose reason has passed (the sale happened),
    so it stops sitting in a caller's list as work to do. Completing can be undone in the app."""
    crm.call("/api/internal/task/%s/complete/" % task_uuid, "POST", {})


def task_person(assignee_uuid: str, people: list[dict], load: dict | None = None) -> dict | None:
    """Who a task goes to: the caller who already holds the record, else the active caller this run
    has given the fewest tasks. None when there is nobody to give it to. `people` is the list
    stages.resolve_callers returns ({name, uuid})."""
    if not people:
        return None
    for p in people:
        if assignee_uuid and p["uuid"] == assignee_uuid:
            return p
    load = load if load is not None else {}
    return min(people, key=lambda p: (load.get(p["uuid"], 0), people.index(p)))


# ---------------------------------------------------------------------- a preset's scheduled export
def export_path(preset_uuid: str) -> str:
    return "/api/internal/filter-preset/%s/scheduled-export/" % preset_uuid


def scheduled_export(crm, preset_uuid: str) -> dict | None:
    """The preset's scheduled export, or None when it has none."""
    try:
        r = crm.call(export_path(preset_uuid))
    except Exception as exc:  # noqa: BLE001
        if getattr(exc, "code", None) == 404:
            return None
        raise
    return r if isinstance(r, dict) and (r.get("uuid") or r.get("export_config")) else None


def export_body(tag_uuids: list[str], statuses: list[str], filename: str, start: date, recurrence: str = "weekly") -> dict:
    """The documented body for a dialer export: only phones carrying one of these tags (the tier
    tags) and one of these statuses, on a weekly, bi-weekly or monthly schedule. The message board
    is left out of the file: a power dialer has no use for it and it holds seller notes."""
    if recurrence not in EXPORT_RECURRENCE:
        raise ValueError("an export recurs %s, not %r" % (", ".join(EXPORT_RECURRENCE), recurrence))
    bad = [s for s in statuses if str(s).upper() not in PHONE_STATUSES]
    if bad:
        raise ValueError("unknown phone status %s (statuses are %s)" % (bad, ", ".join(PHONE_STATUSES)))
    must: dict = {}
    if tag_uuids:
        must["any_phone_tags"] = list(tag_uuids)
    if statuses:
        must["phone_status"] = [str(s).upper() for s in statuses]
    return {"export_config": {"columns": {"phones": {"must": must}}, "filename": filename, "export_messages": False},
            "start_date": start.isoformat(), "recurrence": recurrence, "enabled": True}


def set_scheduled_export(crm, preset_uuid: str, body: dict, exists: bool = False) -> dict:
    """Create the preset's scheduled export, or change the one it has."""
    r = crm.call(export_path(preset_uuid), "PATCH" if exists else "POST", body)
    return r if isinstance(r, dict) else {}
