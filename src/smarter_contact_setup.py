#!/usr/bin/env python3
"""Drive Smarter Contact's web app to create what smarter_contact_templates.py defines.

Smarter Contact has no public API (its Zapier app has one read trigger and no
actions), so setup goes through the browser. Two phases exist today; the create
phases are written against what `discover` records, not guessed selectors.

    python src/smarter_contact_setup.py --phase login      # headed; YOU sign in
    python src/smarter_contact_setup.py --phase discover   # headless; maps pages + requests

`login` opens a real browser window on the Smarter Contact sign-in page and
waits for a person to finish signing in (password, Google, or a 2FA code). The
session lives in a persistent browser profile under output/ (gitignored), so no
password is ever stored by this script. A persistent profile rather than a
storage_state file because Smarter Contact authenticates through Firebase, which
keeps its tokens in IndexedDB, and storage_state does not carry IndexedDB.

If SMARTER_CONTACT_EMAIL and SMARTER_CONTACT_PASSWORD are set, `login` fills the
form itself and still waits, in case a verification code is asked for.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent.parent
PROFILE = ROOT / "output" / "smarter_contact" / "browser_profile"
DISCOVER_DIR = ROOT / "output" / "smarter_contact" / "discover"
APP = "https://app.smartercontact.com"
LOGIN_URL = f"{APP}/login"
NOISE = ("google", "hubspot", "hubapi", "stripe", "clarity", "sentry", "doubleclick",
         "reddit", "firebaseinstallations", "firebaseremoteconfig", "facebook", "intercom")


async def _context(pw, headless: bool):
    """The persistent profile, in real Chrome when it is installed.

    Real Chrome without the automation flag matters for Google sign-in, which
    refuses browsers it recognises as automated.
    """
    PROFILE.mkdir(parents=True, exist_ok=True)
    kw = dict(user_data_dir=str(PROFILE), headless=headless, viewport=None,
              args=["--disable-blink-features=AutomationControlled"])
    try:
        return await pw.chromium.launch_persistent_context(channel="chrome", **kw)
    except Exception:
        return await pw.chromium.launch_persistent_context(**kw)


def _signed_in(url: str) -> bool:
    return url.startswith(APP) and "/login" not in url and "/signup" not in url


async def login(timeout_min: int) -> int:
    async with async_playwright() as pw:
        ctx = await _context(pw, headless=False)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto(LOGIN_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(4000)
        if _signed_in(page.url):
            print(f"already signed in: {page.url}")
        else:
            email, pwd = os.environ.get("SMARTER_CONTACT_EMAIL"), os.environ.get("SMARTER_CONTACT_PASSWORD")
            if email and pwd:
                await page.fill('input[name="email"]', email)
                await page.fill('input[name="password"]', pwd)
                box = page.locator('input[name="isAgree"]')
                if await box.count() and not await box.is_checked():
                    await box.check(force=True)
                await page.click('button[type="submit"]')
                print("submitted the saved credentials; finish any verification in the window")
            else:
                print(f"SIGN IN in the browser window that just opened (waiting up to {timeout_min} min)")
            deadline = time.time() + timeout_min * 60
            while time.time() < deadline and not _signed_in(page.url):
                await asyncio.sleep(2)
        if not _signed_in(page.url):
            print(f"FAILED: still on {page.url} after {timeout_min} min")
            await ctx.close()
            return 2
        await page.wait_for_timeout(5000)  # let the app shell and token refresh settle
        DISCOVER_DIR.mkdir(parents=True, exist_ok=True)
        await page.screenshot(path=str(DISCOVER_DIR / "00_after_login.png"))
        print(f"SIGNED IN: {page.url}  (session saved in {PROFILE.relative_to(ROOT)})")
        await ctx.close()
        return 0


async def discover() -> int:
    """Map the app: every nav link, what each page holds, and the API calls it makes.

    Request URLs and methods only. Headers and bodies carry the session token and
    are never written to disk.
    """
    DISCOVER_DIR.mkdir(parents=True, exist_ok=True)
    calls: list[dict] = []
    async with async_playwright() as pw:
        ctx = await _context(pw, headless=True)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        def on_request(r):
            if r.resource_type in ("xhr", "fetch") and not any(n in r.url for n in NOISE):
                calls.append({"page": page.url, "method": r.method, "url": r.url.split("?")[0]})
        page.on("request", on_request)
        await page.goto(APP, wait_until="domcontentloaded")
        await page.wait_for_timeout(6000)
        if not _signed_in(page.url):
            print("NOT SIGNED IN: run --phase login first")
            await ctx.close()
            return 2
        links = await page.evaluate("""() => [...document.querySelectorAll('a[href]')]
            .map(a => ({text: (a.innerText || a.getAttribute('aria-label') || '').trim().slice(0, 40),
                        href: a.getAttribute('href')}))
            .filter(l => l.href && l.href.startsWith('/'))""")
        routes = sorted({l["href"] for l in links})
        report = {"start": page.url, "nav": links, "pages": {}}
        for i, route in enumerate(routes[:40]):
            try:
                await page.goto(APP + route, wait_until="domcontentloaded")
                await page.wait_for_timeout(3500)
            except Exception as e:
                report["pages"][route] = {"error": str(e)[:120]}
                continue
            shot = DISCOVER_DIR / f"{i + 1:02d}_{route.strip('/').replace('/', '_') or 'root'}.png"
            await page.screenshot(path=str(shot), full_page=True)
            report["pages"][route] = await page.evaluate("""() => ({
                title: document.title,
                headings: [...document.querySelectorAll('h1,h2,h3')].map(h => h.innerText.trim()).filter(Boolean).slice(0, 15),
                buttons: [...document.querySelectorAll('button,[role=button]')].map(b => (b.innerText || b.getAttribute('aria-label') || '').trim()).filter(Boolean).slice(0, 40),
                inputs: [...document.querySelectorAll('input,textarea,select')].map(e => ({type: e.type, name: e.name, ph: e.placeholder})).slice(0, 30),
            })""")
        await ctx.close()
    report["api_calls"] = sorted({(c["method"], c["url"]) for c in calls})
    out = DISCOVER_DIR / "discover.json"
    out.write_text(json.dumps(report, indent=1, default=list), encoding="utf-8")
    hosts = sorted({u.split("/")[2] for _, u in report["api_calls"]})
    print(f"{len(routes)} routes, {len(report['api_calls'])} distinct API calls on {hosts}")
    print(f"wrote {out.relative_to(ROOT)} and {len(report['pages'])} screenshots")
    return 0


GQL = "https://api.smartercontact.com/gql"
INTROSPECT = """{ __schema { mutationType { fields { name args { name type { kind name
  ofType { kind name ofType { kind name ofType { kind name } } } } } } }
  queryType { fields { name } } } }"""


class Session:
    """The app's own GraphQL session, borrowed from a live page.

    Smarter Contact's web app sends every read and write as POST /gql. The auth
    header is lifted from the page's first real request and kept in memory only,
    so this module never stores a token; it lives as long as the browser does.
    """

    def __init__(self, ctx, page):
        self.ctx, self.page, self.headers, self.ops = ctx, page, None, {}
        page.on("request", self._on_request)

    def _on_request(self, r):
        if not r.url.startswith(GQL) or r.method != "POST":
            return
        if self.headers is None:
            self.headers = {k: v for k, v in r.headers.items()
                            if k.lower() in ("authorization", "content-type", "x-api-key",
                                             "apollographql-client-name", "x-user-id",
                                             "x-account-id", "x-sub-account-id")}
        try:
            body = r.post_data_json or {}
        except Exception:
            return
        for op in body if isinstance(body, list) else [body]:
            name = op.get("operationName") or "(anonymous)"
            self.ops.setdefault(name, {"query": op.get("query", ""),
                                       "variables": op.get("variables"), "pages": set()})
            self.ops[name]["pages"].add(self.page.url.replace(APP, ""))

    async def call(self, query: str, variables: dict | None = None, name: str | None = None) -> dict:
        if not self.headers:
            raise RuntimeError("no GraphQL request seen yet, so no session header to borrow")
        body = {"query": query, "variables": variables or {}}
        if name:
            body["operationName"] = name
        resp = await self.ctx.request.post(GQL, headers=self.headers, data=json.dumps(body))
        return await resp.json()


class TokenSession:
    """The agent's own Smarter Contact login, for when the browser profile is signed out.

    The Firebase refresh token in .env that sms_agent.sc_client (and the Fly box)
    already reply with. Same call() contract as Session: the RAW response, errors
    included, because the build's validation probe depends on reading a rejection.
    A GraphQL validation error arrives as HTTP 400 with its JSON body, so a 4xx
    is returned as data rather than raised.
    """

    def __init__(self):
        sys.path.insert(0, str(ROOT / "src"))
        from sms_agent import sc_client
        if not sc_client.configured():
            raise RuntimeError("there is no agent login in .env (SMARTERCONTACT_REFRESH_TOKEN / _FIREBASE_API_KEY)")
        self.sc, self.headers = sc_client, None

    def _post(self, body: dict) -> dict:
        req = urllib.request.Request(self.sc.GQL, data=json.dumps(body).encode(), headers={
            "content-type": "application/json", "authorization": f"Bearer {self.sc._id_token()}"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return json.loads(raw)
            except ValueError:
                return {"errors": [{"message": f"HTTP {e.code}: {raw[:200]!r}"}]}

    async def call(self, query: str, variables: dict | None = None, name: str | None = None) -> dict:
        body = {"query": query, "variables": variables or {}}
        if name:
            body["operationName"] = name
        return await asyncio.to_thread(self._post, body)


async def gql_map() -> int:
    """Record the GraphQL operations behind every page this setup touches."""
    DISCOVER_DIR.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as pw:
        ctx = await _context(pw, headless=True)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        s = Session(ctx, page)
        visited, queue = set(), ["/campaigns", "/messenger/quick-replies", "/workflows",
                                 "/contacts", "/settings/notifications", "/connect-si"]
        while queue and len(visited) < 30:
            route = queue.pop(0)
            if route in visited:
                continue
            visited.add(route)
            await page.goto(APP + route, wait_until="domcontentloaded")
            await page.wait_for_timeout(4000)
            if not _signed_in(page.url):
                print("NOT SIGNED IN: run --phase login first")
                await ctx.close()
                return 2
            for href in await page.evaluate(
                    "() => [...document.querySelectorAll('a[href]')].map(a => a.getAttribute('href'))"):
                if href and href.startswith(("/campaigns", "/settings", "/workflows", "/contacts",
                                             "/messenger/quick")) and href not in visited:
                    queue.append(href)
        schema = await s.call(INTROSPECT)
        await ctx.close()
    ops = {k: {**v, "pages": sorted(v["pages"])} for k, v in s.ops.items()}
    (DISCOVER_DIR / "gql_ops.json").write_text(json.dumps(ops, indent=1), encoding="utf-8")
    (DISCOVER_DIR / "gql_schema.json").write_text(json.dumps(schema, indent=1), encoding="utf-8")
    muts = ((schema.get("data") or {}).get("__schema") or {}).get("mutationType") or {}
    print(f"visited {len(visited)} routes: {sorted(visited)}")
    print(f"{len(ops)} operations seen; introspection "
          f"{'returned ' + str(len(muts.get('fields') or [])) + ' mutations' if muts else 'REFUSED: ' + json.dumps(schema)[:200]}")
    return 0


READS = ["Subscriptions", "FindMergeTags", "FindLabels", "FindMacros", "FindMessageTemplates",
         "FindContactWorkflows", "FindCrmIntegrations", "FindGroupsList", "FindKeywordTemplates"]
WRITE_HINT = re.compile(r"(MessageTemplate|Macro|Label|ContactWorkflow|CrmIntegration|"
                        r"Suppression|ResponseRemoval|Filtration|KeywordTemplate|Group)", re.I)


def _documents(js: str) -> list[str]:
    """GraphQL documents compiled into a JS bundle, as their source text.

    Apollo/graphql-tag keeps each document's source in the bundle; it appears
    either as a template literal or as an escaped string in loc.source.body.
    """
    out = []
    for m in re.finditer(r"(mutation|query|fragment)\s+\w+[\s\S]{0,4000}?", js):
        start = m.start()
        depth, i, seen = 0, start, False
        while i < len(js) and i < start + 6000:
            c = js[i]
            if c == "{":
                depth, seen = depth + 1, True
            elif c == "}":
                depth -= 1
                if seen and depth == 0:
                    out.append(js[start:i + 1].replace("\\n", "\n"))
                    break
            i += 1
    return out


async def inspect() -> int:
    """What the account holds now, and the exact write operations the app uses."""
    ops = json.loads((DISCOVER_DIR / "gql_ops.json").read_text(encoding="utf-8"))
    async with async_playwright() as pw:
        ctx = await _context(pw, headless=True)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        s = Session(ctx, page)
        scripts: set[str] = set()
        page.on("response", lambda r: scripts.add(r.url) if r.request.resource_type == "script"
                and "smartercontact" in r.url else None)
        for route in ("/campaigns/message-template", "/messenger/quick-replies", "/workflows/all",
                      "/contacts/all-contacts", "/connect-si/connected-apps"):
            await page.goto(APP + route, wait_until="domcontentloaded")
            await page.wait_for_timeout(3500)
        state = {}
        for name in READS:
            if name not in ops:
                state[name] = {"skipped": "operation not seen"}
                continue
            state[name] = await s.call(ops[name]["query"], ops[name]["variables"], name)
        docs: dict[str, str] = {}
        for url in sorted(scripts):
            try:
                js = await (await ctx.request.get(url)).text()
            except Exception:
                continue
            for d in _documents(js):
                head = d.split("(")[0].split("{")[0].strip()
                if head.startswith("mutation") and WRITE_HINT.search(head):
                    docs.setdefault(head, d)
        await ctx.close()
    (DISCOVER_DIR / "state.json").write_text(json.dumps(state, indent=1), encoding="utf-8")
    (DISCOVER_DIR / "mutations.json").write_text(json.dumps(docs, indent=1), encoding="utf-8")
    print(f"read {len(state)} collections from {len(scripts)} app scripts; "
          f"{len(docs)} relevant mutations found")
    for head in sorted(docs):
        print("  " + head)
    return 0


async def probe(names: list[str], inputs: list[str]) -> int:
    """Learn an input type's fields from Apollo's validation errors.

    Introspection is off, but Apollo validates variables BEFORE any resolver
    runs, so an incomplete input returns the missing field names and creates
    nothing. Each probe is one of `inputs` (a JSON object) per mutation name.
    """
    docs = json.loads((DISCOVER_DIR / "mutations.json").read_text(encoding="utf-8"))
    async with async_playwright() as pw:
        ctx = await _context(pw, headless=True)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        s = Session(ctx, page)
        await page.goto(APP + "/campaigns/message-template", wait_until="domcontentloaded")
        await page.wait_for_timeout(4000)
        for name in names:
            doc = docs[f"mutation {name}"]
            # Strip the selection set down to __typename so fragments are not needed.
            head = doc[: doc.index("{")]
            field = re.search(r"\{\s*(\w+)\s*\(([^)]*)\)", doc)
            slim = f"{head}{{ {field.group(1)}({field.group(2)}) {{ __typename }} }}"
            for raw in inputs:
                r = await s.call(slim, {"input": json.loads(raw), "id": "probe"}, name)
                msgs = [e.get("message", "") for e in r.get("errors") or []]
                print(f"{name} {raw[:60]} -> {msgs[:6] if msgs else json.dumps(r)[:200]}")
        await ctx.close()
    return 0


# ---------------------------------------------------------------------------
# build: create what smarter_contact_templates.py defines, through the app's own
# GraphQL. Every contract below was read from the live app on 2026-10-20 (input
# shapes from Apollo validation errors, enums from the app bundle), not guessed.
#
# Three rules make it safe to run against a live account:
#   1. Existing objects are matched by name and never touched. Rerunning only
#      creates what is missing.
#   2. Every payload is checked by the SERVER first: it is sent once with an
#      extra unknown field, so Apollo rejects it at validation (nothing is
#      written) and the only error must be that field. Anything else means the
#      payload is wrong and the run stops before writing.
#   3. Every write is read back and compared.
M_LABEL = "mutation CreateLabel($input: CreateLabelInput!) { createLabel(input: $input) { id title } }"
M_MACRO = "mutation CreateMacro($input: CreateMacroInput!) { createMacro(input: $input) { id name content } }"
M_TEMPLATE = ("mutation CreateMessageTemplate($input: CreateMessageTemplateInput!) { "
              "createMessageTemplate(input: $input) { id name template { content messageType optOut segments } } }")
M_TEMPLATE_UPDATE = ("mutation UpdateMessageTemplate($input: UpdateMessageTemplateInput!, "
                     "$updateMessageTemplateId: String!) { updateMessageTemplate(input: $input, "
                     "id: $updateMessageTemplateId) { id name template { content messageType optOut segments } } }")
M_WORKFLOW = ("mutation CreateContactWorkflow($input: UpsertContactWorkflowInput!) { "
              "createContactWorkflow(input: $input) { id name sequences { delay content } } }")
M_WORKFLOW_UPDATE = ("mutation UpdateContactWorkflow($input: UpsertContactWorkflowInput!, "
                     "$updateContactWorkflowId: String!) { updateContactWorkflow(input: $input, "
                     "id: $updateContactWorkflowId) { id name sequences { delay content } } }")
DAY_MS = 86_400_000


def _delay_ms(days: int) -> int:
    """A workflow wait as the app stores it: days in ms plus 1-30 random minutes,
    the same jitter its own editor adds so a batch does not all fire at once."""
    import random
    return days * DAY_MS + random.randint(1, 30) * 60_000 if days else 0


M_CRM = ("mutation UpdateCrmIntegration($input: UpdateCrmIntegrationInput!, $id: String!) { "
         "updateCrmIntegration(input: $input, id: $id) { id } }")
Q_CRM = ("query getCrmIntegration($id: String!) { getCrmIntegration(id: $id) { id name status type "
         "provider { apiKey sendNotes labelsPropertyStatuses { label { id title } status } "
         "labelsPhoneStatuses { label { id title } status } labelsPhoneTags { id title } } } }")
LABEL_COLOR = "#cce5ff"  # the color the account's own custom labels use
DRY_FIELD = "zzzDryRunProbe"


def _items(resp: dict) -> list:
    if resp.get("errors"):
        raise RuntimeError(json.dumps(resp["errors"])[:300])
    v = next(iter((resp.get("data") or {}).values()), None)
    return (v.get("items") or []) if isinstance(v, dict) else (v or [])


async def _list(s: Session, ops: dict, name: str) -> list:
    variables = json.loads(json.dumps(ops[name]["variables"] or {}))
    variables.setdefault("pagination", {})["limit"] = 200
    resp = await s.call(ops[name]["query"], variables, name)
    items = _items(resp)
    v = next(iter(resp["data"].values()))
    total = v.get("total") if isinstance(v, dict) else None
    if total is not None and total > len(items):
        raise RuntimeError(f"{name}: read {len(items)} of {total}; refusing to guess the rest")
    return items


async def _server_validates(s: Session, doc: str, variables: dict) -> list[str]:
    """Errors the server finds in this payload, without writing anything."""
    probe = json.loads(json.dumps(variables))
    probe["input"][DRY_FIELD] = True
    resp = await s.call(doc, probe)
    errs = [e.get("message", "") for e in resp.get("errors") or []]
    if not errs:
        raise RuntimeError("dry-run probe was ACCEPTED; the validation trick no longer holds")
    # Match ONLY the probe's own "not defined" error. Apollo echoes the whole
    # input value in every message, so filtering on the bare field name also
    # swallowed real errors about other fields (how an UpdateMessageTemplate
    # payload passed the dry run and failed on commit, 2026-10-21).
    mine = f'Field "{DRY_FIELD}" is not defined'
    return [e for e in errs if mine not in e]


async def build(commit: bool, sender: str, land: bool = False) -> int:
    sys.path.insert(0, str(ROOT / "src"))
    import smarter_contact_templates as T
    ops = json.loads((DISCOVER_DIR / "gql_ops.json").read_text(encoding="utf-8"))
    created, skipped, problems = [], [], []

    async with async_playwright() as pw:
        ctx = await _context(pw, headless=True)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        s = Session(ctx, page)
        await page.goto(APP + "/campaigns/message-template", wait_until="domcontentloaded")
        for _ in range(10):  # a signed-in page fires its first GraphQL call within a few seconds
            await page.wait_for_timeout(1000)
            if s.headers:
                break
        if not (_signed_in(page.url) and s.headers):
            # A signed-out profile renders the login form AT the app URL, so the URL alone
            # read "signed in" and the run died later on a missing header (2026-11-10).
            await ctx.close()
            ctx = None
            try:
                s = TokenSession()
            except RuntimeError as e:
                print(f"NOT SIGNED IN: the browser profile is signed out and {e}. Run --phase login first")
                return 2
            print("browser profile is signed out; building on the agent's Smarter Contact login instead")

        async def write(kind: str, name: str, doc: str, variables: dict, check) -> dict | None:
            bad = await _server_validates(s, doc, variables)
            if bad:
                problems.append(f"{kind} {name}: server rejects the payload: {bad[:2]}")
                return None
            if not commit:
                created.append(f"(dry) {kind}: {name}")
                return None
            resp = await s.call(doc, variables)
            if resp.get("errors"):
                problems.append(f"{kind} {name}: {json.dumps(resp['errors'])[:200]}")
                return None
            obj = next(iter(resp["data"].values()))
            miss = check(obj)
            if miss:
                problems.append(f"{kind} {name}: read-back mismatch: {miss}")
            created.append(f"{kind}: {name}")
            return obj

        # 1. Labels. Created only where the account lacks one (case-insensitive).
        labels = {l["title"].casefold(): l for l in await _list(s, ops, "FindLabels")}
        for title, *_ in T.LABELS:
            if title.casefold() in labels:
                skipped.append(f"label: {title} (exists)")
                continue
            obj = await write("label", title, M_LABEL,
                              {"input": {"title": title, "color": LABEL_COLOR}},
                              lambda o, t=title: None if o.get("title") == t else o.get("title"))
            if obj:
                labels[title.casefold()] = obj

        # 2. DataSift mapping: ADD missing rows, never change or drop an existing one.
        crm = [c for c in await _list(s, ops, "FindCrmIntegrations") if c.get("type") == "REISIFT"]
        if len(crm) != 1:
            problems.append(f"expected one DataSift integration, found {len(crm)}; mapping untouched")
        else:
            cur = (await s.call(Q_CRM, {"id": crm[0]["id"]}))["data"]["getCrmIntegration"]
            prov = cur["provider"]
            props = [{"labelId": r["label"]["id"], "status": r["status"]} for r in prov["labelsPropertyStatuses"] or []]
            phones = [{"labelId": r["label"]["id"], "status": r["status"]} for r in prov["labelsPhoneStatuses"] or []]
            tags = [t["id"] for t in prov["labelsPhoneTags"] or []]
            adds = []
            for title, _, pstatus, phstatus, tag in T.LABELS:
                lab = labels.get(title.casefold())
                if not lab:
                    if commit:
                        problems.append(f"mapping {title}: label missing, row skipped")
                    else:
                        adds.append(f"(after label is created) {title}")
                    continue
                for rows, status, kind in ((props, pstatus, "status"), (phones, phstatus, "phone status")):
                    if not status:
                        continue
                    have = [r for r in rows if r["labelId"] == lab["id"]]
                    if have and have[0]["status"] != status:
                        problems.append(f"mapping {title}: already maps {kind} to {have[0]['status']}, left alone")
                    elif not have:
                        rows.append({"labelId": lab["id"], "status": status})
                        adds.append(f"{title} -> {kind} {status}")
                if tag and lab["id"] not in tags:
                    tags.append(lab["id"])
                    adds.append(f"{title} -> phone tag")
            if adds:
                variables = {"id": crm[0]["id"], "input": {
                    "name": cur["name"], "status": cur["status"],
                    "provider": {"apiKey": prov["apiKey"], "sendNotes": prov["sendNotes"],
                                 "labelsPropertyStatuses": props, "labelsPhoneStatuses": phones,
                                 "labelsPhoneTags": tags}}}
                bad = await _server_validates(s, M_CRM, variables)
                if bad:
                    problems.append(f"mapping: server rejects the payload: {bad[:2]}")
                elif not commit:
                    created += [f"(dry) mapping: {a}" for a in adds]
                else:
                    resp = await s.call(M_CRM, variables)
                    if resp.get("errors"):
                        problems.append(f"mapping: {json.dumps(resp['errors'])[:200]}")
                    else:
                        back = (await s.call(Q_CRM, {"id": crm[0]["id"]}))["data"]["getCrmIntegration"]["provider"]
                        got = {(r["label"]["id"], r["status"]) for r in back["labelsPropertyStatuses"] + back["labelsPhoneStatuses"]}
                        want = {(r["labelId"], r["status"]) for r in props + phones}
                        if want - got or back["apiKey"] != prov["apiKey"] or {t["id"] for t in back["labelsPhoneTags"] or []} != set(tags):
                            problems.append("mapping: read-back does not match what was sent")
                        created += [f"mapping: {a}" for a in adds]
            else:
                skipped.append("mapping: nothing missing")

        # 3. Quick replies.
        macros = {m["name"].casefold() for m in await _list(s, ops, "FindMacros")}
        for _, name, _, _, body in T.QUICK_REPLIES:
            if name.casefold() in macros:
                skipped.append(f"quick reply: {name} (exists)")
                continue
            content = T.to_sc(body, sender)
            await write("quick reply", name, M_MACRO, {"input": {"name": name, "content": content}},
                        lambda o, c=content: None if o.get("content") == c else "content differs")

        # 4. Message templates. The server's own segment count is the check that
        # every touch really fits one message with the opt-out footer attached.
        # A template whose copy changed in the module is updated in place (same
        # name, same id), so a rewrite never leaves a stale variant in rotation.
        names = {t["name"].casefold(): t for t in await _list(s, ops, "FindMessageTemplates")}
        def _sets():
            for n, arm, named, noname in T._arms():
                yield n, (lambda aud, i, n=n, arm=arm: T.title(n, arm.title(), aud, i)), named, noname
            if land:  # the land lanes (--land): same create, compare and read-back path
                for lane, n, named, noname in T.land_arms():
                    yield n, (lambda aud, i, n=n, lane=lane: T.land_title(lane, n, aud, i)), named, noname

        for n, title_of, named, noname in _sets():
            for aud, pool in (("named", named), ("noname", noname)):
                for i, body in enumerate(pool, 1):
                    title = title_of(aud, i)
                    content = T.to_sc(body, sender)
                    have = names.get(title.casefold())
                    tpl = (have or {}).get("template") or {}
                    if have and tpl.get("content") == content and tpl.get("optOut") == T.SC_OPT_OUT:
                        skipped.append(f"template: {title} (exists, matches)")
                        continue
                    variables = {"input": {"name": title, "type": "SMS", "template": {
                        "content": content, "optOut": T.SC_OPT_OUT,
                        "messageType": "INITIAL" if n == 1 else "FOLLOWUP"}}}
                    check = (lambda o, c=content: None if o["template"]["content"] == c
                             and o["template"]["segments"] == 1
                             and o["template"]["optOut"] == T.SC_OPT_OUT
                             else f"segments={o['template']['segments']} optOut={o['template']['optOut']}")
                    if have:  # `type` is fixed at creation; the update input refuses it
                        upd = {k: v for k, v in variables["input"].items() if k != "type"}
                        await write("template update", title, M_TEMPLATE_UPDATE,
                                    {"input": upd, "updateMessageTemplateId": have["id"]}, check)
                        continue
                    await write("template", title, M_TEMPLATE, variables, check)

        # 5. Contact Workflows. Step days are cumulative in the module; the app
        # stores each step's wait relative to the one before, in MILLISECONDS
        # (its editor shows floor(ms as days)). The first build sent days and
        # every wait read "0 days", so an existing workflow is compared step by
        # step and updated in place when it differs, rather than skipped.
        flows = {w["name"].casefold(): w for w in await _list(s, ops, "FindContactWorkflows")}
        for name, (_, _, steps) in T.WORKFLOWS.items():
            # `days` must be non-empty ([] is refused, not "any day"); Mon-Fri as
            # ISO weekdays 1-5 (verified in the editor). Window = the campaigns'
            # noon-6pm Eastern. No `rule`: these workflows take no reply action.
            seqs, prev = [], 0
            for day, body in steps:
                seqs.append({"content": T.to_sc(body, sender), "delay": _delay_ms(day - prev),
                             "days": [1, 2, 3, 4, 5], "hasCustomTimeRestrictions": True,
                             "timeRange": {"from": {"hour": 12, "minute": 0},
                                           "to": {"hour": 18, "minute": 0}}})
                prev = day
            want = [(q["delay"] // DAY_MS, q["content"]) for q in seqs]
            check = (lambda o, w=want: None if [((q.get("delay") or 0) // DAY_MS, q.get("content"))
                                                for q in o.get("sequences") or []] == w
                     else "steps differ on read-back")
            have = flows.get(name.casefold())
            if have:
                got = [((q.get("delay") or 0) // DAY_MS, q.get("content")) for q in have.get("sequences") or []]
                if got == want:
                    skipped.append(f"workflow: {name} (exists, matches)")
                    continue
                await write("workflow update", name, M_WORKFLOW_UPDATE,
                            {"input": {"name": name, "sequences": seqs},
                             "updateContactWorkflowId": have["id"]}, check)
                continue
            await write("workflow", name, M_WORKFLOW, {"input": {"name": name, "sequences": seqs}}, check)
        if ctx:
            await ctx.close()

    print(f"{'COMMITTED' if commit else 'DRY RUN'}: {len(created)} to create, {len(skipped)} already there")
    for c in created:
        print("  + " + c)
    for p in problems:
        print("  ! " + p)
    if not commit:
        print("Nothing was written. Rerun with --commit.")
    return 1 if problems else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--phase", choices=["login", "discover", "gql", "inspect", "probe", "build"],
                    required=True)
    ap.add_argument("--commit", action="store_true", help="build: write (default is a dry run)")
    ap.add_argument("--sender", default="Adriana", help="build: first name that signs the texts")
    ap.add_argument("--land", action="store_true", help="build: also create the land lot and tract templates")
    ap.add_argument("--names", default="", help="probe: comma-separated mutation names")
    ap.add_argument("--inputs", default="{}", help="probe: '|'-separated JSON inputs")
    ap.add_argument("--timeout", type=int, default=20, help="minutes to wait for sign-in")
    a = ap.parse_args()
    if a.phase == "login":
        return asyncio.run(login(a.timeout))
    if a.phase == "gql":
        return asyncio.run(gql_map())
    if a.phase == "inspect":
        return asyncio.run(inspect())
    if a.phase == "probe":
        return asyncio.run(probe(a.names.split(","), a.inputs.split("|")))
    if a.phase == "build":
        return asyncio.run(build(a.commit, a.sender, a.land))
    return asyncio.run(discover())


if __name__ == "__main__":
    sys.exit(main())
