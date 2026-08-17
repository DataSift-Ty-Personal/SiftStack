"""Upload the Knox first-to-market CSV into DataSift entirely over the API.

Auth: mints a user JWT from DATASIFT_EMAIL / DATASIFT_PASSWORD in .env via
POST /api/token/. No pasted token, nothing to expire mid-run. The Open API key
cannot do this job: custom fields do not exist anywhere in its 93-route surface,
so every write to them 401s. The minted JWT reaches the internal API where they do.

Three contract details, each of which fails SILENTLY or cryptically:
  * tags must be an ARRAY. A comma string creates one tag literally named
    "Courthouse Data, code_violation, Knox".
  * a select field's value must be the OPTION'S UUID, not its label. Sending
    "LEN" returns {"non_field_errors": ["'LEN' is not a valid UUID."]}.
  * `notes` on the property payload is accepted with 200 and then discarded.

    python src/datasift_api_upload.py --limit 1            # dry run
    python src/datasift_api_upload.py --limit 1 --commit   # one record, verified
    python src/datasift_api_upload.py --commit             # the whole file
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import time
import urllib.error
import urllib.request

BASE = "https://apiv2.reisift.io"

# CSV column -> custom-field label
FIELD_MAP = {
    "Outstanding Lien Amount": "Outstanding Lien Amount",
    "Liens Active vs Total": "Liens Active vs Total",
    "Lien Type": "Lien Type",
    "Total Delinquency": "Total Delinquency",
    "Est Equity": "Est Equity",
    "Debt vs Equity Pct": "Debt vs Equity Pct",
    "Upside Down": "Upside Down",
    "Upside Down Reason": "Upside Down Reason",
    "Tax Deliquent Value": "Tax delinquency amount",   # DataSift's own typo
    "Notice Type": "Notice Type",
    "County": "County",
    "Source URL": "Source URL",
    "Personal Representative": "Personal Representative",
    "Decedent Name": "Decedent Name",
}


def env() -> dict:
    """Credentials from the process environment, with .env as a fallback.

    Environment FIRST, on purpose. This used to read ./.env unconditionally,
    which meant the uploader could only ever run from a workstation checkout
    with that file present and the right working directory. On a Fly machine
    every secret arrives as an env var and there is no .env at all, so the old
    version died on FileNotFoundError before it made a single call.
    """
    out = dict(os.environ)
    try:
        with open(".env", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    # Never let a stale file shadow an explicitly-set env var.
                    out.setdefault(k.strip(), v.strip())
    except OSError:
        pass
    return out


class Api:
    def __init__(self):
        e = env()
        try:
            self.email, self.pw = e["DATASIFT_EMAIL"], e["DATASIFT_PASSWORD"]
        except KeyError as exc:
            raise RuntimeError(
                f"{exc.args[0]} is not set. The uploader mints its own JWT from "
                "DATASIFT_EMAIL / DATASIFT_PASSWORD; set them as env vars "
                "(Fly secrets) or in .env."
            ) from None
        self.token = ""
        self.minted = 0.0
        self._mint()

    def _mint(self):
        body = json.dumps({"email": self.email, "password": self.pw}).encode()
        req = urllib.request.Request(BASE + "/api/token/", data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=45) as r:
            self.token = json.loads(r.read())["access"]
        self.minted = time.time()

    def call(self, path, method="GET", body=None, _retry=True):
        # JWTs are short-lived; refresh transparently rather than dying mid-run.
        if time.time() - self.minted > 1800:
            self._mint()
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            BASE + path, data=data, method=method,
            headers={"Authorization": "Bearer " + self.token,
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                t = r.read().decode()
                return json.loads(t) if t.strip().startswith(("{", "[")) else t
        except urllib.error.HTTPError as e:
            if e.code == 401 and _retry:
                self._mint()
                return self.call(path, method, body, _retry=False)
            raise RuntimeError("HTTP %s on %s %s: %s"
                               % (e.code, method, path, e.read().decode()[:200]))


def field_index(api: Api) -> dict:
    """label -> {uuid, field_type, options{label: option_uuid}}"""
    r = api.call("/api/internal/custom-fields/?limit=999")
    out = {}
    for f in r.get("results") or []:
        out[f["label"]] = {
            "uuid": f["uuid"], "field_type": f.get("field_type"),
            "options": {o.get("label"): o.get("uuid") for o in (f.get("options") or [])},
        }
    return out


def build_property(row: dict) -> dict:
    addr = {"street": row["Property Street Address"],
            "city": row.get("Property City") or "",
            "state": row.get("Property State") or "TN",
            "postal_code": row.get("Property ZIP Code") or ""}
    mail = {"street": row.get("Mailing Street Address") or addr["street"],
            "city": row.get("Mailing City") or addr["city"],
            "state": row.get("Mailing State") or addr["state"],
            "postal_code": row.get("Mailing ZIP Code") or addr["postal_code"]}
    # Entity owners (LLC, INC, TRUST...) have no first name, and the API
    # rejects a blank one: {"owner":{"first_name":["This field may not be blank."]}}.
    # Send them as `company` instead and omit the person fields entirely --
    # omitting a key is not the same as sending "".
    first = (row.get("Owner First Name") or "").strip()
    last = (row.get("Owner Last Name") or "").strip()
    owner: dict = {"address": mail}
    if first:
        owner["first_name"] = first
        if last:
            owner["last_name"] = last
    elif last:
        owner["company"] = last
    body = {"address": addr, "owner": owner}
    if row.get("Lists"):
        body["lists"] = row["Lists"]
    if row.get("Tags"):
        # ARRAY, not a comma string
        body["tags"] = [t.strip() for t in row["Tags"].split(",") if t.strip()]
    return body


def field_pairs(row: dict, idx: dict, warn: set) -> list[dict]:
    pairs = []
    for col, label in FIELD_MAP.items():
        # values arrive as str from CSV but as int/float when called in-process
        raw = str(row.get(col) if row.get(col) is not None else "").strip()
        if not raw:
            continue
        f = idx.get(label)
        if not f:
            warn.add(label)
            continue
        val = raw
        if f["field_type"] in ("select", "multiselect"):
            val = f["options"].get(raw)
            if not val:                      # option missing: skip, never guess
                warn.add("%s option %r" % (label, raw))
                continue
        pairs.append({"field_uuid": f["uuid"], "value": val})
    return pairs


def upload_rows(rows: list[dict], *, commit: bool = False,
                sleep: float = 0.1, out_dir: str = "output") -> dict:
    """Push rows into DataSift. Returns counts so a caller can act on them.

    Broken out of main() so the scheduled runner can call it in-process and
    report real numbers to Slack, instead of shelling out and parsing stdout.
    """
    api = Api()
    idx = field_index(api)
    print("[%s] %d records | JWT minted | %d custom fields known\n"
          % ("COMMIT" if commit else "DRY RUN", len(rows), len(idx)))

    warn: set = set()
    created = fielded = failed = 0
    errors: list[str] = []
    t0 = time.time()
    for i, row in enumerate(rows, 1):
        prop = build_property(row)
        pairs = field_pairs(row, idx, warn)
        if not commit:
            if i <= 2:
                print(json.dumps(prop, indent=1)[:420])
                print("  -> %d custom-field values\n" % len(pairs))
            continue
        try:
            res = api.call("/property/", "POST", prop)
            uuid = res.get("uuid") if isinstance(res, dict) else None
            created += 1
            if uuid and pairs:
                api.call("/api/internal/property/%s/custom-field/update-values/" % uuid,
                         "PATCH", pairs)
                fielded += 1
            if uuid and row.get("Notes"):
                # notes are dropped from the property payload; post separately
                try:
                    api.call("/api/internal/property/%s/add-notes/" % uuid,
                             "POST", {"notes": row["Notes"][:2000]})
                except Exception:
                    pass
        except Exception as e:
            failed += 1
            msg = "%s | %s" % (str(row.get("Property Street Address", ""))[:30], str(e)[:150])
            errors.append(msg)
            if len(errors) <= 5:
                print("  FAIL", msg)
        if i % 100 == 0:
            print("   %d/%d created=%d fields=%d failed=%d %.0fs"
                  % (i, len(rows), created, fielded, failed, time.time() - t0), flush=True)
        time.sleep(sleep)

    if warn:
        print("\nWARNINGS (values skipped, never guessed):")
        for w in sorted(warn):
            print("   ", w)

    err_path = ""
    if commit:
        print("\ncreated=%d  custom-fields-set=%d  failed=%d  %.0fs"
              % (created, fielded, failed, time.time() - t0))
        if errors:
            os.makedirs(out_dir, exist_ok=True)
            err_path = os.path.join(out_dir, "upload_errors.txt")
            with open(err_path, "w", encoding="utf-8") as f:
                f.write("\n".join(errors))
            print("errors -> %s (%d)" % (err_path, len(errors)))
    else:
        print("\nNothing written. Re-run with --commit.")

    return {
        "submitted": len(rows), "created": created, "fielded": fielded,
        "failed": failed, "warnings": sorted(warn), "errors": errors,
        "error_file": err_path, "committed": commit,
        "seconds": round(time.time() - t0, 1),
    }


def upload_csv(path: str, *, commit: bool = False, limit: int = 0,
               start: int = 0, sleep: float = 0.1, out_dir: str = "output") -> dict:
    """Read an upload CSV and push it. Same return shape as upload_rows."""
    with open(path, encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    if start:
        rows = rows[start:]
    if limit:
        rows = rows[:limit]
    return upload_rows(rows, commit=commit, sleep=sleep, out_dir=out_dir)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default="output/knox_ftm_pull.csv")
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--start", type=int, default=0)
    ap.add_argument("--sleep", type=float, default=0.1)
    a = ap.parse_args()

    res = upload_csv(a.csv, commit=a.commit, limit=a.limit,
                     start=a.start, sleep=a.sleep)
    # A commit run that created nothing from a non-empty file is a failure,
    # not a quiet success, the same class of bug as the 13 "successful"
    # zero-notice scrapes.
    if a.commit and res["submitted"] and not res["created"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
