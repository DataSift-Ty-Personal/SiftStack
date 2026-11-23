# The method, and every number in it

The idea is simple and old: listed houses are the most public inventory there
is, and most of them will never take a lowball. A few will. The ones that do
are sitting where everyone can see them, waiting for someone to ask. So you
ask, every week, on every listing that fits, with a number that holds up.

What this engine adds to the plain version of that method:

- The cash number comes from what investors actually paid nearby, not a flat
  share of list. A plain 70% of list leaves no room for a fee on a large share
  of listings in a typical market.
- The creative offer is built from the seller's real loan: the amount, when it
  recorded, the term, and the rate (the actual rate where public data names
  it).
- Motivation comes from the listing's own history: days on market, price cuts,
  expiries and relists, a pending that fell through, an agent change.

## 1. Which listings get looked at

- Active single-family listings in the market.
- List price between $50,000 and $700,000. Cash offers only up to $300,000;
  above that only a creative offer can fit.
- Out: new construction (built within the last two years), builder spec homes
  (a company with a builder's name owning a house built in the last five
  years), anything with no street number, and anything the Zillow check finds
  no longer for sale.

## 2. Motivation score, 0 to 100

Time on market and price cuts carry most of it, because those are the seller
telling the market the price is wrong.

| Signal | Points |
|---|---|
| Days on market | up to 30 (full credit at 180 days) |
| Price cuts | 5 each, up to 15 |
| Total cut from the original list | up to 15 (one point per percent) |
| Expired and relisted | 5 each, up to 10 |
| Back on market after a pending | 8 |
| Changed agents | 4 |
| Distress flags | up to 20 together (below) |
| Words in the listing remarks (Zillow check) | up to 10 |
| Bank-owned or foreclosure listing (Zillow check) | 6 |
| Off-market AI score | up to 15 (none at 30 or under, full at 90) |

Distress flags: probate, notice of default and foreclosure notice 10 each;
pre-probate, inherited, heirs, divorce, lis pendens, vacant and zombie 8;
obituary, estate sale, bankruptcy and tax delinquent 6; tired landlord 5;
out-of-state owner, federal lien, state tax lien and eviction 4; judgment lien
3; absentee and senior owner 2.

Remark words: as-is, investor, cash only, TLC, fixer, handyman, needs work,
motivated, estate, bring all offers, priced to sell, assumable, owner or seller
financing, rehab, flip.

A score of 45 or more counts as motivated.

Before a listing's detail is read, the screen only knows its days-on-market
bucket (30, 90 or 365 days) and its distress flags. The full score needs the
listing history from the detail.

## 3. The cash offer

1. Find investor purchases near the listing: houses whose last sale was an
   investor buy (an investor bought and still holds: the pending, rental,
   wholetail and wholesale labels) in the last 24 months.
2. Keep sales of a similar size (70% to 135% of the listing's square feet)
   within half a mile, widening to 1 and then 2 miles until 3 such sales show
   up.
3. Prefer discounted buys (paid at or under 90% of today's value). A turnkey
   rental bought at full price says nothing about what an ugly house is worth.
4. Drop deeds priced at twice the parcel's value or more. Those are portfolio
   deeds that carried other houses.
5. Median price per square foot times the listing's size is what investors pay
   for it. Less the $15,000 fee is the offer.
6. Never above 70% of list (65% when motivated).
7. No investor sales nearby: value x 60% minus repairs minus the fee. Repairs
   are a flat $15 a square foot until someone reads the photos or walks it.

Checks on the number:

- **The 60% check.** Flip resales nearby give an after-repair value. When the
  cash offer sits above that value x 60% minus repairs minus the fee, the row
  is flagged for a condition read.
- **Lowballs wait.** Under half of list, the offer is the kind that burns an
  agent. It goes to the Price watch sheet with the list price at which it
  becomes sendable (offer divided by 0.5). Re-run weekly: a price cut can move
  it into range.

On the search route a sale's price is known to within half a price band: the
search can filter by last-sale price but does not return it, so the sales are
pulled band by band and each takes its band's midpoint. Bands are $10,000 wide
under $300,000 and wider above.

## 4. The seller's loans

From each shortlisted listing's detail: every recorded loan with its amount,
record date, due date and type. A loan recorded before the last sale belongs to
the previous owner. A refinance replaces the loan before it. Credit lines,
construction and commercial loans are payoffs: they are paid at closing, never
taken over, and they stay out of the monthly payment.

The rate, best evidence first:

1. **HMDA**, the public federal mortgage disclosure. It lists nearly every
   loan made, with its actual rate, by census tract, loan type, a $10,000
   amount band and term, with no address. When exactly one HMDA loan sits on
   the key, that is the loan and that is its rate. When 2 or more sit there
   with rates within a quarter point, their median.
2. **Otherwise Freddie Mac's weekly rate** in the week the loan locked, plus a
   local spread measured on the exact matches. On one measured county the
   fallback missed the real rate by a median of about a third of a point.

Traps the model handles: HMDA used 2010 census tracts through 2021 and 2020
tracts from 2022, so each house needs both (the free Census geocoder gives
them); a loan recorded in January may have closed in December, so it also tries
the prior year; HMDA has no rates before 2018.

Then the balance and the payment are rebuilt from the original amount and the
real term (the due date shows a 15-year loan), plus FHA's annual premium (0.55%
for loans after 2023-03-20, 0.85% before), USDA's fee, or private mortgage
insurance when HMDA shows the loan started above 80% of value.

Owed / list = what is left on the term loans over the list price. The
"with credit lines" column adds the payoffs. A balance over 105% of list is
flagged for a payoff check: a short sale, or a loan on more than this house.

## 5. Subject-to

Offered on every term loan at or under 5.0%, because today's rates are well
above that and the old rate is the asset.

- Price: the list price.
- The seller nets what they owe against the list, up to $12,000 at closing.
- Equity above that is carried by the seller as a 0% note over 30 years. Its
  payment counts in the monthly cost.
- The buyer pays the listing commission (2.5%) and buyer closing costs (2%) on
  top.
- Monthly cost = principal and interest + mortgage insurance + taxes +
  insurance (0.65% of value a year) + the carry payment.
- Cash flow = 80% of the rent estimate minus the monthly cost. Cash on cash =
  cash flow x 12 / cash in (cash to seller + commission + closing).
- Under 10% cash on cash is kept and flagged: as a rental it does not pay, so
  the exit is a wrap or a resale of the loan. With no rent estimate it is kept
  with a note to confirm the rent.
- No subject-to on a loan above 5.0%, a loan under $40,000 (cheaper to pay
  off), only a credit line, a blanket loan (the same loan on 2 or more listed
  houses), or debt over 125% of the price.

## 6. Seller finance

Only on houses the data flags free and clear. "No loan on record" is often
missing data, not a paid-off house.

- Price: the list price, 10% down, 4% over 30 years, buyer pays the commission.
- It has to return at least 10% cash on cash at its own price to be offered.

## 7. Owner type

Read from the owner's name, because a company flag in property data often marks
a married couple or a family trust as a company:

- Bank or government (a lender or agency owns it: a different conversation)
- Company (including funds, acquisition trusts and iBuyers)
- Estate
- Trust
- Individual

## 8. Every rule is a lever

Each number above is a flag on the command line (`--help` lists them), so a
market that runs differently can be tuned without touching code. Change a rule,
then run `--phase offers` and `--phase export` to rebuild without pulling
again.
