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

Leave values bare, no surrounding quotes — every value in this file
(`.env`/`.env.example`) follows that convention. `python-dotenv` treats
quoting as meaningful, not cosmetic: unquoted (this project's convention)
strips surrounding whitespace with no escape processing; single-quoted is
fully literal; double-quoted processes escape sequences like `\n`. None of
this project's values (API keys, dates, host:port pairs) need any of that,
so bare is simplest — the one case quoting would actually matter is a
value containing a literal `#`, which unquoted gets truncated as a comment
from that point on.

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

## Test With Curl

Normal request:

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

## Model Choice

Two separate model-selection questions, both answered from
`config/model-selection.json`, not hardcoded in the code:

**Per-request override** — if a caller passes `model=`, that exact OpenAI
model is used directly (`selected_model`/`supported_models`; default
`gpt-4.1-nano`, cheapest acceptable model for a cost-observable `/ask`
contract at this stage — revisit once answer quality becomes load-bearing).

**No override given (the default path)** — a free-tier-first fallback
chain (`provider_chain`) is tried in order before spending against the
paid OpenAI key, which is always the chain's last entry: Groq and Gemini
first (both independently confirmed, from their own docs, to support the
schema-constrained structured output `/ask`/`/summarize`/`/analyze-sentiment`
need), then Mistral/OpenRouter/SambaNova/Cloudflare Workers AI (usable in
`/ask/stream`'s freeform chain now; not yet independently confirmed for
structured output, so excluded from the structured endpoints until they
are). Any entry whose API key isn't set in `.env` is skipped — see
`GET /providers/status` to check what's actually configured, and every
response's `model` field reports which provider/model actually served it
(e.g. `"groq:openai/gpt-oss-20b"` vs. `"openai:gpt-4.1-nano"`).

A free tier is a rate/volume allowance, not a $0 price — `cost_usd` always
reflects that provider's real per-token rate, never a fabricated zero (see
`pricing_config.ProviderConfig`'s docstring for why that distinction
matters). This also addresses Module 1.D2 — provider portability beyond
OpenAI — with a real fallback chain rather than just a design note.

### LAN-local inference (optional)

Point the fallback chain at your own OpenAI-compatible server(s) on the
local network (e.g. llama.cpp) instead of, or alongside, the cloud
providers above. This is **opt-in**: if none of the variables below are
set, LAN-local is skipped entirely — it is never probed at some
conventional default just because the block is empty, since that would
let a server outside this app's knowledge silently win over OpenAI on
every request with no way to tell from the response alone.

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
├── requirements.txt
├── requirements-dev.txt            # Optional: pricing-page screenshot verification (Playwright)
├── scripts/
│   ├── refresh_openai_pricing.py       # Machine-readable price extraction (no browser needed)
│   ├── append_model_pricing.py         # Manual PricingRecord append helper
│   ├── capture_openai_pricing.py       # Rare manual audit screenshots only (needs requirements-dev.txt)
│   └── chromium-deps.sh                # System libs for the above
├── .env.example
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
