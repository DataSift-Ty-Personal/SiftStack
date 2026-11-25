"""Run log and report rendering.

Every substitution, drop, gap and TODO is a Log entry, because a clone that
prints "created 132 presets" and hides that 6 of them now point at the wrong
human is exactly the silent success this repo keeps re-learning to distrust.
"""
from __future__ import annotations

import json
import os
import tempfile
import time

ACTIONS = ("translated", "created", "exists", "exists_differs", "verified", "dropped",
           "gap", "placeholder", "manual_todo", "skipped", "probe", "warn", "mismatch")


class Log:
    def __init__(self):
        self.entries: list[dict] = []

    def add(self, family: str, action: str, obj: str = "", detail: str = "", **extra) -> dict:
        assert action in ACTIONS, action
        e = {"family": family, "action": action, "object": obj, "detail": detail}
        e.update(extra)
        self.entries.append(e)
        return e

    def by(self, action: str, family: str | None = None) -> list[dict]:
        return [e for e in self.entries if e["action"] == action
                and (family is None or e["family"] == family)]

    def counts(self, family: str) -> dict:
        out = {}
        for e in self.entries:
            if e["family"] == family:
                out[e["action"]] = out.get(e["action"], 0) + 1
        return out

    def print_family(self, family: str, actions=("dropped", "gap", "placeholder",
                                                 "manual_todo", "mismatch", "warn")) -> None:
        for e in self.entries:
            if e["family"] == family and e["action"] in actions:
                print("    [%s] %s%s" % (e["action"], e["object"],
                                         (": " + e["detail"]) if e["detail"] else ""))


def atomic_json(obj, path: str) -> None:
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp_", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        json.dump(obj, f, indent=1, ensure_ascii=False, default=str)
    os.replace(tmp, path)


def render_markdown(header: dict, summary: list[dict], log: Log,
                    counts: list[dict] | None, verdict: str) -> str:
    L = []
    L.append("# Account blueprint apply report")
    L.append("")
    for k, v in header.items():
        L.append("- %s: %s" % (k, v))
    L.append("")
    L.append("## Families")
    L.append("")
    L.append("| family | wanted | existed | created | verified | gaps | manual |")
    L.append("|---|---|---|---|---|---|---|")
    for s in summary:
        L.append("| %(family)s | %(wanted)s | %(existed)s | %(created)s | %(verified)s | %(gaps)s | %(manual)s |" % s)
    L.append("")
    if counts:
        L.append("## Preset record counts (target vs source)")
        L.append("")
        L.append("| folder | preset | target | source | verdict |")
        L.append("|---|---|---|---|---|")
        for c in counts:
            L.append("| %(folder)s | %(title)s | %(target)s | %(source)s | %(verdict)s |" % c)
        L.append("")
    todos = log.by("manual_todo")
    L.append("## Configure by hand (%d)" % len(todos))
    L.append("")
    if any(e.get("steps") for e in todos):
        L.append("In build order: statuses, then boards and their columns, then sequence folders and "
                 "sequences. Run apply again afterwards: it creates whatever was waiting on these.")
        L.append("")
    for i, e in enumerate(todos, 1):
        L.append("%d. [%s] %s: %s" % (i, e["family"], e["object"], e["detail"]))
        for s in e.get("steps") or []:
            L.append("    - %s" % s)
    L.append("")
    for title, action in (("Gaps", "gap"), ("Placeholders", "placeholder"),
                          ("Dropped references", "dropped"), ("Mismatches", "mismatch"),
                          ("Warnings", "warn")):
        rows = log.by(action)
        L.append("## %s (%d)" % (title, len(rows)))
        L.append("")
        for e in rows:
            L.append("- [%s] %s: %s" % (e["family"], e["object"], e["detail"]))
        L.append("")
    L.append("## Translation log (%d)" % len(log.by("translated")))
    L.append("")
    for e in log.by("translated"):
        L.append("- [%s] %s: %s" % (e["family"], e["object"], e["detail"]))
    L.append("")
    L.append("## Verdict")
    L.append("")
    L.append(verdict)
    L.append("")
    return "\n".join(L)


def stamp() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


# ------------------------------------------------ the by-hand checklist, in words
# What a person clicks for each object the published API cannot create. Written from the
# blueprint's own titles, so it needs nothing from the target account.

TRIGGER_CARDS = {
    "property.status.updated": "Property Status Change",
    "property.assigned": "Property Assignee Change",
    "property.tags.added": "Property Tags Added",
    "property.tags.removed": "Property Tags Removed",
    "property.lists.added": "Property Lists Added",
    "property.lists.removed": "Property Lists Removed",
    "task.created": "Task Created",
    "task.completed": "Task Completed",
    "card.created": "SiftLine Card Created",
    "card.moved": "SiftLine Card Moved",
}


def _name(v) -> str:
    """A reference, a list of them, or a plain value, as a person reads it."""
    if isinstance(v, list):
        seen, out = set(), []
        for x in v:
            n = _name(x)
            if n not in seen:
                seen.add(n)
                out.append(n)
        return ", ".join(out)
    if isinstance(v, dict):
        if "$ref" in v:
            kind, t = v["$ref"], v.get("title") or ""
            if kind == "self":
                return "you (the user applying the blueprint)"
            if kind == "column" and v.get("board"):
                return "%s (board %s)" % (t, v["board"])
            if kind == "task_preset" and v.get("group"):
                return "%s (task group %s)" % (t, v["group"])
            return t
        if "$unresolved" in v:
            return "(something the export could not name; leave it out)"
        return ", ".join("%s %s" % (k, _name(x)) for k, x in v.items())
    return "" if v is None else str(v)


def _title(v) -> str:
    return (v.get("title") or "") if isinstance(v, dict) and "$ref" in v else _name(v)


def _condition(c: dict) -> str:
    kind, p = c.get("condition") or "", c.get("payload") or {}
    field = p.get("field") or ""
    vals = p.get("values") if p.get("values") is not None else p.get("value")
    if field in ("tags_uuid", "tags", "lists_uuid", "lists"):
        noun = "tags" if field.startswith("tags") else "lists"
        how = {"has_all": "all of the %s", "has_any": "any of the %s"}.get(kind, "the %s") % noun
        return "the record has %s: %s" % (how, _name(vals))
    if kind == "from_to":
        what = {"status": "the status", "column": "the card's column",
                "assigned_to": "the assignee"}.get(field, field or "the value")
        s = "%s changes to %s" % (what, _title(p.get("updated_to")))
        if p.get("updated_from"):
            s = "%s changes from %s to %s" % (what, _title(p.get("updated_from")), _title(p.get("updated_to")))
        boards = (p.get("meta") or {}).get("boards")
        return s + (" (board %s)" % _name(boards) if boards else "")
    if kind == "equals_to":
        return "%s is %s" % ({"title": "the task's title"}.get(field, field), _name(vals))
    return " ".join(x for x in (kind, field, _name(vals)) if x)


def _action(a: dict) -> str:
    act, p = a.get("action") or "", a.get("payload") or {}
    if a.get("$manual"):
        card = {"send-sms": "Send SMS", "send-email": "Send Email"}.get(act, act)
        return ("%s, with your own integration and your own message (the source's text is kept "
                "in the blueprint file under `kept`)" % card)
    if act == "set-field-value":
        if p.get("field") == "status":
            return "Change Property Status to %s" % _name(p.get("value"))
        return "Set %s to %s" % (p.get("field"), _name(p.get("value")))
    if act in ("remove", "add"):
        noun = {"lists": "Lists", "tags": "Tags"}.get(p.get("field"), str(p.get("field")))
        return "%s Property %s: %s" % ("Remove" if act == "remove" else "Add", noun, _name(p.get("values")))
    if act == "property-tasks-delete-all":
        return "Clear Property Tasks"
    if act == "create-siftline-card":
        v = p.get("values") or {}
        return "Create New Card on board %s, column %s" % (_name(v.get("board")), _title(v.get("column")))
    if act == "move-siftline-card":
        v = p.get("values") or {}
        to, frm = v.get("to") or {}, v.get("from") or {}
        s = "Move Card to board %s, column %s" % (_name(to.get("board")), _title(to.get("column")))
        return s + (" (from board %s)" % _name(frm.get("board")) if frm.get("board") else "")
    if act == "create-task-by-preset":
        return "Create New Task from the task preset %s" % _name(p.get("task_preset"))
    if act == "property-assign":
        return "Assign Property to %s" % _name(p.get("value"))
    if act == "send-webhook":
        return ("Send Webhook, to a webhook of your own (Settings, Integrations, Webhooks); "
                "leave this action out if you have none")
    rest = {k: v for k, v in p.items() if k != "label"}
    return "%s (%s)" % (act, _name(rest)) if rest else act


def sequence_steps(q: dict) -> list[str]:
    """The Sequence Builder clicks for one blueprint sequence."""
    steps = ["Open Sequences and click Create Sequence. Title: %s. Folder: %s."
             % (q.get("title"), q.get("folder") or "default")]
    trig = q.get("trigger") or ""
    steps.append('Drag the trigger card "%s" onto the trigger area.' % TRIGGER_CARDS.get(trig, trig))
    conds = q.get("conditions") or []
    steps.append(("Add the condition: %s." % "; and ".join(_condition(c) for c in conds)) if conds
                 else "Add no condition.")
    steps.append('Click "Set the Following Actions".')
    for i, a in enumerate(q.get("actions") or []):
        lead = "Drag the action card: " if i == 0 else 'Click "Add new Action +", then drag the action card: '
        steps.append(lead + _action(a) + ".")
    manual = any(a.get("$manual") for a in q.get("actions") or [])
    on = bool(q.get("is_active")) and not manual
    steps.append('Turn it %s, then click "Save Sequence".'
                 % ("on" if on else "off (it starts inactive; turn it on once it is right)"))
    return steps


def board_steps(board: str, columns: list, *, create_board: bool) -> list[str]:
    """`columns` is [(position, title)] in the blueprint's order."""
    steps = ["In SiftLine, add a board titled %s." % board] if create_board else []
    for pos, col in columns:
        steps.append("On board %s, add the column %s (position %d)." % (board, col, pos))
    return steps


def status_steps(s: dict) -> list[str]:
    return ["In the DataSift app, add a custom lead status titled %s with the color %s."
            % (s.get("title"), s.get("color") or "of your choice")]
