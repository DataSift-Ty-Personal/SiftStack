"""Classify foreclosure notices — keep only first-to-market trustee sales.

Title variations observed on tnpublicnotice.com (Feb 2026):
  - SUBSTITUTE TRUSTEE'S NOTICE OF SALE
  - SUBSTITUTE TRUSTEE'S SALE
  - SUBSTITUTE TRUSTEE'S NOTICE OF FORECLOSURE SALE
  - SUCCESSOR TRUSTEE'S NOTICE OF SALE OF REAL ESTATE
  - NOTICE OF TRUSTEE'S SALE
  - NOTICE OF TRUSTEE'S FORECLOSURE SALE
  - NOTICE OF SUBSTITUTE TRUSTEE'S SALE
  - NOTICE OF DEFAULT AND FORECLOSURE SALE
  - FORECLOSURE SALE NOTICE
"""

import logging

from notice_parser import NoticeData

logger = logging.getLogger(__name__)

# ── Inclusion keywords ─────────────────────────────────────────────────
# These phrases identify real first-to-market foreclosure / trustee sale notices.
# Matched case-insensitively against the full notice text.
INCLUDE_PHRASES = [
    # Substitute trustee variants
    "substitute trustee's notice of sale",
    "substitute trustee's sale",
    "substitute trustee's notice of foreclosure sale",
    "substitute trustee sale",
    "substituted trustee's sale",
    "substituted trustee sale",
    "notice of substitute trustee's sale",
    "notice of substitute trustee sale",
    # Successor trustee
    "successor trustee's notice of sale",
    "successor trustee's sale",
    "successor trustee sale",
    # General trustee sale
    "notice of trustee's sale",
    "notice of trustee's foreclosure sale",
    "notice of trustee sale",
    "trustee's sale",
    "trustee sale",
    # Default / foreclosure sale (with trustee guard below)
    "notice of default and foreclosure sale",
    "foreclosure sale notice",
    "notice of foreclosure sale",
    # Generic — only accepted if "trustee" also appears
    "notice of sale",
]

# ── Exclusion keywords ─────────────────────────────────────────────────
# These override inclusion — the notice is NOT a first-to-market foreclosure.
EXCLUDE_PHRASES = [
    "non-resident notice",
    "non resident notice",
    "nonresident notice",
    "order of publication",
    "notice to creditors",
    "notice of lien",
    "order to sell",
    "divorce",
    "dissolution",
]


# Language that means "this is a trustee/foreclosure sale", used to catch a
# foreclosure notice that surfaced in a NON-foreclosure saved search. The
# probate saved search matches on the word "probate", which also appears in
# trustee sale notices ("...the estate of the deceased borrower, probate..."),
# so a successor-trustee sale can land in the probate results and be uploaded as
# a probate lead with the trustee's law firm parsed as the personal
# representative. Seen live on a Marinosci Law Group successor-trustee notice.
_TRUSTEE_SALE_MARKERS = (
    "substitute trustee's sale",
    "substitute trustee sale",
    "successor trustee's sale",
    "successor trustee sale",
    "notice of trustee's sale",
    "notice of trustee sale",
    "notice of substitute trustee",
    "notice of foreclosure sale",
    "foreclosure sale notice",
    "notice of default and foreclosure",
)

# Markers that a notice really is a probate filing. A trustee sale can MENTION
# an estate, so the reclassification below requires trustee-sale language AND
# the absence of a genuine probate anchor.
_PROBATE_ANCHORS = (
    "notice to creditors",
    "letters testamentary",
    "letters of administration",
    "personal representative",
    "executor",
    "executrix",
    "administratrix",
    "probate division",
    "claims against the estate",
)


def looks_like_trustee_sale(notice: NoticeData) -> bool:
    """True when a non-foreclosure notice is actually a trustee/foreclosure sale.

    Used to keep foreclosure notices out of the probate pipeline, where they
    would otherwise be uploaded to the Probate list with a law firm as the
    decision maker.
    """
    text = (notice.raw_text or "").lower()
    if not text:
        return False
    if not any(m in text for m in _TRUSTEE_SALE_MARKERS):
        return False
    # A real probate filing that happens to reference a trustee is not a sale.
    return not any(a in text for a in _PROBATE_ANCHORS)


def is_valid_foreclosure(notice: NoticeData) -> bool:
    """Determine if a foreclosure notice is a real first-to-market trustee sale.

    Non-foreclosure notice types (tax_sale, tax_lien, probate) pass through,
    EXCEPT when the body is plainly a trustee sale that leaked into another
    saved search: see looks_like_trustee_sale.
    """
    if notice.notice_type != "foreclosure":
        if looks_like_trustee_sale(notice):
            logger.debug(
                "Excluded %s (body is a trustee sale, not a %s): %s",
                notice.notice_type, notice.notice_type, notice.source_url,
            )
            return False
        return True  # Non-foreclosure notices pass through unfiltered

    text = notice.raw_text.lower()

    if not text:
        logger.debug("Excluded foreclosure (empty text): %s", notice.source_url)
        return False

    # Check exclusions first — they take priority
    for phrase in EXCLUDE_PHRASES:
        if phrase in text:
            logger.debug("Excluded foreclosure (matched '%s'): %s", phrase, notice.source_url)
            return False

    # Check for inclusion phrases
    for phrase in INCLUDE_PHRASES:
        if phrase in text:
            # "notice of sale" alone isn't specific enough —
            # must also mention a trustee somewhere in the text
            if phrase == "notice of sale" and "trustee" not in text:
                continue
            return True

    # No inclusion phrase matched — exclude by default
    logger.debug("Excluded foreclosure (no trustee sale language): %s", notice.source_url)
    return False
