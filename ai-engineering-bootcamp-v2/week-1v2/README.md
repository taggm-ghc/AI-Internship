# Agentic AI Engineering Bootcamp: Capstone Service

This folder is the capstone service built across the bootcamp's first three
sessions, one FastAPI app and one Streamlit UI growing week by week:

| Session | What it adds | Where |
|---|---|---|
| 1 — LLM API | Typed `/ask`: structured output, validation + retry guardrail, model selection, token/cost/latency metadata | [Guardrail demo](#try-the-guardrail-demo), [Model choice](#model-choice) |
| 2 — RAG | `/ingest` (chunk, embed, upsert), `/debug/retrieve`, grounded + cited `/ask` that refuses when the corpus doesn't cover a question; PostgreSQL + pgvector storage | [RAG](#retrieval-augmented-generation-rag) |
| 3 — Agents | `/agent`: a LangGraph agent that decides for itself whether to search the corpus, with a visible Think → Act → Observe trace | [Agent](#agent-session-3) |

Answers from `/ask` and `/agent` carry **APA 7 in-text citations and a
reference list**, rendered from verified publisher metadata. The model never
writes an author, year or title itself; see [Citations](#citations-apa-7).
The `stages/` files are optional Session 1 teaching references that show how
`/ask` grew step by step.

The main idea: an LLM call becomes more useful in software when it has a
predictable request shape, a predictable response shape, and observable
runtime metadata, and a RAG answer is only as trustworthy as its sources
are traceable.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Liveness, no tokens spent |
| GET | `/docs`, `/redoc`, `/openapi.json` | Auto-generated API documentation (FastAPI) |
| GET | `/providers/status` | Which model providers are configured and reachable |
| POST | `/ask` | Grounded, cited answer (structured JSON) |
| POST | `/ask/stream` | Same retrieval, streamed freeform text (no citations) |
| POST | `/agent` | LangGraph agent with a `search_corpus` tool |
| POST | `/ingest`, `/ingest/batch`, `/ingest-pdf` | Add documents to the corpus |
| GET/POST | `/ingest/versions`, `/ingest/versions/diff`, `/ingest/versions/accept` | Review and accept staged re-ingests |
| GET | `/debug/retrieve` | Raw retrieval (top-k chunks + distances), no LLM involved |
| GET | `/debug/similar-documents`, `/debug/corpus-summary`, `/debug/events` | Corpus and observability introspection |
| POST | `/summarize`, `/analyze-sentiment` | Session 1 structured-output siblings of `/ask` |

`/ask` returns `answer` (a structured object), `tokens_used`, `model`,
`latency_ms`, `cost_usd`, `attempts` (the guardrail's validation/retry log),
and the RAG fields `status` (`supported` / `insufficient` /
`not_applicable`), `citations` (chunk IDs), `references` (APA 7 entries) and
`embedding_cost_usd`. `/agent` returns `answer`, `trace`, `grounding` (including `tool_error`
when its search failed), `sources` and `references`.

## Quick Start

Requires **Python 3.12** (the Dockerfile's version; the code uses 3.10+
syntax). Run these commands from this `week-1v2` folder:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
test -f .env || cp .env.example .env
```

Optional dev tooling, for a rare manual audit only — the actual price
collection pipeline (`scripts/refresh_openai_pricing.py`) is already fully
automated and needs none of this. `pip install -r requirements-dev.txt`
adds Playwright, used only by `scripts/capture_openai_pricing.py` to
screenshot OpenAI's live pricing page for an occasional human sanity-check
of that pipeline's assumptions; see that file's comments for the one-time
Chromium setup it requires.

Open `.env` and add your key **and a PostgreSQL connection string**. The app
connects to Postgres at startup and won't boot without it:

```bash
OPENAI_API_KEY=sk-...
EXTERNAL_DB_URL=postgresql://user:password@host:5432/dbname
```

Create the schema once. This runs `migrations/001_operational_store.sql`,
which also enables the `pgvector` extension (your Postgres must have it
available):

```bash
python -c "from dotenv import load_dotenv; load_dotenv(); from operational_store import install_schema; install_schema()"
```

A fresh database starts with an empty corpus; add documents with
[`/ingest`](#ingesting-documents). (`scripts/migrate_operational_store.py`
is the one-off migration from the legacy local Chroma store, and needs
`chroma_store/`, which a fresh clone doesn't have.)
**Caution:** if your `EXTERNAL_DB_URL` points at the same database your
deployed service uses, every local run reads and writes production data.

Leave values bare, no quotes — `python-dotenv` treats quoting as
meaningful (single-quoted is literal, double-quoted processes `\n`
escapes), and none of this project's values need that. Exception: a
value containing a literal `#` needs quoting, or everything after it is
parsed as a comment. See [Configuration](#configuration) for every
optional variable.

## Terminal 1: Start the API

```bash
source .venv/bin/activate
uvicorn main:app --host 127.0.0.1 --port 8000 --reload
```

Or use `./run.sh [port]`, which also refuses to start if the port's already taken and records the actual address it started on (so `MVP_Layered_Ask.py`'s sidebar default tracks it automatically instead of assuming 8000).

Check that the API is running without spending tokens:

```bash
curl http://127.0.0.1:8000/health
```

You can also open the generated API docs:

```text
http://127.0.0.1:8000/docs
```

## Terminal 2: Start the Streamlit UI

```bash
source .venv/bin/activate
streamlit run MVP_Layered_Ask.py
```

Open `http://localhost:8501`. The sidebar lists five pages:

| Page | What it's for |
|---|---|
| **MVP Layered Ask** | Ask a question, pick provider/model and RAG mode, ingest a document, inspect the JSON, copy the equivalent `curl`; shows citations and the APA reference list |
| **Observability Dashboard** | Retrieval/HTTP events, confidence and cost-by-outcome panels |
| **MVP Layered Health** | A view of `GET /health` |
| **Setup** | Local setup instructions |
| **Agent** | The Session 3 agent: answer, grounding line, references, and the Think → Act → Observe trace |

The sidebar also tracks this session's running costs and, per
provider/model, a latency boxplot built from real `/ask` response times
(`/ask/stream` reports no latency — see `call_stream`, so it contributes no
samples). Its **Custom API base URL** field is blank by default so a
screenshot can't leak your deployed URL; left blank, pages use the
`API_BASE_URL` env var, then whatever `run.sh` last recorded in
`.faststream-local-url`, then `http://127.0.0.1:8000`.

## Try the Guardrail Demo

Turn on **Force a bad first response to demo validation + retry** in the Streamlit page.
The API intentionally asks the model for malformed JSON on the first attempt, validates
that response with Pydantic, records the failure, and retries with structured output.

This is a small classroom-friendly example of a production habit: do not trust free-form
LLM output at the boundary of your application.

### How it works

`ask_service.synthetic_malformed_json()` returns a fixed, free (no
OpenAI call) payload with `confidence` as a string, where the `Answer`
schema requires a `float`. `main.py`'s `ask()` handler validates that
on attempt 1, catches the `ValidationError`, logs it as a failed
`AttemptResult`, and retries — attempt 2 makes a real call and returns
a valid `Answer`. The response's `attempts` array shows both tries, so
the catch-and-recover is visible, not just asserted.

### Why it matters

Skip this layer and one of two things happens: the malformed payload
ships as a "successful" 200 with a string where a caller was promised
a float — silently breaking any downstream code that trusts the
contract, with no link back to the LLM call that caused it — or, if
nothing catches the validation exception, the caller gets a bare `500`
with no explanation. The second case already happened once in this
codebase for a different exception type (`AuthenticationError`, before
it was mapped to a clean `401`) — the same class of gap the guardrail
closes here for schema mismatches.

### The rest of the guardrail stack

`force_bad` shows one layer — schema validation and retry. `/ask` and
its siblings also have:

- **Typed error mapping** — `AuthenticationError` → `401`,
  `RateLimitError` → `429`/`402`, other OpenAI errors → `502`, instead
  of a generic `500`.
- **A global exception handler** — no uncaught exception, from any
  code path, ever reaches a caller as a raw traceback.
- **A proactive key check** — an obviously invalid key returns a clean
  `503` before spending a network call, not after.
- **Input and output caps** — `max_length=4000` on request text and a
  1,000-token completion cap keep per-call cost bounded and
  calculable: typical usage runs about **$0.165/day**, worst case
  (maximum-length input/output on every request) about **$0.96/day** —
  comfortably inside a small wallet budget.
- **A three-window rate limiter** (`10/min; 30/hour; 300/day`,
  tightest window checked first) on every endpoint, so a burst of
  traffic against the public demo URL can't run up an unbounded bill.
- **Client timeout/retry** (20s, 3 retries) and **multi-provider
  fallback** — a slow or unavailable provider doesn't hang the request
  or take the whole endpoint down.

Together: a malformed, oversized, or otherwise bad request meets a
typed, bounded response at every layer — never a silent failure or an
open-ended bill.

## Test With Curl

Normal request, against a local server. Only `question` is required; with
no `model`, the default free-tier-first chain answers (see
[Model choice](#model-choice)):

```bash
curl -s -X POST http://127.0.0.1:8000/ask \
  -H "Content-Type: application/json" \
  -d '{"question": "What is the memory wall problem for mixture-of-experts models on SSDs?"}' | jq
```

Validation and retry demo:

```bash
curl -s -X POST http://127.0.0.1:8000/ask \
  -H "Content-Type: application/json" \
  -d '{"question": "What is a vector database?", "force_bad": true}'
```

Agent (Session 3). Compare a corpus question, where the agent chooses to
search, with `"What is 2 + 2?"`, where it answers without the tool:

```bash
curl -s -X POST http://127.0.0.1:8000/agent \
  -H "Content-Type: application/json" \
  -d '{"question": "What is the memory wall problem for mixture-of-experts models on SSDs?"}' | jq '{answer, grounding, sources, references}'
```

Retrieval only, no LLM. `?q=` is the course contract; `?query=` also works:

```bash
curl -s "http://127.0.0.1:8000/debug/retrieve?q=memory+wall+mixture+of+experts&top_k=5" | jq
```

Ingest a document (see [Ingesting documents](#ingesting-documents)):

```bash
curl -s -X POST http://127.0.0.1:8000/ingest \
  -H "Content-Type: application/json" \
  -d '{"document_id": "my-note-1", "text": "Paste document text here."}'
```

Against a deployed instance (see [Deploy](#deploy)), replace
`http://127.0.0.1:8000` with `$DEPLOYED_URL`, a variable you export in your
own shell. Keep that URL out of public commits, PRs and this README; see the
note in Deploy.

Render's free tier spins the service down after periods of inactivity, so the
first request after a while may take up to a minute while it wakes back up —
that's expected, not an error.

## Instructor Flow

Use `main.py` and `MVP_Layered_Ask.py` for the live student demo. Open the stage files only when
you want to explain how each capability was introduced:

| Stage | File | Teaching point |
|-------|------|----------------|
| 1 | `stages/stage_1_bare_ask.py` | Smallest typed `/ask`: question in, string answer out. |
| 2 | `stages/stage_2_structured_output.py` | Add a Pydantic `Answer` schema and OpenAI structured output. |
| 3 | `stages/stage_3_guardrails_and_observability.py` | Add validation retry, model selection, latency, and cost. |

Run one stage at a time if you want to teach the build-up live:

```bash
uvicorn stages.stage_1_bare_ask:app --host 127.0.0.1 --port 8000 --reload
uvicorn stages.stage_2_structured_output:app --host 127.0.0.1 --port 8000 --reload
uvicorn stages.stage_3_guardrails_and_observability:app --host 127.0.0.1 --port 8000 --reload
```

## Tests, Evals and Verification

| Command | What it checks | Cost / side effects |
|---|---|---|
| `python smoke_test.py` | Starts the API, checks `/health` and `/docs` | No tokens |
| `python -m unittest test_citations test_pdf_extract` | APA formatter and marker rendering; PDF extraction | No network, no DB |
| `python test_all_stages.py` | Live `/ask` contract: structured answer, guardrail retry, model override, cost scaling | Real calls, well under a cent (5 calls ≈ $0.0006) |
| `python golden_eval.py [--base-url URL]` | 10 golden questions: retrieval hit, supported/refusal correctness, content overlap | Real calls via `/ask`; writes request, retrieval and `golden_eval` events to the DB |
| `python scripts/retrieval_eval.py` | 200-question retrieval eval (`config/retrieval_eval_set.json`): dense vs. hybrid vs. reranked, paired t-test + permutation test, per-stratum | Real embedding + reranker calls; read-only against the DB |
| `python scripts/rerank_gate_calibration.py` | Calibrates the reranker's high-confidence skip threshold from saved eval results | Embedding calls only |

The eval set is rebuilt with `python scripts/build_retrieval_eval_set.py`
(defaults to the lowest-cost model, `gpt-4.1-nano`; `--model` to override).
Citation metadata is rebuilt with `python scripts/build_citation_metadata.py`
after the corpus changes.

## Deploy

The `Dockerfile` in this folder is the deploy artifact — it installs only
`requirements.txt` (never `requirements-dev.txt`, see the Dockerfile's own
comments) and runs `uvicorn main:app --host 0.0.0.0 --port 8000`.

> **Critical — do not share your live URL publicly.** Never post your
> Render (or other) service URL in this README, in commits/PRs on this
> public repo, or on LinkedIn, Twitter/X, blogs, or any other public page.
> Anyone with the URL can spend your API credits. Share it only through
> the private channel your course/instructor specifies (e.g. an LMS
> submission field), and keep it out of version control — e.g. a local,
> gitignored note, or a `DEPLOYED_URL` var you export in your own shell.

**Render:**

1. New **Web Service** → connect this GitHub repo.
2. Runtime: **Docker**.
3. **Root Directory**: `ai-engineering-bootcamp-v2/week-1v2` — this is a
   monorepo, so Render needs to be told which subfolder holds the
   `Dockerfile`. Build and start commands are then read from that
   Dockerfile automatically; leave them blank.
4. **Environment Variables**: add `OPENAI_API_KEY` (required) and, if you
   want the free-tier fallback chain, any of `GROQ_API_KEY`,
   `GEMINI_API_KEY`, `MISTRAL_API_KEY`, `OPENROUTER_API_KEY`,
   `SAMBANOVA_API_KEY`, `CLOUDFLARE_API_TOKEN` / `CLOUDFLARE_ACCOUNT_ID`
   — see `.env.example` for what each one does. **Also required as of the
   Session 2 RAG layer:** `INTERNAL_DB_URL` — the app now needs Postgres
   at startup (`db.py`) and will fail to boot without it. Render sets
   `RENDER=true` itself in every one of its environments, which makes the
   app read `INTERNAL_DB_URL` rather than `EXTERNAL_DB_URL` — copy the
   **Internal Database URL** from your Render Postgres instance's own page
   (not the external one; it won't resolve from inside Render's network).
   Optional: `INGEST_API_KEY` (see [Security](#security)) and the
   retrieval switches under [Configuration](#configuration); the defaults
   are the tested ones. Set these directly in Render's dashboard; never
   commit a `.env` file or paste real key/URL values into the repo.
5. **Health Check Path**: `/health`.
6. Deploy. Render builds the image from the Dockerfile and routes traffic
   to the port it `EXPOSE`s (8000) — no `$PORT` wiring needed on this
   runtime. If a push to your branch doesn't trigger a build, check the
   service's auto-deploy setting, or use **Manual Deploy → Deploy latest
   commit**. Peak memory measured on a clean install is about 184 MB, within
   the free tier's 512 MB (see `RAG_HYBRID` under
   [Configuration](#configuration) before enabling hybrid search).

On Render's free tier the service spins down after inactivity — see the
cold-start note under [Test With Curl](#test-with-curl). To point the
Streamlit demo at a deployed instance instead of localhost, either paste
that URL into the **API base URL** field in the sidebar for the current
session (nothing committed anywhere), or see below to deploy the
Streamlit UI itself as a second Render service with that URL wired in
via an env var.

### PostgreSQL runtime and recovery (p3m3 item #2.g)

All durable state lives in one Postgres database, `internship` schema,
eight tables: `documents` (source text + provenance), `document_versions`
(staged re-ingests awaiting acceptance), `vectors` (chunk and
document-centroid embeddings), `artifacts` (raw file bytes, SHA-256-keyed —
uploaded PDFs and quarantined originals), `events` (the audit/observability
log every `record_event()` call writes to), `provider_observations`,
`collections` (with a revision counter that invalidates cached indexes),
`schema_version`. Plus one read-only view, `document_provenance`, which
flattens each document's provenance JSON into plain columns. Nothing the
deployed app needs lives only on local disk or in `chroma_store/` (legacy,
pre-migration, excluded from the deploy image — see `.dockerignore`).

**Recovery, in order of what's actually durable:**
1. **Render's own Postgres backups** (if enabled on your plan) are the
   first line of defense — check your Postgres instance's own dashboard
   for retention settings; this project does not configure or rely on a
   separate backup job.
2. **`internship.artifacts`** already holds a DB-side copy of every PDF
   ever ingested through `/ingest-pdf` or the original migration, keyed by
   SHA-256 — a `pg_dump`/restore of this table alone recovers original
   source files even if `ingestion_quarantine/` (the local, gitignored
   copy) is lost.
3. **`ingestion_quarantine/`** is the local-disk copy of original source
   PDFs, kept for provenance/recovery reference — never read by the
   deployed app at runtime (excluded from the Docker build context), and
   not itself backed up beyond normal filesystem/git-host redundancy.
4. A full `pg_dump` of the `internship` schema is the actual restorable
   snapshot; no automated snapshot job exists in this project as of this
   writing (see p3m3's programme-closeout item for the planned one-time
   export before the course ends).

### Deploying the Streamlit UI as its own Render service

The same repo and `Dockerfile` also serve `MVP_Layered_Ask.py` — Render just
needs a different start command, since the Dockerfile's own `CMD` runs
the API. This is optional; the sidebar's manual URL paste (above) covers
the common case of a solo student pointing their own local Streamlit at
their own deployed API.

1. New **Web Service** → same GitHub repo, same **Root Directory**
   (`ai-engineering-bootcamp-v2/week-1v2`), **Runtime**: Docker.
2. Under **Advanced → Docker Command**, override the Dockerfile's `CMD`
   with:

   ```text
   streamlit run MVP_Layered_Ask.py --server.address 0.0.0.0 --server.port 8000
   ```

   Use port `8000` here (not Streamlit's default `8501`) — Render's
   Docker runtime routes traffic to whatever port the `Dockerfile`
   `EXPOSE`s, and this one only exposes `8000`.
3. **Environment Variables**: add `API_BASE_URL` set to your deployed
   API service's URL, e.g. `https://your-api-service.onrender.com` —
   **set this in Render's dashboard only, never commit it** (see the
   warning above). `MVP_Layered_Ask.py` reads it as the sidebar's default so
   it doesn't need to be pasted in by hand.
4. **Health Check Path**: `/_stcore/health` (Streamlit's built-in health
   endpoint).
5. Deploy. You'll get a second, separate Render URL for the UI — same
   "don't share it publicly" rule applies to this one too.

**Elsewhere (Fly.io, Railway, a VM, etc.):** the same `Dockerfile` works
anywhere that can build and run a container and inject env vars at
runtime — build with `docker build -t week-1v2 .`, run with `docker run -p
8000:8000 -e OPENAI_API_KEY=sk-... week-1v2`, and set the platform's
equivalent of a health check to `GET /health`.

## Model Choice

Default: `gpt-4.1-nano` — OpenAI's cheapest structured-output-capable
tier ($0.10/1M input, $0.40/1M output tokens). It fits `/ask`'s bounded
single-turn Q&A contract, where a predictable schema matters more than
deep reasoning; a real call against the deployed instance cost
**$0.000096** (118 prompt + 210 completion tokens) — see [Cost per
call](#cost-per-call).

Model selection is sourced from `config/model-selection.json`, not
hardcoded, and covers two cases:

- **Per-request override** — `model=` routes straight to that OpenAI
  model.
- **No override (default path)** — a free-tier-first fallback chain
  (`provider_chain`) tries Groq and Gemini first (confirmed
  structured-output-capable), then Mistral/OpenRouter/SambaNova/
  Cloudflare (freeform-only until confirmed), with OpenAI as the
  always-available last entry. Unconfigured providers are skipped —
  `GET /providers/status` shows what's live, and every response's
  `model` field reports who actually served it (e.g.
  `"groq:openai/gpt-oss-20b"`). The chain falls back on availability
  errors (auth, rate limit, timeout, 5xx) and on one kind of 400: a model
  whose own structured output failed validation (`json_validate_failed`).
  Any other 400 means the request itself was wrong and stops the chain
  rather than being masked. Groq's `gpt-oss-20b` runs at
  `reasoning_effort: "low"` (per-provider, in `config/model-selection.json`):
  its hidden reasoning shares the 1,000-token completion budget, and low
  effort measured about 4× fewer completion tokens on `/ask`.

A free tier is a rate/volume allowance, not a $0 price — `cost_usd`
always reflects the provider's real per-token rate.

**Project rule: lowest-cost model unless a capability requirement
overrides.** Everything else that calls a model is pinned to
`gpt-4.1-nano`: the agent (`agent_service.AGENT_MODEL`), the reranker
(`RAG_RERANK_MODEL`), `golden_eval.py`, and the eval-set builder's
default. A stronger model is used only where a need has been shown, and
that reason is recorded where the model is set.

### Cost per call

Real `cost_usd` values from live calls, not estimates:

| Call | Tokens (prompt + completion) | `cost_usd` |
|------|-------------------------------|------------|
| `/ask` roundtrip | 118 + 210 | `$0.000096` |
| `/ask` roundtrip | — | `$0.000039` |
| `test_all_stages.py`, 5 live calls | — | `$0.00061` total |

**Roughly $0.00004–$0.0001 per short call** — a $5 OpenAI credit covers
on the order of 50,000–100,000 of them at this default model and
question length; cost scales with answer length for longer calls like
`/summarize`.

Two caveats: this isn't free-tier-inclusive (a Groq/Gemini call still
reports its real per-token rate even when nothing was actually billed —
see `free_tier_note` and the sidebar's per-provider breakdown for the
truer "what did this cost" view), and it's not a cap (that's what the
input/output limits under [Try the Guardrail
Demo](#try-the-guardrail-demo) are for). Reproduce it yourself with
`python test_all_stages.py` or the Streamlit sidebar's running-costs
panel.

### LAN-local inference (optional)

Point the fallback chain at your own OpenAI-compatible server(s) on the
local network (e.g. llama.cpp) instead of, or alongside, the cloud
providers above. Opt-in only: with none of the variables below set,
LAN-local is skipped entirely.

One server, in `.env`:

```bash
LOCAL_INFERENCE_HOST=127.0.0.1
LOCAL_INFERENCE_PORT=8010
```

More than one server — a single comma-separated variable instead, one
`host:port` pair per server, which takes priority over the two above:

```bash
LOCAL_INFERENCE_SERVERS=127.0.0.1:8010,192.168.1.42:8011
```

A bare host with no `:port` reuses `LOCAL_INFERENCE_PORT` (or `8010` if
that's unset too). Each configured server's models are discovered live
from its own `/v1/models` endpoint — no model name is ever hardcoded,
committed, or logged — and each server is tracked under its own
`local-inference@host:port` identity, so one server being unreachable
never affects another that's fine (see `/providers/status`). Whichever
candidate — local or cloud — is actually cheapest and reachable wins the
no-override default path, per the economic utility frontier described
above.

## Retrieval-Augmented Generation (RAG)

`/ask` always retrieves before answering. If the closest retrieved chunk is
near enough (`RAG_RELEVANCE_THRESHOLD`, squared L2 distance 1.2), the answer
is grounded in retrieved context and cites its sources; otherwise it answers
from general knowledge (`status: "not_applicable"`) or refuses
(`status: "insufficient"`) rather than guess.

### Architecture

```text
Ingestion
  PDF (/ingest-pdf, or the baseline corpus)
    → extract_pdf_text()        pdf_extract.py — page text; References section truncated;
                                 NUL bytes replaced with a visible U+FFFD
  text (/ingest, /ingest/batch)
    → content scan               detect_adversarial_content() — see Security
    → chunk                      RecursiveCharacterTextSplitter, 800 chars / 100 overlap
    → embed                      text-embedding-3-small
    → upsert                     PostgreSQL + pgvector (internship.vectors), plus a
                                 per-document centroid; provenance in internship.documents

/ask
  → embed_query()
  → build_candidate_pool()       dense top-15 (OVERFETCH_K); hybrid BM25+dense only if RAG_HYBRID=1
  → relevance gate               closest distance <= RAG_RELEVANCE_THRESHOLD, else no retrieval
  → confidence-gated reranker    skipped when one document is clearly ahead (gap > 0.20);
                                 otherwise a listwise LLM rerank to the top 8 (RERANK_K)
  → select_context_chunks()      per-document cap (3), dedup, top 5 (CONTEXT_K)
  → GROUNDED_PROMPT              numbered passages [1]..[N]; retrieved text is data, never instructions
  → model writes [n] markers     citations.py → APA 7 in-text citations + reference list
```

`chunk_id` format is `{document_id}::{chunk_index}`; the two are distinct
fields, never conflate a document ID with a chunk ID.

### Ingesting documents

- **`POST /ingest`** (`{document_id, text}`), **`/ingest/batch`**, and
  **`/ingest-pdf`** (multipart, ≤ 10 MB) chunk, embed and upsert into the
  live corpus without a rebuild.
- A **new** `document_id` goes live immediately. **Re-ingesting an existing
  `document_id` never overwrites it:** it stages a new version, reviewed via
  `GET /ingest/versions` and `/ingest/versions/diff`, and goes live through
  `POST /ingest/versions/accept`. Staged versions are accepted automatically
  only for an authenticated caller whose content scan is clean.
- `rag_ingest.py` is the original one-off baseline-corpus builder; the live
  corpus is maintained through the endpoints above.

### Corpus

About 260 documents: open-access AI/ML research papers (arXiv, PMLR/ICML,
ACL Anthology, NeurIPS, JMLR), a few news and blog articles, and design
references. Each document's provenance (source, URL, fetch time, content
hash, content-scan result) is stored with it, queryable through the
`internship.document_provenance` view. Source PDFs are kept locally in
`ingestion_quarantine/` (gitignored, excluded from the image) and
DB-side in `internship.artifacts`.

### Retrieval decisions, and the evidence behind them

- **Reranker on, confidence-gated.** On the 200-question eval it improved
  ranking significantly (doc reciprocal rank +0.028, p = 0.011). Skipping it
  when one document is clearly ahead cut 45% of reranker calls with no loss
  on a held-out half.
- **Hybrid search off** (`RAG_HYBRID`). It showed no significant gain,
  trended worse on paraphrased questions, and its in-process BM25 index
  adds about 232 MB (about 600 MB peak during a post-ingest rebuild).
- **Citation precision.** Only passages the model says it used are cited.
- **`RAG_RELEVANCE_THRESHOLD`** is calibrated for this corpus, embedding
  model and distance metric, not a universal constant. Re-check it with
  `golden_eval.py` after corpus or chunking changes.

## Agent (Session 3)

`POST /agent` runs a LangGraph `StateGraph` (agent node → `ToolNode` →
back, routed by `tools_condition`) with one read-only tool, `search_corpus`,
which reuses the RAG retrieval above. The model decides per question whether
to search: a corpus question makes one tool call, `"What is 2 + 2?"` makes
none. That's the agent-vs-workflow distinction. `/ask` is a workflow (it
always retrieves); `/agent` is an agent.

- **Bounded, and fails closed:** `recursion_limit` caps the loop. Hitting
  it returns **HTTP 503** "stopped at its step limit… try a narrower
  question", never a 200 that reads as success.
- **Tool errors are observations, not crashes:** a failing search (e.g. the
  database is unreachable) is returned to the model as an error message
  (type only, never connection details) telling it not to retry, because
  the client already retried transient errors. The answer then starts with
  a fixed note that the corpus couldn't be checked, and `grounding` is
  `tool_error`. "Nothing found" is a normal observation too.
- **Trust boundary:** the system prompt treats tool results as data, never
  instructions (tested against a corpus passage containing an embedded
  "you are now…" prompt). The system prompt itself is kept out of the
  returned trace.
- **`grounding`** (`no_tool_call` / `tool_found_nothing` / `tool_error` / `tool_sources`)
  and **`sources`** are derived from the trace, not from the model's claim.
  `tool_sources` means the search returned passages, not that the answer is
  verified to rely on them.
- **`trace`** is the Think → Act → Observe record, rendered on the
  Streamlit **Agent** page.
- **Audit log:** every run writes one `agent_run` event (OpenTelemetry-style
  `gen_ai.*` fields: tool name, call ID, outcome, error type, returned
  document IDs, model turns, duration, whether it stopped at the limit).
  The question and the tool's arguments and results are logged only when
  `AGENT_LOG_TOOL_CONTENT=1`, since they can contain sensitive text. A
  failed audit write never breaks the response.

Curriculum demos (dev-only, not deployed): `scripts/raw_agent_loop_demo.py`
(the same loop by hand with the raw OpenAI SDK), `scripts/mcp_corpus_server.py`
(the corpus as an MCP server over stdio), `scripts/a2a_corpus_agent.py`
(an A2A agent on `127.0.0.1:9000`, no auth, local only). They need
`mcp<2` and `a2a-sdk==0.3.26`, deliberately not in `requirements.txt`.

## Citations (APA 7)

The model never writes an author, year or title. `/ask`'s model places `[n]`
passage markers and `/agent`'s places `[document-id]` markers; `citations.py`
replaces them with APA 7 in-text citations built from verified publisher
records, and returns an APA reference list containing exactly one entry per
work cited in the text.

- **Metadata:** `config/citation_metadata.json`, CSL-JSON records built by
  `scripts/build_citation_metadata.py` from arXiv abstract pages,
  PMLR/NeurIPS/JMLR `citation_*` metadata, ACL Anthology `.bib` files, and
  provenance for news and blog posts. A document without a record falls back
  to APA's no-author form (title and `n.d.`), never a guessed author.
- **Guards:** unknown or out-of-range markers are dropped, never guessed;
  `/agent` accepts only documents its search actually returned. Refusals
  carry no citations.
- **Known limits:** titles keep their published capitalization rather than
  APA sentence case (as the CSL APA style does); `/agent` places citations
  in roughly 4 of 6 runs, otherwise returning its answer uncited but with
  its grounding line; `/ask/stream` has no citations (it can't rewrite text
  mid-stream).

## Security

- **Keys and URLs** live only in `.env` (gitignored) or the host's dashboard.
  Never commit them, and never post your deployed URL publicly.
- **`/ingest` content scan** (`detect_adversarial_content`): invisible
  Unicode is always rejected. Prompt-injection phrasing and
  corpus-exfiltration patterns are rejected for unauthenticated callers.
  A caller is authenticated by sending `X-Ingest-Key` matching
  `INGEST_API_KEY` (compared in constant time). **If `INGEST_API_KEY` is
  unset, every caller counts as authenticated.**
- **Retrieved text is data, never instructions**, in both `/ask`'s grounded
  prompt and `/agent`'s system prompt. `/ask` also strips invisible
  Unicode from every retrieved chunk, and `/agent` does the same for the
  passages its tool returns.
- **Rate limits** (`10/minute; 30/hour; 300/day`) on the model-calling
  endpoints; input and output caps bound per-call cost.

## Configuration

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `OPENAI_API_KEY` | yes | — | OpenAI calls (embeddings; paid fallback) |
| `EXTERNAL_DB_URL` | yes, off Render | — | Postgres URL used locally |
| `INTERNAL_DB_URL` | yes, on Render | — | Render's internal Postgres URL (`RENDER=true` selects it) |
| `GROQ_API_KEY`, `GEMINI_API_KEY`, `MISTRAL_API_KEY`, `OPENROUTER_API_KEY`, `SAMBANOVA_API_KEY`, `CLOUDFLARE_API_TOKEN` / `CLOUDFLARE_ACCOUNT_ID` | no | — | Free-tier providers tried first; see `.env.example` |
| `LOCAL_INFERENCE_HOST` / `_PORT` / `_SERVERS` / `_API_KEY` | no | — | Optional LAN inference servers |
| `INGEST_API_KEY` | no | unset (everyone authenticated) | See [Security](#security) |
| `API_BASE_URL` | no | — | Streamlit service: which API the pages call |
| `RAG_RERANK` | no | on | Reranker in `/ask` |
| `RAG_RERANK_MODEL` | no | `gpt-4.1-nano` | Reranker model (pinned) |
| `RAG_RERANK_HIGH_GAP` | no | `0.20` | Skip the reranker when the top document leads by more than this |
| `RAG_HYBRID` | no | off | Hybrid BM25 + dense retrieval; adds about 232 MB of memory |
| `AGENT_LOG_TOOL_CONTENT` | no | off | Also log the question and tool arguments/results in `/agent`'s `agent_run` audit events (metadata only by default) |

The `RAG_*` switches are read at request time, so changing one in the host's
dashboard and restarting is enough; no redeploy is needed.

## File Map

```text
week-1v2/
├── README.md, CLAUDE.md, .env.example, Dockerfile, run.sh
├── requirements.txt / requirements-dev.txt
├── main.py                     # FastAPI app: every endpoint above
├── ask_service.py              # Structured-output calls, guardrail, ungrounded fallback
├── providers.py                # Free-tier-first provider chain + LAN discovery
├── rag_service.py              # Retrieval, reranker, confidence gate, grounded prompt
├── rag_ingest.py               # Chunking/embedding; one-off baseline corpus builder
├── pdf_extract.py              # PDF → text
├── citations.py                # APA 7 in-text citations + reference lists
├── agent_service.py            # Session 3 LangGraph agent
├── db.py, operational_store.py, operational_audit.py   # Postgres access, events, audit middleware
├── pricing_config.py           # Cost calculation from config/model-pricing.json
├── MVP_Layered_Ask.py          # Streamlit entry page
├── pages/                      # Observability Dashboard, Health, Setup, Agent
├── api_client.py, ui_theme.py, ui_widgets.py            # Shared Streamlit helpers
├── config/                     # model selection/pricing, golden set, retrieval eval set, citation metadata
├── migrations/                 # Postgres schema (001_operational_store.sql)
├── scripts/                    # evals, calibration, metadata builder, curriculum demos, pricing tools
├── stages/                     # Session 1 teaching references
├── smoke_test.py, golden_eval.py, test_*.py
├── .claude/skills/             # Project Claude Code skills (rag-scaffold, research-informed-planning)
├── chroma_store/               # Legacy pre-Postgres vector store (gitignored, not deployed)
└── ingestion_quarantine/       # Local source PDFs (gitignored, not deployed)
```

## Troubleshooting

- `Cannot reach http://127.0.0.1:8000`: start the API server in another terminal.
- `OPENAI_API_KEY` error: make sure `.env` exists and contains a real key.
- `EXTERNAL_DB_URL is not set` / `INTERNAL_DB_URL is not set` at startup: add the Postgres URL (see [Quick Start](#quick-start)); on Render, set `INTERNAL_DB_URL`.
- `Address already in use`: another server is already using port `8000`; stop it or use a different port.
- Streamlit opens but requests fail: confirm the sidebar's Custom API base URL is blank or correct (or, on a deployed Streamlit service, that `API_BASE_URL` is set correctly).
- A push didn't redeploy on Render: check the service's auto-deploy setting, or use Manual Deploy.
- `/agent` returns `503 … stopped at its step limit`: the agent looped without reaching an answer; ask a narrower question. (This is the intended fail-closed response, not a crash.)
- An `/agent` answer starts with "Note: the corpus search failed…": the search tool errored (see server logs for the error type); the answer is from general knowledge, and `grounding` is `tool_error`.
