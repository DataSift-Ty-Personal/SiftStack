"""Self-contained SiftMap client, so the county pull can run in the cloud.

The Deal Room `clients/siftmap_api.py` is the fuller client, but it imports
`reisift_auth` (multi-account JWT store, impersonation, Slack alerting) and
`throttle`, none of which exist on a Fly machine. That single import is why
`ftm_runner`'s `county` stage skipped on every scheduled run. This mirrors what
`sms_agent/crm_standalone.py` did for the reisift CRM: same endpoints, no repo
checkout, one env var.

Routes:
    POST /properties/search/                documented (SiftMap API 1.0), Api-Key
    GET  /properties/detail/{dataflik_id}/  documented, Api-Key
    POST /properties/search-autocomplete/   NOT documented, no auth (verified 2026-08-19)

The Open API key works on all of them, so there is no 48 hour JWT to babysit.

    python src/siftmap_standalone.py "158 Old State Rd, Knoxville, TN"

ALWAYS pass a dataflik_id to get_detail. It is SiftMap's canonical key, and it
is what search and autocomplete return as `id`. A CRM uuid or a reapi id will
not resolve.

Autocomplete is kept only for the callers that parse its rows today (the FTM ZIP
fallback, the notice intake, the dispo sweeps). Acceptable use forbids volume
through it, so new code looks an address up with `search_address`, which is the
documented search route, and `resolve` does too.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://map.reisift.io"

# SiftMap sits behind the beta app; these headers match what the app sends and
# the unauthenticated autocomplete endpoint expects them.
_STD = {
    "accept": "application/json, text/plain, */*",
    "content-type": "application/json",
    "origin": "https://beta.reisift.io",
    "referer": "https://beta.reisift.io/",
}


class SiftMapError(RuntimeError):
    pass


class SiftMapClient:
    """search + get_detail (documented), plus the legacy autocomplete."""

    def __init__(self, api_key: str | None = None, base: str = BASE,
                 min_interval: float = 0.35, max_retries: int = 4) -> None:
        self._key = api_key or os.getenv("REISIFT_API_KEY", "")
        self._base = base.rstrip("/")
        self._min_interval = min_interval
        self._max_retries = max_retries
        self._last = 0.0

    # ── plumbing ─────────────────────────────────────────────────────
    def _wait(self) -> None:
        gap = time.time() - self._last
        if gap < self._min_interval:
            time.sleep(self._min_interval - gap)
        self._last = time.time()

    def _request(self, path: str, method: str = "GET", body: dict | None = None,
                 auth: bool = True, timeout: float = 40.0):
        headers = dict(_STD)
        if auth:
            if not self._key:
                raise SiftMapError(
                    "REISIFT_API_KEY is not set; the detail endpoint needs it")
            headers["authorization"] = f"Api-Key {self._key}"
        data = json.dumps(body).encode() if body is not None else None

        for attempt in range(self._max_retries + 1):
            self._wait()
            req = urllib.request.Request(self._base + path, data=data,
                                         headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    return json.loads(resp.read())
            except urllib.error.HTTPError as e:
                body_txt = ""
                try:
                    body_txt = e.read().decode("utf-8", "replace")[:300]
                except Exception:
                    pass
                # Honour the server's own backoff hint rather than guessing.
                if e.code == 429 and attempt < self._max_retries:
                    wait = 5.0 * (attempt + 1)
                    ra = e.headers.get("Retry-After") if e.headers else None
                    if ra:
                        try:
                            wait = float(ra)
                        except ValueError:
                            pass
                    time.sleep(wait)
                    continue
                if e.code in (500, 502, 503, 504) and attempt < self._max_retries:
                    time.sleep(2.0 * (attempt + 1))
                    continue
                if e.code == 401:
                    raise SiftMapError(
                        f"401 on {method} {path}. The Api-Key was rejected. {body_txt}")
                raise SiftMapError(f"HTTP {e.code} on {method} {path}: {body_txt}")
            except urllib.error.URLError as e:
                if attempt < self._max_retries:
                    time.sleep(2.0 * (attempt + 1))
                    continue
                raise SiftMapError(f"Network error on {method} {path}: {e}")
        raise SiftMapError(f"exhausted retries on {method} {path}")

    # ── the calls ────────────────────────────────────────────────────
    def search(self, body: dict) -> dict:
        """POST /properties/search/, the documented SiftMap search.

        `body` carries any of `addresses`, `polygon` and `filters`, plus `size`
        and `result_index`. Unknown filter keys are IGNORED by the server, so a
        misspelled key widens the search instead of failing: probe a new key by
        the change in `total_results`, never by the absence of an error.
        Rows carry no sale price and no owner name; those are on get_detail.
        """
        r = self._request("/properties/search/", "POST", body)
        return r if isinstance(r, dict) else {"data": r or []}

    def search_address(self, address: str, *, size: int = 3) -> list[dict]:
        """Rows for one free-text address through the documented search route.

        This is the replacement for autocomplete on a single lookup. Each row
        carries `id` (the dataflik_id), `address` (one string) and the map
        point, so a caller still checks the house number before trusting it.
        """
        body = {"addresses": [{"type": "address", "search": address}],
                "size": max(1, int(size)), "result_index": 0}
        return self.search(body).get("data") or []

    def autocomplete(self, partial: str, *, limit: int = 10) -> list[dict]:
        """Candidate properties for a partial address. No auth needed.

        Each row carries `id`, which IS the dataflik_id get_detail wants.
        Legacy and undocumented: kept for the callers that parse these rows.
        """
        r = self._request("/properties/search-autocomplete/", "POST",
                          {"search": partial, "limit": limit}, auth=False)
        if isinstance(r, list):
            return r
        return r.get("results") or r.get("data") or []

    def get_detail(self, property_id: str | int) -> dict:
        """Full property record for a dataflik_id.

        Carries sale_history, mortgage_history, mls_history, owner_info
        (portfolio, equity) and the AI scores.
        """
        return self._request(f"/properties/detail/{property_id}/")

    # ── convenience ──────────────────────────────────────────────────
    def resolve(self, address: str, *, limit: int = 3) -> dict | None:
        """One-shot address to detail. Returns None when nothing matches.

        The first search row is taken only when its house number is the one
        asked for: the search answers the same street name in another town
        without blinking, and a detail read on the wrong house is worse than none.
        """
        hits = self.search_address(address, size=limit)
        want = (address or "").strip().split(" ", 1)[0]
        for hit in hits:
            got = str(hit.get("address") or "").strip().split(" ", 1)[0]
            if want.isdigit() and got != want:
                continue
            pid = hit.get("id") or hit.get("dataflik_id")
            if not pid:
                continue
            d = self.get_detail(pid)
            if isinstance(d, dict):
                d.setdefault("_search", hit)
            return d
        return None


def selftest(address: str) -> int:
    c = SiftMapClient()
    print(f"search: {address!r}")
    hits = c.search_address(address, size=3)
    print(f"  {len(hits)} hit(s)")
    for h in hits[:3]:
        print(f"    {h.get('id')}  {h.get('address')}")
    if not hits:
        return 1
    d = c.get_detail(hits[0]["id"])
    print(f"detail: {len(d)} field(s)")
    for k in ("address", "owner_info", "sale_history", "estimated_value",
              "investor_score", "equity"):
        if k in d:
            print(f"    {k}: {str(d[k])[:90]}")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    try:
        import config  # noqa: F401  (loads .env for local runs)
    except Exception:
        pass
    q = sys.argv[1] if len(sys.argv) > 1 else "158 Old State Rd, Knoxville, TN"
    raise SystemExit(selftest(q))
