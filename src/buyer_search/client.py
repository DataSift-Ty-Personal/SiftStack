"""SiftStack's side of the buyer search: run it, and mark the buyers SiftStack never sells to.

  search_open(...)    SiftStack's own version (open_api.py): SiftMap reads on the caller's Open API key. What a
                      user, the Slack workflow and the CLI run.
  search_cluster(...) the data team's model (engine.py) through the workstation's read-only `opensearch` MCP.
                      Staff only: the property cluster is not something a user can reach.
  annotate(...)       marks each buyer that is not a dispo buyer, with the reason: the install's own company
                      (AGENTS_OWN_BUYER_NAMES), production builders, iBuyers and SFR funds
                      (deal_buyer_match.NAMED_EXCLUSIONS, the dispo blast's list), land banks and nonprofits
                      (NOT_BUYERS), churches, and lenders, servicers and government bodies taking title at
                      foreclosure. Marked, never dropped.

The cluster run asks for investor sales only (CLUSTER_PARAMS). The model's own default also counts company
purchases that are not investor sales, which in Knox are lenders' foreclosure take-backs, the VA, HUD, the
city and the county (50 of the 1,038 company purchases, read 2026-11-23).
"""
from __future__ import annotations

import os
import re
from pathlib import Path

CLUSTER_PARAMS = {"is_investor_sale": ["true"]}

# Whole words only: "BANK" must not match WILLBANKS (the enterprise classifier's old trap).
LENDER_PHRASES = ("MORTGAGE", "LENDING", "LOAN SERVICING", "LOAN SERVICES", "SERVICING", "CREDIT UNION",
                  "SAVINGS FUND", "SAVINGS BANK", "NATIONAL ASSOCIATION", "FEDERAL NATIONAL", "FEDERAL HOME LOAN",
                  "HOME LOAN", "BANK", "BANCORP", "FSB", "N A", "NEWREZ", "CIM TRUST", "DIP FUND")
GOVERNMENT_PHRASES = ("SECRETARY OF", "HOUSING AND URBAN", "CITY OF", "TOWN OF", "STATE OF", "COUNTY OF",
                      "UNITED STATES", "HOUSING AUTHORITY", "COMMUNITY DEVELOPMENT", "METROPOLITAN GOVERNMENT")
_COUNTY_BODY = re.compile(r"^[A-Z ]+ COUNTY$")
# A church buys a parsonage or a lot next door, not a deal (one ranked among the buyers to call on a live test,
# 2026-11-23). Whole phrases only: "CHURCH STREET HOLDINGS LLC" is an investor named for a street.
RELIGIOUS_PHRASES = ("ASSEMBLY OF GOD", "CHURCH OF", "BAPTIST", "METHODIST", "PRESBYTERIAN", "LUTHERAN", "EPISCOPAL",
                     "PENTECOSTAL", "DIOCESE", "PARISH", "CONGREGATION", "MINISTRY", "TABERNACLE", "SYNAGOGUE",
                     "MOSQUE")
_CHURCH_NAME = re.compile(r"\bCHURCH( INC)?$")
# What a record shows when it has no buyer's name: "Record Owner" stood in a buyer list as a buyer to call
# (3014 Sampleville, 2026-11-23). Whole names only.
PLACEHOLDER_NAMES = {"RECORD OWNER", "CURRENT OWNER", "OWNER", "OWNER OF RECORD", "UNKNOWN", "UNKNOWN OWNER",
                     "NOT AVAILABLE", "NA", "N A", "NONE"}


class SearchFailed(RuntimeError):
    def __init__(self, status: int, message: str, detail=None):
        super().__init__(message)
        self.status, self.detail = status, detail


def _words(name: str) -> str:
    return " %s " % " ".join(re.sub(r"[^A-Z0-9]", " ", (name or "").upper()).split())


def _named_lists():
    try:
        from deal_buyer_match import NAMED_EXCLUSIONS, NOT_BUYERS
        return NAMED_EXCLUSIONS, NOT_BUYERS
    except Exception:  # noqa: BLE001
        return (), ()


def own_names() -> list[str]:
    """The install's own company names (AGENTS_OWN_BUYER_NAMES, comma separated): it buys houses too, and a list
    of buyers to call must not offer the operator its own company."""
    return [n.strip() for n in os.environ.get("AGENTS_OWN_BUYER_NAMES", "").split(",") if n.strip()]


def not_a_target(name: str) -> str:
    """Why a buyer is not someone SiftStack sells a deal to, or ""."""
    up = (name or "").upper()
    if not _words(name).strip() or _words(name).strip() in PLACEHOLDER_NAMES:
        return "no buyer name on the record"
    hit = next((n for n in own_names() if _words(n).strip() and _words(n) in _words(name)), "")
    if hit:
        return "our own company (%s)" % hit
    named, not_buyers = _named_lists()
    hit = next((n for n in named if n in up), "")
    if hit:
        return "builder, iBuyer or fund (%s)" % hit.strip()
    hit = next((n for n in not_buyers if n in up), "")
    if hit:
        return "not a buyer (%s)" % hit.strip()
    w = _words(name)
    hit = next((p for p in GOVERNMENT_PHRASES if " %s " % p in w), "")
    if hit or _COUNTY_BODY.match(w.strip()):
        return "government (%s)" % (hit or "county")
    hit = next((p for p in RELIGIOUS_PHRASES if " %s " % p in w), "")
    if hit or _CHURCH_NAME.search(w.strip()):
        return "church or religious body (%s)" % (hit or "church")
    hit = next((p for p in LENDER_PHRASES if " %s " % p in w), "")
    if hit:
        return "lender or servicer (%s)" % hit
    return ""


def annotate(result: dict) -> dict:
    """Marks result["buyers"] in place: `not_a_target` (a reason or "") and `target_rank` (1, 2, ... over
    the buyers SiftStack would call; None for a marked one). Returns the result."""
    rank = 0
    for b in result.get("buyers") or []:
        why = not_a_target(b.get("buyer_full_name") or "")
        b["not_a_target"] = why
        if why:
            b["target_rank"] = None
        else:
            rank += 1
            b["target_rank"] = rank
    result.setdefault("counts", {})["targets"] = rank
    return result


# ------------------------------------------------------------------ SiftStack's own version

def search_open(*, subject: dict | None = None, address: str = "", client=None, work: Path,
                radius_mi: float | None = None, months: int | None = None, read_cap: int | None = None,
                params: dict | None = None, geocode=None, arv=None) -> dict:
    """One search on the caller's own key. `subject` is a house already known (open_api.subject_from_record);
    otherwise `address` is placed with the Census geocoder and found in SiftMap. `client` is a SiftMap client
    (siftmap_standalone.SiftMapClient reads REISIFT_API_KEY). Raises SearchFailed when there is no house.
    Each buyer carries a guess at what they would pay (open_api.price_guesses), priced against `arv` when
    given, else the house's estimated value."""
    from . import engine, open_api
    if client is None:
        from siftmap_standalone import SiftMapClient
        client = SiftMapClient(min_interval=0.4)
    try:
        p = engine.clean_params({k: v for k, v in (params or {}).items() if k in ("limit", "min_score", "weights")},
                                trusted=True)
    except ValueError as exc:
        raise SearchFailed(400, str(exc)) from exc
    if subject is None:
        subject, why = open_api.subject_from_siftmap(client, address, geocode=geocode)
        if not subject:
            raise SearchFailed(404, why)
    kw = {k: v for k, v in (("radius_mi", radius_mi), ("months", months), ("read_cap", read_cap)) if v is not None}
    return open_api.price_guesses(annotate(open_api.find_buyers(subject, client=client, work=Path(work), params=p,
                                                                 **kw)), arv)


# ------------------------------------------------------------- the data team's model (staff)

def search_cluster(payload: dict, *, backend=None, compat: bool = False) -> dict:
    """The data team's model in this process through the read-only MCP (workstation, staff only).
    `payload`: dataflik_id, or address + zip, plus engine.Params names."""
    from . import engine
    from .backends import MCPBackend
    own = backend is None
    be = backend or MCPBackend()
    try:
        body = dict(CLUSTER_PARAMS, **payload)
        did = body.pop("dataflik_id", None)
        address, zip5 = body.pop("address", None), body.pop("zip", None)
        try:
            params = engine.clean_params(body, trusted=True)
            resolved = None
            cands = engine.resolve_address(be, address or "", zip5 or "") if did is None else []
        except ValueError as exc:
            raise SearchFailed(400, str(exc)) from exc
        if did is None:
            exact = [c for c in cands if c["exact"]]
            pick = exact if exact else cands
            if len(pick) != 1:
                raise SearchFailed(404 if not pick else 409,
                                   "no property at %s %s" % (address, zip5) if not pick else
                                   "%d properties match %s %s" % (len(pick), address, zip5),
                                   {"candidates": pick[:10]})
            resolved, did = pick[0], pick[0]["dataflik_id"]
        out = engine.find_buyers(did, be, params, compat=compat)
        out["source"] = "cluster"
        if resolved:
            out["resolved"] = resolved
        return annotate(out)
    finally:
        if own:
            be.close()
