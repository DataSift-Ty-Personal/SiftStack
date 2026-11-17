"""returned_mail: the scanned stack of returned envelopes read, and each address flagged on its record.

Ty, 2026-11-15: "Returned mail: read the scanned stack and flag those addresses." The lesson behind it:
collect the returned mail and scan the whole stack, front and back, into one PDF (Office Depot or
Staples will do it) so a VA or an admin can work the addresses. Handwritten letters carry no tracking,
so nothing in DataSift ever says one came back: until a person goes through the envelopes, the mail
lane keeps writing to a dead address every month. Somebody posts the PDF (or photos of the envelopes)
in Slack and asks Quinn; this workflow is the VA.

How it decides, and why each rule exists:
  * EVERY PAGE IS READ ON ITS OWN. A PDF is split into page images (pypdfium2) and each image is read
    by Claude Haiku 5.5 with vision, thinking off, into a small strict JSON: who the letter was
    addressed to, the delivery address, the post office's reason, a forwarding address. One envelope
    is usually two pages, the front and the back. The address side is the envelope; the back only
    lends the reason when the front shows none. The sender's return address and the yellow
    forwarding label are never taken for the delivery address.
  * A BACK PAGE JOINS ONLY ITS OWN FRONT. Whether the stack was scanned front first or back first is
    read off the whole stack once, and a back joins the front on that side of it or stands alone. A
    back that drifted onto the next envelope after one misread page could carry a "deceased" stamp to
    a stranger's record.
  * NOTHING IS GUESSED. The reader is told to leave a field empty rather than guess a digit. A page it
    cannot read is listed for a person, and an address with no house number, or with no ZIP and no
    city, is never matched.
  * TWO CANDIDATES IS NO MATCH. The record is found by street and ZIP the way the uploader finds one
    (the same house number, the same street name after abbreviations, the same ZIP), searched on the
    house number and the first street word, because DataSift standardizes a street on ingest and a
    prefix search on the raw line misses "Street" stored as "St". Two records at one address are never
    chosen between, and a search that comes back full is treated the same way.
  * THE NAME MUST FIT. A matched record is flagged only when the name on the envelope shares the
    surname of the record's owner. A different name is a different person (the house sold, a tenant),
    so it is listed for a person and nothing is written.
  * WHAT IS WRITTEN, LIVE ONLY: the tag Return Mail (the mail presets skip it, the research queue reads
    it as a return, and ftm_return_mail hands the record to research) and one note with the reason and
    the scan date. A forwarding address goes in that note for a person; it is never written into the
    owner's mailing address, because a yellow label can carry a forward that ran out years ago or the
    wrong household's new address. A deceased mark adds Verify Deceased and nothing more: the gate then
    holds the record for research, and an owner is never marked dead on a post office stamp (the death
    screen's rule). Every tag added is recorded with what was there before it (ledger.change), so
    `autopilot undo <run>` takes it off again; a note has no delete route, and the answer says so.
  * THE ANSWER CARRIES NO PHONE NUMBER: counts in the thread and a CSV in the run's files folder with
    every envelope and what happened to it. A number printed on an envelope is removed from the page's
    text before anything keeps it.
  * THE COST IS BOUNDED BEFORE IT IS SPENT. A stack longer than AGENTS_RETURNED_MAIL_ASK_PAGES gets the
    page count and the estimate first and is read only when the ask says go; no ask reads more than
    AGENTS_RETURNED_MAIL_MAX_PAGES pages or spends more than AGENTS_RETURNED_MAIL_RUN_USD, and the
    brain's daily cap still applies. On a dry box the pages are read when a person asked in Slack (a
    page costs well under a tenth of a cent, less than the turn that asked for it), so the plan says
    exactly what a live run would flag; AGENTS_RETURNED_MAIL_DRY_READ=0 stops that.

It never texts, calls or mails anyone, and it never changes an owner, a status or a mailing address.
"""
from __future__ import annotations

import base64
import csv
import io
import json
import logging
import re
import uuid as uuid_mod
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .. import attachments, config, store
from ..autopilot import budget, ledger
from ..autopilot.crm import titles
from ..autopilot.sources import same_street
from . import quick_screen as qs

log = logging.getLogger(__name__)

WORKFLOW = "returned_mail"
TZ = "America/New_York"
CSV_NAME = "returned_mail.csv"
# Written as code points: the house rule keeps the dash characters themselves out of the source.
EM_DASH, EN_DASH, RIGHT_QUOTE = chr(0x2014), chr(0x2013), chr(0x2019)

# Stand-ins the selftest sets, so no test renders a real PDF or reaches Claude, Slack or DataSift.
VISION = None   # (image bytes, media type, label) -> (fields dict, input tokens, output tokens)
SPLIT = None    # (pdf path) -> pages: len(pages), pages.render(i) -> (image bytes, media type), pages.close()
FILES = None    # (task, workspace) -> rows shaped like attachments.fetch_for_turn
CRM = None      # the budgeted DataSift client (agents.autopilot.crm.CRM) on the Open API key
NOW = None      # a fixed "now"

# Input and output price per million tokens (claude-api skill, 2026-11-15). An unknown model is priced
# as Opus, so the books run high rather than low.
PRICES = {"claude-haiku-5-5": (0.10, 0.50), "claude-sonnet-5-5": (2.0, 10.0), "claude-opus-5-5": (4.0, 20.0)}
DEFAULT_PRICE = (4.0, 20.0)

SIDES = ("address", "back", "other", "unreadable")
REASONS = {"not_deliverable": "not deliverable as addressed", "vacant": "vacant",
           "unable_to_forward": "unable to forward", "deceased": "deceased", "refused": "refused",
           "forwarding_address": "moved, with a forwarding address", "other": "another reason",
           "none": "no reason printed"}
GO_WORDS = {"yes", "y", "go", "go ahead", "true", "1", "ok", "okay", "read it", "read them", "read all"}
SEARCH_LIMIT = 100              # a records search that comes back this full cannot prove it found the only one
FAILS_IN_A_ROW = 3              # the reader failing this many pages running stops the read (the service is down)
MAX_IMAGE_BYTES = 4_500_000     # the API refuses an image over 5 MB; a larger photo is scaled down first
LONG_SIDE = 1568                # the long edge the model reads at full detail
MEDIA = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".gif": "image/gif", ".webp": "image/webp"}
DIRECTIONS = {"N": "NORTH", "S": "SOUTH", "E": "EAST", "W": "WEST", "NE": "NORTHEAST", "NW": "NORTHWEST",
              "SE": "SOUTHEAST", "SW": "SOUTHWEST"}
NAME_STOP = {"THE", "ESTATE", "OF", "AND", "OR", "MR", "MRS", "MS", "MISS", "DR", "JR", "SR", "II", "III", "IV",
             "CURRENT", "RESIDENT", "OCCUPANT", "HEIRS", "HEIR", "CO", "ATTN", "TO", "FAMILY", "TRUST", "TRUSTEE",
             "TRUSTEES", "LIVING", "REVOCABLE", "DECEASED", "LATE", "LLC", "INC", "CORP", "COMPANY", "LP", "LTD",
             "PROPERTIES", "HOLDINGS", "INVESTMENTS", "GROUP", "HOMES", "REALTY", "PARTNERS"}
PARTICLES = {"VAN", "VON", "DE", "DA", "DI", "DEL", "DELA", "LA", "LE", "ST", "MC", "MAC"}

# What happened to an envelope, in the words the answer and the CSV use.
WORDS = {"flagged": "flagged", "would_flag": "would flag", "already": "already flagged from this scan",
         "no_tag": "not written, the tag is missing", "write_failed": "written but not read back",
         "no_match": "no record at this address", "two_matches": "two or more records at this address",
         "different_name": "a different name", "no_name": "no name to check", "unreadable": "unreadable",
         "not_read": "not read", "not_looked_up": "not looked up"}
FLAG = ("flagged", "would_flag", "already")
PERSON = ("no_match", "two_matches", "different_name", "no_name", "unreadable")
MATCHED = ("flagged", "would_flag", "already", "no_tag", "write_failed", "different_name", "no_name")

SCHEMA = {"type": "object", "additionalProperties": False,
          "required": ["side", "recipient", "street", "city", "state", "zip", "reason", "reason_text",
                       "forwarding", "readable"],
          "properties": {"side": {"type": "string", "enum": list(SIDES)},
                         "recipient": {"type": "string"}, "street": {"type": "string"}, "city": {"type": "string"},
                         "state": {"type": "string"}, "zip": {"type": "string"},
                         "reason": {"type": "string", "enum": list(REASONS)},
                         "reason_text": {"type": "string"}, "forwarding": {"type": "string"},
                         "readable": {"type": "boolean"}}}

SYSTEM = (
    "You read one scanned page from a stack of returned mail: envelopes the post office sent back to the "
    "sender. Answer with the JSON object only.\n"
    "side: \"address\" when the page shows the delivery address (where the letter was mailed, usually the "
    "middle of the front); \"back\" for the back of an envelope or any page with no delivery address; "
    "\"other\" for a page that is not an envelope (a cover sheet, a letter, a blank page); \"unreadable\" when "
    "you cannot tell.\n"
    "The delivery address is never the sender's return address (small, top left) and never the new address "
    "on a yellow forwarding label.\n"
    "recipient: the name the letter was addressed to, as written. street: the delivery street line (house "
    "number, street, unit). city, state (two letters), zip (five digits).\n"
    "reason: the post office's reason when one is printed, stamped or on a label: not_deliverable (not "
    "deliverable as addressed, no such number or street, insufficient address, attempted not known), vacant, "
    "unable_to_forward, deceased, refused, forwarding_address (a label with a new address, or forward time "
    "expired), other, or none. reason_text: those words as printed, short. forwarding: the new address on a "
    "forwarding label, as printed; empty when there is none.\n"
    "readable: false when the delivery address is covered, cut off or too blurry to read with confidence.\n"
    "Copy what is on the page. Never guess a letter or a digit: leave the field empty instead. Leave out any "
    "phone number.")


class NoPdfReader(RuntimeError):
    """The box has no PDF library. Said plainly, because photos of the envelopes still work."""


class ReadCap(RuntimeError):
    """This ask has used the DataSift reads it is allowed."""


# ---------------------------------------------------------------------- settings and small helpers
def settings() -> dict:
    """Read on use, so an operator (or the selftest) can change one without a restart."""
    return {"model": str(qs.setting("AGENTS_RETURNED_MAIL_MODEL", "claude-haiku-5-5")),
            "ask_pages": max(1, int(qs.setting("AGENTS_RETURNED_MAIL_ASK_PAGES", 40))),
            "max_pages": max(1, int(qs.setting("AGENTS_RETURNED_MAIL_MAX_PAGES", 200))),
            "run_usd": float(qs.setting("AGENTS_RETURNED_MAIL_RUN_USD", 1.0)),
            "page_usd": float(qs.setting("AGENTS_RETURNED_MAIL_PAGE_USD", 0.001)),
            "max_reads": max(1, int(qs.setting("AGENTS_RETURNED_MAIL_MAX_READS", 300))),
            "dry_read": bool(qs.setting("AGENTS_RETURNED_MAIL_DRY_READ", True)),
            "tag": str(qs.setting("AGENTS_RETURNED_MAIL_TAG", "Return Mail")),
            "deceased_tag": str(qs.setting("AGENTS_RETURNED_MAIL_DECEASED_TAG", "Verify Deceased"))}


def _now() -> datetime:
    return NOW or datetime.now(timezone.utc)


def crm():
    """The budgeted DataSift client on the Open API key only (quick_screen.crm builds it from the key
    explicitly): the brain never logs in with a password."""
    return CRM if CRM is not None else qs.crm()


_PHONE = re.compile(r"(?<![\d$])(?:\+?1[\s.-]?)?\(?(\d{3})\)?[\s.-]?(\d{3})[\s.-]?(\d{4})(?!\d)")


def clean_text(text, keep: int = 200) -> str:
    """A piece of text as it is kept: a phone number taken out (the answer, the CSV and the note carry
    none), a long or short dash made a plain hyphen, the spaces collapsed, the length capped."""
    t = _PHONE.sub("", str(text or ""))
    t = t.replace(EM_DASH, "-").replace(EN_DASH, "-")
    return " ".join(t.split())[:keep]


def _tokens(text) -> list[str]:
    return "".join(ch if ch.isalnum() else " " for ch in str(text or "").upper()).split()


def _plural(n: int, one: str, many: str = "") -> str:
    return "%d %s" % (n, one if n == 1 else (many or one + "s"))


def _inside(p: Path, root: Path) -> bool:
    try:
        p.resolve().relative_to(Path(root).resolve())
        return True
    except (ValueError, OSError):
        return False


def _close_all(sources: list) -> None:
    for _, pages, _ in sources:
        pages.close()


def cost(tokens_in: int, tokens_out: int, model: str = "") -> float:
    pin, pout = PRICES.get(model or settings()["model"], DEFAULT_PRICE)
    return round(tokens_in * pin / 1e6 + tokens_out * pout / 1e6, 6)


# ---------------------------------------------------------------------- one page, read
def parse_reply(text: str) -> dict:
    """The JSON object in the reader's reply. With the schema the whole reply is the object; without it
    a model may wrap it in a fence or a sentence, so the first { to the last } is tried as well."""
    t = (text or "").strip()
    tries = [t]
    if "{" in t and "}" in t:
        tries.append(t[t.find("{"): t.rfind("}") + 1])
    for cand in tries:
        try:
            v = json.loads(cand)
        except ValueError:
            continue
        if isinstance(v, dict):
            return v
    return {}


def conform(raw) -> dict:
    """The reader's answer in exactly the shape this module reads, whatever came back: a missing key is
    empty, an unknown side is unreadable, an unknown reason is "other" (or "none" when no words came
    with it), a ZIP is five digits or nothing, a state two letters or nothing, and a forwarding address
    with no reason is a forwarding address. An address side with no street cannot be read."""
    r = raw if isinstance(raw, dict) else {}
    side = str(r.get("side") or "").strip().lower()
    out = {"side": side if side in SIDES else "unreadable"}
    for k in ("recipient", "street", "city", "reason_text", "forwarding"):
        out[k] = clean_text(r.get(k))
    state = "".join(ch for ch in str(r.get("state") or "") if ch.isalpha()).upper()
    out["state"] = state if len(state) == 2 else ""
    z = "".join(ch for ch in str(r.get("zip") or "") if ch.isdigit())
    out["zip"] = z[:5] if len(z) in (5, 9) else ""
    reason = str(r.get("reason") or "").strip().lower()
    if reason not in REASONS:
        reason = "other" if out["reason_text"] else "none"
    if out["forwarding"] and reason in ("none", "other"):
        reason = "forwarding_address"
    out["reason"] = reason
    readable = r.get("readable")
    out["readable"] = readable is True or str(readable).strip().lower() == "true"
    if out["side"] == "address" and not out["street"]:
        out["readable"] = False
    return out


def ask_claude(image: bytes, media: str, label: str) -> tuple[dict, int, int]:
    """One page read by the model. Thinking is off and effort low: copying an address off a page needs
    no reasoning, and the first live voice rewrite spent its whole reply on thinking (voice.py). The
    answer is held to a strict JSON schema; a model or an SDK that refuses the schema, or thinking off,
    is asked again without it (a refusal is a 400, which is not billed). The page's file name is not
    sent: the reader needs the picture, and a file name is text anybody could have chosen."""
    import anthropic
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    model = settings()["model"]
    content = [{"type": "image", "source": {"type": "base64", "media_type": media,
                                            "data": base64.standard_b64encode(image).decode("ascii")}},
               {"type": "text", "text": "Read this page."}]
    tries = ({"max_tokens": 800, "thinking": {"type": "disabled"},
              "output_config": {"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}}},
             {"max_tokens": 800, "thinking": {"type": "disabled"}, "output_config": {"effort": "low"}},
             {"max_tokens": 4000})
    msg, last = None, None
    for extra in tries:
        try:
            msg = client.messages.create(model=model, system=SYSTEM,
                                         messages=[{"role": "user", "content": content}], **extra)
            break
        except anthropic.BadRequestError as exc:
            last = exc
    if msg is None:
        raise last
    u = getattr(msg, "usage", None)
    tin, tout = int(getattr(u, "input_tokens", 0) or 0), int(getattr(u, "output_tokens", 0) or 0)
    if getattr(msg, "stop_reason", "") in ("refusal", "max_tokens"):
        log.info("returned mail: the reader stopped early on %s", label)
        return {"side": "unreadable"}, tin, tout
    text = "".join(getattr(b, "text", "") for b in msg.content if getattr(b, "type", "") == "text")
    return parse_reply(text), tin, tout


# ---------------------------------------------------------------------- the files on the thread, as pages
def _pdfium():
    try:
        import pypdfium2
    except ImportError as exc:
        raise NoPdfReader("this box cannot split a PDF into pages yet (the pypdfium2 package is not installed); "
                          "post photos of the envelopes instead") from exc
    return pypdfium2


class PdfPages:
    """A PDF's pages, rendered one at a time when they are read, so a 200-page stack never sits in
    memory as 200 images. The long edge is LONG_SIDE pixels and never past 200 dpi."""

    def __init__(self, path: Path):
        self._doc = _pdfium().PdfDocument(str(path))

    def __len__(self) -> int:
        return len(self._doc)

    def render(self, i: int) -> tuple[bytes, str]:
        page = self._doc[i]
        try:
            w, h = page.get_size()
            scale = min(LONG_SIDE / max(float(w), float(h), 1.0), 200 / 72)
            img = page.render(scale=scale).to_pil().convert("RGB")
        finally:
            page.close()
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=85)
        return buf.getvalue(), "image/jpeg"

    def close(self) -> None:
        try:
            self._doc.close()
        except Exception:  # noqa: BLE001  (a failed close says nothing the reader needs)
            pass


class PhotoPages:
    """One photo of an envelope is one page. Slack's own preview is what was saved (a JPEG even for an
    iPhone HEIC); a photo too large for the API, or in a format it does not take, becomes a JPEG first."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def __len__(self) -> int:
        return 1

    def render(self, i: int) -> tuple[bytes, str]:
        data = self.path.read_bytes()
        media = MEDIA.get(self.path.suffix.lower())
        if media and len(data) <= MAX_IMAGE_BYTES:
            return data, media
        from PIL import Image
        img = Image.open(io.BytesIO(data)).convert("RGB")
        img.thumbnail((LONG_SIDE, LONG_SIDE))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=85)
        return buf.getvalue(), "image/jpeg"

    def close(self) -> None:
        return None


def thread_files(task: dict) -> tuple[list[dict], list[dict]]:
    """(the PDFs and photos on this thread, the files listed for a person with why). The employee's
    turn already saved them in its own workspace; asking again re-uses what is there and saves only
    what is missing (a teammate the thread was handed to has a workspace of its own)."""
    ws = qs.workspace_of(task)
    try:
        rows = (FILES or attachments.fetch_for_turn)(task, ws)
    except Exception as exc:  # noqa: BLE001  (Slack or the disk: said, never raised into the worker)
        return [], [{"name": "the files on this thread", "why": "they could not be saved (%s)" % type(exc).__name__}]
    usable, skipped = [], []
    for r in rows or []:
        if r.get("kind") not in ("pdf", "photo"):
            continue                       # a boundary or a text file is not a scan
        name = r.get("name") or "file"
        if not r.get("path"):
            skipped.append({"name": name, "why": r.get("why") or "it was not saved"})
            continue
        p = Path(ws) / r["path"]
        if not _inside(p, ws) or not p.is_file():
            skipped.append({"name": name, "why": "it is not in the workspace"})
            continue
        usable.append({"name": name, "kind": r["kind"], "path": p})
    return usable, skipped


def pick(files: list[dict], wanted, task: dict) -> tuple[list[dict], str]:
    """The files the ask named (names apart by commas, or a path in the workspace), or every file."""
    names = [w.strip() for w in str(wanted or "").split(",") if w.strip()]
    if not names:
        return files, ""
    out: list[dict] = []
    for n in names:
        if "/" in n or "\\" in n:
            path, why = qs.find_file(n, "scan", task)
            if not path:
                return [], why
            ext = path.suffix.lower()
            if ext != ".pdf" and ext not in MEDIA:
                return [], "%s is not a PDF or a photo" % path.name
            hit = [{"name": path.name, "kind": "pdf" if ext == ".pdf" else "photo", "path": path}]
        else:
            low = n.lower()
            hit = [f for f in files if low in (f["name"].lower(), f["path"].name.lower(),
                                               f["path"].name.lower().split("-", 1)[-1])]
        if not hit:
            return [], "no file named %s is on this thread" % n
        out += [f for f in hit if f not in out]
    return out, ""


def scan_date(task: dict, given) -> tuple[str, str, str]:
    """(the scan day, how the note names it, why the ask is refused). Given, it is the day the stack was
    scanned; otherwise the day it was posted, and the note says posted, because that is all we know."""
    g = str(given or "").strip()
    today = _now().astimezone(ZoneInfo(TZ)).date()
    if g:
        d = None
        for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
            try:
                d = datetime.strptime(g, fmt).date()
                break
            except ValueError:
                continue
        if d is None:
            return "", "", "The scan date has to be written like 2026-11-14 or 11/14/2026; nothing was read."
        if d > today:
            return "", "", "The scan date %s is in the future; nothing was read." % d.isoformat()
        return d.isoformat(), "scanned %s" % d.isoformat(), ""
    try:
        d = datetime.fromisoformat(str(task.get("created") or "")).astimezone(ZoneInfo(TZ)).date()
    except (TypeError, ValueError):
        d = today
    return d.isoformat(), "from the scan posted %s" % d.isoformat(), ""


# ---------------------------------------------------------------------- pages into envelopes
def _envelope(row: dict) -> dict:
    f = row.get("fields") or {}
    env = {k: f.get(k, "") for k in ("recipient", "street", "city", "state", "zip", "reason", "reason_text",
                                     "forwarding")}
    env.update(pages=[row["n"]], file=row["file"], readable=bool(f.get("readable")), result="", why="",
               record="", record_street="", owner="", status="")
    if row.get("error"):
        env.update(result="not_read", why=row["error"])
    elif f.get("side") != "address" or not env["readable"]:
        env.update(result="unreadable", why="the delivery address could not be read with confidence")
    return env


def _lend_back(env: dict, back: dict) -> None:
    """A back page joins its envelope; its reason and forwarding address count only where the front
    shows none (the yellow label is on the front more often than not)."""
    f = back.get("fields") or {}
    env["pages"] = sorted(env["pages"] + [back["n"]])
    if env.get("reason") in ("", "none") and f.get("reason") not in ("", "none", None):
        env["reason"], env["reason_text"] = f["reason"], f.get("reason_text", "")
    if not env.get("forwarding") and f.get("forwarding"):
        env["forwarding"] = f["forwarding"]
        if env.get("reason") in ("", "none", "other"):
            env["reason"] = "forwarding_address"


def envelopes(rows: list[dict]) -> tuple[list[dict], int]:
    """(the envelopes and the rows for a person, the pages that were not envelopes), in posting order.

    The stack's scan order is read once: when more back pages sit right after an address side than
    right before one, it was scanned front first, and every back joins the address side just before it;
    otherwise the one just after it. A back with no front on that side (the front misread, or missing
    from the scan) stands as its own row for a person and lends nothing to anyone. A page that was not
    read, or could not be read, is its own row too; a page that is not an envelope is only counted."""
    sides = ["error" if r.get("error") else (r.get("fields") or {}).get("side") for r in rows]
    before = sum(1 for i, s in enumerate(sides) if s == "back" and i > 0 and sides[i - 1] == "address")
    after = sum(1 for i, s in enumerate(sides) if s == "back" and i + 1 < len(sides) and sides[i + 1] == "address")
    back_first = after > before
    fronts = {i: _envelope(r) for i, (r, s) in enumerate(zip(rows, sides)) if s == "address"}
    used: set = set()
    out, other = [], 0
    for i, (row, side) in enumerate(zip(rows, sides)):
        if side == "address":
            out.append(fronts[i])
        elif side == "back":
            j = i + 1 if back_first else i - 1
            if j in fronts and j not in used:
                _lend_back(fronts[j], row)
                used.add(j)
            else:
                env = _envelope(row)
                env.update(result="unreadable", why="a back page with no front beside it")
                out.append(env)
        elif side == "other":
            other += 1
        else:
            out.append(_envelope(row))
    return out, other


# ---------------------------------------------------------------------- an envelope to its record
def is_po_box(street: str) -> bool:
    t = _tokens(street)
    return t[:2] == ["PO", "BOX"] or t[:3] == ["P", "O", "BOX"] or t[:1] == ["POBOX"] \
        or t[:3] == ["POST", "OFFICE", "BOX"]


def queries(street: str) -> list[str]:
    """The search prefixes for a street: its house number and first street word, written the way
    DataSift stores a street ("12 Testwood"), and for a direction its other spelling as well ("40 North"
    against a stored "40 N"). Empty when there is no house number to anchor on."""
    t = _tokens(street)
    if len(t) < 2 or not t[0].isdigit():
        return []

    def word(w: str) -> str:
        return w.title() if len(w) > 2 else w

    out = ["%s %s" % (t[0], word(t[1]))]
    alt = DIRECTIONS.get(t[1]) or next((k for k, v in DIRECTIONS.items() if v == t[1]), "")
    if alt:
        out.append("%s %s" % (t[0], word(alt)))
    return out


def same_place(env: dict, address: dict) -> bool:
    """The ZIP decides when both sides carry one; otherwise the city must agree. Neither is no match."""
    z = str(address.get("zip5") or address.get("postal_code") or "")[:5]
    if env.get("zip") and z:
        return env["zip"] == z
    city = " ".join(_tokens(address.get("city")))
    if env.get("city") and city:
        return " ".join(_tokens(env["city"])) == city
    return False


class Reads:
    """The DataSift reads one ask may make (searches, records, read-backs)."""

    def __init__(self, cap: int):
        self.cap, self.used = int(cap), 0

    def take(self, n: int = 1) -> None:
        if self.used + n > self.cap:
            raise ReadCap("the read limit for one ask (%d) is used up" % self.cap)
        self.used += n


def candidates(crm_, env: dict, reads: Reads) -> tuple[list[dict], str, str]:
    """(the one record's search row, or nothing; the result when there is not exactly one; why)."""
    street = env.get("street") or ""
    if is_po_box(street):
        return [], "no_match", "a PO box is not a property address: find the record by the owner's name"
    qlist = queries(street)
    if not qlist:
        return [], "unreadable", "the address has no house number to match on"
    if not (env.get("zip") or env.get("city")):
        return [], "unreadable", "the address has no ZIP and no city to match on"
    seen, rows = set(), []
    for q in qlist:
        reads.take(1)
        total, got = crm_.search({"property_type": "clean", "search": "address_prefix:%s" % q}, limit=SEARCH_LIMIT)
        got = list(got or [])
        if len(got) >= SEARCH_LIMIT or int(total or 0) > len(got):
            return [], "two_matches", "more records start with %s than one search can show" % q
        for row in got:
            u = row.get("uuid") or ""
            a = row.get("address") or {}
            if u and u not in seen and same_street(street, a.get("street") or "") and same_place(env, a):
                seen.add(u)
                rows.append(row)
    if not rows:
        return [], "no_match", "no record in this account at this street and %s" % ("ZIP" if env.get("zip") else "city")
    if len(rows) > 1:
        return [], "two_matches", "%d records share this address" % len(rows)
    return rows, "", ""


def name_keys(text) -> set[str]:
    """The words a surname can be matched on: each word of two letters or more and each pair of
    neighbors joined ("Mc Donald" is McDonald, "Van Buren" is VanBuren), with an apostrophe dropped
    (O'Brien is OBrien), minus titles, suffixes and the estate and company words."""
    raw = str(text or "").replace("'", "").replace(RIGHT_QUOTE, "")
    toks = [t for t in _tokens(raw) if not t.isdigit()]
    keys = {t for t in toks if len(t) >= 2 and t not in NAME_STOP and t not in PARTICLES}
    keys |= {a + b for a, b in zip(toks, toks[1:]) if a not in NAME_STOP and b not in NAME_STOP}
    return keys


def surname_fits(recipient: str, rec: dict) -> tuple[str, str]:
    """("yes" | "no" | "unknown", the owner as shown). The owner's last name decides; a company owner's
    own words stand in only when no person is named. Unknown when either side names nobody."""
    ow = rec.get("owner") or {}
    shown = " ".join(x for x in (ow.get("first_name"), ow.get("last_name")) if x) or str(ow.get("company") or "")
    have = name_keys(ow.get("last_name"))
    if not have and ow.get("company"):
        have = name_keys(ow.get("company"))
    want = name_keys(recipient)
    if not want or not have:
        return "unknown", shown
    return ("yes" if want & have else "no"), shown


def match(crm_, env: dict, reads: Reads, cache: dict) -> None:
    """Decide one envelope: its result, why, and the record it belongs to. Reads only; a record two
    envelopes share is read once."""
    rows, kind, why = candidates(crm_, env, reads)
    if kind:
        env.update(result=kind, why=why)
        return
    u = rows[0]["uuid"]
    if u not in cache:
        reads.take(1)
        cache[u] = crm_.record(u) or {}
    rec = cache[u]
    if not rec.get("uuid"):
        env.update(result="not_looked_up", why="the record could not be read")
        return
    a = rec.get("address") or {}
    env.update(record=rec["uuid"], record_street=clean_text(a.get("street")), status=str(rec.get("status") or ""),
               rec=rec)
    fits, shown = surname_fits(env.get("recipient") or "", rec)
    env["owner"] = clean_text(shown)
    if fits == "no":
        env.update(result="different_name", why="addressed to %s, and the owner of record is %s" % (
            env.get("recipient") or "someone", env["owner"] or "unnamed"))
    elif fits == "unknown":
        env.update(result="no_name", why="no name on the envelope" if not name_keys(env.get("recipient"))
                   else "the owner of record has no name to check")


# ---------------------------------------------------------------------- the writes
def _address_line(env: dict) -> str:
    tail = " ".join(x for x in (env.get("state"), env.get("zip")) if x)
    place = " ".join(x for x in (env.get("city"), tail) if x)
    return ", ".join(x for x in (env.get("street"), place) if x)


def _pages_label(envs: list[dict]) -> str:
    out = []
    for e in envs:
        ps = e["pages"]
        out.append("%s %s of %s" % ("pages" if len(ps) > 1 else "page", " and ".join(str(p) for p in ps), e["file"]))
    return "; ".join(out)


def _said(envs: list[dict]) -> str:
    out = []
    for e in envs:
        r = e.get("reason") or "none"
        if r == "none":
            continue
        phrase = ("the label reads %s" % e["reason_text"]) if r == "other" and e.get("reason_text") \
            else "the post office marked it %s" % REASONS[r]
        if phrase not in out:
            out.append(phrase)
    return "; ".join(out) or "no reason was printed"


def note_text(envs: list[dict], scan_label: str, tag: str, dead_tag: str) -> str:
    """The note on the record: what came back, when, why, and what was and was not changed."""
    first = envs[0]
    parts = ["Returned mail, %s: %s." % (scan_label, _said(envs)),
             "It was addressed to %s at %s (%s)." % (first.get("recipient") or "no name", _address_line(first),
                                                     _pages_label(envs)),
             "Tagged %s so this address is not mailed again." % tag]
    fwd = next((e["forwarding"] for e in envs if e.get("forwarding")), "")
    if fwd:
        parts.append("The forwarding label reads: %s. A person checks it before the mailing address is changed; "
                     "nothing on the owner was changed." % fwd)
    if any(e.get("reason") == "deceased" for e in envs):
        parts.append("The post office marked the addressee deceased: tagged %s for research. The owner is not "
                     "marked dead." % dead_tag)
    return clean_text(" ".join(parts), keep=1500)


def write_record(crm_, uuid_: str, rec: dict, envs: list[dict], s: dict, scan_day: str, scan_label: str,
                 run_id: str, reads: Reads) -> tuple[str, str]:
    """(result, why) for one record on a live box. The change is remembered BEFORE the write (a write
    that then fails leaves a change that undo simply re-applies), the tags go on, the note goes on, and
    the record is read back. Nothing else on the record or its owner is touched."""
    want = [s["tag"]] + ([s["deceased_tag"]] if any(e.get("reason") == "deceased" for e in envs) else [])
    have = {t.lower() for t in titles(rec.get("tags"))}
    add = [t for t in want if t.lower() not in have]
    if add:
        ledger.change(run_id, WORKFLOW, uuid_, "add_tags", None, add)
        crm_.add_tags(uuid_, add, rec)
    note = note_text(envs, scan_label, s["tag"], s["deceased_tag"])
    crm_.message(uuid_, note)
    reads.take(1)
    back = crm_.record(uuid_) or {}
    got = {t.lower() for t in titles(back.get("tags"))}
    missing = [t for t in want if t.lower() not in got]
    if missing or note[:60] not in str(back.get("notes") or ""):
        return "write_failed", "the %s did not read back on the record" % (
            ("tag %s" % missing[0]) if missing else "note")
    prior = ledger.fact(uuid_, "returned_mail") or {}
    ledger.set_fact(uuid_, "returned_mail", {"dates": sorted(set(list(prior.get("dates") or []) + [scan_day]))})
    ledger.event(run_id, WORKFLOW, uuid_, "return_mail",
                 {"scan": scan_day, "reasons": sorted({e.get("reason") or "none" for e in envs}), "tags": add})
    return "flagged", ""


def already(uuid_: str, scan_day: str) -> bool:
    """A record this workflow already flagged from the same scan: the same stack posted twice must not
    put a second note on every record."""
    return scan_day in ((ledger.fact(uuid_, "returned_mail") or {}).get("dates") or [])


def flag_records(crm_, envs: list[dict], s: dict, scan_day: str, scan_label: str, live: bool, reads: Reads,
                 run_id: str, problems: list) -> int:
    """Flag each matched record once (two envelopes to one house are one flag and one note). Live, the
    tags and the note are written and read back; dry, each is counted as what a live run would do.
    Returns how many records are (or would be) flagged."""
    by_record: dict = {}
    for e in envs:
        if not e["result"] and e.get("record"):
            by_record.setdefault(e["record"], []).append(e)
    if not by_record:
        return 0
    needed = [s["tag"]]
    if any(e.get("reason") == "deceased" for group in by_record.values() for e in group):
        needed.append(s["deceased_tag"])
    try:
        missing = [t for t in needed if not crm_.tag_uuid(t)]
    except Exception as exc:  # noqa: BLE001
        missing = list(needed)
        problems.append("The account's tags could not be read (%s), so nothing was written." % type(exc).__name__)
    if missing:
        one = len(missing) == 1
        problems.append("The tag%s %s %s not exist in this account yet: create %s in DataSift, then ask again. "
                        "Until then nothing is written." % ("" if one else "s", " and ".join(missing),
                                                            "does" if one else "do", "it" if one else "them"))
    flagged, stopped = 0, ""
    for u, group in by_record.items():
        rec = group[0].get("rec") or {}
        if already(u, scan_day):
            result, why = "already", "flagged from this scan before"
        elif stopped:
            result, why = "not_looked_up", stopped
        elif not live:
            result, why = "would_flag", ""
        elif missing:
            result, why = "no_tag", "the tag %s does not exist in this account yet" % missing[0]
        else:
            try:
                result, why = write_record(crm_, u, rec, group, s, scan_day, scan_label, run_id, reads)
            except (ReadCap, budget.BudgetExhausted) as exc:
                stopped = "not written: %s" % exc
                result, why = "not_looked_up", stopped
                problems.append("Stopped writing: %s." % exc)
            except Exception as exc:  # noqa: BLE001  (one record failing must not stop the rest)
                result, why = "write_failed", "the write failed (%s)" % type(exc).__name__
        if result in FLAG:
            flagged += 1
        for e in group:
            e.update(result=result, why=why)
    return flagged


# ---------------------------------------------------------------------- the CSV
COLUMNS = ["Row", "File", "Pages", "Result", "Why", "Addressed to", "Street", "City", "State", "ZIP",
           "Post office reason", "Forwarding address", "Record", "Record street", "Owner of record", "Status"]


def write_csv(path: Path, envs: list[dict]) -> None:
    """Every envelope and what happened to it, for the person who works the rest. No phone number:
    every text cell passes clean_text."""
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(COLUMNS)
        for i, e in enumerate(envs, 1):
            r = e.get("reason") or "none"
            reason = REASONS.get(r, "") + (" (%s)" % e["reason_text"] if e.get("reason_text") else "")
            cells = [str(i), e.get("file"), " ".join(str(p) for p in e.get("pages") or []),
                     WORDS.get(e.get("result"), ""), e.get("why"), e.get("recipient"), e.get("street"), e.get("city"),
                     e.get("state"), e.get("zip"), reason, e.get("forwarding"), e.get("record"),
                     e.get("record_street"), e.get("owner"), e.get("status")]
            w.writerow([clean_text(c, keep=400) for c in cells])


# ---------------------------------------------------------------------- the run
def run(task: dict) -> dict:
    s = settings()
    inputs, _details = qs.inputs_of(task)
    folder = qs.files_dir(task, WORKFLOW)
    live = config.live()
    out = {"ok": False, "live": live, "run_dir": str(folder.parent), "files": [], "problems": [], "usd": 0.0}
    scan_day, scan_label, why = scan_date(task, inputs.get("scan_date"))
    if why:
        out["say"] = why
        return out
    out["scan_day"] = scan_day
    files, skipped = thread_files(task)
    files, why = pick(files, inputs.get("files"), task)
    if why:
        out["say"] = "%s; nothing was read." % qs.cap1(why)
        return out
    sources, n_pages = [], 0
    for f in files:
        try:
            pages = (SPLIT or PdfPages)(f["path"]) if f["kind"] == "pdf" else PhotoPages(f["path"])
            n = len(pages)
        except NoPdfReader as exc:
            skipped.append({"name": f["name"], "why": str(exc)})
            continue
        except Exception as exc:  # noqa: BLE001  (a damaged or locked PDF is one file for a person, not a crash)
            skipped.append({"name": f["name"], "why": "it could not be opened (%s)" % type(exc).__name__})
            continue
        sources.append((f, pages, n))
        n_pages += n
    out["skipped"] = [{"name": clean_text(x["name"]), "why": clean_text(x["why"], 300)} for x in skipped]
    if not sources:
        out["say"] = nothing_to_read(out["skipped"])
        return out
    take = min(n_pages, s["max_pages"])
    out.update(pages=n_pages, take=take, estimate_usd=round(take * s["page_usd"], 2),
               over_limit=max(0, n_pages - s["max_pages"]), ask_pages=s["ask_pages"], max_pages=s["max_pages"],
               run_cap_usd=s["run_usd"], page_usd=s["page_usd"])
    go = str(inputs.get("go") or "").strip().lower() in GO_WORDS
    if n_pages > s["ask_pages"] and not go:
        _close_all(sources)
        out.update(ok=True, held="needs_go")
        return out
    if not live and not s["dry_read"]:
        _close_all(sources)
        out.update(ok=True, held="dry", held_why="reading on a dry box is switched off (AGENTS_RETURNED_MAIL_DRY_READ)")
        return out
    if not live and not qs.from_slack(task):
        _close_all(sources)
        out.update(ok=True, held="dry", held_why="on a dry box only a person's ask in Slack starts a read")
        return out
    if VISION is None and not config.ANTHROPIC_API_KEY:
        _close_all(sources)
        out["say"] = "No Claude key is set on this box, so the scans cannot be read."
        return out
    try:
        crm_ = crm()
    except qs.MissingKey as exc:
        _close_all(sources)
        out["say"] = "%s; nothing was read." % qs.cap1(str(exc))
        return out

    rows, stop = read_pages(task, s, sources, take)
    out["usd"] = round(store.spent(task_id=int(task.get("id") or 0)), 4)
    out["read"] = sum(1 for r in rows if not r.get("error"))
    if stop:
        out["problems"].append(stop)
    envs, other = envelopes(rows)
    out["other_pages"] = other
    reads, cache = Reads(s["max_reads"]), {}
    for i, env in enumerate(envs):
        if env["result"]:
            continue
        try:
            match(crm_, env, reads, cache)
        except (ReadCap, budget.BudgetExhausted) as exc:
            for e in envs[i:]:
                if not e["result"]:
                    e.update(result="not_looked_up", why=str(exc))
            out["problems"].append("Stopped looking up records: %s." % exc)
            break
        except Exception as exc:  # noqa: BLE001  (one lookup failing must not stop the rest)
            env.update(result="not_looked_up", why="the lookup failed (%s)" % type(exc).__name__)
    run_id = "%s-%d-%s" % (WORKFLOW, int(task.get("id") or 0), uuid_mod.uuid4().hex[:8])
    records = flag_records(crm_, envs, s, scan_day, scan_label, live, reads, run_id, out["problems"])
    for e in envs:
        e.pop("rec", None)
    write_csv(folder / CSV_NAME, envs)
    counts = {k: sum(1 for e in envs if e.get("result") == k) for k in WORDS}
    out.update(ok=True, files=[CSV_NAME], counts=counts, records=records, reads=reads.used,
               envelopes=sum(1 for e in envs if e.get("result") != "not_read"),
               matched=sum(counts[k] for k in MATCHED),
               deceased=sum(1 for e in envs if e.get("result") in FLAG and e.get("reason") == "deceased"),
               forwarding=sum(1 for e in envs if e.get("result") in FLAG and e.get("forwarding")),
               person=[clean_text("%s: %s" % (e.get("street") or "page %s" % e["pages"][0], WORDS[e["result"]]), 160)
                       for e in envs if e.get("result") in PERSON][:6])
    # Any tag this run added can be taken off, including one on a record whose note then failed to read back.
    if live and ledger.changes(run_id=run_id):
        out["run_id"] = run_id
    return out


def nothing_to_read(skipped: list[dict]) -> str:
    """Why no page could be read, and what to post instead."""
    say = ("No scanned PDF or photo on this thread could be read. Post the scan here (one PDF of the whole "
           "stack, front and back, or photos of the envelopes) and ask again.")
    if skipped:
        say += " " + " ".join("%s: %s." % (x["name"], x["why"]) for x in skipped[:2])
        if any("larger than" in x["why"] for x in skipped):
            say += " A big stack scans smaller in black and white, or post it in parts."
    return say


def read_pages(task: dict, s: dict, sources: list, take: int) -> tuple[list[dict], str]:
    """Every page in posting order, read inside the caps: ([{n, file, page, fields, error}], why the read
    stopped early or ""). A page past a cap or the page limit is kept as not read, with the reason."""
    money = qs.Budget(task.get("id") or 0, s["run_usd"])
    rows, n, fails, stop = [], 0, 0, ""
    for f, pages, count in sources:
        try:
            for i in range(count):
                n += 1
                label = ("page %d of %s" % (i + 1, f["name"])) if count > 1 else f["name"]
                row = {"n": n, "file": f["name"], "page": i + 1, "fields": None, "error": ""}
                rows.append(row)
                if stop:
                    row["error"] = stop
                    continue
                if n > take:
                    row["error"] = "past the %d-page limit for one ask" % s["max_pages"]
                    continue
                if not money.allows(s["page_usd"], "the read of %s" % label):
                    stop = row["error"] = "the spending cap for one ask or for the day was reached"
                    continue
                try:
                    image, media = pages.render(i)
                    fields, tin, tout = (VISION or ask_claude)(image, media, label)
                    money.charge(cost(tin, tout, s["model"]), "returned mail read: %s" % label)
                    row["fields"] = conform(fields)
                    fails = 0
                except Exception as exc:  # noqa: BLE001  (a page the reader fails on is a page for a person)
                    fails += 1
                    row["error"] = "the page could not be read (%s)" % type(exc).__name__
                    if fails >= FAILS_IN_A_ROW:
                        stop = "the reader failed %d pages in a row, so the rest were not read" % FAILS_IN_A_ROW
        finally:
            pages.close()
    return rows, (qs.cap1(stop) + ".") if stop else ""


# ---------------------------------------------------------------------- Slack
def _no_dash(text: str) -> str:
    return (text or "").replace(EM_DASH, "-").replace(EN_DASH, "-")


def render(task: dict, r: dict) -> str:
    """The thread answer. Every name, street and reason in it was cleaned where it was read, so the
    lines are joined as they are: running the signed file link through the phone scrub could cut a run
    of ten digits out of its signature."""
    if not r.get("ok"):
        return _no_dash(r.get("say") or "Returned mail: nothing was read.")
    if r.get("held") == "needs_go":
        lines = ["*Returned mail: %s on this thread*" % _plural(r["pages"], "page"),
                 "That is more than the %d pages I read without asking. Reading %d costs about $%.2f (about $%.3f a "
                 "page)." % (r["ask_pages"], r["take"], r["estimate_usd"], r["page_usd"])]
        if r.get("estimate_usd", 0) > r.get("run_cap_usd", 0):
            lines.append("That is past the $%.2f cap for one ask, so the read stops at the cap." % r["run_cap_usd"])
        if r.get("over_limit"):
            lines.append("The last %d are past the %d-page limit for one ask; they wait for a second ask." % (
                r["over_limit"], r["max_pages"]))
        lines.append("Say go and I will read them.")
        return _no_dash("\n".join(lines))
    if r.get("held") == "dry":
        return _no_dash("*Returned mail: %s on this thread*\nNothing was read: %s. Reading them would cost about $%.2f." % (
            _plural(r["pages"], "page"), r.get("held_why") or "this box is dry", r["estimate_usd"]))
    c = r.get("counts") or {}
    live = r.get("live")
    tag = settings()["tag"]
    lines = ["*Returned mail: %s from %s*  (%s)" % (_plural(r.get("envelopes", 0), "envelope"),
                                                    _plural(r.get("read", 0), "page"),
                                                    "live" if live else "dry run, nothing written")]
    if live:
        flag = "Flagged %s on %s: tagged %s, with a note." % (_plural(c.get("flagged", 0), "envelope"),
                                                             _plural(r.get("records", 0), "record"), tag)
    else:
        flag = "Would flag %s on %s: tag %s and a note." % (_plural(c.get("would_flag", 0), "envelope"),
                                                           _plural(r.get("records", 0), "record"), tag)
    lines.append("Matched %d to a record. %s" % (r.get("matched", 0), flag))
    extra = []
    if r.get("deceased"):
        one = r["deceased"] == 1
        extra.append("%s marked deceased by the post office also %s %s (nobody is marked dead)" % (
            _plural(r["deceased"], "envelope"), "gets" if one else "get", settings()["deceased_tag"]))
    if r.get("forwarding"):
        one = r["forwarding"] == 1
        extra.append("%s %s in the %s for a person (no mailing address is changed)" % (
            _plural(r["forwarding"], "forwarding address", "forwarding addresses"), "goes" if one else "go",
            "note" if one else "notes"))
    if c.get("already"):
        extra.append("%d already flagged from this scan" % c["already"])
    if extra:
        lines.append(qs.cap1("; ".join(extra)) + ".")
    person = sum(c.get(k, 0) for k in PERSON)
    if person:
        lines.append("Needs a person: %d (%s). They are in the file." % (
            person, ", ".join("%s %d" % (WORDS[k], c[k]) for k in PERSON if c.get(k))))
    rest = [k for k in ("not_read", "not_looked_up", "write_failed", "no_tag") if c.get(k)]
    if rest:
        lines.append("Also: %s." % ", ".join("%s %d" % (WORDS[k], c[k]) for k in rest))
    lines += ["  %s" % p for p in (r.get("person") or [])[:3]]
    if r.get("other_pages"):
        lines.append("%s that %s not an envelope (a cover sheet or a letter) %s skipped." % (
            _plural(r["other_pages"], "page"), "was" if r["other_pages"] == 1 else "were",
            "was" if r["other_pages"] == 1 else "were"))
    lines += ["Not read: %s, %s." % (x["name"], x["why"]) for x in (r.get("skipped") or [])[:3]]
    usd = float(r.get("usd") or 0)
    lines.append("Reading cost %s." % ("under a cent" if 0 < usd < 0.01 else "$%.2f" % usd))
    if r.get("run_id"):
        lines.append("To take the tags off again: autopilot undo %s (a note has no delete route, so the notes stay)."
                     % r["run_id"])
    lines += ["Note: %s" % p for p in (r.get("problems") or [])[:3]]
    lines.append(qs.file_line(task, "File", CSV_NAME))
    return _no_dash("\n".join(lines))
