# Taxonomy

13 listing asset classes and 76 sub-types (the tree behind the major listing sites' property type
filters), plus three buckets so every parcel in a file gets a class: Residential, Civic/Exempt,
Other. Labels must use these exact names; `cc label` rejects anything else.

| Class | Sub-types |
|---|---|
| Office | Industrial Live/Work, Loft/Creative Space, Medical, Office Live/Work Unit, Office/Residential, Office/Storefront Retail |
| Industrial | Data Center, Distribution, Flex, Manufacturing, R&D, Refrigeration/Cold Storage, Service, Showroom, Telecom Facility, Truck Terminal, Warehouse, Other Industrial |
| Retail | Bank, Day Care Facility/Nursery, Drug Store, Freestanding, Grocery/Convenience Store, Service/Gas Station, Showroom, Storefront, Storefront Retail/Office, Storefront Retail/Residential, Vehicle Related, Other Retail |
| Restaurant | none |
| Shopping Center | Airport Retail, Community Center, Lifestyle Center, Neighborhood Center, Outlet Center, Power Center, Regional Mall, Strip Center, Super Regional Mall, Theme/Festival Center |
| Multifamily | Dormitory, Garden, High-Rise, Low-Rise, Manufactured Housing/Mobile Home, Mid-Rise |
| Specialty | Car Wash, Marina, Parking, Religious Facility, Schools, Self-Storage, Other Specialty |
| Health Care | Assisted Living, Congregate Senior Housing, Continuing Care Retirement Community, Hospital, Rehabilitation Center, Skilled Nursing Facility |
| Hospitality | Bed and Breakfast, Hostel, Hotel, Serviced Apartment |
| Sports & Entertainment | Amusement Park, Baseball Field, Casino, Golf / Driving Range, Horse Stables, Race Track, Skating Rink, Swimming Pool, Theater/Concert Hall |
| Lab | none |
| Land | Agricultural, Commercial, Industrial, Residential/Multifamily |
| Residential Income | none (2 to 4 units) |
| Residential | none (one dwelling: house, condo unit, townhouse, single mobile home) |
| Civic/Exempt | none (government, utility, charitable, right of way) |
| Other | none (nothing in the file says what it is) |

## Where things go that people get wrong

| Property | Class / sub-type | Why |
|---|---|---|
| Gas station, truck stop, convenience store with fuel | Retail / Service/Gas Station | It is retail with pumps, not a "specialty" use |
| Car dealer, auto repair, body shop, tire store | Retail / Vehicle Related | |
| Bank branch | Retail / Bank | |
| Pharmacy | Retail / Drug Store | |
| Day care, preschool | Retail / Day Care Facility/Nursery | Not Schools |
| Veterinary clinic | Office / Medical | |
| Funeral home, cemetery, kennel, campground | Specialty / Other Specialty | |
| Church, temple, mosque, synagogue | Specialty / Religious Facility | Even when the county also codes it exempt |
| Public school, college | Specialty / Schools | |
| Mobile home PARK | Multifamily / Manufactured Housing/Mobile Home | A single mobile home on its own lot is Residential |
| Condo UNIT (one home) | Residential | Even if the record carries the complex's unit count |
| Commercial or office condo unit | Office (or Retail / Storefront for a retail condo) | |
| Duplex, triplex, fourplex | Residential Income | 5 or more units is Multifamily |
| Dorm, fraternity, rooming house | Multifamily / Dormitory | |
| Utility land, railroad, right of way | Civic/Exempt | No listing class fits |
| Vacant commercial / industrial / residential lot | Land / Commercial, Industrial, Residential/Multifamily | |
| Farm, timber, pasture, current-use agricultural | Land / Agricultural | |

## Settling ambiguity

| Ambiguity | Rule |
|---|---|
| Office vs medical office | Half or more of the tenants medical or dental: Office / Medical |
| Retail vs restaurant | Food service with seating or a drive-through: Restaurant |
| Industrial vs flex | Both office and warehouse, office over about 20 to 30% of the area: Industrial / Flex |
| Shopping center vs retail | Three or more tenants sharing a structure and parking: Shopping Center. One tenant: Retail |
| Shopping center sub-type | The engine sizes it: under 30,000 SF Strip, under 100,000 Neighborhood, under 350,000 Community |
| Multifamily sub-type | Stories: 1 to 3 Garden, 4 to 6 Mid-Rise, 7+ High-Rise |
| Mixed use | Classify by the dominant use; the review queue flags real conflicts |
| Truly unknown | Leave the sub-type blank. A blank is honest; a guess repeats on every parcel with the code |
