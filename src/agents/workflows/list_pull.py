"""list_pull: a ranked list of property ids pulled into the account, quoted first, by approval only.

Atlas's workflow (team/roster.yaml, spend class `records`: the employee asks the operator first, with
the count). Three inputs: the ids (comma-separated, or the name of a JSON or CSV file of rows that
carry a dataflik_id, left on the brain's volume by an earlier task or put there), the list the
records go on, and how many NEW records the operator approved.

The order of every run, and why:
  1. Which ids SiftMap knows and which are already records (POST /properties/search/, ids-typed):
     an unknown id would make the quote and the add disagree.
  2. The quote (POST /properties/add-properties-by-query/quote/) on exactly the query the add sends:
     how many are new and billable. Nothing is stored or charged. A chunk whose quote counts fewer
     ids than were asked about is refused, because the add would be cut short the same way.
  3. Nothing is pulled unless the brain is live, the operator's approved count covers the billable
     count, and the quote's own allowance figures (when it reports them) leave room. Approved 0 asks
     for the quote only; that is how the operator learns the count to approve.
  4. One new id first. It must land, be read back with the same dataflik_id, and carry the list,
     before any other id is sent: a pull of thousands that attaches nothing is found on record one.
  5. The rest in chunks of 250, then a bounded wait for them to land. A re-run on the same list and
     ids sends nothing twice (state on the volume) and keeps watching.

The add is d4d_call_list's route A (verified on 31,714 records): the list and tag ride along by name
and attach to ids that were already records too. Nothing here texts, calls or mails anyone.
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from .. import config, slack, store

SRC = Path(__file__).resolve().parents[2]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import d4d_call_list as d4d  # noqa: E402
import pull_by_ids as pbi  # noqa: E402

CRM = None      # the selftest injects a fake DataSift; otherwise the budgeted Open API key client
MAP = None      # the selftest injects a fake SiftMap; otherwise SiftMap on the key, inside the app's budget
NAME = "list_pull"
QUOTE = "/properties/add-properties-by-query/quote/"
ADD = "/properties/add-properties-by-query/"
CHUNK = 250


def _setting(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        return default


def _crm():
    if CRM is not None:
        return CRM
    from ..autopilot.crm import CRM as Client  # noqa: PLC0415
    return Client()


class _BudgetedMap:
    """SiftMap's documented routes (map.reisift.io) on the Open API key, each request counted against
    the app's shared request budget like every DataSift read. d4d_call_list's search helpers call
    `_request(path, method, body)`, so this is shaped the same."""

    def __init__(self):
        from siftmap_standalone import SiftMapClient  # noqa: PLC0415
        key = (os.environ.get("REISIFT_API_KEY") or "").strip()
        if not key:
            raise RuntimeError("REISIFT_API_KEY is not set: SiftMap needs the account's Open API key")
        self.client = SiftMapClient(key, min_interval=0.5)

    def _request(self, path, method="POST", body=None):
        from ..autopilot import budget  # noqa: PLC0415
        budget.acquire()
        return self.client._request(path, method=method, body=body)


def _map():
    return MAP if MAP is not None else _BudgetedMap()


def _folder(task: dict) -> Path:
    d = config.RUNS_DIR / ("%s-%d" % (NAME, int(task["id"])))
    (d / "files").mkdir(parents=True, exist_ok=True)
    return d


def _slug(s: str) -> str:
    return "_".join(p for p in "".join(c if c.isalnum() else " " for c in str(s)).split() if p) or "list"


# --------------------------------------------------------------------------- the ids

def _ids_from_rows(rows) -> list[str]:
    out = []
    for r in rows or []:
        v = r if not isinstance(r, dict) else (r.get("dataflik_id") or r.get("DataSift id") or r.get("PropertyID"))
        v = str(v or "").strip()
        if v.isdigit() and v not in out:
            out.append(v)
    return out


def _find_file(name: str) -> Path | None:
    """A file an earlier task left (runs/*/files/<name>) or one put in the volume's list_pull folder.
    Only a bare file name is accepted, so a request cannot walk the disk."""
    if not name or any(x in name for x in ("/", "\\", "..")):
        return None
    hits = sorted(config.RUNS_DIR.glob("*/files/%s" % name), key=lambda p: p.stat().st_mtime)
    own = Path(config.DATA_DIR) / NAME / name
    if own.is_file():
        hits.append(own)
    return hits[-1] if hits else None


def resolve_ids(raw: str) -> tuple[list[str], str]:
    """(ids in order, without repeats) or ([], why)."""
    text = str(raw or "").strip()
    if not text:
        return [], "give the ids (comma-separated) or the name of a file of rows with dataflik_id"
    if all(c.isdigit() or c in ", \n\t;" for c in text):
        ids = _ids_from_rows([x for x in text.replace(";", ",").replace("\n", ",").replace("\t", ",").replace(" ", ",").split(",")])
        return (ids, "") if ids else ([], "no ids in %r" % text[:60])
    path = _find_file(text)
    if not path:
        return [], "no file named %r on this box (an earlier task's file, or one put in %s)" % (
            text, Path(config.DATA_DIR) / NAME)
    if path.suffix.lower() == ".csv":
        with path.open(encoding="utf-8-sig", newline="") as f:
            ids = _ids_from_rows(list(csv.DictReader(f)))
    else:
        try:
            ids = _ids_from_rows(json.loads(path.read_text(encoding="utf-8")))
        except ValueError as exc:
            return [], "%s is not JSON: %s" % (path.name, exc)
    return (ids, "") if ids else ([], "%s holds no dataflik_id" % path.name)


# --------------------------------------------------------------------------- DataSift bits

def _rows(r) -> list:
    return r if isinstance(r, list) else ((r or {}).get("results") or (r or {}).get("data") or [])


def ensure_title(crm, kind: str, title: str, live: bool) -> str:
    """The uuid of the list or tag with this title, created only when live. Lists are unique ignoring
    case, so a case variant is the same list."""
    rows = _rows(crm.call("/api/internal/%s/?limit=999" % kind))
    for row in rows:
        if (row.get("title") or "").strip().casefold() == title.strip().casefold():
            return row.get("uuid") or ""
    if not live:
        return ""
    return (crm.call("/api/internal/%s/" % kind, "POST", {"title": title}) or {}).get("uuid") or ""


def _titles(items) -> list[str]:
    return [(x.get("title") if isinstance(x, dict) else str(x)) or "" for x in items or []]


def quote(m, ids: list[str], list_name: str, tag: str) -> dict:
    """The documented quote, chunk by chunk, on exactly the query the add will send."""
    out = {"total": 0, "in_account": 0, "new": 0, "billable": 0, "allowance_left": None, "shortfall": 0,
           "blocked": 0, "problem": ""}
    for i in range(0, len(ids), CHUNK):
        chunk = ids[i:i + CHUNK]
        q = m._request(QUOTE, "POST", {"query": d4d.add_query_body(chunk, list_name, tag)["query"]}) or {}
        if not isinstance(q, dict) or not isinstance(q.get("total"), int):
            out["problem"] = "the quote came back without a total: %s" % str(q)[:160]
            return out
        if q["total"] != len(chunk):
            out["problem"] = ("the quote counted %d of %d ids in one chunk, so the add would be cut short the "
                              "same way; nothing was pulled" % (q["total"], len(chunk)))
            return out
        out["total"] += q["total"]
        out["in_account"] += int(q.get("in_account") or 0)
        out["new"] += int(q.get("new") or 0)
        bill = q.get("billable")
        out["billable"] += int(bill if isinstance(bill, int) else (q.get("new") or 0))
        out["shortfall"] += int(q.get("shortfall") or 0) if isinstance(q.get("shortfall"), int) else 0
        out["blocked"] += int(q.get("blocked_count") or 0) if isinstance(q.get("blocked_count"), int) else 0
        left = (q.get("property_usage") or {}).get("allowance_left")
        if isinstance(left, (int, float)):
            out["allowance_left"] = left if out["allowance_left"] is None else min(out["allowance_left"], left)
    return out


def refusal(q: dict, approved: int) -> str:
    """Why the pull must not run on this quote, or "". Each line is the overspend it prevents."""
    if q.get("problem"):
        return q["problem"]
    if q["billable"] > approved:
        return ("the quote says %d new records would use the allowance and %d were approved; nothing was pulled"
                % (q["billable"], approved))
    if q["allowance_left"] is not None and q["billable"] > q["allowance_left"]:
        return ("the quote says %d new records and the account has %s left in its allowance; nothing was pulled"
                % (q["billable"], q["allowance_left"]))
    if q["shortfall"]:
        return "the quote reports a shortfall of %d records; nothing was pulled" % q["shortfall"]
    return ""


def probe(crm, m, pid: str, list_name: str, tag: str, wait_min: float) -> tuple[str, str]:
    """(record uuid, "") once the first new id landed, reads back with the same dataflik_id and carries
    the list; else ("", why). Nothing else is sent until this passes."""
    m._request(ADD, "POST", d4d.add_query_body([pid], list_name, tag))
    landed = d4d.poll_saved(m, [pid], wait_min=wait_min, every=30.0)
    if pid not in landed:
        return "", "the first id (%s) did not land in %g minutes, so nothing else was sent" % (pid, wait_min)
    uuid = landed[pid]
    rec = crm.call("/api/internal/property/%s/" % uuid) or {}
    if str(rec.get("dataflik_id") or "") != pid:
        return "", "the first id landed with dataflik_id %s, not %s; nothing else was sent" % (rec.get("dataflik_id"), pid)
    if list_name.casefold() not in [t.casefold() for t in _titles(rec.get("lists"))]:
        crm.call("/api/internal/property/%s/add-lists/" % uuid, "POST", {"lists": list_name})
        rec = crm.call("/api/internal/property/%s/" % uuid) or {}
        if list_name.casefold() not in [t.casefold() for t in _titles(rec.get("lists"))]:
            return "", "the first record landed without the list %r and adding it did not stick" % list_name
    return uuid, ""


# --------------------------------------------------------------------------- state

def _state_path(list_name: str, ids: list[str]) -> Path:
    h = hashlib.sha1(",".join(ids).encode()).hexdigest()[:10]
    return Path(config.DATA_DIR) / NAME / ("%s_%s" % (_slug(list_name).lower(), h)) / "state.json"


def _load(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save(p: Path, st: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=1), encoding="utf-8")
    tmp.replace(p)


# --------------------------------------------------------------------------- the workflow

def run(task: dict) -> dict:
    inputs = store.payload_of(task).get("inputs") or {}
    folder = _folder(task)
    list_name = str(inputs.get("list") or "").strip()
    try:
        approved = int(float(str(inputs.get("approved_count") or 0).replace(",", "")))
    except ValueError:
        approved = 0
    base = {"run_dir": str(folder), "list": list_name, "approved": approved, "live": config.live()}
    if not list_name:
        return dict(base, ok=False, mode="refused", problem="name the DataSift list the records go on")
    ids, why = resolve_ids(inputs.get("ids") or "")
    if not ids:
        return dict(base, ok=False, mode="refused", problem=why)
    cap = int(_setting("LIST_PULL_MAX_IDS", 5000))
    if len(ids) > cap:
        return dict(base, ok=False, mode="refused", ids=len(ids),
                    problem="%d ids is over the %d a run takes (LIST_PULL_MAX_IDS); split the file" % (len(ids), cap))
    tag = list_name
    crm, m = _crm(), _map()
    sp = _state_path(list_name, ids)
    st = _load(sp)
    sent, new = set(st.get("sent") or []), dict(st.get("new") or {})
    saved, unknown = pbi.saved_uuids(m, ids)
    existing = dict(st["existing"]) if st.get("existing") is not None else {i: u for i, u in saved.items() if i not in new}
    new.update({i: u for i, u in saved.items() if i in sent and i not in existing})
    todo = [i for i in ids if i not in saved and i not in unknown and i not in sent]
    send = todo + [i for i in existing if i not in sent]
    q = quote(m, send, list_name, tag) if send else {"total": 0, "in_account": 0, "new": 0, "billable": 0,
                                                       "allowance_left": None, "shortfall": 0, "blocked": 0, "problem": ""}
    base.update(ids=len(ids), unknown=len(unknown), already_records=len(existing), to_pull=len(todo), quote=q,
                sent_before=len(sent))
    (folder / "files" / "quote.json").write_text(json.dumps(dict(base, unknown_ids=unknown[:200]), indent=1),
                                                 encoding="utf-8")
    base["files"] = ["quote.json"]
    why = refusal(q, approved) if send else ""
    if q.get("problem"):
        return dict(base, ok=False, mode="refused", problem=q["problem"])
    if not send and not st.get("sent"):
        return dict(base, ok=True, mode="nothing", problem="")
    if send and (approved <= 0 or not config.live()):
        return dict(base, ok=True, mode="quote", problem="")
    if why:
        return dict(base, ok=False, mode="refused", problem=why)

    probe_uuid = st.get("probe_uuid") or ""
    if send:
        ensure_title(crm, "list", list_name, live=True)
        ensure_title(crm, "tag", tag, live=True)
        if todo and not st.get("probed"):
            pid = todo.pop(0)
            sent.add(pid)
            _save(sp, {**st, "sent": sorted(sent), "new": new, "existing": existing, "list": list_name})
            probe_uuid, why = probe(crm, m, pid, list_name, tag, _setting("LIST_PULL_PROBE_WAIT_MIN", 5))
            if why:
                return dict(base, ok=False, mode="refused", problem=why, sent=len(sent))
            new[pid] = probe_uuid
            st.update(probed=True, probe_uuid=probe_uuid)
        rest = todo + [i for i in existing if i not in sent]
        for i in range(0, len(rest), CHUNK):
            chunk = rest[i:i + CHUNK]
            m._request(ADD, "POST", d4d.add_query_body(chunk, list_name, tag))
            sent.update(chunk)
            _save(sp, {**st, "sent": sorted(sent), "new": new, "existing": existing, "list": list_name})
    pending = [i for i in sent if i not in new and i not in existing]
    if pending:
        new.update(d4d.poll_saved(m, pending, wait_min=_setting("LIST_PULL_WAIT_MIN", 5), every=30.0))
    still = [i for i in sent if i not in new and i not in existing]
    _save(sp, {**st, "sent": sorted(sent), "new": new, "existing": existing, "list": list_name,
               "pending": still, "when": datetime.now().isoformat(timespec="seconds")})
    # A run that only attached existing records had no new id to probe: read one of them back, so a
    # list that never attached is said out loud rather than assumed.
    checked = None
    if not probe_uuid and existing and any(i in sent for i in existing):
        first = next(u for i, u in existing.items() if i in sent)
        rec = crm.call("/api/internal/property/%s/" % first) or {}
        checked = list_name.casefold() in [t.casefold() for t in _titles(rec.get("lists"))]
    return dict(base, ok=True, mode="pulled", probe_uuid=probe_uuid, sent=len(sent), landed=len(new),
                still_landing=len(still), existing_readback=checked, problem="")


def render(task: dict, result: dict) -> str:
    name = result.get("list") or "the list"
    if result.get("mode") == "refused" or not result.get("ok"):
        return "List pull into %s refused: %s." % (name, str(result.get("problem") or "no reason given").rstrip("."))
    q = result.get("quote") or {}
    head = ("List pull into %s: %d ids, %d already records, %d new, %d unknown to SiftMap." % (
        name, result.get("ids", 0), result.get("already_records", 0), result.get("to_pull", 0), result.get("unknown", 0)))
    if result.get("mode") == "nothing":
        return head + " Every id was already sent or already on the account; nothing to pull."
    if result.get("mode") == "quote":
        why = ("The brain is in dry mode, so nothing was pulled." if result.get("approved", 0) > 0
               else "Nothing was pulled.")
        return ("%s The quote: %d of them would use the record allowance. %s To pull, the operator approves "
                "%d new records and I run it again with that count." % (head, q.get("billable", 0), why,
                                                                         q.get("billable", 0)))
    first = ("the first record landed and read back on the list" if result.get("probe_uuid") else
             "no new record to probe" if result.get("existing_readback") is None else
             "an existing record read back on the list" if result.get("existing_readback") else
             "an existing record read back without the list, so check that the list attached")
    return ("%s Pulled: %s; %d ids sent in all, %d landed, %d still landing (a re-run keeps watching and sends "
            "nothing twice)." % (head, first, result.get("sent", 0), result.get("landed", 0),
                                 result.get("still_landing", 0)))
