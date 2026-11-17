# Scraping infrastructure: proxies, gates, browsers

Only relevant if you run the acquisition pipeline. None of the skills need any
of this.

The through-line: **most scraping failures are not code failures.** Before
debugging a parser, prove you can reach the page at all from where you are.

## Diagnose egress before anything else

A site can decide, per IP, that it will not serve you, and it does not have to
say so in a way that looks like blocking.

Verified live against one site, same login, same code, three networks:

| From | What the page did |
|---|---|
| Office IP | **No challenge at all.** Just "You are not permitted to view public notices from this computer at this time" |
| Datacenter proxy | Normal challenge, then the content |
| Residential proxy | Served with no gate |

The office case is the dangerous one: no challenge, no error, no HTTP failure.
It reads as a solver bug and it is a network problem.

**Treat a blocked run as its own outcome.** Ours exits `3` rather than `1`,
because no retry fixes it and a generic failure sends you into the parser.

### Volume matters as much as address

One month of scraping through a single sticky session viewed about **204
notices** before that IP began refusing, and every later job then failed in
about 60 seconds against the same dead address.

Two things fix it, and both are load-bearing:
- rotate the session id **per process**, not per run
- split the work so no single session exceeds the threshold

Then accept that some jobs still hit a burned IP. A retry pass with a fresh
address converges. Do not retry in a tight loop: the pool is small and
addresses need to cool.

## Apify proxy

Resolution order that works: explicit proxy URL, then Apify groups looked up
with the API token, then direct.

### The 407 that looks like bad credentials

**Apify session ids accept only letters, digits, underscore and dot.**

`session-tnpn-fly` returns `407 Proxy Authentication Required`. `tnpn_fly`,
`tnpnfly`, `tnpn.fly` and `tnpn` all return 200, on the same credentials.

A hyphen in a session name is indistinguishable from a rejected password in the
error. Sanitize the session id and log the substitution.

Also: the API token is **not** the proxy password. It is used to look the
password up.

### Group availability is per plan

`RESIDENTIAL` reported `availableCount: 0` on our plan. A datacenter group with
27 US addresses worked. Check availability before assuming a group exists.

## Cloudflare Turnstile

The gate changed from reCAPTCHA to Turnstile on 2026-07-13, and this is the
clearest instance of a moved contract in the whole system.

The config recorded the migration. The solver did not: it still called the
reCAPTCHA method and injected into `g-recaptcha-response`, a field the page no
longer reads. **Every solve was billed and discarded**, and the failure looked
like a flaky gate rather than a wrong integration.

What the working version does:

- selects both the solve method and the response field from one config value,
  so they cannot drift apart
- reads the **sitekey off the live page**, and logs `SITEKEY ROTATED` rather
  than dying quietly when it changes
- **creates the `cf-turnstile-response` input** when the headless widget never
  renders one
- runs the blocking solve **in a thread**, so the browser event loop keeps
  servicing the page while waiting

**The gate is session level.** One solve covers the rest of the run, which
changes the cost model completely: budget per run, not per page.

## Scrapfly

Residential proxy, anti-bot and rendering in one call. `asp=True` plus
`render_js=True` clears most JS walls.

### The trap: a fresh session per scrape

Every Scrapfly scrape gets a **new cookieless ASP.NET session**. So a direct
detail-page fetch lands in a session that never ran a search, and the server
returns an unpopulated shell.

The result is `gate_not_cleared` on every page, taking about three minutes
each, before falling back. It looks like the gate beating you. It is not.

**The fix is to do the whole walk inside one call**: search, then navigate to
the record, in a single scenario. That works and returns real content with no
gate from a residential address.

### Where it is worth it

County records, assessor datalets, genealogy and court pages that block plain
fetches. Hardened people-search aggregators frequently IP-ban it outright, so
do not plan around those.

## Browser automation

Some sites need a real browser and there is no way around it. ASP.NET WebForms
is the clearest case: navigation is `__doPostBack` with ViewState, so an HTTP
client has to hand-manage ViewState and EventValidation for every click.

Two practical notes that cost real time:

- **Wait on `domcontentloaded`, not `networkidle`.** Through a proxy,
  networkidle regularly never settles, and it abandoned a working search after
  30 seconds.
- **Pin the browser image to the client version.** A mismatch fails in ways
  that look like site changes.

## Persist state only after success

Ordering, not code, is the lesson.

Our seen-ID cache used to be written during the scrape. An aborted run then
left notices flagged as handled that were never sent anywhere, and a retry
skipped every one of them permanently. The first egress block did exactly that
to **204 notices**.

Persist the cache **after** the downstream write succeeds, and roll it back on
failure, on dry runs, and on `--no-upload`.

## Zero results is a failure

There is deliberately no inference anywhere that a missing challenge means a
cleared gate. That exact reasoning reported **13 consecutive dead runs as
successful over 19 days**.

Success requires positively seeing the content you came for.
