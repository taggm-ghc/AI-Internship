---
name: rag-scaffold
description: Scaffold the simplest possible RAG loop (chunk documents, embed chunks, store vectors, embed a question, retrieve nearest chunks, insert into a prompt) over a folder of plain-text documents, for the purpose of deliberately triggering and naming its three characteristic failure modes -- boundary cut, keyword miss, confident-and-ungrounded -- before fixing any of them. Use when asked to build a naive/baseline RAG loop, reproduce a RAG failure mode from first principles, or demonstrate why naive retrieval-augmented generation needs chunking discipline, hybrid search, and a grounding prompt.
---

# rag-scaffold

Builds the naive RAG loop the curriculum's Module 2.2 ("Build Naive RAG, and Watch It Fail") describes, then breaks it on purpose. The point isn't a production pipeline -- this project already has one (`rag_service.py`, `main.py`'s `/ask`). The point is a minimal, disposable loop simple enough to see exactly where and why each failure mode happens, so the fix in each later module (2.3 chunking, 2.4 hybrid search, 2.2's own grounding prompt) can be pointed at a concrete, named cause instead of a vague "the answer was wrong."

## The loop, in order

1. **Chunk** every document in the target folder. Reuse `rag_ingest.chunk_text` (the project's actual production splitter: `RecursiveCharacterTextSplitter`, `CHUNK_SIZE`/`CHUNK_OVERLAP`) as the default -- don't hand-roll a second chunking implementation. To deliberately reproduce the *boundary-cut* failure, also produce a naive fixed-width split (slice every N characters, no boundary awareness, no overlap) as the "before" comparison; the production splitter is the "after."
2. **Embed** every chunk with the project's locked embedding model (`text-embedding-3-small`, from `rag_ingest.EMBEDDING_MODEL`).
3. **Store** the (chunk_id, document_id, text, embedding) tuples. For a standalone/disposable scaffold like this one, an in-memory list is correct -- there is no reason to write throwaway demonstration data into Postgres or any persistent store. Only the real corpus in `rag_service.get_collection()` is durable; this loop's storage is not.
4. **Embed the question**, same model.
5. **Retrieve** the nearest chunks by squared-L2 distance (matching the project's own metric convention -- see `rag_service.py`'s `RAG_RELEVANCE_THRESHOLD` comment).
6. **Insert into a prompt** and call the model.

## Triggering and naming all three failures in one run

Use one document set (the Northwind `sample_docs/` pack, or whatever real corpus is in scope) and one loop instance for all three -- not three separate throwaway scripts. Each failure needs its own targeted question against the same underlying data:

- **Boundary cut**: pick a document containing a rule that spans a natural chunk boundary under the naive fixed-width split (search for one rather than assuming; not every document has one at every chunk size). Ask a question about that rule. Compare the naive split's retrieved chunk against the production splitter's.
- **Keyword miss**: pick a question containing a rare, low-semantic-weight token (a policy code, a product ID, an acronym) that appears verbatim in exactly one document. Compare dense-only retrieval against a lexical (BM25) or hybrid pass over the *same* stored chunks.
- **Confident and ungrounded**: pick a question the corpus genuinely does not answer. Run it through step 6's prompt twice -- once with no grounding instruction (name the observed behavior honestly, whether it fabricates or correctly declines -- don't assume fabrication without checking), once with the project's actual `GROUNDED_PROMPT` (`rag_service.py`, import it, don't retype it).

## What this skill deliberately does not do

- Does not call any live app endpoint (`/ask`, `/ingest`, `/debug/retrieve`) -- this loop is self-contained so it can safely target a corpus (like the Northwind pack) that isn't in the production Postgres collection, without ever writing to it.
- Does not replace `rag_service.py`'s production retrieval path. Nothing built here should be wired into `/ask`.
- Does not skip the naming step. Each failure must be explicitly labeled (boundary cut / keyword miss / confident-and-ungrounded) in whatever script or output this skill produces -- the module's own point is building a mental map for diagnosing failures later by name, not just observing "it didn't work."
