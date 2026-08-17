# County data portals

The free layer, and the most valuable one. Assessor, recorder and court data is
public, and in most counties it is machine readable if you look past the search
page someone built on top of it.

- **Used by:** `first-market-county-data`, `probate-property-finder`,
  `buyer-prospector`
- **Cost:** free, everywhere in this file

## The general method

Every one of these was found the same way, and it works on portals not listed
here:

1. **Open the portal's own search in a browser with devtools open.** Run the
   search you want by hand.
2. **Watch the network tab.** The page is almost always calling a JSON endpoint
   that you can call directly.
3. **Replay that request.** Copy as cURL, strip it down to what actually
   matters.

The search UI is the thing that blocks you. The endpoint underneath usually
does not.

## The four traps, in order of how much time they cost

### 1. The empty table shell

TPAD (Blount County TN) serves an HTML page whose results table is **an empty
shell**. Its id is `searchResultsTable`, not the `resultsTable` a parser might
guess, and either way it contains no rows.

The rows come from `POST /TPAD/Search/GetSearchResults`, which returns clean
JSON.

Scraping the page returns zero rows, silently, forever. Every downstream lookup
fails, the address stays empty, and validation later drops the record as
"missing address". Nothing anywhere says the parser is looking at the wrong
thing. A Blount probate backfill would have produced about 950 records and zero
usable ones.

### 2. The User-Agent wall

TPAD **403s anything without a browser User-Agent**. Bare `requests` gets
nothing. One header fixes it.

Worth trying first on any portal that returns 403 to code and 200 to a browser.

### 3. The house number comes last

TPAD prints addresses as `"LAKESHORE DR  5705"`.

That fails validation and fails address standardization, so it looks like bad
data rather than a formatting convention. Normalize to `5705 Lakeshore Dr`
before anything downstream sees it.

### 4. The stale path with the fresh file

Franklin County OH publishes owner rolls on an open file server. The **folder
path is stale on purpose**: the newest-looking folder said `2025/07` while the
file inside it had a Last-Modified three days old.

**Check the header, not the path.** And the newer-looking "Outside_User_Files"
tab-delimited appraisal extract has **no owner fields at all**, only values and
situs, so it looks like an upgrade and is useless for this.

## Result caps

Portals truncate, usually without telling you.

The TN public notice site caps **every** result set at 20 pages / 1000 rows,
newest first. A plain 12-month search silently loses its tail, and two of four
searches sat exactly on that ceiling.

**If a result count is suspiciously round, it is a cap.** Chunk the query, by
month or by price band, until each chunk comes back under the ceiling.

## Records that are not online at all

Some counties genuinely do not publish. Knox County TN estate and probate case
files are not online, and deed images sit behind a paid subscription.

That is a phone call or a visit, not a scraping problem, and recognising it
early saves a day. `first-market-county-data` tells you which is which for your
county, and carries FOIA request templates for the rest.

## Name-based joins

Liens and judgments are indexed **against the person**, not the parcel. Zero
percent of lien rows carry a parcel id.

The join is debtor name to the county tax API. Measured hit rate on a full run:
**40 percent**. A 500-name sample read 64 percent only because it was sorted by
lien count, which put the most active debtors first. Beware sorted samples.

**Release filtering is not optional.** Against 12 months of liens there were
27,493 release documents. **8 percent of lead debtors had every lien already
satisfied** and were dead leads. Compute active as recorded minus released, and
match at instrument level: a name-only match tells you that person had
something released, not that this lien was.

## Open ArcGIS layers

Several counties publish parcels with owner fields as ArcGIS feature services,
queryable with no key. Licking County OH serves 100,000 rows per call and
inlines the last three transfers.

Ohio's statewide OGRIP layer **strips owner fields** from the public view, so
county-level layers are the ones worth finding.

Six Columbus-metro counties came to **811,146 owner rows for $0**.

## Vendor search UIs

Schneider Beacon, DEVNET Pivot and similar are bot-walled and, in every case we
hit, unnecessary. The county publishes the same data elsewhere.
