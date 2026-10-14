"""Keyword classifier for free-text property signals.

Three vocabularies, because the same word means different things in different
fields:

* USE   land-use descriptions, structure types, vendor property types. The
        strongest text signal. Capped at 0.6 confidence.
* NAME  a business or tenant name on the parcel. "Hampton Inn" is a hotel.
* OWNER the recorded owner. Weak and supporting only: a bank as OWNER is a
        foreclosure, not a bank branch, so banks are not in this list.

Every pattern is matched on whole words. Substring matching is how "BANK"
inside "WILLBANKS" once excluded a real buyer, so nothing here is a bare
substring. Rules are ordered most specific first, and the first hit wins;
a second hit pointing at a different class is reported as a conflict.
"""
from __future__ import annotations

import os
import re
from collections import namedtuple

Hit = namedtuple("Hit", "asset_class sub_type confidence reason")

_DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


def clean(s):
    """'single_family_residence' -> 'SINGLE FAMILY RESIDENCE'."""
    s = (s or "").upper().replace("&", " AND ").replace("'", "").replace("’", "")
    s = re.sub(r"[_/\\\-,;:()\[\].]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _w(*alts):
    return re.compile(r"\b(?:%s)\b" % "|".join(alts))


# ---- USE vocabulary, ordered most specific first ----------------------------
# (pattern, class, sub, confidence)
USE_RULES = [
    # mixed forms first, so "office warehouse" is Flex and not Office or Warehouse
    (_w(r"OFFICE ?WAREHOUSE", r"OFFICE ?SHOWROOM", r"FLEX(?: SPACE)?", r"WAREHOUSE ?OFFICE"), "Industrial", "Flex", 0.6),
    (_w(r"RETAIL ?OFFICE", r"OFFICE ?RETAIL", r"STORE AND OFFICE", r"OFFICE AND STORE"), "Retail", "Storefront Retail/Office", 0.6),
    (_w(r"RETAIL ?RESIDENTIAL", r"STORE ?(?:WITH|AND|W) ?APARTMENTS?", r"APARTMENTS? (?:OVER|ABOVE) (?:STORE|RETAIL)",
        r"COMMERCIAL ?RESIDENTIAL"), "Retail", "Storefront Retail/Residential", 0.6),
    (_w(r"OFFICE ?RESIDENTIAL", r"OFFICE ?(?:WITH|AND|W) ?APARTMENTS?"), "Office", "Office/Residential", 0.6),
    (_w(r"LIVE ?WORK"), "Office", "Office Live/Work Unit", 0.55),
    # a commercial condo is a business unit, never a home (FA's own default is Office)
    (_w(r"(?:COMMERCIAL|BUSINESS|OFFICE|PROFESSIONAL|MEDICAL) CONDO(?:MINIUM)?S?(?: UNITS?)?"), "Office", "", 0.5),
    (_w(r"(?:RETAIL|STORE) CONDO(?:MINIUM)?S?(?: UNITS?)?"), "Retail", "Storefront", 0.5),
    (_w(r"(?:INDUSTRIAL|WAREHOUSE|FLEX) CONDO(?:MINIUM)?S?(?: UNITS?)?"), "Industrial", "Flex", 0.5),
    # vacant land, sub-typed below by _land()
    (_w(r"VACANT", r"UNIMPROVED", r"UNDEVELOPED", r"RAW LAND", r"BARE LAND", r"UNUSED LAND", r"LOT ONLY",
        r"VACANT LOT", r"LAND ONLY"), "Land", "", 0.6),
    (_w(r"MOBILE HOME PARKS?", r"TRAILER PARKS?", r"TRAILER COURTS?", r"MANUFACTURED HOUSING (?:COMMUNITY|PARK)",
        r"MANUFACTURED HOME PARK", r"MH PARK", r"MOBILE HOME COURT"), "Multifamily", "Manufactured Housing/Mobile Home", 0.6),
    (_w(r"DORM(?:ITORY|ITORIES|S)?", r"STUDENT HOUSING", r"FRATERNITY", r"SORORITY"), "Multifamily", "Dormitory", 0.6),
    (_w(r"SELF ?STORAGE", r"MINI ?STORAGE", r"MINI ?WAREHOUSES?", r"MINI ?WHSE", r"STORAGE UNITS", r"PUBLIC STORAGE",
        r"STORAGE RENTAL"), "Specialty", "Self-Storage", 0.6),
    (_w(r"CAR ?WASH(?:ES)?", r"AUTO ?WASH"), "Specialty", "Car Wash", 0.6),
    (_w(r"PARKING(?: GARAGE| LOT| DECK| STRUCTURE| RAMP)?", r"PARKING"), "Specialty", "Parking", 0.6),
    (_w(r"MARINAS?", r"BOAT (?:DOCKS?|SLIPS?|LAUNCH)"), "Specialty", "Marina", 0.6),
    (_w(r"DAY ?CARE", r"CHILD ?CARE", r"NURSERY SCHOOL", r"PRE ?SCHOOL", r"KINDERCARE"), "Retail", "Day Care Facility/Nursery", 0.6),
    (_w(r"CHURCH(?:ES)?", r"CHAPEL", r"SYNAGOGUE", r"MOSQUE", r"TEMPLE", r"WORSHIP", r"RELIGIOUS", r"CATHEDRAL"),
     "Specialty", "Religious Facility", 0.6),
    (_w(r"FUNERAL(?: HOME)?", r"MORTUARY", r"CREMATORY", r"CEMETER(?:Y|IES)", r"MAUSOLEUM"), "Specialty", "Other Specialty", 0.6),
    (_w(r"VETERINAR(?:Y|IAN)", r"ANIMAL (?:HOSPITAL|CLINIC)"), "Office", "Medical", 0.55),
    (_w(r"KENNELS?", r"ANIMAL BOARDING"), "Specialty", "Other Specialty", 0.5),
    (_w(r"NURSING HOMES?", r"SKILLED NURSING", r"NURSING (?:FACILITY|CENTER|CARE)", r"CONVALESCENT", r"SNF"),
     "Health Care", "Skilled Nursing Facility", 0.6),
    (_w(r"ASSISTED LIVING", r"MEMORY CARE", r"RESIDENTIAL CARE", r"PERSONAL CARE HOMES?", r"ALF"),
     "Health Care", "Assisted Living", 0.6),
    (_w(r"CONTINUING CARE", r"CCRC"), "Health Care", "Continuing Care Retirement Community", 0.6),
    (_w(r"REHAB(?:ILITATION)? (?:CENTER|FACILITY|HOSPITAL)"), "Health Care", "Rehabilitation Center", 0.6),
    (_w(r"SENIOR (?:HOUSING|LIVING|APARTMENTS|CITIZENS? HOUSING)", r"INDEPENDENT LIVING", r"RETIREMENT (?:HOMES?|CENTER|COMMUNITY|HOUSING)"),
     "Health Care", "Congregate Senior Housing", 0.6),
    (_w(r"HOSPITALS?", r"ACUTE CARE"), "Health Care", "Hospital", 0.6),
    (_w(r"MEDICAL OFFICES?", r"MEDICAL (?:BUILDING|BLDG|CENTER|CLINIC|PLAZA|ARTS)", r"CLINICS?", r"DENTAL", r"DENTIST",
        r"DOCTORS?", r"PHYSICIANS?", r"URGENT CARE", r"DIALYSIS", r"SURGERY CENTER", r"SURGICAL", r"MEDICAL"),
     "Office", "Medical", 0.6),
    (_w(r"BED AND BREAKFAST", r"B AND B", r"GUEST HOUSE"), "Hospitality", "Bed and Breakfast", 0.6),
    (_w(r"HOSTELS?"), "Hospitality", "Hostel", 0.6),
    (_w(r"SERVICED APARTMENTS?", r"CORPORATE HOUSING", r"EXTENDED STAY"), "Hospitality", "Serviced Apartment", 0.55),
    (_w(r"HOTELS?", r"MOTELS?", r"INNS?", r"RESORTS?", r"LODGING", r"MOTOR LODGE"), "Hospitality", "Hotel", 0.6),
    (_w(r"MINI(?:ATURE)? GOLF", r"PUTT ?PUTT", r"AMUSEMENT", r"THEME PARKS?", r"WATER PARKS?", r"FUN PARK", r"GO ?KARTS?"),
     "Sports & Entertainment", "Amusement Park", 0.6),
    (_w(r"GOLF(?: COURSE| CLUB)?", r"DRIVING RANGE", r"COUNTRY CLUB"), "Sports & Entertainment", "Golf / Driving Range", 0.6),
    (_w(r"DRIVE ?IN THEATER", r"THEATERS?", r"THEATRES?", r"CINEMAS?", r"MOVIES?", r"AUDITORIUM", r"CONCERT HALL",
        r"PERFORMING ARTS", r"AMPHITHEATER"), "Sports & Entertainment", "Theater/Concert Hall", 0.6),
    (_w(r"CASINO", r"GAMING"), "Sports & Entertainment", "Casino", 0.55),
    (_w(r"RACE ?TRACKS?", r"RACEWAY", r"SPEEDWAY", r"DRAG STRIP", r"MOTOR SPEEDWAY"), "Sports & Entertainment", "Race Track", 0.6),
    (_w(r"SKATING", r"ICE RINK", r"ROLLER RINK", r"SKATE"), "Sports & Entertainment", "Skating Rink", 0.6),
    (_w(r"SWIMMING POOL", r"AQUATIC", r"NATATORIUM"), "Sports & Entertainment", "Swimming Pool", 0.6),
    (_w(r"STABLES?", r"EQUESTRIAN", r"HORSE (?:FARM|BARN|BOARDING)"), "Sports & Entertainment", "Horse Stables", 0.55),
    (_w(r"BASEBALL", r"BALL ?PARK", r"BALL ?FIELDS?", r"SOFTBALL"), "Sports & Entertainment", "Baseball Field", 0.6),
    (_w(r"STADIUM", r"ARENA", r"SPORTS COMPLEX", r"ATHLETIC FIELDS?", r"BOWLING", r"HEALTH CLUB", r"FITNESS",
        r"GYM", r"ATHLETIC CLUB", r"TENNIS", r"RECREATION(?:AL)?(?: CENTER| FACILITY)?"), "Sports & Entertainment", "", 0.55),
    (_w(r"DATA CENTERS?", r"SERVER FARM", r"COLOCATION"), "Industrial", "Data Center", 0.6),
    (_w(r"COLD STORAGE", r"REFRIGERAT(?:ED|ION)", r"FREEZER", r"ICE PLANT"), "Industrial", "Refrigeration/Cold Storage", 0.6),
    (_w(r"TRUCK TERMINAL", r"FREIGHT TERMINAL", r"TRUCKING", r"MOTOR FREIGHT", r"TRUCK STOP TERMINAL"), "Industrial", "Truck Terminal", 0.6),
    (_w(r"DISTRIBUTION(?: CENTER| WAREHOUSE)?", r"FULFILLMENT", r"LOGISTICS"), "Industrial", "Distribution", 0.6),
    (_w(r"RESEARCH AND DEVELOPMENT", r"R AND D"), "Industrial", "R&D", 0.6),
    (_w(r"LABORATOR(?:Y|IES)", r"LABS?", r"BIOTECH", r"LIFE SCIENCES?", r"RESEARCH LAB"), "Lab", "", 0.55),
    (_w(r"TELECOM(?:MUNICATIONS?)?", r"TELEPHONE (?:EXCHANGE|BUILDING)", r"CELL (?:TOWER|SITE)", r"COMMUNICATIONS? (?:TOWER|FACILITY)",
        r"RADIO (?:TOWER|STATION)", r"TV STATION", r"TOWER SITE", r"SWITCHING STATION"), "Industrial", "Telecom Facility", 0.6),
    (_w(r"SALVAGE", r"JUNK ?YARD", r"RECYCL(?:ING|ER)", r"SCRAP", r"CONTRACTORS? (?:YARD|STORAGE|SHOP)", r"EQUIPMENT (?:YARD|STORAGE|RENTAL)",
        r"SERVICE (?:GARAGE|SHOP|BUILDING)", r"MAINTENANCE (?:SHOP|FACILITY|BUILDING)", r"MACHINE SHOP"), "Industrial", "Service", 0.6),
    (_w(r"MANUFACTUR(?:ING|ED|ER)", r"FACTORY", r"FACTORIES", r"ASSEMBLY PLANT", r"FOUNDRY", r"FABRICATION", r"PROCESSING PLANT",
        r"PRINTING", r"CANNERY", r"BREWERY", r"BOTTLING", r"SAWMILL", r"LUMBER MILL", r"MILL", r"REFINERY",
        r"CHEMICAL PLANT", r"INDUSTRIAL PLANT", r"PLANT"), "Industrial", "Manufacturing", 0.6),
    (_w(r"WAREHOUSES?", r"WHSE", r"STORAGE (?:BUILDING|WAREHOUSE|BLDG)"), "Industrial", "Warehouse", 0.6),
    (_w(r"SHOWROOMS?"), "Industrial", "Showroom", 0.5),
    (_w(r"INDUSTRIAL", r"LIGHT INDUSTRY", r"HEAVY INDUSTRY", r"MFG"), "Industrial", "", 0.55),
    # retail specifics before the generic store word
    (_w(r"GAS STATIONS?", r"SERVICE STATIONS?", r"FILLING STATIONS?", r"FUEL(?: STATION)?", r"GASOLINE", r"TRUCK STOPS?",
        r"CONVENIENCE STORE WITH (?:GAS|FUEL)", r"TRAVEL CENTER"), "Retail", "Service/Gas Station", 0.6),
    (_w(r"CONVENIENCE(?: STORE| MARKET)?", r"GROCER(?:Y|IES)", r"SUPER ?MARKETS?", r"FOOD (?:STORE|MARKET)"),
     "Retail", "Grocery/Convenience Store", 0.6),
    (_w(r"PHARMAC(?:Y|IES)", r"DRUG ?STORES?"), "Retail", "Drug Store", 0.6),
    (_w(r"BANKS?", r"SAVINGS AND LOAN", r"CREDIT UNIONS?", r"FINANCIAL INSTITUTIONS?", r"BRANCH BANK"), "Retail", "Bank", 0.6),
    (_w(r"AUTO(?:MOBILE|MOTIVE)? (?:DEALERS?|DEALERSHIPS?|SALES|SHOWROOM|AGENCY|REPAIR|SERVICE|BODY|PARTS)",
        r"CAR (?:DEALERS?|DEALERSHIPS?|LOTS?|SALES)", r"USED CARS?", r"DEALERSHIPS?", r"BODY SHOPS?", r"TIRE (?:STORE|SHOP|CENTER)",
        r"QUICK LUBE", r"OIL CHANGE", r"MUFFLER", r"RV SALES", r"BOAT SALES", r"MOTORCYCLE"), "Retail", "Vehicle Related", 0.6),
    (_w(r"RESTAURANTS?", r"FAST FOOD", r"CAFES?", r"CAFETERIA", r"DINERS?", r"DRIVE ?IN RESTAURANT", r"BARS?", r"TAVERNS?",
        r"PUBS?", r"NIGHT ?CLUBS?", r"LOUNGES?", r"FOOD SERVICES?", r"EATING", r"DRINKING", r"QSR"), "Restaurant", "", 0.6),
    # shopping centers
    (_w(r"SUPER REGIONAL(?: MALL| CENTER)?"), "Shopping Center", "Super Regional Mall", 0.6),
    (_w(r"REGIONAL (?:SHOPPING )?(?:MALL|CENTER)", r"ENCLOSED MALL"), "Shopping Center", "Regional Mall", 0.6),
    (_w(r"OUTLET (?:CENTER|MALL)", r"FACTORY OUTLET"), "Shopping Center", "Outlet Center", 0.6),
    (_w(r"POWER CENTER", r"BIG BOX CENTER"), "Shopping Center", "Power Center", 0.6),
    (_w(r"LIFESTYLE CENTER"), "Shopping Center", "Lifestyle Center", 0.6),
    (_w(r"STRIP (?:CENTER|MALL|SHOPPING)", r"MINI ?MALL"), "Shopping Center", "Strip Center", 0.6),
    (_w(r"NEIGHBORHOOD (?:SHOPPING )?CENTER"), "Shopping Center", "Neighborhood Center", 0.6),
    (_w(r"COMMUNITY SHOPPING CENTER"), "Shopping Center", "Community Center", 0.6),
    (_w(r"SHOPPING (?:CENTER|MALL|PLAZA)", r"MALLS?"), "Shopping Center", "", 0.55),
    (_w(r"DEPARTMENT STORES?", r"DISCOUNT STORES?", r"BIG BOX", r"HOME IMPROVEMENT", r"FREESTANDING"), "Retail", "Freestanding", 0.6),
    (_w(r"STOREFRONT", r"STORE FRONT"), "Retail", "Storefront", 0.6),
    (_w(r"RETAIL", r"STORES?", r"SHOPS?", r"SALESROOM", r"MERCANTILE", r"COMMERCIAL RETAIL"), "Retail", "", 0.55),
    # office
    (_w(r"LOFTS?", r"CREATIVE (?:SPACE|OFFICE)", r"CO ?WORKING"), "Office", "Loft/Creative Space", 0.55),
    (_w(r"OFFICES?(?: BUILDING| BLDG| PARK| CONDO(?:MINIUM)?)?", r"PROFESSIONAL (?:BUILDING|BLDG|OFFICE)", r"CORPORATE (?:CAMPUS|HEADQUARTERS|HQ)",
        r"ADMINISTRATIVE", r"BUSINESS PARK"), "Office", "", 0.6),
    # schools after day care, before generic institutional
    (_w(r"SCHOOLS?", r"ACADEM(?:Y|IES)", r"COLLEGES?", r"UNIVERSIT(?:Y|IES)", r"EDUCATION(?:AL)?", r"CLASSROOMS?", r"CAMPUS"),
     "Specialty", "Schools", 0.55),
    # residential income before multifamily
    (_w(r"DUPLEX(?:ES)?", r"TRIPLEX(?:ES)?", r"FOURPLEX(?:ES)?", r"QUADPLEX(?:ES)?", r"QUADRUPLEX", r"2 TO 4 UNITS?", r"2 4 UNITS?",
        r"TWO FAMILY", r"THREE FAMILY", r"FOUR FAMILY", r"2 FAMILY", r"3 FAMILY", r"4 FAMILY", r"TWO TO FOUR"),
     "Residential Income", "", 0.6),
    (_w(r"HIGH ?RISE(?: APARTMENTS?| RESIDENTIAL)?"), "Multifamily", "High-Rise", 0.55),
    (_w(r"MID ?RISE"), "Multifamily", "Mid-Rise", 0.55),
    (_w(r"LOW ?RISE", r"WALK ?UP"), "Multifamily", "Low-Rise", 0.55),
    (_w(r"GARDEN (?:APARTMENTS?|STYLE)"), "Multifamily", "Garden", 0.6),
    (_w(r"APARTMENTS?", r"APTS?", r"MULTI ?FAMILY", r"MULTIPLE FAMILY", r"FIVE OR MORE", r"5 OR MORE UNITS?", r"5 UNITS",
        r"MULTI FAMILY COMMERCIAL", r"MULTIFAMILY"), "Multifamily", "", 0.6),
    # agricultural land
    (_w(r"FARMS?", r"AGRICULTUR(?:E|AL)", r"RANCH(?:ES)?", r"TIMBER(?:LAND)?", r"ORCHARDS?", r"CROPS?", r"PASTURE",
        r"FOREST(?:RY)?", r"GREENBELT", r"DAIRY", r"POULTRY", r"LIVESTOCK", r"VINEYARDS?", r"WOODLAND"), "Land", "Agricultural", 0.6),
    # institutional and government, after every specific use above
    (_w(r"EXEMPT", r"GOVERNMENT(?:AL)?", r"GOVT", r"MUNICIPAL", r"COUNTY OWNED", r"STATE OWNED", r"CITY OWNED", r"FEDERAL",
        r"PUBLIC (?:LAND|PROPERTY|USE|BUILDING)", r"UTILIT(?:Y|IES)", r"RIGHT OF WAY", r"PARKS? AND REC(?:REATION)?",
        r"POLICE", r"FIRE (?:STATION|HALL)", r"LIBRAR(?:Y|IES)", r"COURTHOUSE", r"JAIL", r"PRISON", r"POST OFFICE",
        r"INSTITUTIONAL", r"CHARITABLE", r"NON ?PROFIT", r"FRATERNAL", r"LODGE HALL"), "Civic/Exempt", "", 0.5),
    # residential last, so "commercial condominium" never reads as a home
    (_w(r"MOBILE HOMES?", r"MANUFACTURED HOMES?", r"MODULAR"), "Residential", "", 0.6),
    (_w(r"SINGLE ?FAMILY", r"SFR", r"SFD", r"RESIDENTIAL", r"RESIDENCES?", r"DWELLINGS?", r"HOUSES?", r"TOWN ?HOUSES?",
        r"TOWN ?HOMES?", r"CONDO(?:MINIUM)?S?", r"ROW ?HOUSES?", r"ZERO LOT", r"PATIO HOMES?", r"CABINS?", r"COTTAGES?",
        r"HOMESITES?"), "Residential", "", 0.6),
    # generic commercial: a class we cannot pin down, kept low so it is reviewed
    (_w(r"COMMERCIAL", r"NON ?RESIDENTIAL", r"BUSINESS", r"INCOME PROPERTY", r"MIXED USE", r"MULTI ?USE"), "Retail", "", 0.35),
]

_LAND_SUBS = [
    (_w(r"COMMERCIAL", r"COMM", r"BUSINESS", r"RETAIL", r"OFFICE"), "Commercial"),
    (_w(r"INDUSTRIAL", r"IND", r"MANUFACTURING"), "Industrial"),
    (_w(r"RESIDENTIAL", r"RES", r"MULTI ?FAMILY", r"APARTMENTS?", r"SINGLE FAMILY", r"SFR", r"HOMESITE"), "Residential/Multifamily"),
    (_w(r"AGRICULTUR(?:E|AL)", r"FARM", r"TIMBER", r"RANCH"), "Agricultural"),
]

# ---- NAME vocabulary (business / tenant names on the parcel) ---------------
def _load_lines(name):
    path = os.path.join(_DATA, name)
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.split("#", 1)[0].strip()
            if line:
                out.append(line)
    return out


_FUEL = _load_lines("fuel_brands.txt")
FUEL_RX = _w(*_FUEL) if _FUEL else re.compile(r"(?!x)x")

NAME_RULES = [
    (_w(r"PUBLIC STORAGE", r"EXTRA SPACE", r"CUBE ?SMART", r"LIFE STORAGE", r"U ?HAUL", r"STORAGE MART", r"STORQUEST",
        r"SIMPLY SELF STORAGE", r"SECURCARE", r"STORAGE KING", r"SAFEGUARD SELF", r"STORAGE"), "Specialty", "Self-Storage", 0.6),
    (_w(r"MARRIOTT", r"HILTON", r"HYATT", r"HOLIDAY INN", r"HAMPTON INN", r"COMFORT INN", r"COMFORT SUITES", r"BEST WESTERN",
        r"DAYS INN", r"SUPER 8", r"LA QUINTA", r"RED ROOF", r"MOTEL 6", r"RESIDENCE INN", r"COURTYARD", r"FAIRFIELD INN",
        r"SLEEP INN", r"QUALITY INN", r"EMBASSY SUITES", r"DOUBLETREE", r"SPRINGHILL SUITES", r"TOWNEPLACE", r"CANDLEWOOD",
        r"STAYBRIDGE", r"HOME2", r"ECONO LODGE", r"AMERICINN", r"WYNDHAM", r"RAMADA", r"HOTEL", r"MOTEL", r"INN"),
     "Hospitality", "Hotel", 0.6),
    (_w(r"CAR ?WASH", r"EXPRESS WASH", r"AUTO SPA"), "Specialty", "Car Wash", 0.6),
    (_w(r"WALGREENS?", r"CVS", r"RITE AID", r"PHARMACY"), "Retail", "Drug Store", 0.6),
    (_w(r"KROGER", r"PUBLIX", r"ALDI", r"LIDL", r"FOOD LION", r"INGLES", r"HARRIS TEETER", r"WINN DIXIE", r"SAFEWAY",
        r"ALBERTSONS", r"MEIJER", r"HY VEE", r"PIGGLY WIGGLY", r"SPROUTS", r"WHOLE FOODS", r"TRADER JOE", r"FOOD CITY",
        r"GROCERY", r"SUPERMARKET", r"MARKET"), "Retail", "Grocery/Convenience Store", 0.55),
    (_w(r"WALMART", r"WAL MART", r"HOME DEPOT", r"LOWES HOME", r"MENARDS", r"TRACTOR SUPPLY", r"DOLLAR GENERAL",
        r"DOLLAR TREE", r"FAMILY DOLLAR", r"BIG LOTS", r"AUTOZONE", r"ADVANCE AUTO", r"O ?REILLY AUTO", r"NAPA AUTO",
        r"TARGET CORP", r"TARGET STORE", r"BEST BUY", r"PETSMART", r"PETCO", r"ACADEMY SPORTS"), "Retail", "Freestanding", 0.6),
    (_w(r"MCDONALDS", r"WENDYS", r"BURGER KING", r"TACO BELL", r"KFC", r"CHICK FIL A", r"SUBWAY", r"ARBYS", r"SONIC DRIVE",
        r"HARDEES", r"DAIRY QUEEN", r"CRACKER BARREL", r"WAFFLE HOUSE", r"STARBUCKS", r"DUNKIN", r"POPEYES", r"ZAXBYS",
        r"CULVERS", r"DOMINOS", r"PIZZA HUT", r"PAPA JOHNS", r"APPLEBEES", r"CHILIS", r"OLIVE GARDEN", r"RED LOBSTER",
        r"OUTBACK", r"TEXAS ROADHOUSE", r"IHOP", r"DENNYS", r"BOJANGLES", r"CHIPOTLE", r"PANERA", r"KRYSTAL",
        r"WHITE CASTLE", r"JACK IN THE BOX", r"WHATABURGER", r"RAISING CANES", r"COOK OUT", r"STEAK N SHAKE",
        r"BOB EVANS", r"GOLDEN CORRAL", r"RUBY TUESDAY", r"BUFFALO WILD WINGS", r"RESTAURANT", r"GRILL", r"CAFE",
        r"DINER", r"PIZZA", r"BBQ", r"BARBECUE", r"TAVERN", r"BREWING", r"KITCHEN"), "Restaurant", "", 0.6),
    (_w(r"BANK", r"CREDIT UNION", r"SAVINGS", r"TRUIST", r"REGIONS", r"WELLS FARGO", r"CHASE", r"PNC", r"FIFTH THIRD",
        r"HUNTINGTON", r"FIRST HORIZON", r"PINNACLE"), "Retail", "Bank", 0.55),
    (_w(r"KINDERCARE", r"BRIGHT HORIZONS", r"LA PETITE", r"GODDARD SCHOOL", r"PRIMROSE SCHOOL", r"CHILDCARE", r"CHILD CARE",
        r"DAY ?CARE", r"PRESCHOOL", r"LEARNING CENTER", r"MONTESSORI"), "Retail", "Day Care Facility/Nursery", 0.6),
    (_w(r"DENTAL", r"DENTISTRY", r"ORTHODONTIC", r"CHIROPRACTIC", r"DERMATOLOGY", r"OPTOMETRY", r"PHYSICAL THERAPY",
        r"URGENT CARE", r"DIALYSIS", r"PEDIATRICS", r"FAMILY MEDICINE", r"MEDICAL", r"CLINIC", r"HEALTH CENTER",
        r"VETERINARY", r"ANIMAL HOSPITAL"), "Office", "Medical", 0.55),
    (_w(r"HOSPITAL", r"REGIONAL MEDICAL CENTER"), "Health Care", "Hospital", 0.6),
    (_w(r"ASSISTED LIVING", r"MEMORY CARE", r"SUNRISE SENIOR", r"BROOKDALE"), "Health Care", "Assisted Living", 0.6),
    (_w(r"NURSING", r"REHABILITATION", r"REHAB CENTER", r"HEALTH AND REHAB"), "Health Care", "Skilled Nursing Facility", 0.55),
    (_w(r"CHURCH", r"BAPTIST", r"METHODIST", r"CATHOLIC", r"LUTHERAN", r"PRESBYTERIAN", r"EPISCOPAL", r"ASSEMBLY OF GOD",
        r"MINISTRIES", r"TABERNACLE", r"CONGREGATION", r"MOSQUE", r"TEMPLE", r"SYNAGOGUE"), "Specialty", "Religious Facility", 0.6),
    (_w(r"SCHOOL", r"ACADEMY", r"COLLEGE", r"UNIVERSITY"), "Specialty", "Schools", 0.55),
    (_w(r"CINEMA", r"THEATER", r"THEATRE", r"AMC", r"REGAL", r"CINEMARK"), "Sports & Entertainment", "Theater/Concert Hall", 0.6),
    (_w(r"GOLF", r"COUNTRY CLUB", r"DRIVING RANGE"), "Sports & Entertainment", "Golf / Driving Range", 0.6),
    (_w(r"MARINA", r"YACHT CLUB", r"BOAT DOCK"), "Specialty", "Marina", 0.6),
    (_w(r"PARKING", r"LAZ PARKING", r"PARK N RIDE"), "Specialty", "Parking", 0.55),
    (_w(r"FUNERAL", r"MORTUARY", r"MEMORIAL GARDENS?", r"CEMETERY"), "Specialty", "Other Specialty", 0.6),
    (_w(r"APARTMENTS?", r"APTS"), "Multifamily", "", 0.55),
]

# ---- OWNER vocabulary: supporting evidence only ---------------------------
OWNER_RULES = [
    (_w(r"HOTEL", r"HOSPITALITY", r"LODGING", r"MOTEL", r"INNS?"), "Hospitality", "Hotel", 0.4),
    (_w(r"SELF STORAGE", r"STORAGE"), "Specialty", "Self-Storage", 0.4),
    (_w(r"APARTMENTS?", r"APTS"), "Multifamily", "", 0.4),
    (_w(r"CHURCH", r"MINISTR(?:Y|IES)", r"DIOCESE", r"PARISH", r"BAPTIST", r"METHODIST", r"CATHOLIC", r"PRESBYTERIAN",
        r"LUTHERAN", r"EPISCOPAL", r"CONGREGATION", r"TABERNACLE", r"ASSEMBLY OF GOD"), "Specialty", "Religious Facility", 0.45),
    (_w(r"FARMS?", r"RANCH"), "Land", "Agricultural", 0.35),
    (_w(r"SENIOR", r"RETIREMENT", r"ASSISTED LIVING", r"NURSING"), "Health Care", "", 0.4),
    (_w(r"PARKING"), "Specialty", "Parking", 0.4),
    (_w(r"INDUSTRIAL", r"WAREHOUSE", r"LOGISTICS", r"DISTRIBUTION"), "Industrial", "", 0.35),
    (_w(r"MEDICAL", r"HEALTHCARE", r"HEALTH CARE", r"DENTAL"), "Office", "Medical", 0.35),
]


def _land(text_):
    for rx, sub in _LAND_SUBS:
        if rx.search(text_):
            return sub
    return ""


# Words that describe a building's form or a qualifier rather than a second
# use: "condo office", "dwelling converted to office", "warehouse, 6-15%
# office", "motels and tourist cabins", "commercial vacant land". A second hit
# in one of these classes never counts as a mixed use.
_NOT_A_SECOND_USE = {"Residential", "Land", "Civic/Exempt"}
_QUALIFIER_PAIRS = {("Industrial", "Office"), ("Multifamily", "Residential")}


def _match(rules, s, kind):
    t = clean(s)
    if not t:
        return None, None
    first, first_span, conflict = None, None, None
    for rx, ac, st, conf in rules:
        if first is None:
            m = rx.search(t)
        else:
            rest = t[:first_span[0]] + " | " + t[first_span[1]:]
            m = rx.search(rest)
        if not m:
            continue
        hit = Hit(ac, st, conf, "%s keyword '%s'" % (kind, m.group(0).lower()))
        if first is None:
            if ac == "Land" and not st and kind == "use":
                sub = _land(t[:m.start()] + " " + t[m.end():])
                if sub:
                    hit = Hit("Land", sub, conf, hit.reason)
            first, first_span = hit, m.span()
            continue
        if (hit.asset_class != first.asset_class and hit.confidence >= 0.55
                and first.asset_class != "Land" and hit.asset_class not in _NOT_A_SECOND_USE
                and (first.asset_class, hit.asset_class) not in _QUALIFIER_PAIRS):
            conflict = hit
            break
    return first, conflict


def use(s):
    """Classify a land-use description, structure type or property type."""
    hit, conflict = _match(USE_RULES, s, "use")
    return hit, conflict


def name(s):
    """Classify a business or tenant name. Fuel brands are a gas station."""
    t = clean(s)
    if t and FUEL_RX.search(t):
        return Hit("Retail", "Service/Gas Station", 0.6, "fuel brand '%s'" % FUEL_RX.search(t).group(0).lower()), None
    return _match(NAME_RULES, s, "name")


def owner(s):
    """Supporting hint from an owner name (low confidence, never decisive)."""
    hit, _ = _match(OWNER_RULES, s, "owner")
    return hit


def has_fuel_brand(s):
    t = clean(s)
    return bool(t and FUEL_RX.search(t))


# ---- zoning: a confirming signal, rarely decisive --------------------------
_ZONING = [
    (re.compile(r"^(?:AG|A|AGR|AR|EA|RA)(?:\b|\d)"), "Land", "Agricultural", 0.35),
    (re.compile(r"^(?:M|I|IL|LI|IH|HI|IND|MI|IG|IP|BP)(?:\b|\d|$)"), "Industrial", "", 0.4),
    (re.compile(r"^(?:O|OP|OF|OI|OB|PO)(?:\b|\d|$)"), "Office", "", 0.4),
    (re.compile(r"^(?:RM|MF|RMF|RH|R ?[3-9]|RA ?[3-9])(?:\b|\d|$)"), "Multifamily", "", 0.35),
    (re.compile(r"^(?:R|RS|RE|SF|RR|R ?1|R ?2)(?:\b|\d|$)"), "Residential", "", 0.35),
    (re.compile(r"^(?:MU|MX|PUD|PD|TC|TOD)(?:\b|\d|$)"), None, None, 0.0),
    (re.compile(r"^(?:C|B|CN|NC|CG|GC|CC|CBD|D|DT|CS|SC|HC|GB|NB|CB)(?:\b|\d|$)"), "Retail", "", 0.35),
]


def zoning(code, desc=None):
    """Zoning code or description -> a weak class hint."""
    if desc:
        hit, _ = use(desc)
        if hit:
            return Hit(hit.asset_class, hit.sub_type, min(hit.confidence, 0.45), "zoning " + hit.reason)
    z = clean(code)
    if not z:
        return None
    z = z.replace(" ", "")
    for rx, ac, st, conf in _ZONING:
        if rx.match(z):
            if not ac:
                return None
            return Hit(ac, st, conf, "zoning code '%s'" % code)
    hit, _ = use(code)
    if hit:
        return Hit(hit.asset_class, hit.sub_type, min(hit.confidence, 0.45), "zoning " + hit.reason)
    return None
