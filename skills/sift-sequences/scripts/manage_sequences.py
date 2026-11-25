"""Read what your DataSift account has for sequences, and print click-through checklists.

Sequences are not in DataSift's published API, so this skill never builds one by script. Claude
walks you through the Sequence Builder in the app (SKILL.md, "Creating a Sequence", Steps 1 to 7).
This script only reads, on your Open API key, so the walkthrough skips what already exists and names
anything a sequence needs that the account is missing (a tag, a status, a list, a board, a folder).

    python manage_sequences.py --list                      # every sequence folder and sequence
    python manage_sequences.py --sold-sequence             # the Sold Property Cleanup checklist
    python manage_sequences.py --sold-sequence --offline   # the same checklist, no key needed
    python manage_sequences.py --check --tags "Sold" --statuses "Sold" \
        --lists "Foreclosure,Probate" --boards "Acquisitions" --folders "Transactions"
    python manage_sequences.py --selftest                  # the rules on invented data, no network

Your key: REISIFT_API_KEY, your DataSift Open API key (see datasift_core.py). Every request is a
GET, so nothing in your account changes.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from datasift_core import API, ApiError, DataSiftApi  # noqa: E402

SEQ_FOLDERS = "/api/internal/sequence-folder/?limit=999"
SEQUENCES = "/api/internal/sequence/?limit=999"
TAGS = "/api/internal/tag/?offset=0&limit=10000&ordering=title"
LISTS = "/api/internal/list/?offset=0&limit=10000"
STATUSES = "/api/internal/status/?limit=999"
BOARDS = "/api/internal/siftline/board/?offset=0&limit=999"

# The trigger cards in the Sequence Builder, by the event each one stores.
TRIGGERS = {
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

# Template 13 in SKILL.md, as a checklist.
SOLD_SEQUENCE = {
    "title": "Sold Property Cleanup",
    "folder": "Transactions",
    "trigger": "Property Tags Added",
    "condition": 'Property Tags: has the tag "Sold"',
    "actions": ["Change Property Status to Sold",
                "Remove Property Lists: Foreclosure, Probate, Tax Sale",
                "Clear Property Tasks",
                "Clear the assignee"],
    "needs": {"tags": ["Sold"], "statuses": ["Sold"], "lists": ["Foreclosure", "Probate", "Tax Sale"],
              "folders": ["Transactions"]},
    "note": "Removing lists is permanent for that record: a record tagged Sold by mistake loses them.",
}


def rows_of(r) -> list:
    if isinstance(r, list):
        return r
    return (r.get("results") or r.get("data") or []) if isinstance(r, dict) else []


def get_all(api, path: str) -> list:
    """Every row of a listing, following its `next` link if the server pages it."""
    out, seen = [], set()
    while path and path not in seen:
        seen.add(path)
        r = api.get(path)
        out.extend(rows_of(r))
        nxt = r.get("next") if isinstance(r, dict) else None
        path = nxt[len(API):] if isinstance(nxt, str) and nxt.startswith(API) else None
    return out


def fold(s) -> str:
    return re.sub(r"[\s._]+", " ", str(s or "")).strip().casefold()


def folder_title(row: dict) -> str:
    f = row.get("folder")
    return (f.get("title") if isinstance(f, dict) else f) or "(no folder)"


def list_sequences(api, out=print) -> dict:
    folders = [f.get("title") for f in get_all(api, SEQ_FOLDERS)]
    seqs = get_all(api, SEQUENCES)
    by_folder = {}
    for s in seqs:
        by_folder.setdefault(folder_title(s), []).append(s)
    out(f"{len(seqs)} sequences in {len(folders)} folders")
    for name in folders + sorted(set(by_folder) - set(folders)):
        rows = sorted(by_folder.get(name, []), key=lambda s: fold(s.get("title")))
        out(f"\n{name}  ({len(rows)})")
        for s in rows:
            trig = TRIGGERS.get(str(s.get("trigger") or ""), str(s.get("trigger") or "no trigger"))
            runs = s.get("runs")
            extra = f", {runs} runs" if isinstance(runs, int) else ""
            out(f"  [{'on ' if s.get('is_active') else 'off'}] {s.get('title')}  "
                f"(trigger: {trig}, {len(s.get('actions') or [])} actions{extra})")
    return {"folders": folders, "sequences": [s.get("title") for s in seqs]}


def check_names(api, wanted: dict) -> dict:
    """{family: {"present": [...], "missing": [...]}} for tags, statuses, lists, boards and sequence folders."""
    sources = {"tags": TAGS, "statuses": STATUSES, "lists": LISTS, "boards": BOARDS, "folders": SEQ_FOLDERS}
    result = {}
    for family, names in wanted.items():
        names = [n for n in names if n.strip()]
        if not names:
            continue
        have = {fold(r.get("title")) for r in get_all(api, sources[family])}
        result[family] = {"present": [n for n in names if fold(n) in have],
                          "missing": [n for n in names if fold(n) not in have]}
    return result


def sold_checklist(api=None, out=print) -> dict:
    t = SOLD_SEQUENCE
    state = {"exists": None, "active": None, "needs": {}}
    if api is not None:
        found = [s for s in get_all(api, SEQUENCES) if fold(s.get("title")) == fold(t["title"])]
        state["exists"] = bool(found)
        state["active"] = any(s.get("is_active") for s in found)
        state["needs"] = check_names(api, t["needs"])
        if found:
            out(f"Already in the account: {t['title']} in {folder_title(found[0])}, "
                f"{'on' if state['active'] else 'off'}. Nothing to build.")
            return state
        out(f"{t['title']} is not in the account yet.")
        missing = [(fam, n) for fam, r in state["needs"].items() for n in r["missing"]]
        for fam, n in missing:
            out(f"  Create first: the {fam.rstrip('s')} {n!r} (it does not exist in this account)")
    out(f"\nBuild it in the Sequence Builder:")
    steps = [f"Open Sequences in the left sidebar and click Create Sequence.",
             f"Title: {t['title']}. Folder: {t['folder']}.",
             f"Drag the trigger card \"{t['trigger']}\" onto the trigger area.",
             f"Add one condition: {t['condition']}.",
             "Click \"Set the Following Actions\"."]
    for i, action in enumerate(t["actions"]):
        step = f"drag the action card for: {action}."
        steps.append(step[0].upper() + step[1:] if i == 0 else "Click \"Add new Action +\", then " + step)
    steps.append("Click \"Save Sequence\". If DataSift says the title is taken, add \" V2\" to the title.")
    for n, s in enumerate(steps, 1):
        out(f"  {n}. {s}")
    out(f"\nNote: {t['note']}")
    state["steps"] = steps
    return state


# ---- selftest: invented account, no network ----
class FakeApi:
    def __init__(self, with_sold=False):
        self.calls = []
        self.data = {
            SEQ_FOLDERS: [{"uuid": "sf1", "title": "Transactions"}, {"uuid": "sf2", "title": "Lead Management"}],
            SEQUENCES: [{"uuid": "s1", "title": "Call New Lead", "is_active": True, "trigger": "property.status.updated",
                         "folder": {"uuid": "sf2", "title": "Lead Management"}, "actions": [{}, {}], "runs": 12}],
            TAGS: [{"title": "Hot Lead"}],
            STATUSES: [{"title": "sold"}, {"title": "new_lead"}],
            LISTS: [{"title": "Foreclosure"}, {"title": "Probate"}],
            BOARDS: [{"title": "Acquisitions"}],
        }
        if with_sold:
            self.data[SEQUENCES].append({"uuid": "s2", "title": "Sold Property Cleanup", "is_active": False,
                                         "trigger": "property.tags.added",
                                         "folder": {"uuid": "sf1", "title": "Transactions"}, "actions": [{}]})

    def get(self, path, params=None):
        self.calls.append(path)
        rows = self.data[path]
        # The first page of the sequence listing points at a second one, as a paged listing would.
        if path == SEQUENCES and len(rows) > 1:
            return {"count": len(rows), "results": rows[:1], "next": API + SEQUENCES + "&offset=1"}
        if path == SEQUENCES + "&offset=1":
            return {"count": 2, "results": self.data[SEQUENCES][1:], "next": None}
        return {"count": len(rows), "results": rows, "next": None}


class _Paged(FakeApi):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.data[SEQUENCES + "&offset=1"] = []


def selftest() -> int:
    results = []

    def check(label, got, want):
        results.append((label, got == want, got, want))

    quiet = lambda *_: None  # noqa: E731
    lines = []
    listed = list_sequences(FakeApi(), out=lines.append)
    check("list names every sequence", listed["sequences"], ["Call New Lead"])
    check("list shows the trigger by its card name",
          any("Property Status Change" in l for l in lines), True)
    fresh = sold_checklist(FakeApi(), out=quiet)
    check("a missing sequence gets the full checklist", (fresh["exists"], len(fresh["steps"])), (False, 10))
    check("a missing tag is named before building", fresh["needs"]["tags"]["missing"], ["Sold"])
    check("a status matches however it is spelled", fresh["needs"]["statuses"]["present"], ["Sold"])
    check("a missing list is named", fresh["needs"]["lists"]["missing"], ["Tax Sale"])
    built = sold_checklist(_Paged(with_sold=True), out=quiet)
    check("a paged listing is read to the end, and an existing sequence is not rebuilt",
          (built["exists"], built["active"], "steps" in built), (True, False, False))
    offline = sold_checklist(None, out=quiet)
    check("the checklist works with no key", len(offline["steps"]), 10)
    bad = 0
    for label, ok, got, want in results:
        bad += 0 if ok else 1
        print(f"{'ok  ' if ok else 'FAIL'} {label}" + ("" if ok else f": {got!r} (want {want!r})"))
    print(f"{len(results) - bad} of {len(results)} passed")
    return 1 if bad else 0


def _names(s: str) -> list:
    return [x.strip() for x in (s or "").split(",") if x.strip()]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Read your DataSift sequences and print click-through checklists "
                                             "(read only, on your Open API key)")
    ap.add_argument("--list", action="store_true", help="every sequence folder and sequence")
    ap.add_argument("--sold-sequence", action="store_true", help="the Sold Property Cleanup checklist")
    ap.add_argument("--check", action="store_true", help="which of the named things exist in the account")
    ap.add_argument("--tags", default="")
    ap.add_argument("--statuses", default="")
    ap.add_argument("--lists", default="")
    ap.add_argument("--boards", default="")
    ap.add_argument("--folders", default="", help="sequence folders")
    ap.add_argument("--offline", action="store_true", help="print the checklist without reading the account")
    ap.add_argument("--selftest", action="store_true", help="check the rules on invented data, no network")
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()
    if not (args.list or args.sold_sequence or args.check):
        ap.print_help()
        return 0
    if args.offline:
        if args.sold_sequence:
            sold_checklist(None)
        return 0
    api = DataSiftApi()
    try:
        if args.list:
            list_sequences(api)
        if args.check:
            res = check_names(api, {"tags": _names(args.tags), "statuses": _names(args.statuses),
                                    "lists": _names(args.lists), "boards": _names(args.boards),
                                    "folders": _names(args.folders)})
            for fam, r in res.items():
                print(f"{fam}: present {r['present'] or '-'}; missing {r['missing'] or '-'}")
        if args.sold_sequence:
            sold_checklist(api)
    except ApiError as exc:
        print(f"DataSift refused a request: {exc}")
        return 1
    print(f"({api.calls} requests, all reads)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
