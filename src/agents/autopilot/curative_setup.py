"""Set up the Curative Title board and load the houses the curative research says to work now
(Ty, 2026-11-23).

    python src/agents/autopilot/curative_setup.py board [--commit] [--no-login]
    python src/agents/autopilot/curative_setup.py columns
    python src/agents/autopilot/curative_setup.py load [--results DIR] [--only slug,slug] [--limit N] [--commit]
    python src/agents/autopilot/curative_setup.py reset-statuses [--results DIR] [--only slug,slug] [--commit]
    python src/agents/autopilot/curative_setup.py reset-statuses --undo <log> [--commit]

All of them are dry by default: they read and say what they would do. A write needs --commit AND
CURATIVE_ALLOW=1, the two switches every account tool here takes, so a typed flag alone never writes.

The houses and their heir stories come from the curative engine (src/curative_title.py: rank, stories, phones),
which writes <market>/stories/ (stories_results.json, houses/<slug>/{input.json, heirs.json, story.md,
phones_checked.json}) next to <market>/work/scored.json. `load` reads that folder (--results, or the one market
under output/curative_title that has results). docs/curative-title.md walks the whole workflow.

An account with only the Open API key (no DataSift login in .env) gets the tag and the board from `board --commit`,
makes the nine columns by hand in the app (the published API has no column route), and `columns` then reads their
ids for the stage once one card is on the board.

board   the tag Curative, the SiftLine board Curative Title and its nine columns (Research, Ready for
        Marketing, Day 1 to Day 5, Heir Engaged, Exhausted). The tag and the board are made on the Open API
        key (documented routes); the published API has no column route, so the columns are made with this
        workstation's own login (siftline_columns.py), the way the First to Market board's were. A missing
        column is made right after the one before it (Ready for Marketing after Research, added 2026-11-23),
        the board's order is read back, and when the board did not shift the later columns itself each
        column is given its place through the column's own route and read back again. The run prints the
        column ids for the stage's block (column_ids): no documented route lists them.

reset-statuses  clears the status earlier marketing left on the work-now houses (Ty, 2026-11-23: "clear out
        any of the old statuses of the properties that we've marketed to prior, reset them, and then load
        them"), so the board can work the family. The houses are taken the way `load` takes them (each found
        by address). KEEP_STATUSES are never cleared: a sale or a deal (sold, closed, listed, under
        contract ...) and a do-not-contact (dnc, opt_out, compliance, not marketing). Every other status is
        cleared, a lead status included, and each house whose lead status is cleared is named in the output.
        One exception: a house that another hold keeps off the board whatever its status (Do Not Market, a
        litigator, a company or a trust as the owner) keeps its status, because clearing it lets nothing on
        and wipes out what a person decided; it is listed with the hold. Tags, lists, Do Not Market and the
        owner's own flags are never touched. Each change is logged with the status before it, the first is
        read back, and --undo puts the logged statuses back.

load    for each house the research's verdict says to work now ("Work now", with or without a confirmed
        number yet):
          1. its DataSift record, found BY ADDRESS (backfill.find_record: the house number, the street and
             the ZIP; two candidates is no match, never a guess). The record ids the research's scoring
             kept (work/scored.json) are a cross-check only, because they cover a fraction of the houses
             that are in the account: when both exist and disagree, the house goes to a person, and a
             house only the old id names is listed for a person too. A house in neither is listed with its
             SiftMap id for a person to pull in. This tool never pulls a record in.
          2. what the gate holds even from research (Do Not Market, a litigator, an opt-out, sold, a
             finished status), a lead (the lead board owns the owner) and a company or a trust are left
             alone and listed.
          3. each LIVING heir's Trestle-confirmed numbers (the name matched, grade A or B, no litigator risk)
             go on the owner of the record, tagged with the heir's tie, Heir, Curative and the house tier
             from Trestle's activity score. Dial First and Second always, Dial Third only for an heir with
             nothing better (the research write-back's rule), lower never: a caller works the record's
             phone list, and the team's own rule is not to dial those.
          4. the heir story (story.md) is rendered to a PDF and filed in the record's documents.
          5. one pinned note: the heirs, their shares and who to call first. Never a phone number.
          6. the heir tree for the stage: a fact here, and a file for the brain, which keeps its own memory
             (copy <data>/curative/heirs/ to the box's /data/curative/heirs/).
          7. the Curative tag, LAST: the tag is the board's door, so the stage never sees half a load.
        The first record's writes are read back before any other record is touched.

ZIP CODES. The research has none. The real run asks the Census Bureau's free geocoder, one address a
call, paced, and keeps every answer in a file so an address is asked once; no answer means no letter,
never a guessed zip. A dry run never calls it.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

SRC = Path(__file__).resolve().parents[2]
ROOT = SRC.parent
_HERE = str(Path(__file__).resolve().parent)
if __name__ == "__main__":
    # Run as a script, this folder would be first on the path and its modules (mail, policy, windows)
    # would shadow anything else of the same name. Only src belongs there.
    sys.path[:] = [p for p in sys.path if os.path.abspath(p or os.getcwd()) != _HERE]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agents import config, store                     # noqa: E402
from agents.autopilot import curative as cur          # noqa: E402
from agents.autopilot import gate, heirlane, lanes, ledger, mail, stages   # noqa: E402
from agents.autopilot.crm import record_said, rows_of  # noqa: E402

ALLOW_ENV = "CURATIVE_ALLOW"
# The research folder the first heir stories were written in, before the engine shipped: read only when no engine
# results exist and that folder does. research-fallback-ok
PILOT = ROOT / "output" / "research" / "curative_title" / "pilot"


def default_results() -> Path:
    """The curative engine's results folder (src/curative_title.py writes <market>/stories/): CURATIVE_RESULTS when set,
    else the one market under CURATIVE_DIR (default output/curative_title) that has results. With two or more,
    --results must name one; nothing is guessed."""
    env = os.environ.get("CURATIVE_RESULTS", "").strip()
    if env:
        return Path(env)
    base = Path(os.environ.get("CURATIVE_DIR") or (ROOT / "output" / "curative_title"))
    found = sorted({p.parent for p in base.glob("*/stories/stories_results.json")})
    if len(found) == 1:
        return found[0]
    if len(found) > 1:
        raise SystemExit("results for more than one market: name one with --results (%s)"
                         % ", ".join(str(p) for p in found))
    if (PILOT / "stories_results.json").exists():
        return PILOT
    raise SystemExit("no curative results yet: run `python src/curative_title.py stories run` (and summarize) first, "
                     "or name the folder with --results")
WORK_NOW = "Work now"
NOTE_MARK = "Curative title:"
CENSUS = "https://geocoding.geo.census.gov/geocoder/locations/address"
R_TAGS = "/api/internal/tag/?offset=0&limit=10000&ordering=title"
R_TAG_NEW = "/api/internal/tag/"
R_BOARDS = "/api/internal/siftline/board/?limit=100&offset=0"
R_BOARD_NEW = "/api/internal/siftline/board/"
R_BOARD_ONE = "/api/internal/siftline/board/%s/"
TOP_TIERS = ("Dial First", "Dial Second")
RESERVE_TIER = "Dial Third"
THIRD_MODES = ("person", "never", "always")
TIER_RANK = {"Dial First": 0, "Dial Second": 1, "Dial Third": 2}
MISS_DAYS = 30           # a geocoder "no match" is asked again after this many days
STATES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA", "colorado": "CO",
    "connecticut": "CT", "delaware": "DE", "district of columbia": "DC", "florida": "FL", "georgia": "GA",
    "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS", "kentucky": "KY",
    "louisiana": "LA", "maine": "ME", "maryland": "MD", "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
    "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV", "new hampshire": "NH",
    "new jersey": "NJ", "new mexico": "NM", "new york": "NY", "north carolina": "NC", "north dakota": "ND",
    "ohio": "OH", "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI",
    "south carolina": "SC", "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT",
    "virginia": "VA", "washington": "WA", "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
}


class Refused(SystemExit):
    """A write that was not allowed, or a read-back that did not match. Never caught on the way out."""


def may_write(commit: bool) -> bool:
    """Both switches, or nothing is sent."""
    return bool(commit) and os.environ.get(ALLOW_ENV) == "1"


def default_folder() -> Path:
    return Path(config.DATA_DIR) / "curative"


def _read_json(path: Path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _log(path: Path | None, row: dict) -> None:
    if path is None:
        return
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(dict(row, at=datetime.now().isoformat(timespec="seconds")), default=str) + "\n")


# ============================================================================================ board
def _tag_id(crm, title: str) -> str:
    """The tag's id by title (any case), read fresh: a tag made a moment ago must be found."""
    want = title.strip().lower()
    for t in rows_of(crm.call(R_TAGS)):
        if str(t.get("title") or "").strip().lower() == want and t.get("uuid"):
            return str(t["uuid"])
    return ""


def _board_row(boards, title: str = cur.BOARD):
    want = title.strip().lower()
    return next((b for b in boards or [] if str(b.get("title") or "").strip().lower() == want and b.get("uuid")), None)


def board(crm, columns_crm=None, *, commit: bool = False, out=print) -> dict:
    """The tag, the board and its columns: read what exists, then (both switches on) make what is missing,
    each read back. Returns what was found and done, with the column ids by title."""
    import siftline_columns
    allowed = may_write(commit)
    done = {"allowed": allowed, "tag": "", "board": "", "column_ids": {}, "would": [], "made": [], "already": []}
    tag_id = _tag_id(crm, cur.TAG)
    row = _board_row(rows_of(crm.call(R_BOARDS)))
    have = {}
    if row and columns_crm is not None:
        try:
            have = {c["title"].strip().lower(): c for c in siftline_columns.list_columns(columns_crm, row["uuid"])}
        except Exception as exc:  # noqa: BLE001  (a dry run says it could not list them; a real run stops below)
            if allowed:
                raise
            out("could not list the board's columns with this workstation's login (%s)" % str(exc)[:120])
    (done["already"] if tag_id else done["would"]).append("tag %s" % cur.TAG)
    (done["already"] if row else done["would"]).append("board %s" % cur.BOARD)
    for i, title in enumerate(cur.COLUMNS):
        if title.lower() in have:
            done["already"].append("column %s" % title)
            continue
        prev = next((t for t in reversed(cur.COLUMNS[:i]) if t.lower() in have), "")
        done["would"].append("column %s%s" % (title, (", right after %s, then the order read back" % prev) if prev else ""))
    if not allowed:
        out("DRY: nothing sent. %d to make, %d already there. A real run needs --commit and %s=1."
            % (len(done["would"]), len(done["already"]), ALLOW_ENV))
        for w in done["would"]:
            out("would make  %s" % w)
        if columns_crm is None:
            out("With --no-login the tag and the board are made on the Open API key and the columns by hand in the app "
                "(the published API has no column route); the run prints the steps.")
        return done
    if not tag_id:
        crm.call(R_TAG_NEW, "POST", {"title": cur.TAG})
        tag_id = _tag_id(crm, cur.TAG)
        if not tag_id:
            raise Refused("stopped: the tag %s was sent but does not read back" % cur.TAG)
        done["made"].append("tag %s" % cur.TAG)
        out("made     tag %s" % cur.TAG)
    done["tag"] = tag_id
    if row:
        uuid = row["uuid"]
    else:
        made = crm.call(R_BOARD_NEW, "POST", {"title": cur.BOARD}) or {}
        uuid = made.get("uuid") if isinstance(made, dict) else None
        if not uuid:
            uuid = (_board_row(rows_of(crm.call(R_BOARDS))) or {}).get("uuid")
        if not uuid or str((crm.call(R_BOARD_ONE % uuid) or {}).get("title") or "").strip() != cur.BOARD:
            raise Refused("stopped: the board %s was sent but does not read back" % cur.BOARD)
        done["made"].append("board %s" % cur.BOARD)
        out("made     board %s" % cur.BOARD)
    done["board"] = uuid
    if columns_crm is None:
        # The published API makes the tag and the board but not a board's columns, and a key-only account has no
        # login to make them with. They are made by hand once; the stage (and `columns`) then learns their ids.
        done["manual"] = list(cur.COLUMNS)
        out(manual_columns_text())
        return done
    cols = siftline_columns.list_columns(columns_crm, uuid)
    for i, title in enumerate(cur.COLUMNS):
        have = {c["title"].strip().lower(): c for c in cols}
        if title.lower() in have:
            continue
        # A missing column goes right after the one before it in the board's order (Ready for Marketing after
        # Research), not at the end: the board is read left to right, and the round reads that way.
        prev = next((have[t.lower()] for t in reversed(cur.COLUMNS[:i]) if t.lower() in have), None)
        place = int(prev["order"]) + 1 if prev is not None and isinstance(prev.get("order"), int) else i
        siftline_columns.create_column(columns_crm, uuid, title, place)
        cols = siftline_columns.list_columns(columns_crm, uuid)
        if not any(c["title"].strip().lower() == title.lower() for c in cols):
            raise Refused("stopped: the column %s was sent but does not read back; nothing after it was made" % title)
        done["made"].append("column %s" % title)
        out("made     column %s (asked for place %d)" % (title, place))
    why = order_problem(cols)
    if why:
        out("the board read back out of order (%s); putting the columns in order" % why)
        cols, why = reorder(columns_crm, uuid, cols)
    done["order_problem"] = why
    done["column_ids"] = {t: c["uuid"] for t in cur.COLUMNS for c in cols if c["title"].strip().lower() == t.lower()}
    out("The board reads: %s." % ", ".join(c["title"] for c in cols))
    if why:
        out("The column order did not take (%s). Every column exists and the stage works by id; drag the columns into "
            "order in the app (Ready for Marketing right after Research)." % why)
    out("Put these under the curative stage's column_ids in team/autopilot.yaml:")
    for title in cur.COLUMNS:
        out('      "%s": "%s"' % (title, done["column_ids"].get(title, "")))
    return done


def manual_columns_text() -> str:
    """The steps for an account with only the Open API key: the columns are made in the app, once."""
    lines = ["The tag %s and the board %s are in the account. The published API cannot make a board's columns, so make "
             "them once in the app:" % (cur.TAG, cur.BOARD),
             "  1. Open SiftLine, then the %s board." % cur.BOARD,
             "  2. Add these columns, left to right, with these exact titles: %s." % ", ".join(cur.COLUMNS),
             "  3. Put any one record on the Research column (it can be taken off again later).",
             "  4. A minute later run: python src/agents/autopilot/curative_setup.py columns",
             "     It reads the columns' ids from that card through the documented routes and prints them for the "
             "stage's column_ids. The board stage also learns them by itself once a card is on the board."]
    return "\n".join(lines)


def columns(crm, *, out=print, now: datetime | None = None) -> dict:
    """The board's column ids, learned through documented routes only (the board, the account log and one card's
    timeline), for the curative stage's column_ids. Needs one card on the board."""
    from agents.autopilot import leadroutes
    store.init()
    problems: list = []
    b = leadroutes.load_board(crm, cur.BOARD, expect=cur.COLUMNS, now=now or datetime.now(timezone.utc), problems=problems)
    titles = {u: str(v.get("title") or "") for u, v in (b.columns or {}).items()}
    ids = {}
    for t in cur.COLUMNS:
        u = leadroutes.match_title(titles, t)
        if u and str(titles.get(u, "")).strip().lower() == t.lower():
            ids[t] = u
    missing = [t for t in cur.COLUMNS if t not in ids]
    if not b.columns:
        out("The %s board's columns could not be read yet%s. Put any one record on the Research column in the app, wait a "
            "minute, and run this again." % (cur.BOARD, (": " + problems[0]) if problems else ""))
        return {"column_ids": {}, "missing": list(cur.COLUMNS)}
    out("Learned from %s. Put these under the curative stage's column_ids in team/autopilot.yaml:" % b.source)
    for t in cur.COLUMNS:
        out('      "%s": "%s"' % (t, ids.get(t, "")))
    if missing:
        out("Missing on the board (make them in the app with these exact titles): %s" % ", ".join(missing))
    return {"column_ids": ids, "missing": missing}


R_COLUMN_ONE = "/api/internal/siftline/board/%s/column/%s/"


def wanted_order(cols: list) -> list:
    """The board's columns in the order they belong: the stage's columns in COLUMNS order, then any column a
    person added, in the order the board shows them now."""
    want = [t.lower() for t in cur.COLUMNS]
    ours = sorted([c for c in cols if c["title"].strip().lower() in want], key=lambda c: want.index(c["title"].strip().lower()))
    return ours + [c for c in cols if c["title"].strip().lower() not in want]


def order_problem(cols: list) -> str:
    """What is wrong with the board's order as read back (siftline_columns.list_columns sorts by `order`), or
    '' when the stage's columns read in COLUMNS order and no two columns share a place."""
    orders = [c.get("order") for c in cols]
    if len(set(orders)) != len(orders):
        return "two columns share one place"
    want = [t.lower() for t in cur.COLUMNS]
    mine = [c["title"].strip().lower() for c in cols if c["title"].strip().lower() in want]
    if mine != sorted(mine, key=want.index):
        return "the columns read %s" % ", ".join(c["title"] for c in cols)
    return ""


def reorder(columns_crm, board_uuid: str, cols: list) -> tuple[list, str]:
    """Give every column its place (0, 1, 2 ...) in the wanted order through the column's own route, the
    last place first so no two columns share one on the way, then read the board back. The published API
    has no column route, so whether the server takes this is proven only by the read-back: returns (the
    columns as read back, '' or what is still wrong)."""
    import siftline_columns
    want = wanted_order(cols)
    try:
        for idx in reversed(range(len(want))):
            c = want[idx]
            if c.get("order") != idx:
                # The title rides along unchanged, so a server that reads the body whole never blanks it.
                columns_crm.call(R_COLUMN_ONE % (board_uuid, c["uuid"]), "PATCH", {"title": c["title"], "order": idx})
    except Exception as exc:  # noqa: BLE001  (an order that does not take is said; every column still exists)
        return siftline_columns.list_columns(columns_crm, board_uuid), "the board refused a new place (%s)" % str(exc)[:100]
    back = siftline_columns.list_columns(columns_crm, board_uuid)
    return back, order_problem(back)


# ============================================================================================ load
def state_code(raw) -> str:
    """A two-letter state code from 'TN', 'tn' or 'Tennessee'; '' when it is neither."""
    s = " ".join(str(raw or "").split())
    if len(s) == 2 and s.isalpha():
        return s.upper()
    return STATES.get(s.lower(), "")


def line_type(raw) -> str:
    t = str(raw or "").strip().lower()
    if t == "mobile":
        return "MOBILE"
    if t == "landline":
        return "LANDLINE"
    if "voip" in t:
        return "VOIP"
    return "UNKNOWN"


def occupied(d: dict) -> bool:
    """Does the research say a family member lives in the house? Read the way the verdict read it."""
    occ = str((d or {}).get("occupancy") or "")
    if re.match(r"\s*(unknown|probably nobody|reads vacant|not owner occupied)", occ, re.I):
        return False
    return bool(re.search(r"\b(lives?|living) in the house\b|\bboth live in the house\b|\blists the house as (her|his) "
                          r"current address\b|^\s*occupied\b", occ, re.I))


def work_now(results: Path, only=()) -> list[dict]:
    """The houses the research's verdict says to work now, one per street (a rerun of a house wins),
    largest equity a share first."""
    rows = _read_json(Path(results) / "stories_results.json") or []
    best: dict = {}
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict) or not str(r.get("verdict") or "").startswith(WORK_NOW):
            continue
        k = str(r.get("street") or "")
        if k not in best or str(r.get("slug") or "").endswith("-opus"):
            best[k] = r
    out = [dict(r, folder=str(Path(results) / "houses" / str(r.get("slug") or ""))) for r in best.values()]
    if only:
        out = [r for r in out if r.get("slug") in set(only)]
    return sorted(out, key=lambda r: (str(r.get("verdict")) != WORK_NOW, -(r.get("equity_per_heir") or 0), str(r.get("slug"))))


def read_house(folder) -> tuple[dict | None, dict | None, list]:
    """(the heir file, the house's input, the Trestle checks). A person's verified heir file wins."""
    folder = Path(folder)
    d = _read_json(folder / "heirs_verified.json") or _read_json(folder / "heirs.json")
    inp = _read_json(folder / "input.json")
    checked = _read_json(folder / "phones_checked.json")
    return (d if isinstance(d, dict) else None, inp if isinstance(inp, dict) else None,
            [r for r in checked if isinstance(r, dict)] if isinstance(checked, list) else [])


def scored_index(path) -> dict:
    """(house number, zip) -> the scoring rows there, for the cross-check against the address match."""
    out: dict = {}
    rows = _read_json(Path(path)) if path else None
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict):
            continue
        street = " ".join(str(r.get("street") or "").split())
        num = (street.split() or [""])[0]
        out.setdefault((num, str(r.get("zip") or "")[:5]), []).append(r)
    return out


def scored_match(index: dict, street: str, zip5: str) -> dict:
    """The record ids and SiftMap ids the scoring kept for this address: same house number, same street
    after abbreviations, same zip."""
    from agents.autopilot.sources import same_street
    num = (street.split() or [""])[0]
    rows = [r for r in index.get((num, zip5), []) if same_street(street, str(r.get("street") or ""))]
    return {"uuids": sorted({str(u) for r in rows for u in (r.get("uuids") or []) if u}),
            "siftmap": sorted({str(r.get("dataflik_id")) for r in rows if r.get("dataflik_id")})}


def confirmed_numbers(name: str, checked: list, third: str = "person") -> tuple[list, Counter]:
    """(the heir's numbers to write, best first, what was left out and why). A number counts when Trestle
    matched it to this heir's name, graded it A or B, and saw no litigator risk."""
    notes: Counter = Counter()
    items, seen = [], set()
    for r in checked or []:
        if cur.legal_name(r.get("heir")) != name:
            continue
        n = gate.digits(r.get("phone"))
        if not n or n in seen:
            continue
        seen.add(n)
        if not r.get("match") or str(r.get("grade") or "").upper() not in ("A", "B") or r.get("litigator") \
                or r.get("error") or r.get("valid") is False:
            notes["a number Trestle did not confirm for the heir (no name match, a low grade, a litigator risk)"] += 1
            continue
        items.append({"number": n, "tier": cur.tier_from_activity(r.get("activity")), "type": line_type(r.get("linetype"))})
    top = [x for x in items if x["tier"] in TOP_TIERS]
    third_ = [x for x in items if x["tier"] == RESERVE_TIER]
    if third == "always":
        keep = top + third_
    elif third == "never":
        keep = top
    else:
        keep = top or third_
    for x in items:
        if x not in keep:
            notes["a confirmed number below the tiers the team dials (%s): not written" % (x["tier"] or "unscored")] += 1
    keep.sort(key=lambda x: (TIER_RANK.get(x["tier"], 9), 0 if x["type"] == "MOBILE" else 1))
    return keep, notes


def build_tree(slug: str, d: dict, inp: dict, checked: list, third: str = "person") -> tuple[dict, Counter]:
    """The heir tree the stage reads (curative.heirs_of_tree), from one house's research. A heir is
    VERIFIED when the research shows them living with high or medium confidence; only a living heir's
    confirmed numbers are written."""
    notes: Counter = Counter()
    heirs, keys = [], set()
    for raw in (d or {}).get("heirs") or []:
        if not isinstance(raw, dict):
            continue
        name = cur.legal_name(raw.get("name"))
        key = heirlane.heir_key(name)
        if not name or not key or key in keys:
            continue
        keys.add(key)
        said = str(raw.get("living") or "").strip().lower()
        living = True if said.startswith("yes") else (False if said.startswith("no") else None)
        conf = str(raw.get("confidence") or "").strip().lower()
        nums, why = confirmed_numbers(name, checked, third)
        notes.update(why)
        if living is not True and nums:
            notes["numbers of an heir not shown living: not written"] += len(nums)
            nums = []
        share_num = raw.get("share_num")
        heirs.append({"key": key, "name": name, "first": mail._clean_first(name) or "",
                      "relationship": cur.relationship_head(raw.get("relationship")),
                      "tag": cur.relationship_tag(raw.get("relationship")), "share": cur.share_text(raw.get("share")),
                      "share_num": float(share_num) if isinstance(share_num, (int, float)) and not isinstance(share_num, bool) else 0.0,
                      "living": living, "verified": living is True and conf in ("high", "medium"), "confidence": conf,
                      "numbers": nums, "street": " ".join(str(raw.get("street") or "").split()),
                      "city": " ".join(str(raw.get("city") or "").split()), "state": state_code(raw.get("state")), "zip": ""})
    ready = [h for h in heirs if h["living"] is True and h["verified"]]
    pick = sorted([h for h in ready if h["numbers"]] or ready,
                  key=lambda h: (-h["share_num"], TIER_RANK.get((h["numbers"] or [{}])[0].get("tier"), 9), heirs.index(h)))
    tree = {"version": 1, "source": "the loader", "record_uuid": "", "slug": slug,
            "street": " ".join(str((inp or {}).get("street") or "").split()), "loaded": "", "pdf": None,
            "occupied": occupied(d), "call_first": pick[0]["key"] if pick else "", "heirs": heirs}
    return tree, notes


def note_text(tree: dict, pdf_name: str) -> str:
    """The one pinned note: the heirs, their shares and who to call first. Never a phone number."""
    heirs = [h for h in tree.get("heirs") or [] if h.get("living") is not False]
    first = next((h for h in heirs if h.get("key") == tree.get("call_first")), None)
    lines = ["%s the owner of record has died with no will on file and no open probate, so every heir with a share "
             "signs (a deed of their share and a release, each drawn by an attorney)." % NOTE_MARK, "Heirs:"]
    for h in heirs:
        bits = [b for b in (h.get("relationship"), ("%s share" % h["share"]) if h.get("share") else "") if b]
        line = "- %s%s." % (h.get("name"), (", " + ", ".join(bits)) if bits else "")
        if h.get("living") is None:
            line += " Not confirmed living: ask the family."
        elif not h.get("verified"):
            line += " Not verified yet."
        elif not h.get("numbers"):
            line += " No confirmed number yet."
        if first is not None and h.get("key") == first.get("key"):
            line += " Call first."
        lines.append(line)
    if first is not None:
        bits = [b for b in (first.get("relationship"), ("%s share" % first["share"]) if first.get("share") else "") if b]
        lines.append("Call first: %s%s." % (first.get("name"), (" (%s)" % ", ".join(bits)) if bits else ""))
    lines.append("Call 1 is discovery, with no price: say plainly that we are local buyers, and ask who is who in the "
                 "family and who wants out. The offer comes on call 2. Walk away if someone who will not sell lives in "
                 "the house.")
    if tree.get("occupied"):
        lines.append("The research says a family member may live in the house: ask what they want before any offer.")
    lines.append("The full heir map is the PDF %s in this record's documents." % pdf_name)
    return re.sub(r"\d{10,}", "", "\n".join(lines))


def phone_payloads(rec: dict, tree: dict) -> tuple[list, list, Counter]:
    """(the phone objects to upsert, the same numbers as they stood before, what was left alone). A number
    a person marked wrong, dead or do-not-call is never written back; one that already carries everything
    is left as it is."""
    have = {gate.digits(p.get("number")): p for p in ((rec.get("owner") or {}).get("phones") or [])}
    out, before, notes, seen = [], [], Counter(), set()
    for h in tree.get("heirs") or []:
        if h.get("living") is not True:
            continue
        for x in h.get("numbers") or []:
            n = gate.digits(x.get("number"))
            if not n or n in seen:
                continue
            seen.add(n)
            now = have.get(n)
            if now and str(now.get("status") or "UNKNOWN").upper() in gate.BAD_PHONE:
                notes["a number a person marked wrong, dead or do-not-call: left as it is"] += 1
                continue
            base = dict(now) if now else {"number": n, "type": x.get("type") or "UNKNOWN", "tags": [], "status": "UNKNOWN"}
            payload = stages.phone_payload(base, x.get("tier") or "", [h.get("tag") or "Relative", "Heir", cur.TAG])
            if now and sorted(stages.tag_titles(now)) == payload["tags"]:
                notes["already on the record with these tags"] += 1
                continue
            out.append(payload)
            before.append(dict(now) if now else {"number": n, "new": True})
    return out, before, notes


def _gate_cfg(cfg=None):
    """The gate settings the playbook gives, or the gate's defaults (which hold more) when it does not load."""
    if cfg is not None:
        return cfg
    try:
        from agents.autopilot import policy
        return getattr(policy.load(), "gate", None) or gate.GateConfig()
    except Exception:  # noqa: BLE001
        return gate.GateConfig()


def held_why(rec: dict, cfg) -> str:
    """Why the loader leaves a record alone: what the gate holds even from research, a lead, a company."""
    if not rec or not rec.get("uuid"):
        return "the record could not be read"
    s = lanes.status_of(rec)
    if s in lanes.LEAD_STATUSES:
        return "a lead (status %s): the lead board owns the family" % s
    v = gate.check(rec, "research", cfg=cfg)
    if not v.ok:
        return v.reason()
    from deep_prospect_batch import is_entity
    if is_entity(rec.get("owner") or {}):
        return "a company or a trust owns it (the board works people's families only)"
    return ""


# ------------------------------------------------------------------------------------- zip codes
def zip_from_census(data, street: str) -> tuple[str, str]:
    """(zip, how) from one Census geocoder answer: one zip that every match with the same house number
    agrees on, else none."""
    matches = (((data or {}).get("result") or {}).get("addressMatches") or []) if isinstance(data, dict) else []
    num = (str(street or "").split() or [""])[0].upper()
    zips = set()
    for m in matches:
        if not isinstance(m, dict):
            continue
        z = str((m.get("addressComponents") or {}).get("zip") or "")[:5]
        if not re.fullmatch(r"\d{5}", z):
            continue
        if num and not str(m.get("matchedAddress") or "").upper().startswith(num + " "):
            continue
        zips.add(z)
    if len(zips) == 1:
        return zips.pop(), "matched"
    return "", "no match" if not zips else "matches in %d zip codes" % len(zips)


def census_zip(street: str, city: str, state: str) -> tuple[str, str]:
    """One address, one call to the Census Bureau's free geocoder. A failed call raises (and is not
    remembered, so the next run asks again)."""
    import requests                     # late: only a real load asks
    r = requests.get(CENSUS, params={"street": street, "city": city, "state": state, "benchmark": "Public_AR_Current",
                                     "format": "json"},
                     headers={"User-Agent": "siftstack-curative-loader"}, timeout=30)
    if r.status_code != 200:
        raise RuntimeError("HTTP %s" % r.status_code)
    return zip_from_census(r.json(), street)


class ZipCache:
    """Every zip the geocoder gave, in a file, so one address is asked once. A "no match" is asked again
    after MISS_DAYS; a failed call is not kept at all. With no lookup (a dry run) nothing is asked."""

    def __init__(self, path, lookup=None, sleep=time.sleep, gap: float = 1.0, now: datetime | None = None):
        self.path, self.lookup, self.sleep, self.gap = Path(path), lookup, sleep, float(gap)
        self.now = now or datetime.now(timezone.utc)
        got = _read_json(self.path)
        self.data = got if isinstance(got, dict) else {}
        self.asked = self.found = self.missed = 0
        self.changed = False

    def _stale(self, got: dict) -> bool:
        try:
            at = datetime.fromisoformat(str(got.get("at") or ""))
        except ValueError:
            return True
        at = at if at.tzinfo else at.replace(tzinfo=timezone.utc)
        return (self.now - at).days >= MISS_DAYS

    def zip(self, street: str, city: str, state: str) -> tuple[str, str]:
        key = "|".join(" ".join(str(x or "").split()).upper() for x in (street, city, state))
        got = self.data.get(key)
        if isinstance(got, dict) and (got.get("zip") or not self._stale(got)):
            return str(got.get("zip") or ""), "kept from an earlier run"
        if self.lookup is None:
            return "", "not asked in a dry run"
        if self.asked:
            self.sleep(self.gap)
        self.asked += 1
        try:
            z, how = self.lookup(street, city, state)
        except Exception as exc:  # noqa: BLE001  (no answer is no letter; the next run asks again)
            self.missed += 1
            return "", "the geocoder failed (%s)" % str(exc)[:80]
        self.data[key] = {"zip": z, "how": how, "at": self.now.isoformat(timespec="seconds")}
        self.changed = True
        if z:
            self.found += 1
        else:
            self.missed += 1
        return z, how

    def save(self) -> None:
        if not self.changed:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=1, sort_keys=True), encoding="utf-8", newline="\n")


def fill_zips(tree: dict, zips: ZipCache) -> Counter:
    """Each verified living heir with a street, city and state gets the zip the geocoder gives, or none."""
    c: Counter = Counter()
    for h in tree.get("heirs") or []:
        if h.get("living") is not True or not h.get("verified"):
            continue
        if not (h.get("street") and h.get("city") and h.get("state")):
            c["heirs with no full address in the research"] += 1
            continue
        z, how = zips.zip(h["street"], h["city"], h["state"])
        h["zip"] = z
        c["zip found" if z else "no zip (%s)" % how] += 1
    return c


def render_pdf(story_md, pdf_path) -> str:
    """The heir story as a PDF (deep_prospect_pdf.py: the same renderer as the research packs)."""
    from deep_prospect_pdf import generate_pdf
    Path(pdf_path).parent.mkdir(parents=True, exist_ok=True)
    return generate_pdf(story_md, pdf_path)


def _readback(crm, u: str, payloads: list, tag: bool) -> str:
    """What did not hold on the first record, or ''."""
    back = crm.record(u) or {}
    phones = {gate.digits(p.get("number")): p for p in ((back.get("owner") or {}).get("phones") or [])}
    for p in payloads:
        got = phones.get(gate.digits(p.get("number")))
        if not got or cur.TAG not in stages.tag_titles(got):
            return "a number written to the owner does not read back with the %s tag" % cur.TAG
    if tag and cur.TAG.lower() not in {t.lower() for t in stages.tag_titles(back)}:
        return "the %s tag does not read back on the record" % cur.TAG
    return ""


def _write_house(crm, u: str, rec: dict, tree: dict, payloads: list, before: list, story: Path, pdf_name: str,
                 renote: bool, tagged: bool, folder: Path, log_path, render, post, file_document, now: datetime,
                 notes: Counter, first: bool) -> None:
    """One house's writes, in order, the tag last. The first house of a run is read back after its numbers
    and again after its tag; a write that did not hold stops the run before any other house is touched."""
    _log(log_path, {"step": "start", "record": u, "tags_before": stages.tag_titles(rec)})
    if payloads:
        owner = (rec.get("owner") or {}).get("uuid") or ""
        if not owner:
            raise RuntimeError("the record has no owner to hold the numbers")
        crm.upsert_phones(owner, payloads)
        _log(log_path, {"step": "phones", "record": u, "owner": owner, "before": before,
                        "after": [{"number": p["number"], "tags": p["tags"]} for p in payloads]})
        if first:
            bad = _readback(crm, u, payloads, tag=False)
            if bad:
                raise Refused("stopped on the first record: %s; nothing more was written" % bad)
    if story.is_file():
        try:
            pdf = folder / "pdf" / ("%s.pdf" % tree.get("slug"))
            render(story, pdf)
            got = file_document(crm, u, pdf, pdf_name, prop=rec.get("id") if isinstance(rec.get("id"), int) else None,
                                post=post)
            tree["pdf"] = {"name": pdf_name, "filed": True, "at": now.isoformat(timespec="seconds")}
            _log(log_path, {"step": "pdf", "record": u, "name": pdf_name, "result": got})
        except Exception as exc:  # noqa: BLE001  (no PDF: the record waits in Research, and the run says why)
            notes["the PDF could not be filed (%s): the record waits in Research" % str(exc)[:60]] += 1
    if renote:
        try:
            crm.message(u, note_text(tree, pdf_name), pin=True)
            _log(log_path, {"step": "note", "record": u})
        except Exception as exc:  # noqa: BLE001
            notes["the note could not be pinned (%s)" % str(exc)[:60]] += 1
    tree["loaded"] = now.isoformat(timespec="seconds")
    ledger.set_fact(u, cur.TREE_FACT, tree)
    out_file = folder / "heirs" / ("%s.json" % u)
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(json.dumps(tree, indent=1), encoding="utf-8", newline="\n")
    if not tagged:
        crm.add_tags(u, [cur.TAG], rec)
        _log(log_path, {"step": "tag", "record": u, "tag": cur.TAG})
    if first:
        bad = _readback(crm, u, payloads, tag=True)
        if bad:
            raise Refused("stopped on the first record: %s; nothing more was written" % bad)


def load(crm, *, results=None, scored=None, only=(), limit: int = 0, commit: bool = False, renote: bool = False,
         third: str = "person", geocode=census_zip, render=render_pdf, post=None, out=print, folder=None,
         zip_gap: float = 1.0, gate_cfg=None, now: datetime | None = None, sleep=time.sleep) -> dict:
    """Load the work-now houses (the module's docstring says what, in order). Dry unless both switches
    are on: a dry run reads the research, finds each record and says what it would write; it writes
    nothing, renders nothing and never asks the geocoder."""
    from agents.autopilot.budget import BudgetExhausted
    from agents.research.writeback import file_document
    if third not in THIRD_MODES:
        raise SystemExit("--third must be one of %s" % ", ".join(THIRD_MODES))
    allowed = may_write(commit)
    now = now or datetime.now(timezone.utc)
    results = Path(results) if results else default_results()
    folder = Path(folder) if folder else default_folder()
    scored = Path(scored) if scored else results.parent / "work" / "scored.json"
    if allowed:
        store.init()
    gate_cfg = _gate_cfg(gate_cfg)
    houses = work_now(results, only)
    if limit:
        houses = houses[:int(limit)]
    index = scored_index(scored)
    zips = ZipCache(folder / "zip_cache.json", geocode if allowed else None, sleep=sleep, gap=zip_gap, now=now)
    log_path = folder / ("loaded_%s.jsonl" % now.date().isoformat()) if allowed else None
    counts: Counter = Counter()
    notes: Counter = Counter()
    report = {"allowed": allowed, "houses": len(houses), "counts": counts, "notes": notes, "loaded": [], "person": [],
              "not_in_datasift": [], "held": [], "would": []}
    verified_first = False
    for h in houses:
        slug, street = str(h.get("slug") or ""), " ".join(str(h.get("street") or "").split())
        d, inp, checked = read_house(h["folder"])
        if d is None or inp is None:
            counts["research files unreadable"] += 1
            report["person"].append({"slug": slug, "street": street, "why": "the research files could not be read"})
            continue
        m = _match(crm, index, h)
        street, kind = m["street"], m["kind"]
        counts["matched: %s" % kind] += 1
        if kind == "neither":
            report["not_in_datasift"].append({"slug": slug, "street": street, "zip": m["zip"], "siftmap": m["siftmap"]})
            continue
        if kind in ("both disagree", "old id only"):
            why = ("the address finds one record and the scoring kept another" if kind == "both disagree"
                   else "the address finds no single record; only the scoring's old id names one")
            report["person"].append({"slug": slug, "street": street, "why": why, "address": m["record"], "scored": m["scored"]})
            continue
        u = m["record"]
        rec = crm.record(u) or {}
        why = held_why(rec, gate_cfg)
        if why:
            counts["held"] += 1
            report["held"].append({"slug": slug, "street": street, "record": u, "why": why})
            continue
        tree, why_nums = build_tree(slug, d, inp, checked, third)
        notes.update(why_nums)
        tree["record_uuid"] = u
        notes.update(fill_zips(tree, zips))
        payloads, before, why_phones = phone_payloads(rec, tree)
        notes.update(why_phones)
        pdf_name = "%s%s.pdf" % (cur.DOC_PREFIX, slug)
        story = Path(h["folder"]) / "story.md"
        tagged = cur.TAG.lower() in {t.lower() for t in stages.tag_titles(rec)}
        said = record_said(crm, u, rec)
        renote_needed = renote or NOTE_MARK not in said
        heirs = tree["heirs"]
        living = [x for x in heirs if x["living"] is True and x["verified"]]
        plan = {"slug": slug, "street": street, "record": u, "heirs": len([x for x in heirs if x["living"] is not False]),
                "verified": len(living), "numbers": len(payloads), "letters_ready": sum(1 for x in living if x["zip"]),
                "pdf": pdf_name if story.is_file() else "", "note": renote_needed, "tag": not tagged}
        counts["houses to load"] += 1
        counts["numbers to write"] += len(payloads)
        if not story.is_file():
            notes["no story.md to render: the record waits in Research until a PDF is filed"] += 1
        if not allowed:
            report["would"].append(plan)
            continue
        # ---- the real run, in order; the tag last
        try:
            _write_house(crm, u, rec, tree, payloads, before, story, pdf_name, renote_needed, tagged, folder, log_path,
                         render, post, file_document, now, notes, first=not verified_first)
        except (Refused, BudgetExhausted):
            raise
        except Exception as exc:  # noqa: BLE001  (one house that fails is listed; the first one stops the run)
            if not verified_first:
                raise Refused("stopped on the first record (%s): %s; nothing more was written" % (street, str(exc)[:160]))
            report["person"].append({"slug": slug, "street": street, "why": "the load failed part way (%s)" % str(exc)[:120]})
            continue
        verified_first = True
        counts["houses loaded"] += 1
        report["loaded"].append(plan)
    if allowed:
        zips.save()
    counts["zip lookups asked"] += zips.asked
    _say(report, zips, folder, out)
    return report


def _say(report: dict, zips: ZipCache, folder: Path, out) -> None:
    c, n = report["counts"], report["notes"]
    out("%d house(s) to work now. Matched by address only %d, by address and the scoring's id together %d; for a person: "
        "the two disagree %d, only the old id %d; not in DataSift %d." % (
            report["houses"], c["matched: address only"], c["matched: both agree"], c["matched: both disagree"],
            c["matched: old id only"], c["matched: neither"]))
    verb = "Loaded" if report["allowed"] else "Would load"
    rows = report["loaded"] if report["allowed"] else report["would"]
    out("%s %d house(s), %d number(s); %d held by the gate, a lead or a company." % (
        verb, len(rows), c["numbers to write"], c["held"]))
    for r in rows[:40]:
        out("  %-28s record %s  heirs %d (%d verified), numbers %d, letters ready %d%s%s%s" % (
            r["street"][:28], r["record"][:8], r["heirs"], r["verified"], r["numbers"], r["letters_ready"],
            ", PDF" if r["pdf"] else ", no PDF", ", note" if r["note"] else "", ", tag" if r["tag"] else ""))
    for r in report["held"][:20]:
        out("  held     %-28s %s" % (r["street"][:28], r["why"]))
    for r in report["person"][:40]:
        out("  person   %-28s %s" % (r["street"][:28], r["why"]))
    for r in report["not_in_datasift"][:60]:
        out("  not in DataSift: %s, %s %s (SiftMap id %s): pull it in by hand" % (
            r["slug"], r["street"], r["zip"], ", ".join(r["siftmap"]) or "none kept"))
    for k, v in n.most_common():
        out("  note: %s: %d" % (k, v))
    if report["allowed"]:
        out("Zip lookups asked %d (found %d, no answer %d). Heir files: %s" % (zips.asked, zips.found, zips.missed,
                                                                             folder / "heirs"))
        out("Copy the heir files to the brain's /data/curative/heirs/ (fly ssh sftp shell -a siftstack-agents; from Git "
            "Bash set MSYS_NO_PATHCONV=1), so the board there reads them.")
    else:
        out("DRY: nothing written, nothing rendered, the geocoder never asked. A real run needs --commit and %s=1." % ALLOW_ENV)


# ================================================================================ reset-statuses
# A sale or a deal, and a do-not-contact: never cleared (Ty, 2026-11-23: "I just mean the marketing
# statuses, like the sold stuff. Keep those in. Everything else, though, we can clear out."). Compared
# with case, spaces and underscores ignored, so "Close Out" and "close_out" are the same status.
KEEP_STATUSES = ("sold", "closed", "close_out", "listed", "under_contract", "underwrite", "transactions", "lost_deal",
                 "buyer", "buyer_found", "buyer_lost", "push_deal", "dnc", "opt_out")


def _norm_status(s) -> str:
    return " ".join(str(s or "").replace("_", " ").lower().split())


KEEP_NORM = {_norm_status(s) for s in KEEP_STATUSES}


def status_verdict(stored) -> str:
    """keep (a sale, a deal, a do-not-contact), clear (anything else earlier marketing left), or none."""
    n = _norm_status(stored)
    if not n:
        return "none"
    return "keep" if n in KEEP_NORM else "clear"


def is_lead_status(stored) -> bool:
    """A status of the lead pipeline (lanes.LEAD_STATUSES, or any status that names a lead other than a dead
    one): a cleared one is named by street, because somebody may have been talking with that family."""
    s = str(stored or "").strip().lower()
    words = _norm_status(stored).split()
    return s in lanes.LEAD_STATUSES or ("lead" in words and _norm_status(stored) != "dead lead")


def _match(crm, index: dict, h: dict) -> dict:
    """One work-now house matched to its record the way `load` matches it: by address, with the scoring's old
    id as a cross-check. Returns {slug, street, zip, kind, record, scored, siftmap}."""
    from agents.autopilot.backfill import find_record
    slug, street = str(h.get("slug") or ""), " ".join(str(h.get("street") or "").split())
    _, inp, _ = read_house(h["folder"])
    if inp is None:
        return {"slug": slug, "street": street, "kind": "unreadable", "record": "", "zip": "", "scored": "", "siftmap": []}
    street = " ".join(str(inp.get("street") or street).split())
    zip5 = str(inp.get("zip") or "")[:5]
    by_addr = find_record(crm, street, zip5) if street else ""
    sm = scored_match(index, street, zip5)
    by_id = sm["uuids"][0] if len(sm["uuids"]) == 1 else ""
    if by_addr and by_id:
        kind = "both agree" if by_addr == by_id else "both disagree"
    else:
        kind = "address only" if by_addr else ("old id only" if by_id else "neither")
    return {"slug": slug, "street": street, "zip": zip5, "kind": kind, "record": by_addr, "scored": by_id,
            "siftmap": sm["siftmap"]}


def reset_statuses(crm, *, results=None, scored=None, only=(), commit: bool = False, out=print, folder=None,
                   now: datetime | None = None, gate_cfg=None) -> dict:
    """Clear the status earlier marketing left on each work-now house (the module's docstring says which).
    Dry unless both switches are on: a dry run finds each record and says what it would clear."""
    from agents.autopilot.leadroutes import stored_status
    allowed = may_write(commit)
    now = now or datetime.now(timezone.utc)
    results = Path(results) if results else default_results()
    folder = Path(folder) if folder else default_folder()
    scored = Path(scored) if scored else results.parent / "work" / "scored.json"
    index = scored_index(scored)
    houses = work_now(results, only)
    gate_cfg = _gate_cfg(gate_cfg)
    counts: Counter = Counter()
    statuses = {"clear": Counter(), "keep": Counter(), "held": Counter()}
    report = {"allowed": allowed, "houses": len(houses), "counts": counts, "statuses": statuses, "clear": [], "kept": [],
              "held": [], "person": [], "not_in_datasift": [], "cleared": [], "failed": [], "log": ""}
    for h in houses:
        m = _match(crm, index, h)
        counts["matched: %s" % m["kind"]] += 1
        if m["kind"] == "neither":
            report["not_in_datasift"].append({"slug": m["slug"], "street": m["street"], "zip": m["zip"], "siftmap": m["siftmap"]})
            continue
        if m["kind"] in ("both disagree", "old id only", "unreadable"):
            report["person"].append({"slug": m["slug"], "street": m["street"], "why": {
                "both disagree": "the address finds one record and the scoring kept another",
                "old id only": "the address finds no single record; only the scoring's old id names one",
                "unreadable": "the research files could not be read"}[m["kind"]]})
            continue
        rec = crm.record(m["record"]) or {}
        if not rec.get("uuid"):
            report["person"].append({"slug": m["slug"], "street": m["street"], "why": "the record could not be read"})
            continue
        stored = stored_status(rec)
        v = status_verdict(stored)
        if v == "none":
            counts["no status"] += 1
            continue
        row = {"slug": m["slug"], "street": m["street"], "record": m["record"], "before": stored, "lead": is_lead_status(stored)}
        if v == "clear":
            # Cleared only when that lets the house onto the board: with the status gone, would anything else
            # still hold it (Do Not Market, a litigator, a company owner)? Then the status stays as a person set it.
            other = held_why(dict(rec, status=None), gate_cfg)
            if other:
                v, row["why"] = "held", other
        statuses[v][stored] += 1
        {"clear": report["clear"], "keep": report["kept"], "held": report["held"]}[v].append(row)
    if allowed and report["clear"]:
        log_path = folder / ("reset_statuses_%s.jsonl" % now.date().isoformat())
        report["log"] = str(log_path)
        first = True
        for row in report["clear"]:
            # The log first: a write that dies half way must still be one undo can put back.
            _log(log_path, {"step": "status", "record": row["record"], "slug": row["slug"], "street": row["street"],
                            "before": row["before"]})
            try:
                crm.set_status(row["record"], None)
                if first:
                    back = stored_status(crm.record(row["record"]) or {})
                    if back:
                        raise Refused("stopped on the first record (%s): the status still reads %s; nothing more was "
                                      "written" % (row["street"], back))
                    first = False
            except Refused:
                raise
            except Exception as exc:  # noqa: BLE001  (one record that fails is listed; the first one stops the run)
                if first:
                    raise Refused("stopped on the first record (%s): %s; nothing more was written" % (row["street"], str(exc)[:160]))
                report["failed"].append(dict(row, why=str(exc)[:120]))
                continue
            report["cleared"].append(row)
    _say_reset(report, out)
    return report


def _say_reset(report: dict, out) -> None:
    c, s = report["counts"], report["statuses"]
    out("%d house(s) to work now. Matched by address only %d, by address and the scoring's id together %d; for a person: "
        "the two disagree %d, only the old id %d; not in DataSift %d." % (
            report["houses"], c["matched: address only"], c["matched: both agree"], c["matched: both disagree"],
            c["matched: old id only"], c["matched: neither"]))
    done = report["cleared"] if report["allowed"] else report["clear"]
    out("%s %d status(es): %s. Kept %d: %s. Left on %d house(s) another hold keeps off the board. No status on %d." % (
        "Cleared" if report["allowed"] else "Would clear", len(done),
        ", ".join("%s %d" % kv for kv in s["clear"].most_common()) or "none",
        sum(s["keep"].values()), ", ".join("%s %d" % kv for kv in s["keep"].most_common()) or "none",
        len(report["held"]), c["no status"]))
    leads = [r for r in done if r["lead"]]
    for r in leads:
        out("  %s a lead status: %s (%s)" % ("cleared" if report["allowed"] else "would clear", r["street"], r["before"]))
    for r in report["kept"]:
        out("  kept     %-28s %s" % (r["street"][:28], r["before"]))
    for r in report["held"]:
        out("  left     %-28s %s: %s" % (r["street"][:28], r["before"], r["why"]))
    for r in report["person"]:
        out("  person   %-28s %s" % (r["street"][:28], r["why"]))
    for r in report["not_in_datasift"]:
        out("  not in DataSift: %s, %s %s (SiftMap id %s)" % (r["slug"], r["street"], r["zip"], ", ".join(r["siftmap"]) or "none kept"))
    for r in report["failed"]:
        out("  failed   %-28s %s" % (r["street"][:28], r["why"]))
    if report["allowed"]:
        if report["log"]:
            out("Log (for --undo): %s" % report["log"])
        out("Next: load --commit, so these houses get their PDF, heir numbers, note and tag.")
    else:
        out("DRY: nothing written. A real run needs --commit and %s=1." % ALLOW_ENV)


def undo_reset(crm, log_path, *, commit: bool = False, out=print) -> dict:
    """Put back the statuses a reset cleared, newest first, from its log. A record that carries a status
    again (somebody set one since) is left as it is and named: the newer status is a person's decision."""
    from agents.autopilot.leadroutes import stored_status
    allowed = may_write(commit)
    rows = [json.loads(line) for line in Path(log_path).read_text(encoding="utf-8").splitlines() if line.strip()]
    done = {"allowed": allowed, "restored": [], "left": [], "would": []}
    seen: set = set()
    first = True
    for r in reversed([x for x in rows if x.get("step") == "status" and x.get("record") and x.get("before")]):
        u = r["record"]
        if u in seen:
            continue
        seen.add(u)
        now_status = stored_status(crm.record(u) or {})
        if now_status:
            done["left"].append({"street": r.get("street"), "why": "it reads %s now" % now_status})
            out("left     %-28s it reads %s now" % (str(r.get("street"))[:28], now_status))
            continue
        if not allowed:
            done["would"].append({"street": r.get("street"), "status": r["before"]})
            out("would put back %-28s %s" % (str(r.get("street"))[:28], r["before"]))
            continue
        crm.set_status(u, r["before"])
        if first:
            back = stored_status(crm.record(u) or {})
            if (back or "").strip().lower() != str(r["before"]).strip().lower():
                raise Refused("stopped on the first record (%s): it reads %s, not %s; nothing more was written"
                              % (r.get("street"), back, r["before"]))
            first = False
        done["restored"].append({"street": r.get("street"), "status": r["before"]})
        out("restored %-28s %s" % (str(r.get("street"))[:28], r["before"]))
    if not allowed:
        out("DRY: nothing written. A real run needs --commit and %s=1." % ALLOW_ENV)
    return done


# ============================================================================================== cli
def key_only_crm(gap: float = 2.0):
    """The autopilot's client on the Open API key and nothing else."""
    import datasift_api_upload as up
    from agents.autopilot.crm import CRM
    key = (up.env().get("REISIFT_API_KEY") or "").strip()
    if not key:
        raise SystemExit("REISIFT_API_KEY is not set (run from the repo root); this tool uses the Open API key")
    return CRM(api=up.Api(api_key=key), min_gap=max(gap, float(os.environ.get("AUTOPILOT_API_GAP_S") or 0)))


def has_login() -> bool:
    """Does this machine's .env hold a DataSift login (only our own accounts' workstations do)? A user's install
    has the Open API key alone, and the board's columns are then made by hand."""
    import datasift_api_upload as up
    e = up.env()
    return bool((e.get("DATASIFT_EMAIL") or "").strip() and (e.get("DATASIFT_PASSWORD") or "").strip())


class LazyLogin:
    """This workstation's login, minted the first time a request needs it (the board's columns only)."""

    def __init__(self, gap: float = 2.0):
        self.gap, self._crm = gap, None

    def call(self, path, method="GET", body=None, headers=None):
        if self._crm is None:
            import siftline_columns
            self._crm = siftline_columns.minted_crm(self.gap)
        return self._crm.call(path, method, body, headers)


def main(argv: list[str] | None = None) -> int:
    from agents.autopilot.budget import BudgetExhausted
    ap = argparse.ArgumentParser(description="The Curative Title board: make it (board), clear the old marketing statuses "
                                             "(reset-statuses) and load the houses to work now (load). Dry unless --commit "
                                             "and %s=1." % ALLOW_ENV)
    sub = ap.add_subparsers(dest="cmd")
    b = sub.add_parser("board", help="the tag, the board and its nine columns, in order")
    b.add_argument("--commit", action="store_true")
    b.add_argument("--gap", type=float, default=2.0, help="seconds between requests")
    b.add_argument("--no-login", action="store_true", help="do not use this workstation's login to list or make columns "
                                                            "(the default when .env holds no DataSift login)")
    sub.add_parser("columns", help="print the board's column ids for the stage (needs one card on the board)")
    ld = sub.add_parser("load", help="load the work-now houses onto their records")
    ld.add_argument("--results", default="", help="the curative results folder (stories_results.json, houses/); default: the one market under output/curative_title with results")
    ld.add_argument("--scored", default="", help="the scoring file for the cross-check (default <results>/../work/scored.json)")
    ld.add_argument("--only", default="", help="comma list of house slugs")
    ld.add_argument("--limit", type=int, default=0)
    ld.add_argument("--commit", action="store_true")
    ld.add_argument("--renote", action="store_true", help="pin a fresh note even when one is already there")
    ld.add_argument("--third", default="person", choices=THIRD_MODES, help="when a Dial Third number is written")
    ld.add_argument("--gap", type=float, default=2.0, help="seconds between DataSift requests")
    ld.add_argument("--zip-gap", type=float, default=1.0, help="seconds between geocoder calls")
    ld.add_argument("--out", default="", help="where the heir files, PDFs and the zip cache go (default <data>/curative)")
    rs = sub.add_parser("reset-statuses", help="clear the status earlier marketing left on the work-now houses")
    rs.add_argument("--results", default="", help="the curative results folder; default as for load")
    rs.add_argument("--scored", default="", help="the scoring file for the cross-check (default <results>/../work/scored.json)")
    rs.add_argument("--only", default="", help="comma list of house slugs")
    rs.add_argument("--undo", default="", help="a reset log: put its statuses back")
    rs.add_argument("--commit", action="store_true")
    rs.add_argument("--gap", type=float, default=2.0, help="seconds between DataSift requests")
    rs.add_argument("--out", default="", help="where the reset log goes (default <data>/curative)")
    args = ap.parse_args(list(sys.argv[1:] if argv is None else argv) or ["--help"])
    if getattr(args, "commit", False) and not may_write(True):
        print("refused: --commit was passed but %s is not 1. Nothing was sent." % ALLOW_ENV)
        return 3
    try:
        if args.cmd == "board":
            login = None if (args.no_login or not has_login()) else LazyLogin(args.gap)
            board(key_only_crm(args.gap), login, commit=args.commit)
            return 0
        if args.cmd == "columns":
            got = columns(key_only_crm(2.0))
            return 0 if got["column_ids"] and not got["missing"] else 2
        if args.cmd == "load":
            load(key_only_crm(args.gap), results=Path(args.results) if args.results else None, scored=Path(args.scored) if args.scored else None,
                 only=tuple(s.strip() for s in args.only.split(",") if s.strip()), limit=args.limit, commit=args.commit,
                 renote=args.renote, third=args.third, zip_gap=args.zip_gap, folder=Path(args.out) if args.out else None)
            return 0
        if args.cmd == "reset-statuses":
            if args.undo:
                undo_reset(key_only_crm(args.gap), Path(args.undo), commit=args.commit)
                return 0
            reset_statuses(key_only_crm(args.gap), results=Path(args.results) if args.results else None,
                           scored=Path(args.scored) if args.scored else None,
                           only=tuple(s.strip() for s in args.only.split(",") if s.strip()), commit=args.commit,
                           folder=Path(args.out) if args.out else None)
            return 0
    except BudgetExhausted as exc:
        print("stopped: %s. Run it again after the hour: a load picks up where it stopped." % exc)
        return 2
    ap.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
