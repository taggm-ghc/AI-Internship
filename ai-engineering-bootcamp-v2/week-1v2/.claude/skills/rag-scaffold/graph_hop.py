"""Offline one-hop graph + lexical retrieval demo (module 5.2, A-52-M2).

Retrieval gives the seed hits ("what is this like"). One hop over shared-source and
shared-identifier links expands them ("what is connected to this"). Everything is in
memory: no database, no network, no embedding call. BM25 stands in for the vector
step so the demo is deterministic; swap in the scaffold's dense retriever for the same
seam (`seed_fn`). Nothing here is wired into /ask. Fixture rows are synthetic.

Failure name shown by the demo: "relation miss" -- a relevant item shares no wording
with the question, so similarity search never ranks it, but it cites the same source
as a hit.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field

from rank_bm25 import BM25Okapi

EDGE_SHARED_SOURCE = "shared_source"
EDGE_SHARED_IDENTIFIER = "shared_identifier"


@dataclass(frozen=True)
class Item:
    item_id: str
    text: str
    source_ids: tuple = ()
    identifiers: tuple = ()


@dataclass(frozen=True)
class Connected:
    item_id: str
    edge_type: str
    via: str          # the shared source/identifier value
    from_seed: str    # the seed hit that reached it


def _tok(s: str) -> list:
    return re.findall(r"[a-z0-9]+", s.lower())


def build_edges(items) -> dict:
    """item_id -> list of (neighbour_id, edge_type, via). Undirected, no self-loops."""
    by_key = defaultdict(list)
    for it in items:
        for s in set(it.source_ids):
            by_key[(EDGE_SHARED_SOURCE, s)].append(it.item_id)
        for i in set(it.identifiers):
            by_key[(EDGE_SHARED_IDENTIFIER, i)].append(it.item_id)
    adj = defaultdict(list)
    for (etype, via), ids in by_key.items():
        for a in ids:
            for b in ids:
                if a != b:
                    adj[a].append((b, etype, via))
    return dict(adj)


def vector_search(items, query: str, k: int = 2) -> list:
    """Seed hits: top-k items with a positive BM25 score (zero overlap is not a hit)."""
    if not items:
        return []
    bm = BM25Okapi([_tok(i.text) for i in items])
    scores = bm.get_scores(_tok(query))
    ranked = sorted(range(len(items)), key=lambda n: (-scores[n], items[n].item_id))
    return [items[n].item_id for n in ranked[:k] if scores[n] > 0]


def hop_expand(seeds, edges: dict, limit: int = 10) -> list:
    """One hop only. Seeds are never returned, each item appears once (first edge wins,
    deterministic order), so a cyclic graph cannot loop or duplicate."""
    seen = set(seeds)
    out = []
    for s in seeds:
        for nb, etype, via in sorted(edges.get(s, []), key=lambda e: (e[0], e[1], e[2])):
            if nb in seen:
                continue
            seen.add(nb)
            out.append(Connected(nb, etype, via, s))
            if len(out) >= limit:
                return out
    return out


def retrieve(items, query: str, k: int = 2, edges: dict | None = None, limit: int = 10) -> dict:
    """seeds + connected. An empty/missing graph degrades to seeds only."""
    edges = build_edges(items) if edges is None else edges
    seeds = vector_search(items, query, k)
    connected = hop_expand(seeds, edges, limit) if edges else []
    return {"seeds": seeds, "connected": connected}


# Synthetic fixture mirroring the counted VERA shape (claims -> spans -> sources; a few
# sources cited by >=2 claims; most sources cited once). Invented text, invented ids.
FIXTURE = [
    Item("claim-01", "Remote onboarding programs raised new hire retention in the pilot cohort.", ("src-A",), ("idf-001",)),
    Item("claim-02", "Quarterly invoicing cadence shifted after the budget review.", ("src-A",)),
    Item("claim-03", "Warehouse scanners reduced picking errors during the winter trial.", ("src-B",)),
    Item("claim-04", "Picking errors fell further once shelf labels were standardised.", ("src-C",), ("idf-002",)),
    Item("claim-05", "Standardised labels were also adopted by the returns desk.", ("src-D",), ("idf-002",)),
    Item("claim-06", "Weather delays had no measurable effect on delivery windows.", ("src-E",)),
]


def main() -> None:
    q = "remote onboarding retention"
    r = retrieve(FIXTURE, q)
    print(f"query: {q!r}")
    print("seed hits (similarity only):", r["seeds"])
    for c in r["connected"]:
        print(f"relation miss caught: {c.item_id} via {c.edge_type}[{c.via}] from {c.from_seed}")
    r0 = retrieve(FIXTURE, q, edges={})
    print("empty graph -> vector-only:", r0)


if __name__ == "__main__":
    main()
