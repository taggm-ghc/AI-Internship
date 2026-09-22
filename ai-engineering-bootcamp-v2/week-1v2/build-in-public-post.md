# Build-in-public post (N.5)

> Draft only — this is text for you to post from your own account, on
> whichever platform you choose (LinkedIn, X, etc.). Per this project's
> standing rule, **never include the live deployed URL** in the post or in
> this file — attach a screenshot of the running app instead. Structure
> follows the same hook / what-built / number / honest-bit / link pattern
> used elsewhere in this program (see
> `ai-portfolio-bootcamp/starter-repo/docs/linkedin-post.md`).

---

## Draft

*Refreshed 2026-09-22 — the 50-doc/9-of-9 figures below were stale (corpus grew, eval strengthened, failure narrative no longer matched what testing actually showed); this version is checked against what's actually running.*

```
Spent the last stretch turning a bare `/ask` endpoint into a real
retrieval-augmented system — and the hardest part wasn't the LLM call.

Built a RAG pipeline over a 258-document corpus (250 AI-research papers
plus recent industry coverage on agentic AI/RAG, ingested live through
the same production endpoint, not a separate seed script): PDF
extraction, chunking, embedding, a per-document diversity cap so one
long paper can't crowd every other source out of the answer, hybrid
(dense + BM25) retrieval demonstrated against a real keyword-miss case,
and citations that reflect what the model actually used — not
everything that got retrieved.

Golden-set eval: 10/10 retrieval, 9/10 generation, scored separately,
plus a newer content-overlap check that verifies a cited answer's text
actually reflects the passage it cites — not just that the right
document ID showed up in the citation list. That third check already
caught one real case the first two missed.

The honest bit: I went looking for the "confident and ungrounded"
failure — a question the corpus genuinely doesn't answer — expecting
the model to fabricate. Across several real test runs, it didn't: it
declined honestly every time, with and without an explicit grounding
instruction. Worth reporting as observed, not assumed — a system that
refuses correctly isn't a failed demo, it's the actual goal. The real,
reproduced failure modes were upstream of generation: a naive
fixed-width chunk split that severed a rule from its own exception
(fixed by structure-aware splitting), and a keyword ID that dense
retrieval alone missed outside the top 3 (recovered by hybrid search).

Also added a live/no-retrieval toggle so a question's grounded and
ungrounded answers can be compared side by side on demand, and an
observability dashboard over the durable request/retrieval log —
p50/p90/p99 latency instead of an average that hides the tail, errors
broken out by category instead of one number, and a retrieval-quality
panel built from data the app already computes.

Code (private repo, access on request) + screenshot of a live grounded
answer below.
```

---

**Before posting**

- [ ] No live URL anywhere in the post.
- [ ] Screenshot attached shows a real grounded `/ask` response (status +
      citations visible), not a bare text answer — capture it against the
      **deployed** Render URL once that's live, not the local build (see
      `p3m3/todo-digest.md`'s live-deploy status before assuming this is
      ready to post).
- [ ] Re-run `golden_eval.py` right before posting and use whatever numbers
      it actually reports that day — 10/10 retrieval, 9/10 generation, 5/5
      content-overlap as of 2026-09-22, but corpus/eval state can move.
- [ ] No employer-confidential or paywalled-source content referenced.
