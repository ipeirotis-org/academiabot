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
        assert "and" in normalize_name("Arts & Sciences")

    def test_removes_stop_words(self):
        result = normalize_name("College of the Arts")
        assert "of" not in result
        assert "the" not in result

    def test_removes_punctuation(self):
        assert "'" not in normalize_name("Stern's School")


# ── is_fuzzy_match: should match ────────────────────────────────────────────

class TestIsFuzzyMatchPositives:
    def test_exact_match(self):
        assert is_fuzzy_match("School of Law", "School of Law")

    def test_word_reorder(self):
        assert is_fuzzy_match("School of Law", "Law School")

    def test_case_insensitive(self):
        assert is_fuzzy_match("Tandon School of Engineering", "tandon school of engineering")

    def test_ampersand_vs_and(self):
        assert is_fuzzy_match("College of Arts and Sciences", "College of Arts & Sciences")

    def test_short_university_prefix(self):
        # short prefix like "NYU" should not block the match
        assert is_fuzzy_match("Grossman School of Medicine", "NYU Grossman School of Medicine")

    def test_long_university_prefix(self):
        # full university name prefix - the Rory Meyers / altLabel pattern
        assert is_fuzzy_match(
            "Rory Meyers College of Nursing",
            "New York University Rory Meyers College of Nursing",
        )

    def test_minor_name_variation(self):
        # "College" vs no "College" - common in Wikidata vs real-world usage
        assert is_fuzzy_match(
            "School of Continuing and Professional Studies",
            "New York University School of Continuing and Professional Studies",
        )

    def test_cuny_school_variant(self):
        # CUNY schools often appear with and without "City University of New York"
        assert is_fuzzy_match("Graduate Center", "CUNY Graduate Center")


# ── is_fuzzy_match: should NOT match ────────────────────────────────────────

class TestIsFuzzyMatchNegatives:
    def test_completely_different(self):
        assert not is_fuzzy_match("School of Law", "Tandon School of Engineering")

    def test_different_colleges(self):
        assert not is_fuzzy_match("College of Nursing", "College of Dentistry")

    def test_shared_word_not_enough(self):
        assert not is_fuzzy_match("School of Medicine", "School of Engineering")

    def test_short_substring_no_false_positive(self):
        # "Business" alone should not match "Stern School of Business"
        assert not is_fuzzy_match("Business", "Stern School of Business")

    def test_unrelated_departments(self):
        assert not is_fuzzy_match("Department of Physics", "Department of History")

    def test_similar_policy_vs_health(self):
        # common false positive: "Public Health" vs "Public Policy"
        assert not is_fuzzy_match("School of Public Health", "School of Public Policy")

    def test_stanford_continuing_vs_engineering(self):
        # pilot revealed Stanford hallucinated "School of Continuing Studies"
        # should not match a real engineering school
        assert not is_fuzzy_match("School of Continuing Studies", "School of Engineering")

    def test_college_vs_school_different_field(self):
        # sharing only "school" or "college" is not enough
        assert not is_fuzzy_match("College of Education", "College of Business")

    def test_missouri_hallucination(self):
        # pilot revealed Missouri hallucinated extra schools
        # "School of Journalism" should not match "School of Medicine"
        assert not is_fuzzy_match("School of Journalism", "School of Medicine")
