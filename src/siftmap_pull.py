"""Size and bulk-pull County List Playbook segments into the DataSift account.

The playbook ranks segments per county; this pulls the ones that rank but are
not in the account yet. Two routes on map.reisift.io, both read out of the
web app's own bundle rather than guessed:

    POST /properties/search/                    size a segment (total_results)
    POST /properties/add-properties-by-query/   pull it in, into a list
    POST /filters/                              save it as an auto-add feeder

Contract details that fail silently if you get them wrong:
  * a county address needs search="Knox" (bare name). "Knox County, TN" is
    accepted and returns total_results 0, which reads exactly like an empty
    segment rather than a bad query.
  * /properties/search/ needs the THIN keys `search` + `type` ON TOP of the
    rich county object; /filters/ wants the rich object with `searchType`.
    Sending only one shape 400s or silently returns nothing.
  * the viewport `polygon` is a list of {lon,lat} DICTS. A list of pairs 400s.
  * value_min is 1, not the 100000 the older presets carry. Ty's buy box is
    $1-$700K on purpose: condemned and tax-distressed stock routinely falls
    under $100K and a six-figure floor cuts exactly the rows these lists
    exist to surface.

    python src/siftmap_pull.py --size            # sizes only, writes nothing
    python src/siftmap_pull.py --pull --commit   # pulls the missing P2 rows
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from datasift_api_upload import Api  # noqa: E402

MAP = "https://map.reisift.io"
API = "https://apiv2.reisift.io"

# The whole-world viewport. Searches intersect addresses with the map viewport,
# so a county pull has to hand in a polygon that contains the county.
WORLD = [{"lon": -17.89461189115002, "lat": 72.08452694723852},
         {"lon": -17.89461189115002, "lat": -13.881763595427103},
         {"lon": -163.30518783395442, "lat": -13.881763595427103},
         {"lon": -163.30518783395442, "lat": 72.08452694723852}]

COUNTIES = {
    "47093": {"name": "Knox", "state": "TN"},
    "47009": {"name": "Blount", "state": "TN"},
    "39049": {"name": "Franklin", "state": "OH"},
}

BUY_BOX = {"value_min": 1, "value_max": 700000,
           "extra_mls_status": ["not_listed"], "type_single_family": True}

# EVERY playbook key goes through distressors_include. There is also a family of
# preset_* booleans (preset_absentee_owners, preset_high_equity, ...) and the
# older account presets use them, but they are the wrong tool here for two
# measured reasons:
#   * preset_free_and_clear and preset_vacant_property are SILENTLY IGNORED.
#     Both return 125,653 in Knox, which is the buy box with no segment filter
#     at all, so a pull built on them quietly grabs the whole county.
#   * where they do work they are a DIFFERENT population than the playbook's.
#     preset_absentee_owners is 31,574 in Knox; the is_absentee_owners
#     distressor the playbook measured its lift against is 20,154.
# Verified live 2026-08-17 against Knox: F&C 51,805 / vacant 1,384 / OOS 3,867.
PRESET_KEYS: dict[str, str] = {}


def county_address(fips: str, *, rich: bool) -> dict:
    c = COUNTIES[fips]
    title = "%s County, %s" % (c["name"], c["state"])
    base = {"state": c["state"], "title": title, "value": title,
            "county": c["name"], "searchType": "county",
            "counties": [{"fips": fips, "county_name": c["name"]}]}
    if rich:
        return base
    # The search endpoint additionally requires the thin pair, and `search`
    # must be the BARE county name.
    return dict(base, search=c["name"], type="county")


def seg_filters(keys: list[str]) -> dict:
    """Buy box + the segment's own filter fragment."""
    f = dict(BUY_BOX)
    distressors = []
    for k in keys:
        if k in PRESET_KEYS:
            f[PRESET_KEYS[k]] = True
        else:
            distressors.append(k)
    if distressors:
        f["distressors_include"] = ",".join(distressors)
        # AND, not OR: a combo row means every signal present on the record.
        f["distressors_include_min_match"] = len(distressors)
    return f


class Map:
    def __init__(self, api: Api):
        self.api = api

    def _post(self, path: str, body: dict, *, base: str = MAP, timeout: int = 240):
        data = json.dumps(body).encode()
        for attempt in range(5):
            req = urllib.request.Request(
                base + path, data=data, method="POST",
                headers={"Authorization": "Bearer " + self.api.token,
                         "Content-Type": "application/json",
                         "accept": "application/json",
                         "origin": "https://beta.reisift.io",
                         "referer": "https://beta.reisift.io/"})
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    t = r.read().decode()
                    return json.loads(t) if t.strip().startswith(("{", "[")) else t
            except urllib.error.HTTPError as e:
                if e.code == 401 and attempt < 4:
                    self.api._mint()
                    continue
                if e.code in (429, 500, 502, 504) and attempt < 4:
                    time.sleep(4 * (attempt + 1))
                    continue
                raise RuntimeError("HTTP %s on %s: %s"
                                   % (e.code, path, e.read().decode()[:220]))
        raise RuntimeError("gave up on " + path)

    def size(self, fips: str, keys: list[str]) -> int:
        r = self._post("/properties/search/", {
            "result_index": 1, "with_boundaries": False,
            "filters": seg_filters(keys),
            "addresses": [county_address(fips, rich=False)],
            "polygon": WORLD})
        return r.get("total_results", 0)

    def add(self, fips: str, keys: list[str], *, lists: list, tags: list) -> dict:
        """Bulk-add every match to the account. This SPENDS record allowance."""
        return self._post("/properties/add-properties-by-query/", {
            "auto_add_enabled": False,
            "lists": lists, "tags": tags,
            # Never overwrite owner data we resolved ourselves (PR/heir mapping).
            "replace_owners": False,
            "query": {"result_index": 1, "with_boundaries": False,
                      "filters": seg_filters(keys),
                      "addresses": [county_address(fips, rich=False)],
                      "polygon": WORLD}})

    def save_preset(self, name: str, fips: str, keys: list[str], *,
                    lists: list, tags: list, description: str = "") -> dict:
        """Save the same segment as an auto-add feeder so it stays current."""
        return self._post("/filters/", {
            "name": name, "description": description,
            "auto_add_enabled": True, "replace_owners_enabled": False,
            "lists": lists, "tags": tags,
            "filter_data": {"filters": seg_filters(keys),
                            "addresses": [county_address(fips, rich=True)]}})


def usage(api: Api) -> tuple[int, int]:
    r = api.call("/api/internal/upload/usage/", method="POST", body={})
    return r.get("upload_usage", 0), r.get("upload_limit", 0)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fips", default="47093")
    ap.add_argument("--keys", help="comma-separated segment keys to size")
    args = ap.parse_args()

    api = Api()
    m = Map(api)
    used, limit = usage(api)
    print("record allowance: %s used of %s (%s left)"
          % (f"{used:,}", f"{limit:,}", f"{limit - used:,}"))

    if args.keys:
        keys = args.keys.split(",")
        print("%s -> %s" % (args.keys, f"{m.size(args.fips, keys):,}"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
