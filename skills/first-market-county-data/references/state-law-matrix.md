# State Law Matrix for County Distress Data

The process determines where the data lives. Before you build a pull for any county, you need three facts about that state: how foreclosures move (judicial or nonjudicial), what gets sold at a tax sale (lien, deed, or redeemable deed), and which public-records statute governs your request and its response window. This file is the works-for-any-county reference for all three, plus the edge cases that quietly break a data pull.

Treat every classification and number here as a starting map, not legal advice. State buckets and redemption periods change with legislation, and the 2023 U.S. Supreme Court ruling in Tyler v. Hennepin County has already pushed several states to amend their tax-sale and surplus rules. Verify the current statute and the specific county office before you build a pipeline or quote a window to a seller.

## Table of Contents

- [How to Read This](#how-to-read-this)
- [Foreclosure Process by State](#foreclosure-process-by-state-where-the-data-lives)
- [Tax Sale Mechanism by State](#tax-sale-mechanism-by-state-lien-deed-redeemable)
- [Public Records Laws by State](#state-public-records-laws-for-county-distress-data)
- [Foreclosure Timeline Cadence Note](#foreclosure-timeline-cadence-note)
- [Verify Locally](#verify-locally-read-this-before-acting)
- [Related References](#related-references)

## How to Read This

The single most useful rule in this file:

- Judicial state: the lender must sue to foreclose. Pull the COURT lis pendens or foreclosure complaint docket.
- Nonjudicial state (power of sale): the lender forecloses out of court. Pull the RECORDER-filed Notice of Default or the published trustee sale notice (or the state public-notice feed).
- Either state: the lender picks the instrument, so the path is decided case by case. Check BOTH offices until you confirm which path your target county actually uses.

Classify by COMMON PRACTICE, not by what the statute technically permits. Most states allow both a judicial and a nonjudicial path on paper. What predicts where the record actually shows up is the path lenders normally take in that state, which is what the tables below encode.

This file does not repeat the foreclosure include and exclude filter phrases (the trustee-sale language that separates real first-to-market foreclosures from noise). Those live in the main skill and in research-prompts.md. This file answers the prior question: which office holds the record in the first place.

## Foreclosure Process by State: Where the Data Lives

The foreclosure process determines WHERE the first public record of a default appears, which is the whole game for first-to-market data.

- Judicial states: the lender must sue. The first public signal is a court-filed lis pendens or foreclosure complaint. Pull the courthouse civil docket.
- Nonjudicial states (power of sale): the lender forecloses out of court under a deed-of-trust power-of-sale clause. The first public signal is a recorder-filed Notice of Default or a published trustee sale notice. Pull the recorder feed or the state public-notice site (for example tnpublicnotice.com in Tennessee).

Big caveat: most states technically permit both a judicial and a nonjudicial path. The tables below classify by COMMON PRACTICE, because that is what predicts where the record actually shows up. In the nine "either" states the lender picks the instrument, so check both offices.

### Commonly Nonjudicial (pull the recorder / public-notice feed)

| State | Instrument | Notes |
|-------|-----------|-------|
| Alabama | Either | Lender chooses mortgage or deed of trust |
| Alaska | Deed of trust | |
| Arizona | Either | Commonly deed of trust |
| Arkansas | Either | |
| California | Deed of trust | Trustee sale, about 4 month timeline |
| Colorado | Deed of trust | Routed through county Public Trustee (Notice of Election and Demand) |
| District of Columbia | Deed of trust | |
| Georgia | Security deed (deed to secure debt) | Nonjudicial is the norm; security instrument is a security deed under O.C.G.A. 44-14, not a deed of trust |
| Hawaii | Mortgage | Both paths used; nonjudicial historically common but lenders increasingly choose judicial because borrowers can invoke the Mortgage Foreclosure Dispute Resolution program or convert to judicial. Check both offices for the target county |
| Idaho | Deed of trust | |
| Maryland | Either | Power of sale / assent-to-decree routed through Circuit Court |
| Massachusetts | Deed of trust | Statutory power of sale |
| Michigan | Either | Foreclosure by advertisement |
| Minnesota | Deed of trust | |
| Mississippi | Deed of trust | |
| Missouri | Deed of trust | |
| Montana | Either | |
| Nevada | Deed of trust | |
| New Hampshire | Deed of trust | |
| North Carolina | Deed of trust | |
| Oregon | Deed of trust | |
| Rhode Island | Deed of trust | |
| South Dakota | Either | |
| Tennessee | Deed of trust | Substitute or successor trustee sale notice is the FTM signal |
| Texas | Deed of trust | Power of sale, fast timeline |
| Utah | Deed of trust | |
| Virginia | Deed of trust | |
| Washington | Deed of trust | |
| West Virginia | Deed of trust | |
| Wyoming | Deed of trust | |

### Commonly Judicial (pull the court lis pendens / complaint docket)

| State | Instrument | Notes |
|-------|-----------|-------|
| Connecticut | Mortgage | Strict foreclosure allowed (court can transfer title, no sale) |
| Delaware | Mortgage | |
| Florida | Mortgage | High-volume lis pendens docket |
| Illinois | Either | Judicial in practice |
| Indiana | Mortgage | |
| Iowa | Mortgage | Nonjudicial path exists but judicial is the norm |
| Kansas | Mortgage | |
| Kentucky | Either | Judicial in practice |
| Louisiana | Mortgage | All foreclosures judicial, common path is executory process |
| Maine | Mortgage | Judicial by civil action (14 M.R.S. 6321) |
| Nebraska | Deed of trust | Both permitted, judicial common |
| New Jersey | Mortgage | |
| New Mexico | Mortgage | Home loans must be judicially foreclosed; deeds of trust are rare on residential |
| New York | Mortgage | |
| North Dakota | Mortgage | |
| Ohio | Mortgage | |
| Oklahoma | Mortgage | Judicial is the common path; nonjudicial power-of-sale exists but borrower may opt back into judicial (homestead election). Pull the court docket |
| Pennsylvania | Mortgage | |
| South Carolina | Mortgage | |
| Vermont | Mortgage | Strict foreclosure allowed |
| Wisconsin | Mortgage | |

### The "Either" States (lender picks the instrument: check BOTH offices)

Alabama, Arizona, Arkansas, Illinois, Kentucky, Maryland, Michigan, Montana, South Dakota. In these states the recorded instrument's power-of-sale clause decides whether foreclosure goes nonjudicial. Pull both the recorder trustee-sale feed and the court docket until you confirm which path your target county uses.

These nine are the ones that quietly cost you leads. A user who checks only the recorder in Kentucky (which runs judicial in practice) or only the court docket in Arkansas (which runs nonjudicial in practice) misses half the file. When in doubt in an either state, pull both for one cycle and see which office the live filings land in, then settle on that source.

### Edge Cases Worth Flagging

- Strict foreclosure (CT, VT): the court can transfer title straight to the lender with no auction. The FTM signal is still the court filing, but there may be no sale event to track. Do not wait for a published auction date that may never come, work the court filing itself.
- Louisiana executory process: judicial, but a confession-of-judgment mortgage lets the lender file without prior notice. Docket terminology differs from a standard complaint, so search the executory-process language, not just "foreclosure complaint."
- Colorado Public Trustee: nonjudicial, but the trustee is a county government office, not a private party. The Notice of Election and Demand is filed there. Pull the county Public Trustee, not a private trustee feed.

### Why This Matters for Data Pulls

Nonjudicial foreclosures move fast (roughly 60-180 days), so the marketing window is short and the list churns often: re-pull frequently. Judicial foreclosures can run well over 1,000 days in slow jurisdictions, so the same lead stays actionable for a long time. Match your pull cadence to the process type. Foreclosure statutes change, so confirm against the current state statute before building a county pipeline.

Sourcing note: classifications are cross-referenced across multiple 50-state foreclosure surveys. Where simplified charts conflict (notably Maryland and Massachusetts, which a few mislabel as judicial), the tables follow common-practice consensus. Confirm against the current state statute before building a county pipeline.

## Tax Sale Mechanism by State (Lien, Deed, Redeemable)

There are three core tax-sale mechanisms. Knowing which one your state uses tells you what is actually being sold, how long the prior owner can buy it back, and where to find the list.

| Mechanism | What is sold | Redemption after sale | Who you can market to |
|-----------|--------------|-----------------------|------------------------|
| Tax lien | A lien certificate (the debt), not the property | Yes, set by statute. Owner keeps the property if they redeem | Owner is still in title and motivated. Best window |
| Tax deed | The deed (the property) at auction | Usually none. Bidder becomes owner at sale | Owner loses the property at sale. Market before the sale |
| Redeemable deed (hybrid) | A deed at auction, but subject to a redemption period | Yes, with a penalty paid to the buyer | Owner can still redeem. Both owner and deed-holder are leads |

### Tax Lien States (sell the debt, owner has a redemption window)

Alabama, Arizona, Colorado, District of Columbia, Illinois, Indiana, Iowa, Kentucky, Louisiana, Maryland, Mississippi, Missouri, Montana, Nebraska, New Jersey, New York, Rhode Island, South Carolina, South Dakota, Vermont, West Virginia, Wyoming. (Florida runs a lien sale first, then a deed sale. See hybrids.)

Typical redemption notes (verify locally): Maryland about 6 months, Indiana, Kentucky, Missouri, Rhode Island, South Carolina, Vermont about 1 year, Iowa about 1 year 9 months to 2 years, New Jersey and Mississippi about 2 years, Alabama, Arizona, Colorado, Montana, Nebraska about 3 years, West Virginia about 18 months, Wyoming about 4 years, Illinois about 2-3 years.

### Tax Deed States (sell the property, usually no post-sale redemption)

Alaska, Arkansas, California, Delaware, Idaho, Kansas, Maine, Massachusetts, Michigan, Minnesota, Nevada, New Hampshire, New Mexico, North Carolina, North Dakota, Oklahoma, Oregon, Pennsylvania, Utah, Virginia, Washington, Wisconsin. Most carry no redemption after the sale, so the only marketing window is before the auction.

### Redeemable Deed (Hybrid) States (deed sold, but redeemable with a penalty)

Most consistently cited: Connecticut, Georgia, Tennessee, Texas.

- Texas: 6 months for most property, 2 years for homestead and agricultural, 25 percent penalty in year one.
- Georgia: deed recorded at sale, at least 1-year redemption (12 months from sale), with a 20 percent premium in the first year plus an additional 10 percent for each year or fraction thereafter (30 percent by year two, 40 percent by year three, and so on) per O.C.G.A. 48-4-42.
- Tennessee (home market): 1-year redemption (T.C.A. 67-5-2701).
- Connecticut: about 6 months (60 days for abandoned property).

Some sources also place Delaware, Hawaii, Louisiana, Pennsylvania, and South Carolina in the redeemable or hybrid group. This bucket is the least standardized, so confirm the statute.

### Genuine Hybrids (do not fit one bucket)

- Florida: counties hold a tax lien certificate sale first (about a 2-year redemption on the lien), then unredeemed certificates go to a tax deed auction with no further redemption (F.S. 197.502). Two distinct sale events, two distinct windows: market the owner before the certificate sale, and watch the deed-application back end after the redemption period lapses.
- Ohio: some counties run lien certificate sales and others run deed foreclosure auctions, so classification is county by county. Confirm the mechanism for each target county before you assume a window.

### Where the List Is Published

| Mechanism | Usual office holding the list | Publication |
|-----------|-------------------------------|-------------|
| Tax lien | County Treasurer or Tax Collector | Newspaper legal notice, often once a week for 3-4 successive weeks before the sale |
| Tax deed | County Treasurer or Tax Collector (some states route through the clerk) | Newspaper legal notice, same cadence |
| Redeemable deed | County Treasurer or Tax Collector. In Tennessee, the Clerk and Master of the Chancery Court (the County Trustee handles delinquent collection before suit) | Newspaper legal notice (Tennessee: T.C.A. 67-5-2502, Notice of Sale of Land) |

Practical sourcing: in most states the delinquent and tax-sale list lives with the County Treasurer or Tax Collector and is also published as a newspaper legal notice for several successive weeks before the sale. In Tennessee the auction is conducted by the Clerk and Master of the Chancery Court after the County Trustee refers unpaid accounts to suit, so the published sale list (T.C.A. 67-5-2502, Notice of Sale of Land) comes from the Clerk and Master, not the Trustee. Where a state routes deeds through the clerk, ask the clerk for the published list and its release schedule.

## State Public Records Laws for County Distress Data

Use the right statute citation in your records request and set expectations to the correct response window. These are the major REI states. For any state not listed, cite that state's public records or sunshine law by name and treat the response window as "reasonable time, commonly 10 business days." Citations below were verified against state statute sites and primary sources.

| State | Statute Name | Citation | Response Window | Notes |
|-------|--------------|----------|-----------------|-------|
| TN | Tennessee Public Records Act | T.C.A. 10-7-503 | 7 business days | Produce, deny in writing, or give a written timeline. Copy fee about $0.15/page. No charge to inspect. |
| FL | Public Records Law (Sunshine Law) | F.S. Ch. 119 (production at 119.07) | Prompt, reasonable time (no fixed day count) | Copy fee up to $0.15 per one-sided letter page. Special service charge for extensive IT or clerical time. |
| TX | Texas Public Information Act | Gov. Code Ch. 552 | Promptly, written date if over 10 business days | No intake fee. Charges cover materials, labor, overhead. Deposit allowed if estimate exceeds $100 ($50 for bodies under 16 employees). |
| CA | California Public Records Act | Gov. Code 7920.000 et seq. (recodified 2023, formerly 6250-6270) | 10 days to state whether records will be produced (Gov. Code 7922.535) | Inspection free. Printed copies commonly about $0.35/page or actual cost. No fee waivers in CA. |
| GA | Georgia Open Records Act | O.C.G.A. 50-18-70 to 50-18-77 (timing 50-18-71) | 3 business days | Copy fee up to $0.10/page. Search and redaction billed at lowest-paid qualified employee rate, first quarter hour free. |
| OH | Ohio Public Records Act | O.R.C. 149.43 | Prompt, reasonable time (no fixed day count) | Fees limited to actual copy cost. No search or redaction time charged. |
| NC | NC Public Records Law | G.S. Ch. 132 (fees 132-6.2) | As promptly as possible (no fixed day count) | Copy at actual cost. Reasonable special service charge for extensive IT or clerical use. No fee waivers in NC. |
| SC | SC Freedom of Information Act | S.C. Code Title 30, Ch. 4 (30-4-30) | 10 business days (20 if record is over 24 months old) | Deposit up to 25 percent allowed. After deposit, produced within 30 days (35 if over 24 months). |
| AL | Alabama Open Records Act (SB 270, 2024) | Code of Alabama 36-12-40 et seq. | Acknowledge in 10 business days, standard response in 15 days, time-intensive 45 days | Effective Oct 1, 2024. No statutory fee cap, fees must be reasonable. No fee waivers in AL. |
| MO | Missouri Sunshine Law | RSMo Ch. 610 (fees 610.026) | End of 3rd business day after receipt | Copy fee up to $0.10/page plus clerical and research time. Delays must be explained with an earliest-available date. |
| IN | Access to Public Records Act (APRA) | IC 5-14-3 | 24 hours (in person or phone), 7 days (mail, fax, email) | No response in 7 days is a deemed denial. Non-state copy fee: greater of $0.10/page, $0.25 color, or actual cost. |
| PA | Right-to-Know Law | 65 P.S. 67.101 et seq. | 5 business days (extension notice allowed) | Day of receipt not counted. Standard copy fee $0.25/page. No charge to inspect. |
| NY | Freedom of Information Law (FOIL) | Public Officers Law Art. 6 (84-90) | 5 business days to produce, deny, or acknowledge with a date | Copy up to $0.25/page (to 9x14 in). No fee unless 2+ hours of staff time needed. Search time excluded. |
| AZ | Arizona Public Records Law | A.R.S. 39-121 et seq. (copies 39-121.01) | Promptly (about 5 working days treated as prompt) | Failure to respond promptly is a deemed denial. Copy cost only. No fee waivers in AZ. |
| NJ | Open Public Records Act (OPRA) | N.J.S.A. 47:1A-1 et seq. | 7 business days standard, 14 if redactions, 21 if in storage | S-2930 effective Sep 3, 2024. Commercial requestors (investors) get up to 14 business days, shortenable to 7 by paying up to 2x production cost. |

### Electronic-Format Requests

Always ask for the records in their existing electronic or native format (CSV, Excel, or an existing database export). Copy fees are tied to paper pages, so an electronic export usually avoids per-page printing charges entirely. Phrase it as a request for the data "in the electronic format in which it is already maintained" and ask the agency NOT to convert, reformat, or run custom programming, because states such as Texas, Florida, and North Carolina allow extra special service charges for extensive IT or programming time. If the data already exists as a report or extract, you pay little or nothing beyond media.

### Fee-Waiver Language (Read This Before You Ask)

Public-records fee waivers exist almost everywhere, but they are reserved for disclosures in the public interest that are NOT primarily commercial. A real estate investor pulling distress lists is a commercial requestor, so the public-interest waiver will almost never apply to you. Several states (including Alabama, Arizona, California, Louisiana, Montana, Nebraska, North Carolina, and Washington) do not permit fee waivers at all. Plan to pay the assessed copy or service fee. The useful protection is a cost cap, not a waiver: include a line that says "If fulfilling this request will cost more than $[AMOUNT], please notify me with an itemized estimate before proceeding." That caps your spend and forces the agency to justify charges, which is the realistic lever for a commercial requestor.

## Foreclosure Timeline Cadence Note

Process type sets your re-pull rhythm, not just your source office.

- Nonjudicial states (fast, roughly 60-180 days): the file turns over quickly. A notice published this week may sell within weeks. Re-pull the recorder or public-notice feed often (daily for high-volume counties, at least weekly) or you miss the window. The marketing window is short by design, days 1-30 from notice filing and before the auction date, so speed to contact is everything.
- Judicial states (slow, often 1,000+ days in backed-up jurisdictions): the same lis pendens stays actionable for months or years while the case grinds through court. You do not need to re-pull as aggressively, and a lead pulled today can still be worth working a year from now. The advantage shifts from raw speed to persistence and follow-up.

Set your pull cadence to the process, not to a one-size schedule. A daily nonjudicial cadence wasted on a judicial county burns compute for little gain, and a weekly judicial cadence applied to a fast nonjudicial county loses the freshest, most motivated sellers to faster competitors.

## Verify Locally (read this before acting)

State bucket assignments and redemption periods are not uniform across published sources, and they change with legislation. The 2023 U.S. Supreme Court ruling in Tyler v. Hennepin County (on surplus equity from tax sales) has already pushed several states to amend their tax-sale and surplus rules. Before you build a pipeline or quote a redemption window to a seller, confirm the current rule against the actual state statute and the specific county tax collector, treasurer, or chancery clerk page. Treat the lists above as a starting map, not as legal advice.

Foreclosure classifications shift too. A handful of states have judicial-only or nonjudicial-only mandates that move with legislation, and the "either" states can drift toward one path as case law and lender practice evolve. Re-confirm the process type, the security instrument, and the records statute and its response window for any new target state before you commit engineering time to a pull. The foreclosure and tax-sale buckets here are a routing map for where to look, not a substitute for the current statute or county practice.

This file is general information for data sourcing, not legal advice. For the compliance side of contacting distressed owners (FDCPA, TCPA and DNC, and state foreclosure-consultant and equity-purchaser laws), see the skill's compliance guidance and confirm current rules with a licensed attorney in the target state.

## Related References

- common-offices.md: per-data-type office names, what to search, and access notes that pair with the process classifications here.
- research-prompts.md: copy-paste research prompts by priority, including the foreclosure trustee-sale filter phrases this file points to.
- worked-example.md: Knox County TN (nonjudicial, redeemable deed) and Hillsborough County FL (judicial, lien-then-deed) worked end to end, showing these classifications applied to real offices.
