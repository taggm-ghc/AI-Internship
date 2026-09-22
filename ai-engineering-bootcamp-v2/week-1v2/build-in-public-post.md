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

```
Spent this week turning a bare `/ask` endpoint into a real
retrieval-augmented system — and the hardest part wasn't the LLM call.

Built a RAG pipeline over 50 open-access AI research papers: PDF
extraction, chunking, embedding, a per-document diversity cap so one
long paper can't crowd every other source out of the answer, and
citations that reflect what the model actually used — not everything
that got retrieved.

Golden-set eval: 9/9 on both retrieval and generation, scored
separately, against real (not synthetic) questions I verified by hand.

The honest bit: getting retrieval right is easy on the happy path.
The interesting failures show up at the edges — an in-domain question
whose specific answer just isn't in the corpus pulled in two unrelated
papers' chunks, and the model answered confidently anyway instead of
saying "I don't know." That's exactly the kind of gap that motivates
the next stage: hybrid search + reranking, not just a bigger corpus.

Code (private repo, access on request) + screenshot of a live grounded
answer below.
```

---

**Before posting**

- [ ] No live URL anywhere in the post.
- [ ] Screenshot attached shows a real grounded `/ask` response (status +
      citations visible), not a bare text answer.
- [ ] The 9/9 figure matches the current `golden_eval.py` run (re-check if
      the corpus has changed since this draft).
- [ ] No employer-confidential or paywalled-source content referenced.
