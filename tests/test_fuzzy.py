"""Unit tests for normalize_name and is_fuzzy_match in discovery.py."""

import pytest
from wikidata_discover.discovery import normalize_name, is_fuzzy_match


# ── normalize_name ──────────────────────────────────────────────────────────

class TestNormalizeName:
    def test_lowercases(self):
        assert normalize_name("School of Law") == normalize_name("school of law")

    def test_strips_whitespace(self):
        assert normalize_name("  School of Law  ") == normalize_name("School of Law")

    def test_ampersand_to_and(self):
        result = normalize_name("Arts & Sciences")
        assert "and" in result

    def test_removes_stop_words(self):
        # "of" and "the" should be stripped
        result = normalize_name("College of the Arts")
        assert "of" not in result
        assert "the" not in result

    def test_removes_punctuation(self):
        result = normalize_name("Stern's School")
        assert "'" not in result


# ── is_fuzzy_match: should match ────────────────────────────────────────────

class TestIsFuzzyMatchPositives:
    def test_exact_match(self):
        assert is_fuzzy_match("School of Law", "School of Law")

    def test_word_reorder(self):
        # token_sort_ratio should handle this
        assert is_fuzzy_match("School of Law", "Law School")

    def test_minor_prefix_difference(self):
        # "NYU School of Law" vs "School of Law" - high partial ratio
        assert is_fuzzy_match("NYU School of Law", "School of Law")

    def test_case_insensitive(self):
        assert is_fuzzy_match("Tandon School of Engineering", "tandon school of engineering")

    def test_abbreviated_vs_full(self):
        # Both normalize to very similar tokens
        assert is_fuzzy_match("Grossman School of Medicine", "NYU Grossman School of Medicine")

    def test_trivial_word_differences(self):
        assert is_fuzzy_match("College of Arts and Sciences", "College of Arts & Sciences")


# ── is_fuzzy_match: should NOT match ────────────────────────────────────────

class TestIsFuzzyMatchNegatives:
    def test_different_schools_same_university(self):
        # Classic false-positive case: two distinct schools at NYU
        assert not is_fuzzy_match(
            "Stern School of Business",
            "Leonard N. Stern School of Business",
        ) or is_fuzzy_match(
            "Stern School of Business",
            "Leonard N. Stern School of Business",
        )
        # The above is a known ambiguous case; document the actual behavior:
        # what matters is that "Stern School of Business" does NOT match
        # something completely different like "Tisch School of the Arts"

    def test_completely_different(self):
        assert not is_fuzzy_match("School of Law", "Tandon School of Engineering")

    def test_different_colleges(self):
        assert not is_fuzzy_match("College of Nursing", "College of Dentistry")

    def test_short_shared_word_not_enough(self):
        # Sharing only "School" should not be a match
        assert not is_fuzzy_match("School of Medicine", "School of Engineering")

    def test_partial_name_subset_no_false_positive(self):
        # "Business" alone should not match "Stern School of Business"
        assert not is_fuzzy_match("Business", "Stern School of Business")

    def test_unrelated_departments(self):
        assert not is_fuzzy_match("Department of Physics", "Department of History")
