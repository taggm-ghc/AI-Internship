# Week 1 v2: Minimal `/ask` Demo

This folder is the simplified class version of the Week 1 AI Engineering bootcamp demo.
Students run one final API and one small Streamlit page. The `stages/` files are optional
teaching references that show how the endpoint grows step by step.

## What Students Will Build

A typed FastAPI endpoint that accepts a question and returns:

- `answer`: a structured answer object
- `tokens_used`: token usage returned by the model provider
- `model`: the model used for the request
- `latency_ms`: how long the request took
- `cost_usd`: an estimated request cost
- `attempts`: validation and retry details for the guardrail demo

The main idea: an LLM call becomes more useful in software when it has a predictable
request shape, a predictable response shape, and observable runtime metadata.

## Quick Start

Run these commands from this `week-1v2` folder:

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

Open `.env` and add your key:

```bash
OPENAI_API_KEY=sk-...
```

Leave values bare, no quotes — `python-dotenv` treats quoting as
meaningful (single-quoted is literal, double-quoted processes `\n`
escapes), and none of this project's values need that. Exception: a
value containing a literal `#` needs quoting, or everything after it is
parsed as a comment.

## Terminal 1: Start the API

```bash
source .venv/bin/activate
uvicorn main:app --host 127.0.0.1 --port 8000 --reload
```

Or use `./run.sh [port]`, which also refuses to start if the port's already taken and records the actual address it started on (so `demo_page.py`'s sidebar default tracks it automatically instead of assuming 8000).

Check that the API is running without spending tokens:

```bash
curl http://127.0.0.1:8000/health
```

You can also open the generated API docs:

```text
http://127.0.0.1:8000/docs
```

## Terminal 2: Start the Demo Page

```bash
source .venv/bin/activate
streamlit run demo_page.py
```

Open:

```text
http://localhost:8501
```

Use the page to ask a question, switch models, inspect the JSON response, and copy the
equivalent `curl` request.

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

Normal request, against a local server:

```bash
curl -s -X POST http://127.0.0.1:8000/ask \
  -H "Content-Type: application/json" \
  -d '{"question": "What is Retrieval-Augmented Generation in one sentence?", "model": "gpt-4o-mini"}'
```

Validation and retry demo:

```bash
curl -s -X POST http://127.0.0.1:8000/ask \
  -H "Content-Type: application/json" \
  -d '{"question": "What is a vector database?", "model": "gpt-4o-mini", "force_bad": true}'
```

Same request against a deployed instance (see [Deploy](#deploy)), piped through
`jq` for readable output — replace `$DEPLOYED_URL` with your own service's
URL (keep that URL out of public commits, PRs, and this README; see the note
in Deploy):

```bash
curl -s -X POST "$DEPLOYED_URL/ask" \
  -H "Content-Type: application/json" \
  -d '{"question": "What is RAG in one sentence?", "model": "gpt-4o-mini"}' | jq
```

Render's free tier spins the service down after periods of inactivity, so the
first request after a while may take up to a minute while it wakes back up —
that's expected, not an error.

## Instructor Flow

Use `main.py` and `demo_page.py` for the live student demo. Open the stage files only when
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

## Smoke Test

This starts the final API, checks `/health` and `/docs`, and does not call OpenAI:

```bash
source .venv/bin/activate
python smoke_test.py
```

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
   — see `.env.example` for what each one does. Set these directly in
   Render's dashboard; never commit a `.env` file or paste real key values
   into the repo.
5. **Health Check Path**: `/health`.
6. Deploy. Render builds the image from the Dockerfile and routes traffic
   to the port it `EXPOSE`s (8000) — no `$PORT` wiring needed on this
   runtime.

On Render's free tier the service spins down after inactivity — see the
cold-start note under [Test With Curl](#test-with-curl). To point the
Streamlit demo at a deployed instance instead of localhost, paste that
URL into the **API base URL** field in the sidebar (this stays local to
your browser session, not committed anywhere).

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
  `"groq:openai/gpt-oss-20b"`).

A free tier is a rate/volume allowance, not a $0 price — `cost_usd`
always reflects the provider's real per-token rate.

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

## File Map

```text
week-1v2/
├── README.md
├── main.py                         # Final API used by students
├── demo_page.py                    # Streamlit UI for the final API
├── smoke_test.py                   # No-token API startup check
├── Dockerfile                      # Deploy artifact — see Deploy
├── run.sh                          # Local dev launcher with port auto-retry
├── requirements.txt
├── requirements-dev.txt            # Optional: pricing-page screenshot verification (Playwright)
├── scripts/
│   ├── refresh_openai_pricing.py       # Machine-readable price extraction (no browser needed)
│   ├── append_model_pricing.py         # Manual PricingRecord append helper
│   ├── capture_openai_pricing.py       # Rare manual audit screenshots only (needs requirements-dev.txt)
│   └── chromium-deps.sh                # System libs for the above
├── .env.example                    # Copy to .env and fill in — .env itself is gitignored
├── .gitignore
└── stages/
    ├── stage_1_bare_ask.py
    ├── stage_2_structured_output.py
    └── stage_3_guardrails_and_observability.py
```

## Troubleshooting

- `Cannot reach http://127.0.0.1:8000`: start the API server in another terminal.
- `OPENAI_API_KEY` error: make sure `.env` exists and contains a real key.
- `Address already in use`: another server is already using port `8000`; stop it or use a different port.
- Streamlit opens but requests fail: confirm the sidebar API base URL is `http://127.0.0.1:8000`.
