"""Tests for fatcat_match.py — the shared matching primitives.

Covers the pure helpers in isolation, the two completeness scorers, and the
duplicate-flagging convention. The release/container completeness tests use
the real JAMA 1980 "Postirradiation Screening" duplicate shapes (canonical
Crossref article-DOI vs page-locator-DOI) so the tiebreak is exercised against
the case it was built for.

Run: python3 -m pytest test_fatcat_match.py -v
   or directly: python3 test_fatcat_match.py
"""
import fatcat_match as fm


# ---------- fcid_to_uuid ----------

def test_fcid_to_uuid_known_value():
    # NEJM container from docs/scholar_archive_org.md worked example.
    assert fm.fcid_to_uuid("td5cjnem25b35nugn4qftmwcna") == (
        "98fa24b4-8cd7-43be-b686-6f2059b2c268"
    )


def test_fcid_to_uuid_none():
    assert fm.fcid_to_uuid(None) is None
    assert fm.fcid_to_uuid("") is None


def test_fcid_to_uuid_case_insensitive():
    lo = fm.fcid_to_uuid("td5cjnem25b35nugn4qftmwcna")
    hi = fm.fcid_to_uuid("TD5CJNEM25B35NUGN4QFTMWCNA")
    assert lo == hi


# ---------- value utilities ----------

def test_first_str():
    assert fm.first_str(["a", "b"]) == "a"
    assert fm.first_str([]) is None
    assert fm.first_str("x") == "x"
    assert fm.first_str(None) is None


def test_present():
    assert fm.present("1782") is True
    assert fm.present(["a"]) is True
    assert fm.present(0) is True            # a real value, even if falsy
    assert fm.present(None) is False
    assert fm.present("") is False
    assert fm.present("   ") is False
    assert fm.present([]) is False
    assert fm.present({}) is False


# ---------- title normalization ----------

def test_normalize_title_key_strips_stopwords_and_punct():
    assert fm.normalize_title_key("The New England Journal of Medicine") == (
        "new england journal medicine"
    )


def test_normalize_title_key_empty():
    assert fm.normalize_title_key("") is None
    assert fm.normalize_title_key(None) is None
    assert fm.normalize_title_key("the of and") is None   # all stopwords


def test_normalize_title_fuzzy_keeps_stopwords():
    # The fuzzy normalizer must NOT drop stopwords (it feeds difflib ratio).
    out = fm.normalize_title_fuzzy("Costs of Quality Measurement—Reply")
    assert out == "costs of quality measurement reply"
    assert "of" in out.split()


def test_the_two_normalizers_differ():
    s = "The Journal of Foo"
    assert fm.normalize_title_key(s) != fm.normalize_title_fuzzy(s)


# ---------- flag_duplicates ----------

def test_flag_duplicates_marks_ties():
    ranked = [{"combined": 1.1}, {"combined": 1.1}, {"combined": 0.9}]
    fm.flag_duplicates(ranked, "combined")
    assert ranked[0]["duplicate_of_top"] is False   # top is never a dup
    assert ranked[1]["duplicate_of_top"] is True
    assert ranked[2]["duplicate_of_top"] is False


def test_flag_duplicates_no_ties():
    ranked = [{"combined": 1.0}, {"combined": 0.8}]
    fm.flag_duplicates(ranked, "combined")
    assert all(r["duplicate_of_top"] is False for r in ranked)


def test_flag_duplicates_empty():
    assert fm.flag_duplicates([]) == []


# ---------- release_completeness (the JAMA 1980 duplicate case) ----------

CANONICAL = {  # 10.1001/jama.1980.03310160010003
    "contrib_names": ["Paul C. Royce"],
    "issue": "16",
    "ref_count": 0,
    "first_page": "1782",
    "pages": "1782",
}
LOCATOR = {  # 10.1001/jama.244.16.1782c
    "contrib_names": ["P. C. Royce"],
    "issue": "16",
    "ref_count": 0,
    "first_page": None,
    "pages": "1782c-1782",
}


def test_release_completeness_prefers_canonical():
    assert fm.release_completeness(CANONICAL) > fm.release_completeness(LOCATOR)


def test_release_completeness_well_formed_pages_signal():
    assert fm._NUMERIC_PAGES_RE.match("1782")
    assert fm._NUMERIC_PAGES_RE.match("1273-1278")
    assert fm._NUMERIC_PAGES_RE.match("1273-78")          # abbreviated end
    assert not fm._NUMERIC_PAGES_RE.match("1782c-1782")   # locator artifact
    assert not fm._NUMERIC_PAGES_RE.match("S1-S10")
    assert not fm._NUMERIC_PAGES_RE.match("")


def test_release_completeness_counts_authors():
    one = {"contrib_names": ["A"]}
    three = {"contrib_names": ["A", "B", "C"]}
    assert fm.release_completeness(three) > fm.release_completeness(one)


# ---------- container_completeness ----------

def test_container_completeness_prefers_more_populated():
    full = {"issnl": "0028-4793", "issne": "1533-4406", "issnp": "0028-4793",
            "sim_pubid": "693", "wikidata_qid": "Q1146531",
            "publisher": "Massachusetts Medical Society"}
    sparse = {"issnl": "0028-4793", "issne": None, "issnp": None,
              "sim_pubid": None, "wikidata_qid": None, "publisher": None}
    assert fm.container_completeness(full) > fm.container_completeness(sparse)


def test_container_completeness_handles_missing_keys():
    # Must not raise on a dict missing every optional field.
    assert fm.container_completeness({}) == 0


if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-v"]))
