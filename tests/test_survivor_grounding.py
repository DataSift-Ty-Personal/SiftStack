"""Regression tests for the obituary survivor grounding guard.

Background: on 2026-06-03 a deep-prospect run on one record produced a
37-person heir map with an invented surviving spouse and three invented
children, none of whom appear in the actual obituary. The real obituary lists
only siblings, a niece, and nephews.

The fixture below keeps the SHAPE of that obituary and of the bad heir map
(siblings with spouses in parentheses, a niece, nephews, great nephews, places
that look like names). Every name, place and date in it is fictional, since
this repo is public.

These tests lock in the fix: every survivor/heir kept must be literally named in
the source obituary text, and the heir parse must use the high-quality model.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import obituary_enricher as oe


# Survivor section shaped like the real obituary (funeral home, names, places
# and dates changed).
TOLLIVER_OBIT = (
    "Clarence Tolliver, age 65, gained his heavenly wings on Saturday, March 14, 2026. "
    "He was born on November 2, 1960. Clarence was born in Knoxville, TN he was a "
    "devoted brother, uncle and friend whose kindness touched everyone who knew him. "
    "Clarence leaves cherished memories with his brothers, Otis Tolliver Jr., and "
    "Vernon Tolliver, both of Knoxville, TN; his sisters, Beatrice A. Ashworth (Delmar) "
    "of Dover, Delaware, Ida Kimbrough (Vernon) of Huntsville, AL, and Lavonne Tolliver of "
    "Knoxville, TN. He is also lovingly remembered by his niece, Keisha J. Dobbins, his "
    "nephews, Jamal Ashworth and Curtis Tolliver Jr., two great nephews Ellis, Tanner "
    "Ashworth and a host of family and friends."
)

# What the buggy run hallucinated (subset): NONE of these are in the obituary.
HALLUCINATED = [
    {"name": "Russell L. Ashworth", "relationship": "spouse"},
    {"name": "Dwight Dale Tolliver", "relationship": "son"},
    {"name": "Mac Ashworth", "relationship": "son"},
    {"name": "Morgan McAllister", "relationship": "daughter"},
    {"name": "MacKenna Ann Ashworth", "relationship": "granddaughter"},
    {"name": "Eleanor Whitlock", "relationship": "mother"},
    {"name": "Scott McAllister", "relationship": "son-in-law"},
]

# Real survivors actually named in the obituary.
REAL = [
    {"name": "Otis Tolliver Jr.", "relationship": "brother"},
    {"name": "Vernon Tolliver", "relationship": "brother"},
    {"name": "Beatrice A. Ashworth", "relationship": "sister"},
    {"name": "Ida Kimbrough", "relationship": "sister"},
    {"name": "Lavonne Tolliver", "relationship": "sister"},
    {"name": "Keisha J. Dobbins", "relationship": "niece"},
    {"name": "Jamal Ashworth", "relationship": "nephew"},
    {"name": "Curtis Tolliver Jr.", "relationship": "nephew"},
]


def _names(items):
    return {oe._survivor_name(i) for i in items}


# ── The core regression: the invented-heirs bug must never recur ──────


def test_drops_all_hallucinated_heirs():
    kept = oe._validate_survivors_against_text(
        HALLUCINATED + REAL, TOLLIVER_OBIT, "Clarence Tolliver",
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
        [{"name": "Russell L. Ashworth", "relationship": "spouse"}],
        TOLLIVER_OBIT, "Clarence Tolliver",
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
        "Clarence Tolliver Jr",   # namesake "son" = deceased name + Jr
        "Clarence Tolliver",      # the decedent himself
        "Devoted Tolliver",       # common word + deceased surname
        "Saturday Tolliver",      # day-of-week + deceased surname
        "Dover Tolliver",         # place + deceased surname
        "Delaware Tolliver",      # state + deceased surname
        "Ellis Tolliver",         # real first name (Ellis Ashworth) + deceased surname
        "Vernon Ashworth",        # real first (Vernon Tolliver) + real surname, not adjacent
        "Dover Ashworth",         # place + real surname, not adjacent
    ]
    kept = oe._validate_survivors_against_text(
        [{"name": n} for n in fabricated], TOLLIVER_OBIT, "Clarence Tolliver",
    )
    assert _names(kept) == set(), f"recombinations survived the guard: {_names(kept)}"


def test_real_survivors_survive_the_stricter_guard():
    """The adjacency rule must not over-filter: all 8 real survivors are kept."""
    kept = oe._validate_survivors_against_text(REAL, TOLLIVER_OBIT, "Clarence Tolliver")
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
    text = "Survived by Otis Tolliver Jr. and Lavonne Tolliver."
    kept = oe._validate_survivors_against_text(
        ["Otis Tolliver Jr.", "Lavonne Tolliver", "Morgan McAllister"],
        text, "Clarence Tolliver",
    )
    assert set(kept) == {"Otis Tolliver Jr.", "Lavonne Tolliver"}


def test_generational_suffix_ignored():
    text = "Survived by his son Curtis Tolliver."
    kept = oe._validate_survivors_against_text(
        [{"name": "Curtis Tolliver Jr."}], text, "Clarence Tolliver",
    )
    assert _names(kept) == {"Curtis Tolliver Jr."}


def test_no_source_text_does_not_overfilter():
    """With no text to ground against, do not drop everything."""
    items = [{"name": "Russell Ashworth"}]
    assert oe._validate_survivors_against_text(items, "", "Clarence Tolliver") == items
    assert oe._validate_survivors_against_text(items, "   ", "Clarence Tolliver") == items


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
            "full_name": "Clarence Tolliver",
            "date_of_death": "2026-03-14",
            "survivors": HALLUCINATED + REAL,
            "preceded_in_death": ["Phantom Person"],
            "executor_named": "Russell L. Ashworth",
        }

    monkeypatch.setattr(oe.llm_client, "chat_json", fake_chat_json)

    parsed = oe._parse_obituary_with_llm(
        obituary_text=TOLLIVER_OBIT,
        owner_name="Clarence Tolliver",
        city="Knoxville",
        address="2106 Example St",
        api_key="test-key",
    )

    assert parsed is not None
    kept = _names(parsed["survivors"])
    assert "Russell L. Ashworth" not in kept
    assert "Morgan McAllister" not in kept
    assert {"Otis Tolliver Jr.", "Vernon Tolliver", "Lavonne Tolliver"} <= kept
    # Fabricated executor and predeceased entries are cleared
    assert parsed["executor_named"] == ""
    assert parsed["preceded_in_death"] == []
    # High-quality model was used for the heir-determining parse
    assert captured["model"] == oe._obituary_model()
    assert "sonnet" in (captured["model"] or "").lower()
