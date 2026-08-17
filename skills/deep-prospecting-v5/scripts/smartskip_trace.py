#!/usr/bin/env python3
"""Step 0: Run a SmartSkip bulk skip trace through the app.smartskip.io API.

Replaces the manual "upload the list in the SmartSkip web app and download the
export" step. Full lifecycle: signin -> upload CSV -> auto-map columns ->
calculate (free) -> [pay, explicit] -> poll status -> download export CSV.
The export it downloads is EXACTLY what parse_smartskip.py already consumes
(vertical = Campaign format, horizontal = CRM/wide format).

API contract (verified live 2026-07-29): see references/smartskip-api.md.

Auth: env SMARTSKIP_EMAIL / SMARTSKIP_PASSWORD (or smartskip.config.json next to
SKILL.md / in scripts/: {"email": "...", "password": "..."}). Session tokens are
cached in $SKIPTRACE_RUN_DIR/smartskip_session.json; access token lasts ~15 min
and is auto-refreshed with the ~30-day refresh token, falling back to re-signin.

Money: everything is FREE except `pay` (charges the account's saved card, billed
per row that returns data). `submit` stops after calculate and prints the row
count; nothing is charged until you run `pay` (or pass --pay to `run`).

Usage:
  python3 scripts/smartskip_trace.py submit   --csv list.csv            # upload+map+calculate (free)
  python3 scripts/smartskip_trace.py pay      --id <bulkSkipId>         # charge saved card (BILLS)
  python3 scripts/smartskip_trace.py status  [--id <bulkSkipId>]        # order status
  python3 scripts/smartskip_trace.py download --id <id> --out exp.csv [--format vertical|horizontal]
  python3 scripts/smartskip_trace.py run      --csv list.csv --out exp.csv --pay   # full chain
  python3 scripts/smartskip_trace.py balance                            # wallet + cards on file

Input CSV columns are auto-matched (case/punctuation-blind) to SmartSkip fields:
required First Name, Last Name, Mailing Address; optional Middle Name, Mailing
City/State/Zip, Property Address/City/State/Zip. Override any match with
--map "apiField=CSV Header" (repeatable).
"""
import argparse, csv, io, json, os, re, sys, time, uuid
import urllib.request, urllib.error

API = "https://api.smartskip.io"
RUN_DIR = os.environ.get("SKIPTRACE_RUN_DIR", ".skiptrace_run")
SESSION_FILE = os.path.join(RUN_DIR, "smartskip_session.json")
ORDERS_FILE = os.path.join(RUN_DIR, "smartskip_orders.json")

# canonical header synonyms -> SmartSkip api field (normalized: lowercase alnum only)
SYNONYMS = {
    "firstName": ["firstname", "ownerfirstname", "first", "fname"],
    "lastName": ["lastname", "ownerlastname", "last", "lname"],
    "middleName": ["middlename", "middle", "mi"],
    "mailingAddress": ["mailingaddress", "mailingstreet", "mailingaddr", "owneraddress", "mailaddress"],
    "mailingCity": ["mailingcity", "mailcity"],
    "mailingState": ["mailingstate", "mailstate", "mailingst"],
    "mailingZip": ["mailingzip", "mailingzip5", "mailingzipcode", "mailzip"],
    "propertyAddress": ["propertyaddress", "propertystreet", "propertyaddr", "address", "situsaddress", "street"],
    "propertyCity": ["propertycity", "city", "situscity"],
    "propertyState": ["propertystate", "state", "situsstate"],
    "propertyZip": ["propertyzip", "propertyzip5", "propertyzipcode", "zip", "situszip"],
}


def norm(h):
    return re.sub(r"[^a-z0-9]", "", (h or "").lower())


# ---------- auth ----------

def resolve_creds():
    email = os.environ.get("SMARTSKIP_EMAIL", "")
    pw = os.environ.get("SMARTSKIP_PASSWORD", "")
    if email and pw:
        return email, pw
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (os.path.join(here, "..", "smartskip.config.json"),
                 os.path.join(here, "smartskip.config.json")):
        if os.path.exists(cand):
            try:
                c = json.load(open(cand))
                return c.get("email", email), c.get("password", pw)
            except Exception:
                pass
    return email, pw


def http(method, path, token=None, body=None, raw_body=None, headers=None, timeout=120):
    url = path if path.startswith("http") else API + path
    h = {"Accept": "application/json"}
    if token:
        h["Authorization"] = "Bearer " + token
    if body is not None:
        raw_body = json.dumps(body).encode("utf-8")
        h["Content-Type"] = "application/json"
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=raw_body, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
            ct = resp.headers.get("content-type", "")
            cd = resp.headers.get("content-disposition", "")
            if "json" in ct:
                return resp.status, json.loads(data.decode("utf-8")), cd
            return resp.status, data, cd
    except urllib.error.HTTPError as e:
        data = e.read()
        try:
            return e.code, json.loads(data.decode("utf-8")), ""
        except Exception:
            return e.code, data.decode("utf-8", errors="replace"), ""


class Session:
    def __init__(self):
        self.tokens = {}
        if os.path.exists(SESSION_FILE):
            try:
                self.tokens = json.load(open(SESSION_FILE))
            except Exception:
                pass

    def save(self):
        os.makedirs(RUN_DIR, exist_ok=True)
        json.dump(self.tokens, open(SESSION_FILE, "w"))

    def signin(self):
        email, pw = resolve_creds()
        if not email or not pw:
            sys.exit("ERROR: no SmartSkip creds (SMARTSKIP_EMAIL/SMARTSKIP_PASSWORD or smartskip.config.json).")
        code, data, _ = http("POST", "/auth/signin", body={"email": email, "password": pw})
        if code != 200:
            sys.exit("ERROR: SmartSkip signin failed (%s): %s" % (code, data))
        self.tokens = {"accessToken": data["accessToken"], "refreshToken": data["refreshToken"]}
        self.save()

    def refresh(self):
        rt = self.tokens.get("refreshToken")
        if not rt:
            return False
        code, data, _ = http("GET", "/auth/refresh", token=rt)
        if code == 200 and isinstance(data, dict) and data.get("accessToken"):
            self.tokens = {"accessToken": data["accessToken"], "refreshToken": data.get("refreshToken", rt)}
            self.save()
            return True
        return False

    def call(self, method, path, **kw):
        if not self.tokens.get("accessToken"):
            if not self.refresh():
                self.signin()
        code, data, cd = http(method, path, token=self.tokens["accessToken"], **kw)
        if code in (401, 403):
            if not self.refresh():
                self.signin()
            code, data, cd = http(method, path, token=self.tokens["accessToken"], **kw)
        return code, data, cd


# ---------- order state ----------

def load_orders():
    if os.path.exists(ORDERS_FILE):
        try:
            return json.load(open(ORDERS_FILE))
        except Exception:
            pass
    return []


def save_order(entry):
    orders = load_orders()
    orders = [o for o in orders if o.get("bulkSkipId") != entry["bulkSkipId"]] + [entry]
    os.makedirs(RUN_DIR, exist_ok=True)
    json.dump(orders, open(ORDERS_FILE, "w"), indent=2)


# ---------- steps ----------

def upload(sess, csv_path):
    fname = os.path.basename(csv_path)
    payload = open(csv_path, "rb").read()
    boundary = uuid.uuid4().hex
    buf = io.BytesIO()
    buf.write(("--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"%s\"\r\n"
               "Content-Type: text/csv\r\n\r\n" % (boundary, fname)).encode())
    buf.write(payload)
    buf.write(("\r\n--%s--\r\n" % boundary).encode())
    code, data, _ = sess.call("POST", "/bulk-skip/mapping", raw_body=buf.getvalue(),
                              headers={"Content-Type": "multipart/form-data; boundary=" + boundary})
    if code != 200 and code != 201:
        sys.exit("ERROR: upload failed (%s): %s" % (code, data))
    return data  # {bulkSkipId, parsed: {header: sample}}


def auto_map(sess, headers, overrides):
    code, fields, _ = sess.call("GET", "/bulk-skip/fields")
    if code != 200:
        sys.exit("ERROR: fields fetch failed (%s): %s" % (code, fields))
    api_fields = dict(fields.get("required", {}))
    api_fields.update(fields.get("optional", {}))
    by_norm = {norm(h): h for h in headers}
    schema = {}
    for f, label in api_fields.items():
        if f in overrides:
            if overrides[f] not in headers:
                sys.exit("ERROR: --map %s=%r: no such CSV column" % (f, overrides[f]))
            schema[f] = overrides[f]
            continue
        cands = [norm(label), norm(f)] + SYNONYMS.get(f, [])
        hit = next((by_norm[c] for c in cands if c in by_norm), None)
        if hit:
            schema[f] = hit
    missing = [f for f in fields.get("required", {}) if f not in schema]
    if missing:
        sys.exit("ERROR: could not map required field(s) %s to CSV headers %s. "
                 "Use --map \"field=CSV Header\"." % (missing, list(headers)))
    return schema


def submit(sess, csv_path, overrides):
    headers = next(csv.reader(open(csv_path, newline="", encoding="utf-8-sig")))
    up = upload(sess, csv_path)
    bid = up["bulkSkipId"]
    schema = auto_map(sess, headers, overrides)
    code, preview, _ = sess.call("POST", "/bulk-skip/fields/%s" % bid, body={"schema": schema})
    if code not in (200, 201):
        sys.exit("ERROR: field mapping failed (%s): %s" % (code, preview))
    code, calc, _ = sess.call("POST", "/bulk-skip/calculate/%s" % bid)
    if code not in (200, 201):
        sys.exit("ERROR: calculate failed (%s): %s" % (code, calc))
    entry = {"bulkSkipId": bid, "file": os.path.basename(csv_path), "schema": schema,
             "entities": calc.get("entities"), "duplicates": calc.get("duplicates"),
             "status": calc.get("status"), "paid": False}
    save_order(entry)
    print(json.dumps({"bulkSkipId": bid, "entities": calc.get("entities"),
                      "duplicates": calc.get("duplicates"), "status": calc.get("status"),
                      "mapped": schema}, indent=2))
    print("NOT PAID YET - nothing billed. Run: smartskip_trace.py pay --id %s" % bid, flush=True)
    return entry


def pay(sess, bid, pm_id=None):
    if not pm_id:
        code, pms, _ = sess.call("GET", "/payment/payment-method")
        if code != 200 or not pms:
            sys.exit("ERROR: no saved payment method (%s): %s" % (code, pms))
        pm = next((p for p in pms if p.get("isDefault")), pms[0])
        pm_id = pm["id"]
        print("Using card: %s ...%s (default)" % (pm.get("brand"), pm.get("last4")))
    code, data, _ = sess.call("POST", "/bulk-skip/payment-intent",
                              body={"bulkSkipId": bid, "paymentMethodId": pm_id})
    if code not in (200, 201):
        sys.exit("ERROR: payment-intent failed (%s): %s" % (code, data))
    status = data.get("status")
    if data.get("clientSecret"):
        print("PAYMENT NEEDS 3DS CONFIRMATION (clientSecret returned). Complete the "
              "payment once in the browser at https://app.smartskip.io/bulk-skip, "
              "then re-run status/download here.")
        return False
    print("payment status=%s paymentIntentId=%s" % (status, data.get("paymentIntentId")))
    ok = status in ("succeeded", "processing", "requires_capture")
    if ok:
        orders = load_orders()
        for o in orders:
            if o.get("bulkSkipId") == bid:
                o["paid"] = True
        os.makedirs(RUN_DIR, exist_ok=True)
        json.dump(orders, open(ORDERS_FILE, "w"), indent=2)
    return ok


def fetch_status(sess, bid=None):
    code, data, _ = sess.call("GET", "/bulk-skip?sortField=createdAt&sortOrder=desc")
    if code != 200:
        sys.exit("ERROR: status fetch failed (%s): %s" % (code, data))
    items = data.get("items", [])
    if bid:
        items = [i for i in items if i.get("_id") == bid]
    return items


def download(sess, bid, out, fmt):
    code, data, cd = sess.call("GET", "/bulk-skip/download/%s?type=%s" % (bid, fmt))
    if code != 200:
        sys.exit("ERROR: download failed (%s): %s" % (code, data))
    blob = data if isinstance(data, (bytes, bytearray)) else json.dumps(data).encode()
    open(out, "wb").write(blob)
    m = re.search(r'filename="([^"]+)"', cd or "")
    print("saved %s (%d bytes)%s" % (out, len(blob), " server name: %s" % m.group(1) if m else ""))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["submit", "pay", "status", "download", "run", "balance"])
    ap.add_argument("--csv")
    ap.add_argument("--id", dest="bid")
    ap.add_argument("--out")
    ap.add_argument("--format", default="vertical", choices=["vertical", "horizontal"],
                    help="vertical=Campaign (parse_smartskip fmt 1), horizontal=CRM/wide (fmt 2)")
    ap.add_argument("--map", action="append", default=[], help='override mapping: "apiField=CSV Header"')
    ap.add_argument("--pm", help="Stripe paymentMethodId (default: account default card)")
    ap.add_argument("--pay", action="store_true", help="run only: proceed to payment after calculate")
    ap.add_argument("--wait", type=int, default=900, help="run/download: max seconds to poll for completion")
    a = ap.parse_args()
    overrides = dict(m.split("=", 1) for m in a.map)
    sess = Session()

    if a.cmd == "balance":
        _, wal, _ = sess.call("GET", "/wallet/balance")
        _, pms, _ = sess.call("GET", "/payment/payment-method")
        print(json.dumps({"wallet": wal, "payment_methods": pms}, indent=2))
        return

    if a.cmd == "submit":
        if not a.csv:
            sys.exit("--csv required")
        submit(sess, a.csv, overrides)
        return

    if a.cmd == "pay":
        if not a.bid:
            sys.exit("--id required")
        pay(sess, a.bid, a.pm)
        return

    if a.cmd == "status":
        for i in fetch_status(sess, a.bid):
            print(json.dumps(i, indent=2))
        return

    if a.cmd == "download":
        if not (a.bid and a.out):
            sys.exit("--id and --out required")
        download(sess, a.bid, a.out, a.format)
        return

    if a.cmd == "run":
        if not (a.csv and a.out):
            sys.exit("--csv and --out required")
        entry = submit(sess, a.csv, overrides)
        bid = entry["bulkSkipId"]
        if not a.pay:
            print("Stopping before payment (no --pay). Nothing billed.")
            return
        if not pay(sess, bid, a.pm):
            return
        t0 = time.time()
        while time.time() - t0 < a.wait:
            items = fetch_status(sess, bid)
            st = (items[0].get("status") if items else "?") or "?"
            print("status=%s" % st, flush=True)
            if st.lower() == "completed":
                download(sess, bid, a.out, a.format)
                return
            if st.lower() in ("error", "failed"):
                sys.exit("ERROR: bulk skip ended in status %s" % st)
            time.sleep(15)
        print("TIMEOUT waiting for completion - re-run: smartskip_trace.py download --id %s --out %s" % (bid, a.out))


if __name__ == "__main__":
    main()
