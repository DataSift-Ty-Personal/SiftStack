# Research Guide: Finding Entity Decision-Makers

This guide provides detailed instructions for finding the decision-makers (signing members, trustees, officers) for various entity types in your buyer data.

## Table of Contents

1. [LLC Research](#llc-research) (includes Multi-Owner Tiebreaker and Research Failure Fallback)
2. [Corporation Research](#corporation-research)
3. [Trust Research](#trust-research) (includes Trustee vs Beneficiary guidance)
4. [Estate Research](#estate-research)
5. [Secretary of State URLs by State](#secretary-of-state-urls-by-state)
6. [Confidence Scoring](#confidence-scoring)
7. [Tips for Efficient Research](#tips-for-efficient-research)

## LLC Research

LLCs are the most common entity type in real estate buyer data. The goal is to find the **members** (owners) or **managers** (operators) of the LLC.

### Primary Source: Secretary of State

Every LLC must register with the Secretary of State in its formation state. The `BuyerState` column indicates where to search.

**What to look for:**
- **Registered Agent**: The official contact for the LLC. Often the owner or their attorney.
- **Members/Managers**: Some states list these on formation documents or annual reports.
- **Principal Address**: May differ from the mailing address in your data.
- **Organizer**: The person who filed the formation documents (may be the owner or a third party).

**Research Steps:**
1. Go to the SOS website for the state in `BuyerState`
2. Use the business entity search function
3. Search for the exact LLC name from `BuyerFullName`
4. Click on the entity to view details
5. Look for annual reports or amendments (often have more current info)
6. Document the Registered Agent name and any listed members/managers

### Multi-Owner LLC: Who to Contact

When an LLC lists multiple members or managers, follow this priority:

1. **Registered Agent** — Contact first. This is the entity's designated point of contact.
2. **Largest ownership percentage** — If the RA is a commercial service (CT Corporation, CSC, Northwest Registered Agent, Incorp Services, etc.), contact the member with the highest ownership stake.
3. **First-listed member** — If ownership is equal (e.g., 50/50), contact the member listed first in the SOS filing. The first-listed member is typically the managing member.

### Research Failure Fallback

When the Secretary of State website is down, returning errors, or simply not showing member information, use these alternatives in order:

1. **OpenCorporates.com** (free) — Aggregates business registration data from multiple state databases. Search the exact entity name. Often mirrors SOS data but may be cached from a previous pull when the SOS site itself is down.

2. **County Recorder / Register of Deeds** — Search by the LLC name in the county where it purchased property. Deeds show the signer's name (e.g., "John Smith, Member of XYZ Properties LLC"). This is often the most reliable fallback because deed records are separate from SOS systems.

3. **Google: exact LLC name + state** — Search `"XYZ PROPERTIES LLC" Tennessee` in quotes. Annual report filings, court cases, and business directories frequently surface the principal's name. Check the first 2-3 pages of results.

4. **Property tax records** — Search the county tax assessor site for properties owned by the LLC. The owner name field sometimes shows the individual behind the entity, especially for single-member LLCs.

5. **Last resort: mark as "Research Pending"** — If all methods fail, set `DecisionMaker_FullName` to "Research Pending" (not "Not Found"). This signals the entity needs a second pass rather than being written off. Return to these after completing the rest of the batch — the SOS site may be back up.

**Example:**
```
Input: GDP PROPERTIES LLC (TN)

Tennessee SOS Search:
- Entity Name: GDP PROPERTIES LLC
- Status: Active
- Formation Date: 01/15/2019
- Registered Agent: JOHN DOE
- Principal Address: 11002 Kingston Pike Ste 203, Farragut, TN 37934

Result:
DecisionMaker_Name: John Doe
DecisionMaker_Role: Registered Agent
Verification_Source: TN SOS Business Search
```

### Anonymous LLC States

Some states allow anonymous LLCs where member names are not disclosed: **Delaware, New Mexico, Nevada, Wyoming**. For these, the Registered Agent is often a commercial service, not the actual owner.

**Workaround for Anonymous LLCs:**
1. Check if the LLC owns property in a non-anonymous state
2. Search county deed records for properties owned by the LLC
3. The deed may show the signer's name
4. Contact the Registered Agent directly (may or may not provide info)

## Corporation Research

Corporations (Inc, Corp) have a more formal structure with officers and directors.

### Primary Source: Secretary of State

**What to look for:**
- **Officers**: President, CEO, Secretary, Treasurer
- **Directors**: Board members
- **Registered Agent**: Official contact
- **Annual Reports**: Most current officer information

**Research Steps:**
1. Go to the SOS website for the state in `BuyerState`
2. Search for the corporation name
3. View the entity details and any filed annual reports
4. Document the President or CEO as the primary decision-maker

**If SOS is unavailable:** Use the same [Research Failure Fallback](#research-failure-fallback) steps from the LLC section above — OpenCorporates, county recorder, Google, tax records.

**Example:**
```
Input: DR HORTON INC (TN)

Note: DR Horton is a large public homebuilder. For large corporations,
the local division manager is often more relevant than corporate officers.

For smaller corporations:
DecisionMaker_Name: [President Name from SOS]
DecisionMaker_Role: President
Verification_Source: [State] SOS Annual Report
```

## Trust Research

Trusts are private documents and are NOT registered with the Secretary of State. Finding the trustee requires different methods.

### Method 1: Trust Name Analysis

Many trust names reveal the trustee or grantor:

| Trust Name Pattern | Likely Trustee |
|-------------------|----------------|
| SMITH FAMILY TRUST | Someone named Smith |
| JOHN DOE REVOCABLE LIVING TRUST | John Doe |
| JANE DOE REVOCABLE TRUST | Jane Doe |
| THE [NAME] TRUST | Person with that name |

**For Revocable Living Trusts:** The grantor (creator) is almost always the initial trustee. The name in the trust title is typically the decision-maker.

**Example:**
```
Input: BENNETT REVOCABLE LIVING TRUST

Analysis: "Revocable Living Trust" indicates the grantor is the trustee.
The name "Bennett" is the grantor's surname.

Result:
DecisionMaker_Name: [First Name] Bennett
DecisionMaker_Role: Trustee/Grantor
Verification_Source: Trust naming convention analysis
Note: First name needed - search county records or skip trace "Bennett" at the trust's mailing address
```

### Method 2: County Deed Records

When a property is transferred into a trust, the deed is recorded with the county. The deed shows the trustee's name.

**Research Steps:**
1. Identify the county where the trust owns property
2. Find the county recorder/register of deeds website
3. Search by the trust name or property address
4. Locate the deed where the trust is the grantee (buyer)
5. The trustee's name appears on the deed, often as "John Smith, Trustee of the Smith Family Trust"

### Method 3: Mailing Address Skip Trace

If the trust has a residential mailing address (not a PO Box), the person at that address is likely the trustee.

### Important: Contact the Trustee, Not Beneficiaries

When researching trusts, always identify and contact the **trustee** — the person with legal authority to buy, sell, and manage trust property. Do NOT contact beneficiaries. Beneficiaries have an interest in the trust assets but no authority to transact. Contacting them wastes time and may create legal complications.

## Estate Research

Estates indicate a deceased owner. The decision-maker is the **Executor** or **Personal Representative**.

### Primary Source: Probate Court

Estate cases are handled by the probate court in the county where the deceased resided.

**Research Steps:**
1. Identify the county from your data
2. Find the county probate court website
3. Search for the estate case by the deceased's name
4. The court records will name the Executor/Personal Representative

**Note:** Estate buyers are less common in active buyer lists. If an estate is actively purchasing properties, it may be an investment entity using "Estate" in its name rather than an actual probate estate.

## Secretary of State URLs by State

| State | URL |
|-------|-----|
| Tennessee | https://tnbear.tn.gov/Ecommerce/FilingSearch.aspx |
| Texas | https://www.sos.state.tx.us/corp/sosda/index.shtml |
| Florida | https://dos.myflorida.com/sunbiz/ |
| Georgia | https://ecorp.sos.ga.gov/businesssearch |
| California | https://bizfileonline.sos.ca.gov/search/business |
| New York | https://apps.dos.ny.gov/publicInquiry/ |
| North Carolina | https://www.sosnc.gov/online_services/search/by_title/_Business_Registration |
| Ohio | https://businesssearch.ohiosos.gov/ |
| Pennsylvania | https://www.corporations.pa.gov/search/corpsearch |
| Illinois | https://apps.ilsos.gov/businessentitysearch/ |
| Michigan | https://cofs.lara.state.mi.us/SearchApi/Search/Search |
| Arizona | https://ecorp.azcc.gov/EntitySearch/Index |
| Colorado | https://www.sos.state.co.us/biz/BusinessEntityCriteriaExt.do |
| Virginia | https://cis.scc.virginia.gov/EntitySearch/Index |
| New Jersey | https://www.njportal.com/DOR/BusinessNameSearch/ |
| Washington | https://ccfs.sos.wa.gov/#/BusinessSearch |
| Massachusetts | https://corp.sec.state.ma.us/CorpWeb/CorpSearch/CorpSearch.aspx |
| Indiana | https://bsd.sos.in.gov/publicbusinesssearch |
| Missouri | https://bsd.sos.mo.gov/BusinessEntity/BESearch.aspx |
| Maryland | https://egov.maryland.gov/BusinessExpress/EntitySearch |
| Wisconsin | https://www.wdfi.org/apps/CorpSearch/Search.aspx |
| Minnesota | https://mblsportal.sos.state.mn.us/Business/Search |
| South Carolina | https://businessfilings.sc.gov/BusinessFiling/Entity/Search |
| Alabama | https://arc-sos.state.al.us/cgi/corpname.mbr/output |
| Louisiana | https://coraweb.sos.la.gov/commercialsearch/commercialsearch.aspx |
| Kentucky | https://web.sos.ky.gov/bussearchnprofile/search |
| Oregon | https://egov.sos.state.or.us/br/pkg_web_name_srch_inq.login |
| Oklahoma | https://www.sos.ok.gov/corp/corpInquiryFind.aspx |
| Connecticut | https://service.ct.gov/business/s/onlinebusinesssearch |
| Iowa | https://sos.iowa.gov/search/business/(S(...))/search.aspx |
| Nevada | https://esos.nv.gov/EntitySearch/OnlineEntitySearch |
| Arkansas | https://www.sos.arkansas.gov/corps/search_all.php |
| Utah | https://secure.utah.gov/bes/index.html |
| Kansas | https://www.kansas.gov/bess/flow/main?execution=e1s1 |
| Mississippi | https://corp.sos.ms.gov/corp/portal/c/page/corpBusinessIdSearch/portal.aspx |
| New Mexico | https://portal.sos.state.nm.us/BFS/online/CorporationBusinessSearch |
| Nebraska | https://www.nebraska.gov/sos/corp/corpsearch.cgi |
| West Virginia | https://apps.wv.gov/sos/businessentitysearch/ |
| Idaho | https://sosbiz.idaho.gov/search/business |
| Hawaii | https://hbe.ehawaii.gov/documents/search.html |
| New Hampshire | https://quickstart.sos.nh.gov/online/BusinessInquire |
| Maine | https://icrs.informe.org/nei-sos-icrs/ICRS |
| Montana | https://biz.sosmt.gov/search |
| Rhode Island | https://business.sos.ri.gov/CorpWeb/CorpSearch/CorpSearch.aspx |
| Delaware | https://icis.corp.delaware.gov/ecorp/entitysearch/namesearch.aspx |
| South Dakota | https://sosenterprise.sd.gov/BusinessServices/Business/FilingSearch.aspx |
| North Dakota | https://firststop.sos.nd.gov/search/business |
| Alaska | https://www.commerce.alaska.gov/cbp/Main/Search/Entities |
| Vermont | https://bizfilings.vermont.gov/online/BusinessInquire |
| Wyoming | https://wyobiz.wyo.gov/Business/FilingSearch.aspx |

## Confidence Scoring

Tag every researched entity with a confidence level. This goes in the `Confidence` column of the output and as a prefix in `Verification_Source`.

### HIGH Confidence [H]

All three conditions met:
- SOS match found with **Active** entity status
- Registered agent or member name verified (not a commercial service)
- Phone number or email found for the contact person

Example: `[H] TN SOS - Active, RA: John Smith, phone verified via skip trace`

### MEDIUM Confidence [M]

One of these situations:
- SOS match found but entity status is **Inactive** or **Dissolved** (person may have moved on)
- Only deed records found — name came from a recorded document, not SOS
- SOS match but registered agent is a commercial service and no members disclosed

Example: `[M] TN SOS - Dissolved 2024, deed signer: Jane Doe at 123 Main St`

### LOW Confidence [L]

One of these situations:
- No SOS match found for the entity name
- Only Google/LinkedIn results (unverified)
- Common name with ambiguous match (e.g., "Smith Properties LLC" in a state with 5 matches)

Example: `[L] Google only - LinkedIn profile may be related, needs verification`

### "Research Pending" (no confidence tag yet)

Entity where SOS site was down or all fallback methods failed. These get a second research pass.

## Tips for Efficient Research

1. **Batch by State**: Group entities by `BuyerState` to research multiple entities on the same SOS website.

2. **Start with High-Volume Buyers**: Prioritize entities with the highest `BuyerPurchases6MSum` values — these are your most active buyers. Always start with entities that have 3+ purchases in the last 6 months.

3. **Use Browser Tabs**: Open multiple SOS search results in tabs to compare and verify information.

4. **Document Everything**: Even if you can't find the decision-maker, note what you searched and why it failed. This helps with future research.

5. **Cross-Reference**: If the Registered Agent is a commercial service (like CT Corporation, CSC, etc.), the actual owner is not disclosed. Try searching county deed records instead.

6. **Use Fallback Sources When SOS Fails**: OpenCorporates.com, county recorder deed records, Google exact-name search, and property tax records. See [Research Failure Fallback](#research-failure-fallback) above.

7. **Tag Confidence Immediately**: Assign H/M/L as soon as you finish researching each entity. It's much harder to remember confidence levels after researching 30+ entities.
