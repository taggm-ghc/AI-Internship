"""Offline tests for the 5.2 one-hop graph demo. No DB, no network, no embedding call."""
import importlib.util
import pathlib
import sys

_P = pathlib.Path(__file__).resolve().parent.parent / ".claude/skills/rag-scaffold/graph_hop.py"
_spec = importlib.util.spec_from_file_location("graph_hop", _P)
gh = importlib.util.module_from_spec(_spec)
sys.modules["graph_hop"] = gh
_spec.loader.exec_module(gh)

Q = "remote onboarding retention"


def test_hop_finds_item_vector_search_misses():
    seeds = gh.vector_search(gh.FIXTURE, Q)
    assert "claim-01" in seeds and "claim-02" not in seeds   # relation miss
    r = gh.retrieve(gh.FIXTURE, Q)
    hit = {c.item_id: c for c in r["connected"]}
    assert hit["claim-02"].edge_type == gh.EDGE_SHARED_SOURCE
    assert hit["claim-02"].from_seed == "claim-01"


def test_identifier_edge_type():
    r = gh.retrieve(gh.FIXTURE, "shelf labels standardised", k=1)
    assert r["seeds"] == ["claim-04"]
    assert any(c.item_id == "claim-05" and c.edge_type == gh.EDGE_SHARED_IDENTIFIER for c in r["connected"])
    edges = gh.build_edges(gh.FIXTURE)
    assert ("claim-05", gh.EDGE_SHARED_IDENTIFIER, "idf-002") in edges["claim-04"]


def test_no_cycles_or_duplicates_on_triangle():
    tri = [gh.Item("a", "alpha topic", ("s1", "s2")), gh.Item("b", "beta", ("s1", "s3")),
           gh.Item("c", "gamma", ("s2", "s3"), ("i1",)), gh.Item("d", "delta", (), ("i1",))]
    edges = gh.build_edges(tri)
    out = gh.hop_expand(["a"], edges)
    ids = [c.item_id for c in out]
    assert len(ids) == len(set(ids)) and "a" not in ids
    assert set(ids) == {"b", "c"}          # one hop only: d (two hops away) is not returned
    out2 = gh.hop_expand(["a", "b"], edges)
    assert [c.item_id for c in out2] == ["c"]   # seeds never re-enter


def test_empty_graph_degrades_to_vector_only():
    r = gh.retrieve(gh.FIXTURE, Q, edges={})
    assert r["connected"] == [] and r["seeds"] == gh.vector_search(gh.FIXTURE, Q)
    iso = [gh.Item("x", "lonely onboarding note"), gh.Item("y", "other")]
    assert gh.retrieve(iso, "onboarding")["connected"] == []
    assert gh.retrieve([], "anything") == {"seeds": [], "connected": []}


def test_no_db_or_network_imports():
    src = _P.read_text()
    for banned in ("sqlalchemy", "psycopg", "requests", "httpx", "openai", "socket", "os.environ"):
        assert banned not in src
