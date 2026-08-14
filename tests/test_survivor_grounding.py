"""Regression tests for the obituary survivor grounding guard.

Background: on 2026-06-03 a deep-prospect run on 2106 Brice St (owner Norman
Willis) produced a 37-person heir map with an invented surviving spouse
("Benjamin L. Holland") and three invented children, none of whom appear in the
actual obituary. The real obituary lists only siblings, a niece, and nephews.

These tests lock in the fix: every survivor/heir kept must be literally named in
the source obituary text, and the heir parse must use the high-quality model.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import obituary_enricher as oe


# Verbatim survivor section of the real Unity Mortuary obituary for Norman Willis.
NORMAN_WILLIS_OBIT = (
    "Norman Willis, age 65, gained his heavenly wings on Saturday, March 21, 2026. "
    "He was born on December 8, 1961. Norman was born in Knoxville, TN he was a "
    "devoted brother, uncle and friend whose kindness touched everyone who knew him. "
    "Norman leaves cherished memories with his brothers, Luther Willis Jr., and "
    "Kenneth Willis, both of Knoxville, TN; his sisters, Elizabeth A. Holland (Rosher) "
    "of Bowie, Maryland, Rosa Judge (Kenneth) of Birmingham, AL, and Letitia Willis of "
    "Knoxville, TN. He is also lovingly remembered by his niece, Taja J. Nix, his "
    "nephews, Taurean Holland and Darrell Willis Jr., two great nephews Emory, Tatum "
    "Holland and a host of family and friends."
)

# What the buggy run hallucinated (subset) — NONE of these are in the obituary.
HALLUCINATED = [
    {"name": "Benjamin L. Holland", "relationship": "spouse"},
    {"name": "Kenny Dale Willis", "relationship": "son"},
    {"name": "Mac Holland", "relationship": "son"},
    {"name": "Morgan McGregor", "relationship": "daughter"},
    {"name": "MacKaylee Ann Holland", "relationship": "granddaughter"},
    {"name": "Eleanor Bowen", "relationship": "mother"},
    {"name": "Scott McGregor", "relationship": "son-in-law"},
]

# Real survivors actually named in the obituary.
REAL = [
    {"name": "Luther Willis Jr.", "relationship": "brother"},
    {"name": "Kenneth Willis", "relationship": "brother"},
    {"name": "Elizabeth A. Holland", "relationship": "sister"},
    {"name": "Rosa Judge", "relationship": "sister"},
    {"name": "Letitia Willis", "relationship": "sister"},
    {"name": "Taja J. Nix", "relationship": "niece"},
    {"name": "Taurean Holland", "relationship": "nephew"},
    {"name": "Darrell Willis Jr.", "relationship": "nephew"},
]


def _names(items):
    return {oe._survivor_name(i) for i in items}


# ── The core regression: the Norman Willis bug must never recur ──────


def test_drops_all_hallucinated_norman_willis_heirs():
    kept = oe._validate_survivors_against_text(
        HALLUCINATED + REAL, NORMAN_WILLIS_OBIT, "Norman Willis",
    )
    kept_names = _names(kept)
    # Every fabricated person is removed
    for h in HALLUCINATED:
        assert h["name"] not in kept_names, f"hallucinated {h['name']!r} survived the guard"
    # Every real survivor is preserved
    for r in REAL:
        assert r["name"] in kept_names, f"real survivor {r['name']!r} was wrongly dropped"


def test_invented_spouse_specifically_removed():
    """The headline bug: an invented spouse on a man with no spouse."""
    kept = oe._validate_survivors_against_text(
        [{"name": "Benjamin L. Holland", "relationship": "spouse"}],
        NORMAN_WILLIS_OBIT, "Norman Willis",
    )
    assert kept == []


# ── Behavioral guarantees for legitimate extraction ─────────────────


def test_full_name_kept_but_first_name_only_dropped():
    """Full names are kept; a survivor the obituary named by FIRST NAME ONLY is
    intentionally dropped (we cannot ground a model-guessed surname). Precision
    over recall — the safe direction for a never-again fix."""
    text = "John Smith is survived by his wife Mary Smith and a friend named Paul."
    kept = oe._validate_survivors_against_text(
        [{"name": "Mary Smith"}, {"name": "Paul Smith"}], text, "John Smith",
    )
    assert _names(kept) == {"Mary Smith"}


def test_drops_namesake_and_token_recombinations():
    """The subtle fabrications adversarial review surfaced: a namesake child, the
    decedent himself, and names recombined from words/places that merely appear in
    the obituary. None are adjacent full-name matches, so all must be dropped."""
    fabricated = [
        "Norman Willis Jr",   # namesake "son" = deceased name + Jr
        "Norman Willis",      # the decedent himself
        "Devoted Willis",     # common word + deceased surname
        "Saturday Willis",    # day-of-week + deceased surname
        "Bowie Willis",       # place + deceased surname
        "Maryland Willis",    # state + deceased surname
        "Emory Willis",       # real first name (Emory Holland) + deceased surname
        "Kenneth Holland",    # real first (Kenneth Willis) + real surname, not adjacent
        "Bowie Holland",      # place + real surname, not adjacent
    ]
    kept = oe._validate_survivors_against_text(
        [{"name": n} for n in fabricated], NORMAN_WILLIS_OBIT, "Norman Willis",
    )
    assert _names(kept) == set(), f"recombinations survived the guard: {_names(kept)}"


def test_real_survivors_survive_the_stricter_guard():
    """The adjacency rule must not over-filter: all 8 real survivors are kept."""
    kept = oe._validate_survivors_against_text(REAL, NORMAN_WILLIS_OBIT, "Norman Willis")
    assert _names(kept) == {r["name"] for r in REAL}


def test_accented_names_are_grounded_not_dropped():
    """Accent folding: an accented obituary name matches its ASCII rendering."""
    text = "Maria Garcia is survived by her son Jose Garcia and daughter Renee Beauchene."
    accented_text = "Maria Garcia is survived by her son Jose Garcia and daughter Renée Beauchêne."
    kept = oe._validate_survivors_against_text(
        [{"name": "Jose Garcia"}, {"name": "Renée Beauchêne"}],
        accented_text, "Maria Garcia",
    )
    assert _names(kept) == {"Jose Garcia", "Renée Beauchêne"}


def test_requires_distinct_surname_to_appear():
    """A real first name + a surname NOT in the text = fabricated person, dropped."""
    text = "John Smith is survived by his brother Robert."
    kept = oe._validate_survivors_against_text(
        [{"name": "Robert Johnson"}], text, "John Smith",
    )
    assert kept == []


def test_plain_string_names_supported():
    text = "Survived by Luther Willis Jr. and Letitia Willis."
    kept = oe._validate_survivors_against_text(
        ["Luther Willis Jr.", "Letitia Willis", "Morgan McGregor"],
        text, "Norman Willis",
    )
    assert set(kept) == {"Luther Willis Jr.", "Letitia Willis"}


def test_generational_suffix_ignored():
    text = "Survived by his son Darrell Willis."
    kept = oe._validate_survivors_against_text(
        [{"name": "Darrell Willis Jr."}], text, "Norman Willis",
    )
    assert _names(kept) == {"Darrell Willis Jr."}


def test_no_source_text_does_not_overfilter():
    """With no text to ground against, do not drop everything."""
    items = [{"name": "Benjamin Holland"}]
    assert oe._validate_survivors_against_text(items, "", "Norman Willis") == items
    assert oe._validate_survivors_against_text(items, "   ", "Norman Willis") == items


# ── Model wiring + end-to-end parse with the guard applied ──────────


def test_obituary_model_defaults_to_sonnet():
    assert "sonnet" in oe._obituary_model().lower()


def test_parse_obituary_applies_guard_and_uses_high_quality_model(monkeypatch):
    """_parse_obituary_with_llm must filter hallucinated survivors and call the
    high-quality model, even when the LLM returns a fabricated heir list."""
    captured = {}

    def fake_chat_json(prompt, system="", max_tokens=1024, api_key=None, model=None):
        captured["model"] = model
        return {
            "match": True,
            "confidence": "high",
            "full_name": "Norman Willis",
            "date_of_death": "2026-03-21",
            "survivors": HALLUCINATED + REAL,
            "preceded_in_death": ["Phantom Person"],
            "executor_named": "Benjamin L. Holland",
        }

    monkeypatch.setattr(oe.llm_client, "chat_json", fake_chat_json)

    parsed = oe._parse_obituary_with_llm(
        obituary_text=NORMAN_WILLIS_OBIT,
        owner_name="Norman Willis",
        city="Knoxville",
        address="2106 Brice St",
        api_key="test-key",
    )

    assert parsed is not None
    kept = _names(parsed["survivors"])
    assert "Benjamin L. Holland" not in kept
    assert "Morgan McGregor" not in kept
    assert {"Luther Willis Jr.", "Kenneth Willis", "Letitia Willis"} <= kept
    # Fabricated executor and predeceased entries are cleared
    assert parsed["executor_named"] == ""
    assert parsed["preceded_in_death"] == []
    # High-quality model was used for the heir-determining parse
    assert captured["model"] == oe._obituary_model()
    assert "sonnet" in (captured["model"] or "").lower()
