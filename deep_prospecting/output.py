"""Markdown research-pack renderer.

Renders a `ResearchPack` into the 9-section markdown format defined in
the deep-prospecting skill (SPEC.md Appendix). The on-disk file is the
artifact; this module never stores rendered markdown on the model.

Public surface:
    render(pack: ResearchPack) -> str
        Returns the full markdown body. Caller writes to disk.
    write(pack: ResearchPack, out_path: Path) -> Path
        Convenience: render + write `research_pack.md`.

Design notes:
  - Sections pull only structured data from the model. If a section is
    not applicable (e.g., no heir map for an L1 run), the section is
    rendered with `_(N/A — L1 run, no genealogy required)_` rather than
    being omitted. Operator-friendly: every report has the same shape.
  - Heir-map ASCII tree mirrors the skill template byte-for-byte where
    the operator's eyes already know what to look for.
  - Skip-trace card matches the skill's ASCII template.
"""

from __future__ import annotations

import json
from pathlib import Path

from deep_prospecting.models import (
    Associate, DecisionMaker, Email, Heir, HeirMap, Phone,
    ResearchPack, SkipTraceResult, SourceCheck,
)

# Status markers per skill template.
MARK_DECEASED = "†"
MARK_LIVING = "✓"
MARK_UNVERIFIED = "?"
MARK_EXECUTOR = "★"
MARK_RECOMMENDED = "▸"


def _heir_marker(h: Heir) -> str:
    if h.status == "DECEASED":
        return MARK_DECEASED
    if h.status == "LIVING":
        return MARK_LIVING
    return MARK_UNVERIFIED


def _city_state(name: str | None, state: str | None) -> str:
    if name and state:
        return f"[{name}, {state}]"
    if name:
        return f"[{name}]"
    return ""


def _dod_str(h: Heir) -> str:
    if h.dod:
        return f" (DOD {h.dod.isoformat()})"
    if h.dod_text:
        return f" (DOD ~{h.dod_text})"
    return ""


# ── Section renderers ─────────────────────────────────────────────────


def _section_1_level(pack: ResearchPack) -> str:
    return (
        f"## 1) Level Selected & Why\n"
        f"**{pack.level_selected}** — {pack.level_reason}\n"
    )


def _section_2_checklist(pack: ResearchPack) -> str:
    if not pack.source_checklist:
        return "## 2) Source Checklist\n_(none recorded)_\n"
    lines = ["## 2) Source Checklist", ""]
    for c in pack.source_checklist:
        box = "[x]" if c.status == "HIT" else "[ ]"
        note = f" — {c.notes}" if c.notes else ""
        lines.append(f"- {box} **{c.source}** ({c.status}){note}")
    return "\n".join(lines) + "\n"


def _section_3_title(pack: ResearchPack) -> str:
    lead = pack.lead
    lines = ["## 3) Title & Ownership", ""]
    lines.append(f"- **Current owner:** {lead.title_owner or '[MISSING]'}")
    lines.append(f"- **Mailing address:** {lead.mailing_address or '[MISSING]'}")
    lines.append(f"- **Parcel ID:** {lead.parcel_id or '[MISSING]'}")
    if lead.deed_history:
        lines.append("- **Deed history:**")
        for d in lead.deed_history:
            recorded = d.recorded_date.isoformat() if d.recorded_date else (d.recorded_date_text or "?")
            lines.append(
                f"  - {d.instrument_type} | {d.grantor} → {d.grantee} | recorded {recorded}"
            )
    if lead.red_flags:
        lines.append("- **Red flags:**")
        for r in lead.red_flags:
            lines.append(f"  - {r}")
    if lead.warnings:
        lines.append("- **Flags / non-blocking warnings:**")
        for w in lead.warnings:
            lines.append(f"  - {w}")
    return "\n".join(lines) + "\n"


def _section_4_identity(pack: ResearchPack) -> str:
    lead = pack.lead
    if pack.level_selected != "L2" and not lead.name_variants:
        return "## 4) Identity Resolution\n_(N/A — no variants detected)_\n"
    lines = ["## 4) Identity Resolution", ""]
    if lead.name_variants:
        lines.append("- **Name variants seen:**")
        for v in lead.name_variants:
            lines.append(f"  - {v}")
    if lead.title_owner:
        lines.append(f"- **Resolved canonical owner:** {lead.title_owner}")
    return "\n".join(lines) + "\n"


def _section_5_genealogy(pack: ResearchPack) -> str:
    hm = pack.heir_map
    if pack.level_selected != "L3" or not hm:
        return "## 5) Genealogy / Heir Findings\n_(N/A — not an L3 run)_\n"
    lines = ["## 5) Genealogy / Heir Findings", ""]
    dod = (
        hm.decedent_dod.isoformat() if hm.decedent_dod
        else hm.decedent_dod_text or "[MISSING]"
    )
    lines.append(f"- **Decedent:** {hm.decedent_name} (DOD {dod})")
    if hm.decedent_city:
        lines.append(f"- **Decedent city:** {hm.decedent_city}")
    if hm.heirs:
        lines.append(f"- **Heirs identified:** {len(hm.heirs)}")
    return "\n".join(lines) + "\n"


def _section_6_verification_summary(pack: ResearchPack) -> str:
    hm = pack.heir_map
    if not hm:
        return "## 6) Heir Verification Summary\n_(N/A — no heir map)_\n"
    living = [h for h in hm.heirs if h.status == "LIVING"]
    deceased = [h for h in hm.heirs if h.status == "DECEASED"]
    unverified = [h for h in hm.heirs if h.status == "UNVERIFIED"]
    lines = ["## 6) Heir Verification Summary", ""]
    lines.append(f"- **Total heirs identified:** {len(hm.heirs)}")
    lines.append(f"- **Verified living:** {len(living)}" + (
        f" — {', '.join(h.name for h in living)}" if living else ""
    ))
    lines.append(f"- **Verified deceased:** {len(deceased)}" + (
        " — " + ", ".join(
            f"{h.name}{_dod_str(h)}" for h in deceased
        ) if deceased else ""
    ))
    lines.append(f"- **Unverified:** {len(unverified)}" + (
        f" — {', '.join(h.name for h in unverified)}" if unverified else ""
    ))
    lines.append(f"- **Generations searched:** {hm.generations_searched}")
    if hm.escalation_needed:
        lines.append("")
        lines.append(f"> **L4 ESCALATION RECOMMENDED — {hm.escalation_reason}**")
    return "\n".join(lines) + "\n"


def _section_7_heir_tree(pack: ResearchPack) -> str:
    hm = pack.heir_map
    if not hm:
        return "## 7) Heir Map\n_(N/A — no heir map)_\n"
    dod = (
        hm.decedent_dod.isoformat() if hm.decedent_dod
        else hm.decedent_dod_text or "?"
    )
    lines = ["## 7) Heir Map", "", "```"]
    decedent_loc = _city_state(hm.decedent_city, None)
    lines.append(f"Decedent: {MARK_DECEASED} {hm.decedent_name} (DOD {dod}) {decedent_loc}".rstrip())

    by_rel: dict[str, list[Heir]] = {}
    for h in hm.heirs:
        by_rel.setdefault(h.relationship.lower(), []).append(h)

    def _emit(group_label: str, heirs: list[Heir]):
        if not heirs:
            return
        lines.append(f"│")
        lines.append(f"├─ {group_label}:")
        for h in heirs:
            mark = _heir_marker(h)
            loc = _city_state(h.city, h.state)
            dodstr = _dod_str(h)
            lines.append(f"│  └─ {mark} {h.name} {loc}{dodstr}".rstrip())

    _emit("Spouse/Partner", by_rel.get("spouse", []))
    _emit("Children", by_rel.get("child", []))
    _emit("Siblings", by_rel.get("sibling", []))
    _emit("Grandchildren", by_rel.get("grandchild", []))
    _emit("Executor", by_rel.get("executor", []))
    others = [h for h in hm.heirs if h.relationship.lower() not in
              {"spouse", "child", "sibling", "grandchild", "executor"}]
    _emit("Other", others)

    lines.append("")
    lines.append(f"STATUS: {MARK_DECEASED}=Deceased  {MARK_LIVING}=Living  "
                 f"{MARK_UNVERIFIED}=Unverified  {MARK_EXECUTOR}=Executor  "
                 f"{MARK_RECOMMENDED}=Recommended DM")
    lines.append("```")
    return "\n".join(lines) + "\n"


def _section_8_dm(pack: ResearchPack) -> str:
    dm = pack.decision_maker
    if dm is None:
        return "## 8) Decision-Maker Identified\n_(none — no verified living candidate)_\n"
    age = (
        f"{dm.contact.age_estimate[0]}–{dm.contact.age_estimate[1]}"
        if dm.contact.age_estimate else "[MISSING]"
    )
    addr = dm.contact.addresses_current[0] if dm.contact.addresses_current else "[MISSING]"
    lines = ["## 8) Decision-Maker Identified", ""]
    lines.append(f"- **Name:** {dm.name}")
    lines.append(f"- **Relationship:** {dm.relationship}")
    lines.append(f"- **Subject role:** {dm.subject_role}")
    lines.append(f"- **Verification status:** {dm.status}")
    lines.append(f"- **Current address:** {addr}")
    lines.append(f"- **Estimated age:** {age}")
    lines.append(f"- **Confidence:** {dm.confidence}")
    lines.append("")
    lines.append("**Reasoning:**")
    lines.append(dm.reasoning)
    return "\n".join(lines) + "\n"


def _section_9_skip_trace(pack: ResearchPack) -> str:
    """Render the Skip Trace Results Card per the skill's ASCII template."""
    st = pack.skip_trace
    if st is None:
        return "## 9) Skip Trace Results\n_(none — skip trace not run)_\n"
    dm = st.decision_maker

    age = (
        f"{dm.contact.age_estimate[0]}–{dm.contact.age_estimate[1]}"
        if dm.contact.age_estimate else "—"
    )
    width = 60

    lines = ["## 9) Skip Trace Results", "", "```"]
    lines.append("═" * width)
    lines.append("                  SKIP TRACE RESULTS")
    lines.append("═" * width)
    lines.append("")
    lines.append(f"DECISION-MAKER: {dm.name}")
    lines.append(f"  Relationship: {dm.relationship}")
    lines.append(f"  Status:       {dm.status}")
    lines.append(f"  Est. Age:     {age}")
    lines.append(f"  Confidence:   {dm.confidence}")
    lines.append("")
    lines.append("─── PHONE NUMBERS " + "─" * (width - 17))
    if st.phones:
        lines.append("  #  | Number          | Type     | Source(s)       | Confidence")
        for i, p in enumerate(st.phones, 1):
            srcs = "/".join(p.sources)
            lines.append(
                f"  {i:<2d} | {p.csv_value:<15s} | {p.type:<8s} | {srcs:<15s} | {p.confidence}"
            )
    else:
        lines.append("  (none)")
    lines.append("")
    lines.append("─── EMAIL ADDRESSES " + "─" * (width - 19))
    if st.emails:
        for i, e in enumerate(st.emails, 1):
            srcs = "/".join(e.sources)
            lines.append(f"  {i:<2d} | {e.address} | {srcs}")
    else:
        lines.append("  (none)")
    lines.append("")
    lines.append("─── ADDRESSES " + "─" * (width - 13))
    if dm.contact.addresses_current:
        lines.append(f"  Current:  {dm.contact.addresses_current[0]}")
    if dm.contact.addresses_previous:
        lines.append(f"  Previous: {dm.contact.addresses_previous[0]}")
    lines.append("")
    lines.append("─── ASSOCIATES & RELATIVES " + "─" * (width - 26))
    if st.associates:
        for a in st.associates[:10]:
            rel = a.relationship or "associate"
            loc = _city_state(a.city, a.state)
            srcs = "/".join(a.sources)
            lines.append(f"  • {a.name} — {rel} — {loc} (from {srcs})".rstrip())
    else:
        lines.append("  (none)")
    lines.append("")
    lines.append("─── SITE STATE " + "─" * (width - 14))
    for ss in st.site_state:
        reason = f" ({ss.blocked_reason})" if ss.blocked_reason else ""
        lines.append(f"  {ss.source:<12s} {ss.status}{reason}")
    lines.append("")
    lines.append("SOURCE KEY: tps = TruePeopleSearch | fps = FastPeopleSearch | cbc = CyberBackgroundChecks")
    lines.append("═" * width)
    lines.append("```")
    return "\n".join(lines) + "\n"


def _section_meta(pack: ResearchPack) -> str:
    lines = ["---", "", "## Run Metadata", ""]
    lines.append(f"- **Timestamp (UTC):** {pack.timestamp_utc.isoformat()}")
    lines.append(f"- **Duration:** {pack.duration_seconds:.1f}s")
    lines.append(f"- **Cost:** ${pack.cost.total:.4f} "
                 f"(anthropic ${pack.cost.anthropic:.4f}, "
                 f"serper ${pack.cost.serper:.4f}, "
                 f"firecrawl ${pack.cost.firecrawl:.4f}, "
                 f"smarty ${pack.cost.smarty:.4f}, "
                 f"other ${pack.cost.other:.4f})")
    if pack.aborted:
        lines.append(f"- **Aborted:** YES — {pack.abort_reason} at {pack.aborted_at_phase}")
    return "\n".join(lines) + "\n"


# ── Public ────────────────────────────────────────────────────────────


def render(pack: ResearchPack) -> str:
    """Render a full research pack to markdown.

    The 9-section format matches the skill's template. Sections are
    always present; sections that don't apply for the level get a
    `_(N/A — ...)_` line so operators can skim the same shape every time.
    """
    sections = [
        f"# Deep Prospecting Research Pack — {pack.input.address or pack.input.owner or pack.input.docket or 'unknown'}\n",
        _section_1_level(pack),
        _section_2_checklist(pack),
        _section_3_title(pack),
        _section_4_identity(pack),
        _section_5_genealogy(pack),
        _section_6_verification_summary(pack),
        _section_7_heir_tree(pack),
        _section_8_dm(pack),
        _section_9_skip_trace(pack),
        _section_meta(pack),
    ]
    return "\n".join(sections)


def write(pack: ResearchPack, out_dir: Path) -> Path:
    """Render and write `research_pack.md` and `results.json` to out_dir."""
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / "research_pack.md"
    json_path = out_dir / "results.json"
    md_path.write_text(render(pack))
    json_path.write_text(pack.model_dump_json(indent=2))
    return md_path


# ── DataSift Notes-field renderer ─────────────────────────────────────


def render_notes_block(pack: ResearchPack) -> str:
    """Compact `=== DEEP PROSPECTING ===` block for the DataSift Notes field.

    Mirrors the SiftStack convention used in `enrichment_pipeline.py`'s
    `=== DECEASED OWNER === / === OBITUARY === / === SURVIVORS ===`
    sections. Designed to be appended to existing Notes content with `\n\n`
    separator (preserving any prior blocks).

    L4 escalation is prepended when applicable so it's the first thing
    the operator sees on the row.
    """
    lines: list[str] = []
    if pack.heir_map and pack.heir_map.escalation_needed:
        lines.append(f"[L4 ESCALATION RECOMMENDED — {pack.heir_map.escalation_reason}]")
        lines.append("")

    lines.append("=== DEEP PROSPECTING ===")
    lines.append(f"Level: {pack.level_selected} — {pack.level_reason}")

    dm = pack.decision_maker
    if dm:
        lines.append(
            f"Decision Maker: {dm.name} ({dm.relationship}, "
            f"{dm.subject_role}, {dm.status}, {dm.confidence} confidence)"
        )
    else:
        lines.append("Decision Maker: [none — no verified living candidate]")

    if pack.skip_trace:
        st = pack.skip_trace
        lines.append(f"Phones added: {len(st.phones)}")
        lines.append(f"Emails added: {len(st.emails)}")
        if st.site_state:
            site_summary = ", ".join(
                f"{s.source}:{s.status}" for s in st.site_state
            )
            lines.append(f"Sites: {site_summary}")

    if pack.heir_map:
        hm = pack.heir_map
        living = sum(1 for h in hm.heirs if h.status == "LIVING")
        deceased = sum(1 for h in hm.heirs if h.status == "DECEASED")
        lines.append(
            f"Heir map: {len(hm.heirs)} heirs "
            f"({living} living, {deceased} deceased) "
            f"across {hm.generations_searched} generation(s)"
        )

    if dm and dm.reasoning:
        lines.append("")
        lines.append("Reasoning:")
        lines.append(dm.reasoning.strip())

    lines.append("")
    lines.append(f"Run: {pack.timestamp_utc.isoformat(timespec='seconds')}")
    lines.append(f"Cost: ${pack.cost.total:.4f}")
    if pack.aborted:
        lines.append(f"ABORTED: {pack.abort_reason} at {pack.aborted_at_phase}")
    lines.append("=== END DEEP PROSPECTING ===")
    return "\n".join(lines)
