"""Mail vendors that send, behind the interface in mail.Vendor.

SiftMail is DataSift's own direct mail, printed by Open Letter Connect. It is the default for an
account that has it, for one reason: it moves the record's own counters. After a send, DataSift
sets directmail_attempts and last_direct_mailed and tracks each piece's status (mailed, returned,
undeliverable), which is exactly what the mail presets key on. An outside vendor needs the counter
write-back in mail.log_attempt and still cannot feed Last Mail Status.

The route is the one the app's Send To > Direct Mail uses (read from the public bundle on
2026-11-11; the app sends a staff JWT, and the Open API key is untested on it):
    POST /api/internal/property/direct-mail-campaign/
      {query: {must: {properties: [...]}}, title, campaign_type, estimate: 1|0, start_date,
       confirmation_date (launch only), touches: [{template, external_product_id, envelope, return_*,
       tracking_phone, send_date, delivery_period, number_of_batches, ...}]}
estimate: 1 is a quote. The stage always quotes first and launches only when the quote covers exactly
the records it asked for: scoping is where this API family has bitten before.

Open Letter product ids, as the app labels them: 13 Postcard 4x6, 14 Postcard 6x9, 15 Postcard 6x11,
5 Personal Letter, 1 Professional Letter (windowed), 3 Professional Letter, 9 Bi-fold, 11 Tri-fold,
16 Real Penned Letter. Envelopes: 5 A6 White, 7 A6 Grey Herringbone, 1 Windowed, 3 Non-Windowed.
"""
from __future__ import annotations

from datetime import date

from .mail import Vendor

# ty+2's live SiftMail price table (GET /accounting/direct-mail-pricing/, business plan, read
# 2026-11-11): 13 Postcard 4x6 $0.67, 14 6x9 $0.74, 15 6x11 $1.01, 5 Personal Letter $1.23,
# 1 Professional Letter $1.10, 3 $1.22, 11 Tri-fold $1.18, 16 Real Penned $1.89. Used only to price a
# plan and to hold the caps; a launch is checked against DataSift's own quote.
SIFTMAIL_PRICES = {"handwritten_letter": 1.89, "family_postcard": 0.67, "soft_offer_check": 1.23,
                   "postcard": 0.67, "letter": 1.23}


def _first(d: dict, *keys):
    for k in keys:
        if isinstance(d, dict) and d.get(k) is not None:
            return d.get(k)
    return None


class SiftMail(Vendor):
    name = "siftmail"
    export_only = False
    counts_itself = True
    batch = True

    def __init__(self, prices: dict | None = None):
        super().__init__(None)
        self.prices = dict(SIFTMAIL_PRICES)
        self.prices.update(prices or {})

    def touch(self, spec: dict, sender: dict, today: date | None = None) -> tuple[dict | None, str]:
        """(touch, "") or (None, why). A piece needs its Mail Design template and product id."""
        design = str(spec.get("design") or "")
        product = spec.get("product_id")
        if not design or product is None:
            return None, "no SiftMail design and product_id set for %s (list them: cli.py autopilot mail-designs)" % spec.get("preset")
        if not (sender.get("street") and sender.get("city") and sender.get("state") and sender.get("zip")):
            return None, "no return address (set AUTOPILOT_MAIL_FROM_* or sender in the playbook)"
        t = {"order": 0, "contact_name": sender.get("name") or "", "return_name": sender.get("name") or "",
             "return_address": sender["street"], "return_city": sender["city"], "return_state": sender["state"],
             "return_postal_code": sender["zip"], "tracking_phone": sender.get("phone") or "",
             "website": sender.get("website") or "", "email": sender.get("email") or "",
             "template": design, "external_identifier": spec.get("external_identifier") or "",
             "external_product_id": product, "street_view_enabled": bool(spec.get("street_view", False)),
             "send_date": (today or date.today()).isoformat(), "delivery_period": 1, "number_of_batches": 1}
        if spec.get("envelope") is not None:
            t["envelope"] = spec["envelope"]
        return t, ""

    @staticmethod
    def read_quote(r) -> tuple[int | None, float | None]:
        """(records, cost) out of an estimate response, whatever its exact keys turn out to be."""
        if not isinstance(r, dict):
            return None, None
        n = _first(r, "number_of_records", "records", "count", "total_records", "number_of_mails")
        c = _first(r, "cost", "total_cost", "estimated_cost", "total", "price")
        try:
            return (int(n) if n is not None else None), (float(c) if c is not None else None)
        except (TypeError, ValueError):
            return None, None

    def quote(self, crm, uuids: list[str], touch: dict, title: str) -> tuple[int | None, float | None, dict]:
        r = crm.mail_campaign(uuids, title, [touch], estimate=True)
        n, c = self.read_quote(r)
        return n, c, r if isinstance(r, dict) else {}

    def launch(self, crm, uuids: list[str], touch: dict, title: str) -> dict:
        r = crm.mail_campaign(uuids, title, [touch], estimate=False)
        return r if isinstance(r, dict) else {"raw": str(r)[:200]}


def make(name: str, prices: dict | None = None) -> Vendor:
    if name == "siftmail":
        return SiftMail(prices)
    raise KeyError(name)
