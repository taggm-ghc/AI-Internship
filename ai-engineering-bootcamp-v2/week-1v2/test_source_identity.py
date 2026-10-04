import pytest

import source_identity as si
from source_identity import (CONFLICT, NEW_SOURCE, REVIEW, SAME_SOURCE, extract_identifiers,
                             normalise, resolve)


@pytest.mark.parametrize("raw,expected", [
    ("2101.00001", "2101.00001"),
    ("arXiv:2101.00001v3", "2101.00001"),
    ("https://arxiv.org/abs/2101.00001v2", "2101.00001"),
    ("http://arxiv.org/pdf/2101.00001v1.pdf", "2101.00001"),
    ("https://arxiv.org/pdf/2101.00001.pdf", "2101.00001"),
    ("2101.12345", "2101.12345"),
    ("cs/0112017", "cs/0112017"),
    ("CS/0112017v2", "cs/0112017"),
    ("arXiv:hep-th/9901001", "hep-th/9901001"),
    ("math.GT/0309136v1", "math.gt/0309136"),
    ("not-an-id", None),
    ("21.00001", None),
    ("https://example.com/abs/2101.00001", None),
])
def test_arxiv(raw, expected):
    assert normalise("arxiv", raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("10.18653/V1/2024.ACL-LONG.118", "10.18653/v1/2024.acl-long.118"),
    ("https://doi.org/10.1000/xyz123", "10.1000/xyz123"),
    ("http://dx.doi.org/10.1000/xyz123", "10.1000/xyz123"),
    ("doi:10.1000/xyz123", "10.1000/xyz123"),
    ("DOI: 10.1000/xyz123.", "10.1000/xyz123"),
    ("10.1000/xyz123),", "10.1000/xyz123"),
    ("11.1000/abc", None),
    ("https://example.com/10.1000/abc", None),
    ("", None),
])
def test_doi(raw, expected):
    assert normalise("doi", raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("978-0-306-40615-7", "9780306406157"),
    ("0-306-40615-2", "9780306406157"),
    ("0306406152", "9780306406157"),
    ("ISBN 978 0 306 40615 7", "9780306406157"),
    ("080442957X", "9780804429573"),
    ("080442957x", "9780804429573"),
    ("978-0-306-40615-8", None),
    ("0-306-40615-3", None),
    ("12345", None),
    ("97803064061AB", None),
])
def test_isbn(raw, expected):
    assert normalise("isbn", raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("HTTPS://Example.COM/Path", "https://example.com/Path"),
    ("https://example.com:443/a", "https://example.com/a"),
    ("http://example.com:80/a", "http://example.com/a"),
    ("http://example.com:8080/a", "http://example.com:8080/a"),
    ("https://example.com/a/#frag", "https://example.com/a"),
    ("https://example.com/a/index.html", "https://example.com/a"),
    ("https://example.com/index.html", "https://example.com/"),
    ("https://example.com", "https://example.com/"),
    ("https://example.com/a?utm_source=x&id=5&fbclid=z", "https://example.com/a?id=5"),
    ("https://example.com/a?b=2&a=1", "https://example.com/a?a=1&b=2"),
    ("https://example.com/a?UTM_Medium=x", "https://example.com/a"),
    ("example.com/a", None),
    ("ftp://example.com/a", None),
    ("mailto:a@b.c", None),
])
def test_url(raw, expected):
    assert normalise("url", raw) == expected


def test_pmid_and_other():
    assert normalise("pmid", "PMID: 0012345") == "12345"
    assert normalise("pmid", "abc") is None
    assert normalise("pmid", "000") is None
    assert normalise("other", "  weird  ") == "weird"
    assert normalise("nonsense", "x") is None
    assert normalise("doi", None) is None


def _key(ids):
    return {(i["type"], i["value_norm"], i["is_canonical"], i["origin"]) for i in ids}


def test_extract_declared_original():
    ids = extract_identifiers({"arxiv_id": "2101.00001v2", "doi": "doi:10.1000/AB", "identifier_origin": "original"}, trusted=True)
    assert _key(ids) == {("arxiv", "2101.00001", True, "original"), ("doi", "10.1000/ab", True, "original")}
    assert {i["type"]: i for i in ids}["doi"]["value_raw"] == "doi:10.1000/AB"


def test_extract_default_is_derived():
    ids = extract_identifiers({"arxiv_id": "2101.00001", "doi": "10.1000/xyz", "isbn": "0306406152"})
    assert len(ids) == 3
    assert all(not i["is_canonical"] and i["origin"] == "derived" for i in ids)
    assert {i["type"] for i in ids} == {"arxiv", "doi", "isbn"}


def test_extract_from_urls():
    ids = extract_identifiers({"source_url": "https://arxiv.org/abs/2101.00001v2?utm_source=x"}, trusted=True)
    k = _key(ids)
    assert ("arxiv", "2101.00001", True, "original") in k
    assert ("url", "https://arxiv.org/abs/2101.00001v2", False, "derived") in k
    ids = extract_identifiers({"source_url": "https://doi.org/10.1000/XYZ"}, trusted=True)
    assert ("doi", "10.1000/xyz", True, "original") in _key(ids)


def test_extract_blog_url_only_derived():
    ids = extract_identifiers({"source_url": "https://blog.example.com/post/"})
    assert _key(ids) == {("url", "https://blog.example.com/post", False, "derived")}


def test_extract_canonical_wins_dedup():
    ids = extract_identifiers({"arxiv_id": "2101.00001", "source_url": "https://arxiv.org/abs/2101.00001"}, trusted=True)
    arx = [i for i in ids if i["type"] == "arxiv"]
    assert len(arx) == 1 and arx[0]["is_canonical"]


def test_extract_invalid_becomes_other():
    ids = extract_identifiers({"isbn": "978-0-306-40615-8", "identifier_origin": "original"})
    assert len(ids) == 1
    assert ids[0]["type"] == "other" and not ids[0]["is_canonical"] and ids[0]["origin"] == "derived"


def test_extract_metadata():
    ids = extract_identifiers({}, {"DOI": "10.18653/v1/2024.acl-long.118", "URL": "https://aclanthology.org/2024.acl-long.118/"}, trusted=True)
    k = _key(ids)
    assert ("doi", "10.18653/v1/2024.acl-long.118", True, "original") in k
    assert ("url", "https://aclanthology.org/2024.acl-long.118", False, "derived") in k


def test_extract_empty():
    assert extract_identifiers({}) == []
    assert extract_identifiers(None, None) == []


def I(t, v, canon=False, origin="derived"):
    return {"type": t, "value_norm": v, "value_raw": v, "is_canonical": canon, "origin": origin}


def L(sid, t, v, canon=False, origin="derived"):
    return {"source_id": sid, "type": t, "value_norm": v, "is_canonical": canon, "origin": origin}


def test_resolve_no_ids_new():
    assert resolve([], []).status == NEW_SOURCE
    assert resolve([], [L(1, "doi", "x", True, "original")]).status == NEW_SOURCE


def test_resolve_no_match_new():
    r = resolve([I("arxiv", "2101.00001", True, "original")], [L(1, "arxiv", "2101.00002", True, "original")])
    assert r.status == NEW_SOURCE


def test_resolve_canonical_match_same():
    r = resolve([I("arxiv", "2101.00001", True, "original"), I("url", "https://x.org/a")],
                [L(7, "arxiv", "2101.00001", True, "original")])
    assert r.status == SAME_SOURCE and r.source_id == 7


def test_resolve_canonical_match_other_type_ids_irrelevant():
    r = resolve([I("doi", "10.1/a", True, "original")],
                [L(1, "doi", "10.1/a", True, "original"), L(2, "url", "https://x.org/a")])
    assert r.status == SAME_SOURCE and r.source_id == 1


def test_resolve_different_canonical_same_type_distinct_despite_shared_url():
    new = [I("arxiv", "2101.00002", True, "original"), I("url", "https://x.org/landing")]
    old = [L(1, "arxiv", "2101.00001", True, "original"), L(1, "url", "https://x.org/landing")]
    r = resolve(new, old)
    assert r.status == NEW_SOURCE and r.warnings


def test_resolve_shared_url_two_sources_review():
    r = resolve([I("url", "https://x.org/a")],
                [L(1, "url", "https://x.org/a"), L(2, "url", "https://x.org/a")])
    assert r.status == REVIEW and r.candidates == [1, 2]


def test_resolve_url_only_one_source_review_not_merge():
    r = resolve([I("url", "https://x.org/a")], [L(1, "url", "https://x.org/a")])
    assert r.status == REVIEW and r.candidates == [1]


def test_resolve_derived_strong_match_never_merges():
    r = resolve([I("doi", "10.1/a")], [L(1, "doi", "10.1/a", True, "original")])
    assert r.status == REVIEW
    r = resolve([I("doi", "10.1/a", True, "original")], [L(1, "doi", "10.1/a")])
    assert r.status == REVIEW


def test_resolve_url_match_where_candidate_has_no_canonical_review():
    r = resolve([I("arxiv", "2101.00001", True, "original"), I("url", "https://x.org/a")],
                [L(1, "url", "https://x.org/a")])
    assert r.status == REVIEW and r.candidates == [1]


def test_resolve_conflict_two_sources_via_two_canonical_ids():
    r = resolve([I("arxiv", "2101.00001", True, "original"), I("doi", "10.1/a", True, "original")],
                [L(1, "arxiv", "2101.00001", True, "original"), L(2, "doi", "10.1/a", True, "original")])
    assert r.status == CONFLICT and r.candidates == [1, 2]


def test_resolve_conflict_matched_source_has_other_canonical_of_other_type():
    r = resolve([I("arxiv", "2101.00001", True, "original"), I("doi", "10.1/new", True, "original")],
                [L(1, "arxiv", "2101.00001", True, "original"), L(1, "doi", "10.1/old", True, "original")])
    assert r.status == CONFLICT and r.candidates == [1]


def test_resolve_conflict_internal_two_canonical_same_type():
    r = resolve([I("doi", "10.1/a", True, "original"), I("doi", "10.1/b", True, "original")], [])
    assert r.status == CONFLICT


def test_resolve_derived_conflict_original_wins_with_warning():
    r = resolve([I("arxiv", "2101.00001", True, "original"), I("doi", "10.1/bogus")],
                [L(1, "arxiv", "2101.00001", True, "original"), L(1, "doi", "10.1/real", True, "original")])
    assert r.status == SAME_SOURCE and r.source_id == 1 and r.warnings


def test_resolve_other_type_never_matches():
    r = resolve([I("other", "foo")], [L(1, "other", "foo")])
    assert r.status == NEW_SOURCE


def test_end_to_end_version_of_same_arxiv_paper():
    old = [dict(i, source_id=3) for i in extract_identifiers({"source_url": "https://arxiv.org/abs/2101.00001v1"}, trusted=True)]
    new = extract_identifiers({"source_url": "https://arxiv.org/pdf/2101.00001v2"}, trusted=True)
    r = resolve(new, old)
    assert r.status == SAME_SOURCE and r.source_id == 3


def test_end_to_end_new_arxiv_paper_same_blog_url():
    shared = "https://blog.example.com/list"
    old = [dict(i, source_id=3) for i in extract_identifiers({"arxiv_id": "2101.00001", "identifier_origin": "original", "source_url": shared}, trusted=True)]
    new = extract_identifiers({"arxiv_id": "2101.00009", "identifier_origin": "original", "source_url": shared}, trusted=True)
    assert resolve(new, old).status == NEW_SOURCE


def test_config_normalisers_exist():
    for t, spec in si.CFG["types"].items():
        assert callable(getattr(si, spec["normaliser"])), t


FORGED = [
    {"arxiv_id": "2101.00001", "identifier_origin": "original"},
    {"doi": "10.1000/abc", "identifier_origin": "original"},
    {"source_url": "https://arxiv.org/abs/2101.00001"},
    {"source_url": "https://doi.org/10.1000/abc"},
]


def test_untrusted_never_canonical_original():
    for prov in FORGED:
        ids = extract_identifiers(prov, {"DOI": "10.1000/abc"})
        assert ids and all(not i["is_canonical"] and i["origin"] == "derived" for i in ids), prov
    ids = extract_identifiers({}, {"DOI": "10.1000/abc", "URL": "https://x.org/"})
    assert all(not i["is_canonical"] and i["origin"] == "derived" for i in ids)


def test_forged_identifiers_never_same_source():
    victim = [dict(i, source_id=3) for i in extract_identifiers(
        {"arxiv_id": "2101.00001", "doi": "10.1000/abc", "identifier_origin": "original"}, trusted=True)]
    for prov in FORGED:
        r = resolve(extract_identifiers(prov, {"DOI": "10.1000/abc"}), victim)
        assert r.status in (REVIEW, NEW_SOURCE), prov
        assert r.status != SAME_SOURCE
    assert resolve(extract_identifiers(FORGED[0]), victim).status == REVIEW
