"""Pydantic v2 models for the deep_prospecting module.

These dataclasses are the contract between phases:
  Phase 1 (title)         → Lead
  Phase 2 (genealogy)     → HeirMap (heirs UNVERIFIED)
  Phase 2.5 (verify)      → HeirMap (heirs LIVING/DECEASED/UNVERIFIED)
                           + escalation_needed=True if 3-gen exhaustion
  Phase 3 (target)        → DecisionMaker
  Phase skiptrace         → SkipTraceResult
  Compile                 → ResearchPack (the persisted artifact)

All shapes are Pydantic v2 — runtime validation + free JSON serialization
for results.json. No @dataclass anywhere in this module to keep the
paradigm consistent. (SiftStack's NoticeData stays a dataclass; the
bridge file keeps that paradigm boundary clean.)

Date discipline: every "date" field has a sibling `*_text: str | None`
that captures the raw source string when parsing fails. Obit pages
routinely give "April 2025" or "2025" — the typed `date` is best-effort
structured, the text field is the lossless fallback.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator


# ── Shared enums ───────────────────────────────────────────────────────

Confidence = Literal["HIGH", "MEDIUM", "LOW"]
HeirStatus = Literal["LIVING", "DECEASED", "UNVERIFIED"]
DMStatus = Literal["VERIFIED_LIVING", "UNVERIFIED"]
PhoneType = Literal["MOBILE", "LANDLINE", "VOIP", "UNKNOWN"]
ResearchLevel = Literal["L1", "L2", "L3"]
NoticeType = Literal[
    "foreclosure", "probate", "tax_sale", "tax_delinquent",
    "sheriff_sale", "eviction", "code_violation", "divorce", "bankruptcy",
]
SourceID = Literal[
    "mod_iv", "obit_search", "findagrave", "legacy",
    "tps", "fps", "cbc", "google_dork",
    # Slice 2 sources
    "tracerfy",       # /v1/api/trace/lookup/ — paid per-heir skip trace
    "trestle",        # /3.0/phone_intel + /3.2/phone — scoring + reverse-phone
    "bv_manual",      # operator paste-and-parse, manual workflow (Slice 3+)
]
SourceStatus = Literal["HIT", "EMPTY", "BLOCKED", "ERROR", "SKIPPED"]
County = Literal["Essex", "Middlesex", "Somerset", "Union"]

# How the decision-maker relates to the original record. Drives the role
# tag in the DataSift CSV writer ("Subject" / "Heir" / "Executor" /
# "Family Pivot"). Phase 3's selector classifies; the writer lowercases
# + spaces (FAMILY_PIVOT → "Family Pivot") at write time.
SubjectRole = Literal["SUBJECT", "HEIR", "EXECUTOR", "FAMILY_PIVOT"]


# ── Input ──────────────────────────────────────────────────────────────


class ProspectInput(BaseModel):
    address: str | None = None
    owner: str | None = None
    docket: str | None = None
    county: County | None = None
    notice_type: NoticeType | None = None
    # DataSift `Lists` column, split on comma. Carries the operator's
    # upstream routing signal (e.g. "Probate", "Inheritance",
    # "Notice of Default (Lis Pendens)") — Phase 1 uses these to
    # confirm executor-swap cases where the contact name has already
    # been swapped to the operator-resolved executor.
    list_tags: list[str] = Field(default_factory=list)
    raw_record: dict | None = None  # CSV-row passthrough


# ── Source observability ───────────────────────────────────────────────


class SourceCheck(BaseModel):
    """Top-level checklist entry for the report's "Source Checklist" section."""
    source: SourceID
    status: SourceStatus
    notes: str = ""


class SourceState(BaseModel):
    """Per-site state during skip trace — surfaces blocking/errors so the
    operator can see which sites contributed vs which were challenged."""
    source: SourceID
    status: SourceStatus
    blocked_reason: str | None = None  # "captcha", "rate_limit", "ip_block", etc.


# ── Phase 1: Title / Lead ──────────────────────────────────────────────


class Deed(BaseModel):
    instrument_type: str  # WD, QCD, Sheriff Deed, Probate Deed, etc.
    grantor: str
    grantee: str
    recorded_date: date | None = None
    recorded_date_text: str | None = None  # raw fallback when parse fails
    consideration: float | None = None
    notes: str | None = None


class Lead(BaseModel):
    """Phase 1 output. Always returned, even sparse — never fails the run."""
    input: ProspectInput
    title_owner: str | None = None
    deed_history: list[Deed] = Field(default_factory=list)
    death_signal: bool = False
    death_signal_reason: str | None = None  # which signal fired (estate-of, probate ref, obit hit)
    # Explicit decedent name when Phase 1 has high-confidence evidence
    # (e.g. executor-swap-confirmed via Lists tag). Phase 2 uses this as
    # the obit-search target instead of guessing from title_owner.
    # Distinct from title_owner because title_owner may need
    # MOD-IV-format normalization before obit search.
    decedent_name: str | None = None
    # Operator-resolved role of the DataSift contact (First/Last Name
    # fields) when known — "executor" / "owner" / None. Set on the
    # executor-swap path so Phase 3 can confirm the DM directly without
    # re-deriving from the heir map.
    named_contact_role: str | None = None
    # Slice 5b: title_owner-alive verification. When Phase 1 set
    # executor_swap_confirmed but Phase 2 found no obit, Phase 2 runs a
    # Tracerfy reverse-address probe on the property. If the title_owner
    # is found alive at the property, these fields capture the real
    # subject (title_owner) and demote the DataSift contact to a
    # secondary (FAMILY_PIVOT). Phase 3 reads these to build the 2-DM
    # output; death_signal is flipped back to False on this branch.
    actual_subject_name: str | None = None       # title_owner verified alive
    actual_subject_first_name: str | None = None  # for skip-trace name matching
    actual_subject_last_name: str | None = None
    secondary_contact_name: str | None = None    # DataSift contact when demoted
    # Raw Tracerfy person snapshot (subset) cached so observability /
    # tests can introspect the alive evidence. Phase Skiptrace does NOT
    # currently reuse this — it re-fetches when scoring phones — so the
    # ~$0.10 duplicate cost is accepted in exchange for simpler flow.
    # Worth optimizing if the alive-verification branch fires often.
    title_owner_alive_evidence: dict | None = None
    name_variants: list[str] = Field(default_factory=list)
    red_flags: list[str] = Field(default_factory=list)
    mailing_address: str | None = None
    parcel_id: str | None = None
    # Free-text dump for non-blocking issues that don't fit a typed field.
    # Surfaces in the report's "Flags" section. Examples:
    #   - "Smarty rejected mailing address as undeliverable"
    #   - "Name variant unresolved after L2 sweep"
    #   - "Obit found but no DOD parseable"
    # If a warning becomes repetitive across runs, promote to a typed field.
    warnings: list[str] = Field(default_factory=list)


# ── Phase 2 / 2.5: Heirs ───────────────────────────────────────────────


class Heir(BaseModel):
    name: str
    relationship: str  # "spouse", "child", "sibling", "grandchild", "executor"
    city: str | None = None
    state: str | None = None
    status: HeirStatus
    dod: date | None = None             # parsed
    dod_text: str | None = None         # raw, e.g. "April 2025"
    sources: list[SourceID] = Field(default_factory=list)
    verification_notes: str | None = None


class HeirMap(BaseModel):
    decedent_name: str
    decedent_dod: date | None = None
    decedent_dod_text: str | None = None
    decedent_city: str | None = None
    heirs: list[Heir] = Field(default_factory=list)
    generations_searched: int = 0
    # Escalation signaling — distinct from `aborted` on ResearchPack.
    # An escalation is a LEGITIMATE research outcome that requires a
    # different downstream action (e.g., title attorney consult), not a
    # failure to produce a result. The skill defines L4 explicitly:
    # "Reached 3rd generation with no living heirs → escalate to title attorney."
    escalation_needed: bool = False
    escalation_reason: Literal[
        "all_heirs_exhausted_through_3_generations",
        "ambiguous_executor",
        "needs_title_attorney",
    ] | None = None


# ── Phase 3: Decision-maker ────────────────────────────────────────────


class ContactInfo(BaseModel):
    """Address + age fields bundled for the DM after skip trace."""
    addresses_current: list[str] = Field(default_factory=list)
    addresses_previous: list[str] = Field(default_factory=list)
    age_estimate: tuple[int, int] | None = None


class DecisionMaker(BaseModel):
    name: str
    relationship: str
    status: DMStatus
    subject_role: SubjectRole  # drives the Phone Tag role marker on CSV write
    contact: ContactInfo = Field(default_factory=ContactInfo)
    confidence: Confidence
    reasoning: str  # 1-2 paragraph paragraph written by Sonnet


# ── Phase Skip Trace ───────────────────────────────────────────────────


_E164_RE = re.compile(r"^\+?[1-9]\d{1,14}$")


class Phone(BaseModel):
    number: str  # E.164 normalized (e.g. "+12125551234")
    type: PhoneType
    sources: list[SourceID]
    confidence: Confidence

    # ── Slice 2 fields ─────────────────────────────────────────────────
    # person_name: which heir / DM this phone belongs to. Phase Skiptrace
    # populates this from the Tracerfy / CBC call context (the target
    # we asked about). The writer uses it to derive per-person star
    # markers — phones for the same person share a star count even when
    # sourced from different sites.
    person_name: str | None = None

    # activity_score: 0-100, populated by Phase Trestle (Phone Intel).
    # Higher = more recent activity on the line. Drives the optional
    # dial-rank label in the Phone Tags cell when Trestle scoring runs.
    activity_score: int | None = None

    # Free-cost compliance signals from Tracerfy. None when the phone
    # came from a source that doesn't expose the flag (e.g. CBC, Trestle
    # Reverse Phone). Phase Trestle does NOT backfill these — they stay
    # source-of-truth as whatever the original skip-trace returned.
    dnc: bool | None = None
    carrier: str | None = None
    is_litigator: bool | None = None  # person-level flag; same value on all phones for the same person

    @field_validator("number")
    @classmethod
    def _normalize_e164(cls, v: str) -> str:
        """Coerce common US formats into E.164 and reject invalid shapes.

        Sites format inconsistently — TPS does "(212) 555-1234", FPS does
        "212-555-1234", CBC sometimes has "+1 212 555 1234". Normalize at
        model-construction time so downstream comparisons + dedup work
        without each call site re-implementing this.
        """
        if not v:
            raise ValueError("phone number cannot be empty")
        # Strip every non-digit except a leading +.
        cleaned = re.sub(r"[^\d+]", "", v.strip())
        # If 10 digits, assume US and prepend +1.
        digits = cleaned.lstrip("+")
        if len(digits) == 10:
            cleaned = f"+1{digits}"
        elif len(digits) == 11 and digits.startswith("1"):
            cleaned = f"+{digits}"
        elif not cleaned.startswith("+"):
            cleaned = f"+{digits}"
        if not _E164_RE.match(cleaned):
            raise ValueError(f"invalid E.164 phone: {v!r}")
        return cleaned

    @property
    def csv_value(self) -> str:
        """10-digit US format expected by the DataSift CSV (no `+1`).

        DataSift exports phones as plain digits ("9177506100"). Our
        canonical model uses E.164 ("+19177506100") for de-dup safety
        across sources. Strip the `+1` prefix when we cross the CSV
        boundary so round-trip with DataSift is byte-exact.
        """
        return self.number[2:] if self.number.startswith("+1") else self.number.lstrip("+")


class Email(BaseModel):
    address: str
    sources: list[SourceID]


class Associate(BaseModel):
    name: str
    relationship: str | None = None  # "Possible Relative", "Associate", etc.
    city: str | None = None
    state: str | None = None
    sources: list[SourceID]


class SkipTraceResult(BaseModel):
    # Slice 2: when multiple heirs/DMs get skip-traced, this holds the
    # primary DM (Phase 3's #1 pick). Phones/emails for ALL traced
    # persons live in the flat lists below — each Phone carries its
    # owning person_name. Backups live on ResearchPack.decision_makers
    # so consumers that only want phone data don't need to walk the DM
    # list.
    decision_maker: DecisionMaker
    phones: list[Phone] = Field(default_factory=list)
    emails: list[Email] = Field(default_factory=list)
    associates: list[Associate] = Field(default_factory=list)
    # One entry per skip-trace site we attempted (HIT / EMPTY / BLOCKED / ERROR).
    # Slice 2: site_state entries are recorded PER-target, so a Catherine-
    # case run with 3 traced heirs will have ~3 cbc + 3 tracerfy entries.
    # The render dedupes for display.
    site_state: list[SourceState] = Field(default_factory=list)


# ── Cost tracking ──────────────────────────────────────────────────────


class CostBreakdown(BaseModel):
    """Per-API spend with a single rollup. Mirrors cost_estimator.py shape."""
    anthropic: float = 0.0
    serper: float = 0.0
    firecrawl: float = 0.0
    smarty: float = 0.0
    # Slice 2: per-vendor attribution so the Slack cost line + per-record
    # summary can show spend by source. tracerfy = /trace/lookup/ paid
    # skip-trace; trestle = /3.0/phone_intel scoring + /3.2/phone reverse
    # finder (combined into one field — both endpoints are flat-rate per
    # call and there's no operational value in splitting them).
    tracerfy: float = 0.0
    trestle: float = 0.0
    other: float = 0.0  # 2captcha, etc.

    @property
    def total(self) -> float:
        return round(
            self.anthropic + self.serper + self.firecrawl
            + self.smarty + self.tracerfy + self.trestle + self.other,
            4,
        )


# ── Research Pack (the artifact) ───────────────────────────────────────


class ResearchPack(BaseModel):
    """Compiled output written to results.json + rendered to research_pack.md.

    Note: the rendered markdown is NOT stored on this model — output.py
    renders fresh from the structured fields when needed. Keeps the model
    lean, prevents stale-by-design copies after template changes.
    """
    input: ProspectInput
    level_selected: ResearchLevel
    level_reason: str
    source_checklist: list[SourceCheck] = Field(default_factory=list)
    lead: Lead
    heir_map: HeirMap | None = None

    # Slice 2: Phase 3 returns a *list* of DMs — primary plus up to N
    # backups drawn from the verified-living heirs. L1 cases produce a
    # single-element list. Order is Phase 3's priority ranking: the
    # primary DM is decision_makers[0]. SkipTraceResult.decision_maker
    # mirrors decision_makers[0] for backward compat with the markdown
    # renderer and the CSV writer's `Subject vs Heir` star derivation.
    decision_makers: list[DecisionMaker] = Field(default_factory=list)

    skip_trace: SkipTraceResult | None = None
    cost: CostBreakdown = Field(default_factory=CostBreakdown)
    timestamp_utc: datetime
    duration_seconds: float
    # Abort signaling — distinct from heir_map.escalation_needed.
    # Aborts mean the run could not complete (cost cap, time budget,
    # unrecoverable error). Escalations mean the run completed but the
    # outcome is "needs human / attorney."
    aborted: bool = False
    abort_reason: Literal["cost_ceiling", "time_budget", "hard_failure"] | None = None
    aborted_at_phase: str | None = None  # "phase_1_title", "phase_2_genealogy", etc.

    @property
    def primary_dm(self) -> DecisionMaker | None:
        """Convenience accessor for decision_makers[0].

        Lets the renderer and the CSV writer migrate to multi-DM
        consumption incrementally — Slice 1 code that did
        `pack.decision_maker` now does `pack.primary_dm` and keeps
        working until the consumer is updated to walk the full list.
        """
        return self.decision_makers[0] if self.decision_makers else None
