"""p3m3 item #39, module 3.7 evidence -- a real MCP server, built by hand
with the official FastMCP class, matching module-3.7.md's own toy-CRM
example structure but exposing this project's real corpus instead of a toy
in-memory dict. Two tools, reusing this project's existing Session 2/3
functions unchanged -- no new retrieval logic, same reuse discipline as
agent_service.py's search_corpus.

`mcp` is a curriculum-evidence-only dependency (see p3m3/week3-priority-
checklist.md's D2 section for the scoping decision) -- deliberately NOT
added to requirements.txt, since nothing in the deployed app (main.py,
agent_service.py, rag_service.py) imports it; only this script does, the
same way chromadb-migration scripts are dev-only tooling, but taken one
step further by not adding the dependency to the deployed image's install
list at all.

Run with: python scripts/mcp_corpus_server.py  (stdio transport, per
module-3.7.md's own "simplest to build and debug" guidance)
Inspect with: npx @modelcontextprotocol/inspector python scripts/mcp_corpus_server.py
"""
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
from dotenv import load_dotenv

load_dotenv(BASE / ".env")
from mcp.server.fastmcp import FastMCP
from openai import OpenAI

from operational_store import corpus_summary
from rag_service import OVERFETCH_K, RAG_RELEVANCE_THRESHOLD, embed_query, get_collection, query_store, select_context_chunks

mcp = FastMCP("ai-internship-corpus")


@mcp.tool()
def search_corpus(question: str) -> str:
    """Search this project's RAG corpus (260+ ingested research papers and
    articles) for passages relevant to `question`. Use this before answering
    any question that could plausibly be covered by that corpus."""
    client = OpenAI(timeout=20.0)
    query_embedding, _ = embed_query(client, question)
    candidates = query_store(get_collection(), query_embedding, top_k=OVERFETCH_K)
    if not candidates["distances"] or candidates["distances"][0] > RAG_RELEVANCE_THRESHOLD:
        return "No sufficiently relevant passages found in the corpus."
    retrieved = select_context_chunks(candidates)
    return "\n\n".join(
        f"[{cid.rsplit('::', 1)[0]}] {doc[:600]}"
        for cid, doc in zip(retrieved["ids"], retrieved["documents"])
    )


@mcp.tool()
def corpus_overview(sample_size: int = 10) -> dict:
    """Return the total document count and a random sample of titles
    currently in the corpus. Use this before answering any question about
    what the corpus covers, or how large it is."""
    return corpus_summary(sample_size=sample_size)


if __name__ == "__main__":
    mcp.run(transport="stdio")
