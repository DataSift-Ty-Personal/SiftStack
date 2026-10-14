# Signals: what the evidence in a record means

Use this when you label a county's distinct codes. The engine's keyword classifier already knows
these words and pre-labels every code; your job is the codes where that guess is weak, generic, or
contradicted by the evidence columns.

## 1. Land-use descriptions

County codes differ everywhere, but the words in their descriptions repeat.

| Words in the description | Label |
|---|---|
| office, professional building | Office |
| medical office, clinic, dental, physician, urgent care, dialysis, surgery center | Office / Medical |
| loft, creative, co-working | Office / Loft/Creative Space |
| warehouse, storage building | Industrial / Warehouse |
| distribution, fulfillment, logistics | Industrial / Distribution |
| manufacturing, factory, plant, foundry, processing, printing | Industrial / Manufacturing |
| office/warehouse, flex, small shops with 20%+ office | Industrial / Flex |
| service garage, machine shop, contractor yard, salvage, recycling | Industrial / Service |
| cold storage, refrigerated, freezer | Industrial / Refrigeration/Cold Storage |
| data center, server, colocation | Industrial / Data Center |
| truck terminal, freight terminal | Industrial / Truck Terminal |
| telecom, cell tower, radio or TV station | Industrial / Telecom Facility |
| research and development, R&D | Industrial / R&D |
| laboratory, biotech, life science | Lab |
| retail, store, shop | Retail |
| small detached retail, big box, department store | Retail / Freestanding |
| storefront, retail with apartments over | Retail / Storefront, Storefront Retail/Residential |
| restaurant, fast food, bar, tavern, nightclub | Restaurant |
| shopping center, mall, strip center, plaza | Shopping Center (the engine sizes the sub-type) |
| apartments, multi-family, 5+ units | Multifamily |
| duplex, triplex, two- to four-family | Residential Income |
| mobile home park, trailer park | Multifamily / Manufactured Housing/Mobile Home |
| hotel, motel, inn, tourist cabins | Hospitality / Hotel |
| nursing home, assisted living, senior housing | Health Care (matching sub-type) |
| hospital | Health Care / Hospital |
| self-storage, mini-warehouse | Specialty / Self-Storage |
| car wash | Specialty / Car Wash |
| parking garage or lot | Specialty / Parking |
| church, place of worship | Specialty / Religious Facility |
| school, college, academy | Specialty / Schools |
| golf, theater, bowling, skating, amusement, race track | Sports & Entertainment (matching sub-type) |
| vacant, unimproved, unused land | Land (sub-type from the words around it) |
| farm, agricultural, timber, current agricultural use (CAUV) | Land / Agricultural |
| exempt, government, municipal, utility, right of way | Civic/Exempt |
| single family, residential, condominium unit, townhouse | Residential |

## 2. The evidence columns in distinct_codes.csv

| Column | How to read it |
|---|---|
| `pct_improved` | Near 0: vacant land. Near 100: a building. Settles "lot" vs "structure" codes |
| `median_sqft` | 1,500 to 2,500 is a house; 10,000+ is commercial; 50,000+ with one story is industrial |
| `median_units` | 2 to 4 is Residential Income; 5+ is Multifamily |
| `median_value` | A "garage" code worth $2M is not a home garage |
| `top_name_words` | Recurring owner and business words. STORAGE, CHURCH, HOTEL, FARMS, a data-center operator, a hospital system: these override a vague description |
| `parent_label` | For a county code sitting under a generic national code (FA 8000 vacant), the national label it refines |
| `prelabel_*` | The keyword guess and its reason. Keep it when it is right; that is most of the time |

## 3. Zoning

Zoning says what is allowed, not what is there, so the engine uses it as a weak hint only.
C or B districts: commercial. O: office. M or I: industrial. R-3 and up or RM: multifamily. AG:
agricultural. PUD, PD and MU say nothing on their own.

## 4. Business names on the parcel

When the file or the business-points pass names a tenant, it is one of the strongest signals: a
Hampton Inn is a hotel, a Casey's is a gas station, a Public Storage is self-storage. The engine
reads business names on its own; the review queue shows the ones that disagree with the code.

## 5. Owner names

Weak, supporting only. "XYZ STORAGE LLC" hints at self-storage. A BANK as owner usually means a
foreclosure, not a bank branch, so owner names never decide a class.

## 6. Signal stacking, worked

```
Code "499 Other Commercial Structures", 443 parcels, median 1,854 SF, 88% improved,
owner words OHIO, CITY, LICKING, SIDECAT
  Description is a catch-all, size is small, owners are mixed.
  Label: Retail, no sub-type, confidence 0.5 (review band). Do not invent a sub-type.

Code "305 Research/Chemical Labs", 13 parcels, owner words QTS, AMAZON, EDGECONNEX, DATA
  The description says lab; every owner is a data-center operator.
  Label: Industrial / Data Center, note "owners are data-center operators".

Code "501 Residential Unplatted 0-9.99 Ac", pct_improved 1%
  "Residential" is the zoning-style description; 99% of parcels have no building.
  Label: Land / Residential/Multifamily.
```
