"""Week 1 v2 demo API: one compact `/ask` endpoint for the intro class.

Run:
  uvicorn main:app --host 127.0.0.1 --port 8000 --reload
"""

import logging
import os
import tempfile
import time
from functools import lru_cache
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from openai import AuthenticationError, OpenAIError, RateLimitError
from pydantic import BaseModel, Field, ValidationError
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

import providers
from ask_service import (
    Answer,
    Sentiment,
    Summary,
    call_structured,
    call_structured_model,
    stream_answer,
    synthetic_malformed_json,
)
from openai import OpenAI
from openai_key_check import OPENAI_KEY_ERROR_DETAIL, classify_openai_api_key
from pdf_extract import extract_pdf_text
from pricing_config import (
    latest_pricing_for,
    load_model_pricing,
    load_model_selection,
    load_web_search_pricing,
)
from rag_service import (
    OVERFETCH_K,
    RAG_RELEVANCE_THRESHOLD,
    build_grounded_messages,
    embed_query,
    get_collection,
    query_store,
    select_context_chunks,
    upsert_chunks,
)

THIS_DIR = Path(__file__).resolve().parent
load_dotenv(THIS_DIR / ".env")
load_dotenv(THIS_DIR.parent / ".env")

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

app = FastAPI(title="Week 1 v2 /ask Demo")
from operational_audit import OperationalAuditMiddleware
from operational_store import record_event, put_artifact
app.add_middleware(OperationalAuditMiddleware)

# Caps request *rate*, not just per-request cost — the actual defense against
# something hammering /ask repeatedly (a bozo consumption attack, a stuck
# retry loop, a leaked URL getting scraped). Per-process, in-memory, keyed by
# client IP: no Redis needed for a single-instance Week 1 demo, but also no
# protection shared across multiple worker processes/instances if ever run
# that way.
#
# Three layered windows, all enforced independently (a request is blocked if
# it violates ANY one of them) — a single "N/minute" number can't distinguish
# a legitimate short burst from a sustained hammering that stays just under
# it, so a tighter longer-window cap is what actually bounds that case:
#   - 10/minute  — burst control (a caller reasonably iterating by hand)
#   - 30/hour    — sustained-use control; note this is *tighter* than
#                  10/minute sustained for a full hour (600) would allow, so
#                  it's the one that actually binds if something tries to
#                  stay just under the per-minute cap continuously
#   - 300/day    — hard daily ceiling; also tighter than 30/hour sustained
#                  for 24h (720) would allow, so it's the true cap on total
#                  daily volume from one caller/IP
#
# ORDER MATTERS — list tightest window first. slowapi evaluates the
# semicolon-separated limits left to right and stops at the first one that
# fails, so anything listed after a failing limit is never even checked (or
# incremented) for that request. Verified empirically: with the minute limit
# listed first, a 40-request burst never touches the hourly budget beyond
# the 10 requests actually let through. Reordered to hour-then-minute, the
# same burst exhausts the entire 30/hour budget by request 31 — the hour
# counter kept incrementing on requests that were only ever going to be
# rejected by the (now second) minute check. Reversing this order turns the
# longer windows into nothing more than a slower version of the same minute
# limit, spendable by traffic that was already being blocked.
# None of this replaces the OpenAI account-level spend cap — these bound
# request *count*, that bounds *dollars*, and dollars are what actually
# matters. Sized generously enough for manual testing/grading, not tuned
# against real traffic — adjust via ASK_RATE_LIMIT.
ASK_RATE_LIMIT = "10/minute;30/hour;300/day"
limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """Last-resort safety net, ported 2026-09-13 from
    ../ai-eng-bootcamp.vera/main.py: guarantees no exception — including one
    from code added after this file was last reviewed — ever reaches a
    caller as a bare traceback-revealing 500. This doesn't replace the
    specific except clauses in ask() below; those give a caller a much more
    useful message for the failures we know about (bad key, rate limit,
    provider error) and still fire first. This is only what's left over."""
    logger.exception("Unhandled exception on %s", request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


# SUPPORTED_MODELS/DEFAULT_MODEL are read from config/model-selection.json
# (via ModelSelection.supported_models — added 2026-09-13) rather than
# hardcoded here, so there's one source of truth instead of a Python Literal
# that has to be kept in sync with the JSON by hand — the two briefly
# drifted while switching to gpt-4.1-nano, which is what prompted this fix.
_model_selection = load_model_selection()
if _model_selection is None:
    # Config failed to load/validate — fail loud rather than guess a
    # fallback model list, matching this project's pricing-config philosophy
    # (see pricing_config.py's module docstring: "unknown, not zero").
    raise RuntimeError(
        "config/model-selection.json failed to load or validate — see the "
        "logged exception above for details. /ask cannot start without a "
        "valid supported_models list."
    )
SUPPORTED_MODELS: list[str] = _model_selection.supported_models
ModelName = Literal[tuple(SUPPORTED_MODELS)]
DEFAULT_MODEL: ModelName = _model_selection.selected_model

# Catches a missing/placeholder/malformed OPENAI_API_KEY at startup, before
# the first request — ported 2026-09-13 from
# ../ai-eng-bootcamp.vera/main.py's equivalent startup check. The same
# classification also gates the real-call path inside ask() below, so a bad
# key is caught locally (no network round trip) rather than only surfacing
# as an OpenAI-side 401 after spending a request.
_key_status = classify_openai_api_key(os.getenv("OPENAI_API_KEY"))
if _key_status != "ok":
    logger.warning(
        "OPENAI_API_KEY looks %s at startup — /ask will reject requests "
        "needing a real call until it's fixed in .env", _key_status,
    )

# Warns at startup about any provider_chain credential nearing or past its
# expiry — added 2026-09-14 after a real Groq key was created with a known
# expiry date, which this project previously had no way to surface short of
# waiting for a live 401. See providers.key_expiry_status's docstring for
# the "{api_key_env}_EXPIRES" env var convention this reads.
for _chain_entry in _model_selection.provider_chain:
    _expires, _expiry_state = providers.key_expiry_status(_chain_entry)
    if _expiry_state in ("expiring_soon", "expired"):
        logger.warning(
            "%s's %s %s on %s — update it in .env before it silently starts "
            "failing (or already is).",
            _chain_entry.provider, _chain_entry.api_key_env,
            "expires soon" if _expiry_state == "expiring_soon" else "expired",
            _expires,
        )

# Pricing is loaded from config/model-pricing.json (67 hand-verified,
# provenance-tracked records) via pricing_config.py, ported 2026-09-13 from
# the sibling VERA project (../ai-eng-bootcamp.vera) — replaces what was
# previously a hardcoded MODEL_PRICES_PER_1K dict with no source or date.
# That project found scraping OpenAI's pricing page unreliable (see
# pricing_config.py's module docstring); this loads reviewed, dated data
# instead of re-deriving it at runtime.
_model_pricing = load_model_pricing()

# Web-search-tool pricing (config/web-search-tool-pricing.json) — reference
# data added 2026-09-13, loaded here so compute_web_search_cost() below has
# it available. No endpoint currently calls OpenAI's web_search tool, so
# there's nothing yet for a missing/invalid file to actually break; loaded
# anyway (soft-fails to None) so the calculation function is ready before
# any endpoint depends on it, rather than being wired up under time pressure
# once one does.
_web_search_pricing = load_web_search_pricing()

# Excludes _local_provider_models deliberately: a local model genuinely has
# no committed PricingRecord (see compute_cost_breakdown's local-provider branch
# below) and that's expected, not a startup problem worth warning about.
for _supported_model in _model_selection.supported_models:
    if latest_pricing_for(_supported_model, _model_pricing) is None:
        logger.warning(
            "No usable pricing record for model %s at startup — /ask will "
            "reject requests for this model until config/model-pricing.json "
            "has a record for it", _supported_model,
        )


class AskRequest(BaseModel):
    # max_length bounds the worst-case input-token cost of a single call —
    # ported from ../ai-eng-bootcamp.vera/main.py's AskRequest, which uses
    # the same 4000-char cap. Without it, nothing stops one request (attack
    # or accident — someone pasting a whole document as "question") from
    # costing far more than a typical call.
    question: str = Field(min_length=1, max_length=4000)
    model: ModelName | None = None
    # Forces one specific provider on the no-override chain (e.g. "groq")
    # instead of letting the frontier ranking pick — added 2026-09-14 for
    # the Streamlit demo's provider selector. Validated against
    # GET /providers/status's names and structured-output eligibility by
    # _validate_forced_provider() before any call is attempted. Mutually
    # exclusive with `model` unless provider is "openai" or omitted — an
    # explicit model already means OpenAI, unambiguously.
    provider: str | None = None
    force_bad: bool = False


class AttemptResult(BaseModel):
    attempt: int
    step: str
    ok: bool
    message: str
    raw_output: str | None = None
    validation_error: str | None = None


class AskResponse(BaseModel):
    answer: Answer
    tokens_used: int
    # Split out from tokens_used 2026-09-14 so a caller can show/track
    # input vs. output separately (e.g. the Streamlit demo's cost panels) —
    # tokens_used stays as the pre-existing combined total, unchanged.
    prompt_tokens: int
    completion_tokens: int
    model: str
    latency_ms: int
    # None when this model has no pricing record — a pricing-data gap must
    # not block an otherwise-successful answer (see compute_cost_breakdown).
    cost_usd: float | None
    input_cost_usd: float | None
    output_cost_usd: float | None
    # None unless the serving provider has a free_entitlement_* allowance
    # (see free_tier_note()) — added 2026-09-14 after a real Groq free-tier
    # call showed a nonzero cost_usd with nothing indicating it likely
    # wasn't actually billed money.
    free_tier_note: str | None
    attempts: list[AttemptResult]
    # RAG fields, added 2026-09-17 (Week 2). citations is always [] outside
    # the "supported" case (see ask()'s retrieval-gate logic below) — never
    # omitted, so a caller can rely on the field's presence unconditionally.
    citations: list[str] = []
    status: Literal["supported", "insufficient", "not_applicable"] = "not_applicable"
    # None only when retrieval didn't run at all (force_bad's first attempt)
    # or text-embedding-3-small has no pricing record — see
    # compute_embedding_cost. Not None just because the relevance gate
    # routed to "not_applicable": the question is still embedded either way
    # (see 2.21's always-on-retrieval design), so that cost is real and
    # known even when no grounded answer resulted.
    embedding_cost_usd: float | None = None


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/providers/status")
def providers_status() -> list[dict]:
    """Read-only, unrated-limited (like /health — costs nothing, no OpenAI
    call) view into the fallback chain's current state. Added 2026-09-13
    per explicit review feedback: a skipped provider (missing credential,
    or cooling down after a rate limit) should be visible, not silently
    invisible — otherwise, if every request quietly lands on the paid
    OpenAI fallback, there's no way to tell why the free-first policy isn't
    doing anything without reading server logs. Never returns credential
    values, only whether one is configured."""
    return providers.get_provider_status()


def compute_cost_breakdown(
    model: str, prompt_tokens: int, completion_tokens: int, provider: str | None = None
) -> tuple[float | None, float | None]:
    """Returns (input_cost_usd, output_cost_usd) — split out 2026-09-14 (was
    a single combined compute_cost_usd) so a caller (the Streamlit demo's
    per-roundtrip and running-cost panels) can show/track a real input vs.
    output split, not just the combined total; every call site sums the two
    itself when it needs AskResponse.cost_usd's combined figure.

    (None, None) means "no pricing data for this model" — a data-collection
    gap (config/model-pricing.json is missing a record), not a request
    error. The answer is still good; only the cost readout is unavailable.
    Never guess a price (that's exactly the "plausible-looking wrong number"
    failure mode ../ai-eng-bootcamp.vera/pricing-scraper-design-prv.md was
    written against) and never let a pricing gap turn a successful answer
    into a failed request.

    `provider` (added 2026-09-13 for the multi-provider fallback chain):
    always the *real* canonical price for whichever provider/model actually
    served the request — never a fabricated $0 for a free-tier response.
    See pricing_config.ProviderConfig's docstring for why a free allowance
    is deliberately never represented as a price here.

    A LAN-local inference server is the one deliberate exception, and a
    genuine (0.0, 0.0) rather than (None, None): a locally-hosted model has
    no external API billing relationship at all, unlike a free-tier cloud
    allowance sitting on top of a real per-token price — that's a different
    fact, not the same one collapsed to zero. Checked here directly rather
    than via a committed PricingRecord, since local models are discovered at
    runtime (providers.discover_local_models) and never named in this
    codebase at all. `provider` is "local-inference@host:port" (see
    providers._local_provider_name) once more than one LAN-local server can
    be configured, so this matches on the shared LOCAL_INFERENCE_PROVIDER_NAME
    prefix rather than an exact string."""
    if provider is not None and provider.startswith(f"{providers.LOCAL_INFERENCE_PROVIDER_NAME}@"):
        return 0.0, 0.0
    pricing = latest_pricing_for(model, _model_pricing, provider=provider)
    if pricing is None:
        logger.warning(
            "No pricing record for model %s (provider=%s) — returning "
            "cost_usd=None. Add one via scripts/append_model_pricing.py if "
            "this should be tracked.", model, provider,
        )
        return None, None
    return (
        prompt_tokens / 1_000_000 * pricing.input,
        completion_tokens / 1_000_000 * pricing.output,
    )


def compute_embedding_cost(num_tokens: int) -> float | None:
    """Cost of one `text-embedding-3-small` call (query-side, RAG /ask's
    always-on retrieval step, or POST /ingest's chunk embedding). Same
    never-guess contract as compute_cost_breakdown: None means "no pricing
    record for this model," not $0 — added 2026-09-17 alongside that
    record in config/model-pricing.json. Embeddings have no output tokens,
    so only the record's `input` rate applies."""
    pricing = latest_pricing_for("text-embedding-3-small", _model_pricing, provider="openai")
    if pricing is None:
        logger.warning(
            "No pricing record for text-embedding-3-small — returning "
            "embedding_cost_usd=None. See config/model-pricing.json.",
        )
        return None
    return num_tokens / 1_000_000 * pricing.input


def free_tier_note(provider_name: str | None) -> str | None:
    """None unless `provider_name` is a provider_chain entry carrying a
    free_entitlement_* allowance (e.g. Groq's 1000 requests/day) — added
    2026-09-14 after a real Groq call, served entirely within its free
    daily allowance, returned a nonzero cost_usd with nothing indicating
    that money likely wasn't actually billed to any account.

    This is deliberately NOT a reason to fake cost_usd as $0 for such a
    call — see compute_cost_breakdown's docstring and
    ProviderConfig.free_entitlement_*'s: a free allowance is a separate
    fact from a model's real canonical per-token price, and collapsing
    them would misrepresent the same thing this project has refused to
    misrepresent everywhere else. This is the other half of that honesty:
    telling the caller cost_usd is the call's real per-token *value*, not
    necessarily money actually spent — without claiming to know whether
    *this specific* call fell inside the allowance, since live consumption
    tracking against it is explicitly out of scope for Week 1 (see
    p3m3/week1-module1-checklist.md's Milestone 2 entry).

    Always None for LAN-local (no external quota to be inside/outside of —
    its cost_usd is already exactly $0, unambiguous) and for OpenAI (no
    free_entitlement — the paid path by design)."""
    if provider_name is None:
        return None
    entry = next((p for p in _model_selection.provider_chain if p.provider == provider_name), None)
    if entry is None or entry.free_entitlement_amount is None:
        return None
    return (
        f"{provider_name} grants {entry.free_entitlement_amount:g} "
        f"{entry.free_entitlement_unit}/{entry.free_entitlement_period} free — "
        "cost_usd is this call's real per-token value, not necessarily "
        "money actually billed (live consumption tracking against that "
        "allowance isn't implemented)."
    )


def compute_web_search_cost(
    tool: str, num_calls: int, model: str | None = None, search_content_tokens: int = 0
) -> float | None:
    """Cost of `num_calls` invocations of OpenAI's web-search tool (`tool` is
    one of config/web-search-tool-pricing.json's identifiers, e.g.
    "web_search" or "web_search_preview_non_reasoning") plus, for tools whose
    search-content tokens are billed at model rates rather than free, the
    extra input-token cost `search_content_tokens` represents (for
    gpt-4o-mini/gpt-4.1-mini with the non-preview "web_search" tool, that's a
    fixed 8,000 per call — see that record's `notes`).

    Added 2026-09-13 ahead of any endpoint actually using this tool: the
    calculation itself is written and tested against the real pricing data
    now, so it's ready rather than something to design under time pressure
    once an endpoint needs it. Same fail-open philosophy as
    compute_cost_breakdown: returns None (never a guessed number, never a raised
    exception) on any missing pricing data."""
    records = _web_search_pricing.records if _web_search_pricing else []
    record = next((r for r in records if r.tool == tool), None)
    if record is None:
        logger.warning(
            "No pricing record for web-search tool %s — returning None. See "
            "config/web-search-tool-pricing.json.", tool,
        )
        return None

    call_cost = num_calls * record.price_per_1k_calls / 1000
    if record.search_content_tokens == "free" or search_content_tokens <= 0:
        return call_cost

    if model is None:
        logger.warning(
            "Web-search tool %s bills search-content tokens at model rates, "
            "but no model was given — returning None.", tool,
        )
        return None

    token_pricing = latest_pricing_for(model, _model_pricing)
    if token_pricing is None:
        logger.warning(
            "No pricing record for model %s — can't price web-search "
            "tool %s's search-content tokens. Returning None.", model, tool,
        )
        return None

    return call_cost + (search_content_tokens / 1_000_000 * token_pricing.input)


@lru_cache(maxsize=1)
def _get_embedding_client() -> OpenAI:
    """Separate from ask_service._get_client() (chat completions only,
    module-private) — a short-timeout client for the always-on retrieval
    step in ask() and for POST /ingest's embedding calls. Constructed lazily
    (not at import time) so a missing OPENAI_API_KEY doesn't break app
    startup; the same _require_valid_key() precondition chat calls already
    rely on applies equally here."""
    return OpenAI(timeout=15.0, max_retries=2)


def _require_valid_key() -> None:
    """Catches a missing/placeholder/malformed key locally — no network round
    trip needed to learn OpenAI would reject it. Shared by every endpoint
    that makes a real call.

    503, not 500 (changed 2026-09-13): this is a known, anticipated
    configuration problem, not an unexpected bug — 503 Service Unavailable is
    the standard code for "a dependency/config issue is blocking this
    request," distinct from the bare 500 the global exception handler below
    reserves for genuinely unclassified failures. VERA's equivalent check
    uses 500 here; this is a deliberate improvement on that, not a literal
    port, per the same "avoid halting via 500 wherever possible" reasoning
    that already turned a missing pricing record into cost_usd=None instead
    of a 500 (see compute_cost_breakdown)."""
    key_status = classify_openai_api_key(os.getenv("OPENAI_API_KEY"))
    if key_status != "ok":
        logger.error("Rejected request: OPENAI_API_KEY is %s", key_status)
        raise HTTPException(status_code=503, detail=OPENAI_KEY_ERROR_DETAIL[key_status])


def _validate_forced_provider(provider_name: str | None, require_structured: bool) -> None:
    """Rejects an unknown or (on the structured endpoints) not-yet-verified
    forced `provider=` with a clear 400 *before* any call is attempted —
    added 2026-09-14 for the Streamlit demo's provider selector, so a bad
    provider name fails fast and specifically instead of falling through to
    providers.py's generic "no configured provider" 502 after a wasted
    round trip's worth of internal filtering.

    None (Auto) and "openai" (handled entirely via the pre-existing
    `model=` override path, never through providers.py at all) are both
    no-ops here — `provider="openai"` only makes sense paired with an
    explicit `model=`, and that combination is validated separately at each
    call site (see ask()/ask_stream()), not here."""
    if provider_name is None or provider_name == "openai":
        return
    entries = providers.get_provider_status()
    entry = next((p for p in entries if p["provider"] == provider_name), None)
    if entry is None:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown provider {provider_name!r}. See GET /providers/status for valid names.",
        )
    if require_structured and entry["structured_output"] != "strict":
        raise HTTPException(
            status_code=400,
            detail=(
                f"Provider {provider_name!r} is not confirmed for structured output "
                "(structured_output != 'strict') — use /ask/stream instead, or omit "
                "provider to let Auto pick a verified one."
            ),
        )


def _map_openai_error(exc: Exception) -> HTTPException:
    """Same OpenAI-SDK-exception-to-HTTP-response mapping used in ask()'s
    except clauses, factored out 2026-09-13 so /summarize and
    /analyze-sentiment (added the same day) get identical, tested behavior
    instead of a re-typed copy. ask() itself is left as-is (already verified
    against a live 401) rather than rewired onto this to avoid re-testing a
    working code path for a cosmetic gain."""
    if isinstance(exc, AuthenticationError):
        return HTTPException(
            status_code=401,
            detail=(
                "OpenAI rejected the API key. Check that OPENAI_API_KEY in .env is "
                "current and belongs to an active account."
            ),
        )
    if isinstance(exc, RateLimitError):
        quota_markers = {"insufficient_quota", "credit_balance_exhausted"}
        if getattr(exc, "code", None) in quota_markers or getattr(exc, "type", None) in quota_markers:
            return HTTPException(
                status_code=402,
                detail=(
                    "This OpenAI account has no remaining credit. Add a payment method or "
                    "credits at https://platform.openai.com/settings/organization/billing/ and retry."
                ),
            )
        return HTTPException(
            status_code=429,
            detail="OpenAI rate limit hit (too many requests). Wait a few seconds and retry.",
        )
    return HTTPException(status_code=502, detail=f"OpenAI request failed: {exc}")


def _call_structured_with_guardrail(
    explicit_model: str | None, messages: list[dict], response_format: type[BaseModel]
) -> tuple[BaseModel, int, int, int, str, str]:
    """Runs one structured-output call, retrying once on schema-validation
    failure (same guardrail pattern as /ask's retry loop), and maps OpenAI
    SDK exceptions to HTTP errors via _map_openai_error. Used by /summarize
    and /analyze-sentiment so both get the same "timeout, retry, graceful
    fallback — no 500s" guarantee /ask already has, without each
    reimplementing it.

    Returns (parsed, total_tokens, prompt_tokens, completion_tokens,
    provider_name, model_name) — extended 2026-09-13 for the multi-provider
    fallback chain. The local `_require_valid_key()` pre-check only applies
    when `explicit_model` is given: that path always goes straight to
    OpenAI, where catching a bad key locally (no network round trip) still
    matters. The no-override chain path checks each candidate's own
    credential inside providers.py already, so re-checking OPENAI_API_KEY
    specifically here would incorrectly block a request that a free
    provider could have served without it."""
    last_error: str | None = None
    for _attempt in range(2):
        try:
            if explicit_model is not None:
                _require_valid_key()
            return call_structured(explicit_model, messages, response_format)
        except (ValidationError, ValueError) as exc:
            last_error = str(exc)
            continue
        except (AuthenticationError, RateLimitError, OpenAIError) as exc:
            raise _map_openai_error(exc) from exc

    raise HTTPException(
        status_code=502,
        detail=f"Model response failed schema validation after retry: {last_error}",
    )


class SummarizeRequest(BaseModel):
    # Same 4000-char cost ceiling as AskRequest.question — see that field's
    # comment for why this bound exists at all.
    text: str = Field(min_length=1, max_length=4000)
    model: ModelName | None = None


class SummarizeResponse(BaseModel):
    summary: Summary
    tokens_used: int
    prompt_tokens: int
    completion_tokens: int
    model: str
    cost_usd: float | None
    input_cost_usd: float | None
    output_cost_usd: float | None
    free_tier_note: str | None


class SentimentRequest(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    model: ModelName | None = None


class SentimentResponse(BaseModel):
    sentiment: Sentiment
    tokens_used: int
    prompt_tokens: int
    completion_tokens: int
    model: str
    cost_usd: float | None
    input_cost_usd: float | None
    output_cost_usd: float | None
    free_tier_note: str | None


class AskStreamRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    model: ModelName | None = None
    # Same semantics as AskRequest.provider — see that field's comment.
    # No structured-output eligibility requirement on this endpoint (see
    # _validate_forced_provider's require_structured parameter).
    provider: str | None = None


@app.post("/summarize")
@limiter.limit(ASK_RATE_LIMIT)
def summarize(request: Request, body: SummarizeRequest) -> SummarizeResponse:
    messages = [
        {
            "role": "system",
            "content": "Summarize the given text. Provide a short summary and a list of key points.",
        },
        {"role": "user", "content": body.text},
    ]
    summary, tokens_used, prompt_tokens, completion_tokens, provider, model = _call_structured_with_guardrail(
        body.model, messages, Summary
    )
    input_cost_usd, output_cost_usd = compute_cost_breakdown(
        model, prompt_tokens, completion_tokens, provider=provider
    )
    cost_usd = None if input_cost_usd is None else input_cost_usd + output_cost_usd
    return SummarizeResponse(
        summary=summary,
        tokens_used=tokens_used,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        model=f"{provider}:{model}",
        cost_usd=round(cost_usd, 6) if cost_usd is not None else None,
        input_cost_usd=round(input_cost_usd, 6) if input_cost_usd is not None else None,
        output_cost_usd=round(output_cost_usd, 6) if output_cost_usd is not None else None,
        free_tier_note=free_tier_note(provider),
    )


@app.post("/analyze-sentiment")
@limiter.limit(ASK_RATE_LIMIT)
def analyze_sentiment(request: Request, body: SentimentRequest) -> SentimentResponse:
    messages = [
        {
            "role": "system",
            "content": "Analyze the sentiment of the given text.",
        },
        {"role": "user", "content": body.text},
    ]
    sentiment, tokens_used, prompt_tokens, completion_tokens, provider, model = _call_structured_with_guardrail(
        body.model, messages, Sentiment
    )
    input_cost_usd, output_cost_usd = compute_cost_breakdown(
        model, prompt_tokens, completion_tokens, provider=provider
    )
    cost_usd = None if input_cost_usd is None else input_cost_usd + output_cost_usd
    return SentimentResponse(
        sentiment=sentiment,
        tokens_used=tokens_used,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        model=f"{provider}:{model}",
        cost_usd=round(cost_usd, 6) if cost_usd is not None else None,
        input_cost_usd=round(input_cost_usd, 6) if input_cost_usd is not None else None,
        output_cost_usd=round(output_cost_usd, 6) if output_cost_usd is not None else None,
        free_tier_note=free_tier_note(provider),
    )


@app.post("/ask/stream")
@limiter.limit(ASK_RATE_LIMIT)
def ask_stream(request: Request, body: AskStreamRequest) -> StreamingResponse:
    """Streams the raw model response as plain text chunks (no structured
    output/guardrail retry here — see ask_service.stream_answer's docstring
    for why: a validation retry can't un-send bytes already streamed to the
    caller). The OpenAI call itself is made *before* StreamingResponse is
    constructed, specifically so an auth/rate-limit/provider error still
    comes back as a proper HTTP status instead of a bare or truncated
    stream. With no `model` override, this goes through providers.py's
    fallback chain instead — same before-first-chunk-only fallback
    boundary, see providers.stream_with_fallback's docstring."""
    messages = [{"role": "user", "content": body.question}]
    if body.model is not None:
        _require_valid_key()
        if body.provider not in (None, "openai"):
            raise HTTPException(
                status_code=400,
                detail="model= already implies openai; provider= must be 'openai' or omitted alongside it.",
            )
    _validate_forced_provider(body.provider, require_structured=False)

    try:
        stream, provider, model = stream_answer(body.model, messages, body.provider)
    except (AuthenticationError, RateLimitError, OpenAIError) as exc:
        raise _map_openai_error(exc) from exc

    def generate():
        for chunk in stream:
            delta = chunk.choices[0].delta.content
            if delta:
                yield delta

    # X-Served-By added 2026-09-13: a streamed plain-text response has
    # nowhere else to report which provider/model actually answered — this
    # keeps that visible without changing the response body's shape.
    return StreamingResponse(
        generate(), media_type="text/plain", headers={"X-Served-By": f"{provider}:{model}"}
    )


class IngestRequest(BaseModel):
    # Same cost-ceiling reasoning as AskRequest.question — bounds the
    # worst-case size of a single ad hoc document. Deliberately larger than
    # the 4000-char question/text caps elsewhere: a whole document, not one
    # question, is exactly what this endpoint expects to receive.
    text: str = Field(min_length=1, max_length=200_000)
    document_id: str = Field(min_length=1, max_length=200)
    metadata: dict | None = None
    # Added so ad hoc live ingests aren't a schema gap against the bulk-
    # loaded baseline corpus, whose documents carry a real provenance
    # record (source, source_url, provenance_type, fetched_at — see
    # ingestion_quarantine/new_docs_provenance.json) via a one-time
    # migration path this endpoint never went through. Optional: omitting
    # it just means an empty provenance record, same as before this field
    # existed, not an error.
    provenance: dict | None = None


class IngestResponse(BaseModel):
    document_id: str
    chunks_indexed: int
    status: Literal["indexed", "empty"]


@app.post("/ingest")
@limiter.limit(ASK_RATE_LIMIT)
def ingest(request: Request, body: IngestRequest) -> IngestResponse:
    """Live, persistent document ingestion — distinct from rag_ingest.py's
    one-off baseline-corpus script (see that module's docstring and
    p3m3/week2-priority-checklist.md's "POST /ingest" section for why the
    two aren't the same thing). Writes into the exact same chroma_store/
    collection the 50-doc baseline corpus lives in, via
    rag_service.upsert_chunks — a re-ingested document_id overwrites its
    previous chunks (Chroma upsert semantics), it doesn't duplicate them."""
    if not body.text.strip():
        raise HTTPException(status_code=400, detail="text must not be empty or whitespace-only")
    _require_valid_key()
    try:
        chunks_indexed = upsert_chunks(
            _get_embedding_client(), get_collection(), body.document_id, body.text, body.metadata, provenance=body.provenance
        )
    except (AuthenticationError, RateLimitError, OpenAIError) as exc:
        raise _map_openai_error(exc) from exc
    return IngestResponse(
        document_id=body.document_id,
        chunks_indexed=chunks_indexed,
        status="indexed" if chunks_indexed > 0 else "empty",
    )


class IngestBatchRequest(BaseModel):
    # Capped at 10 documents/request (N.9) -- each document already bounds
    # its own worst-case size (IngestRequest.text's 200_000-char cap), so
    # this bounds worst-case embedding-call *count* per request the same
    # way the per-field caps bound size, rather than leaving batch size
    # unbounded.
    documents: list[IngestRequest] = Field(min_length=1, max_length=10)


class IngestBatchResponse(BaseModel):
    results: list[IngestResponse]


@app.post("/ingest/batch")
@limiter.limit(ASK_RATE_LIMIT)
def ingest_batch(request: Request, body: IngestBatchRequest) -> IngestBatchResponse:
    """Batch form of POST /ingest (N.9) -- same upsert_chunks path, one call
    per document, but validated and billed as one request instead of N
    separate ones. All documents are validated non-empty up front (fail
    before any embedding spend, not partway through the batch) but each
    document is still upserted independently -- one document's chunk count
    or metadata never affects another's, and a later document's failure
    doesn't roll back an earlier one's already-completed upsert."""
    for doc in body.documents:
        if not doc.text.strip():
            raise HTTPException(
                status_code=400,
                detail=f"text must not be empty or whitespace-only (document_id={doc.document_id!r})",
            )
    _require_valid_key()
    client = _get_embedding_client()
    collection = get_collection()
    results: list[IngestResponse] = []
    for doc in body.documents:
        try:
            chunks_indexed = upsert_chunks(client, collection, doc.document_id, doc.text, doc.metadata, provenance=doc.provenance)
        except (AuthenticationError, RateLimitError, OpenAIError) as exc:
            raise _map_openai_error(exc) from exc
        results.append(
            IngestResponse(
                document_id=doc.document_id,
                chunks_indexed=chunks_indexed,
                status="indexed" if chunks_indexed > 0 else "empty",
            )
        )
    return IngestBatchResponse(results=results)


# Generous vs. the baseline corpus's 2.5MB/file cap (ingestion_quarantine/
# README.md) -- that cap is tuned for chroma_store/'s committed-to-git size
# at 50-document scale; this is one ad hoc live upload, not an addition to
# the committed baseline corpus, so a looser bound is fine. Still bounded,
# not unbounded, per this project's standing input-cap pattern.
MAX_PDF_UPLOAD_BYTES = 10 * 1024 * 1024


@app.post("/ingest-pdf")
@limiter.limit(ASK_RATE_LIMIT)
async def ingest_pdf(
    request: Request, file: UploadFile = File(...), document_id: str | None = Form(None)
) -> IngestResponse:
    """Dedicated PDF-upload ingestion path (N.7, stretch item) — distinct
    from POST /ingest (raw text body) and rag_ingest.py's one-off
    baseline-corpus script. Extracts text via pdf_extract.extract_pdf_text
    (the same references/bibliography-truncation logic the baseline corpus
    uses, see 12.1.1) then reuses upsert_chunks, so a live-uploaded PDF gets
    identical treatment to a corpus document rather than a second code path
    with its own quirks. document_id defaults to the uploaded filename
    (stem) when not given explicitly."""
    if not (file.filename or "").lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="file must be a .pdf")
    _require_valid_key()
    contents = await file.read()
    if not contents:
        raise HTTPException(status_code=400, detail="uploaded file is empty")
    if len(contents) > MAX_PDF_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413, detail=f"file exceeds {MAX_PDF_UPLOAD_BYTES // (1024 * 1024)}MB limit"
        )
    resolved_document_id = document_id or Path(file.filename).stem
    with tempfile.NamedTemporaryFile(suffix=".pdf") as tmp:
        tmp.write(contents)
        tmp.flush()
        try:
            text = extract_pdf_text(tmp.name)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"could not extract text from PDF: {exc}") from exc
    if not text.strip():
        raise HTTPException(status_code=400, detail="no extractable text found in PDF")
    try:
        chunks_indexed = upsert_chunks(_get_embedding_client(), get_collection(), resolved_document_id, text)
        import hashlib
        put_artifact("uploads/" + hashlib.sha256(contents).hexdigest() + ".pdf", contents)
    except (AuthenticationError, RateLimitError, OpenAIError) as exc:
        raise _map_openai_error(exc) from exc
    return IngestResponse(
        document_id=resolved_document_id,
        chunks_indexed=chunks_indexed,
        status="indexed" if chunks_indexed > 0 else "empty",
    )


class RetrieveResult(BaseModel):
    chunk_id: str
    document_id: str
    text: str
    distance: float


@app.get("/debug/retrieve")
def debug_retrieve(query: str, top_k: int = 5, document_id: str | None = None) -> list[RetrieveResult]:
    """Read-only introspection into the vector store, no LLM call involved —
    lets a caller (or the grader) see exactly what /ask's retrieval step
    would find for a given query, independent of generation. Not
    rate-limited like /ask (no OpenAI chat cost here — just one embedding
    call plus a local Chroma query) but still costs one real embedding
    call, unlike GET /health or GET /providers/status.

    document_id, when given, narrows the search to that one document's
    chunks via Chroma's own metadata filter (N.8) — e.g. to check how a
    specific document ranks its own passages for a query, or to confirm a
    freshly-/ingest-ed document is actually searchable. Omitted, retrieval
    is corpus-wide as before."""
    if not query.strip():
        raise HTTPException(status_code=400, detail="query must not be empty or whitespace-only")
    _require_valid_key()
    try:
        query_embedding, _tokens = embed_query(_get_embedding_client(), query)
    except (AuthenticationError, RateLimitError, OpenAIError) as exc:
        raise _map_openai_error(exc) from exc
    where = {"document_id": document_id} if document_id else None
    retrieved = query_store(get_collection(), query_embedding, top_k=top_k, where=where)
    return [
        RetrieveResult(
            chunk_id=chunk_id,
            document_id=meta["document_id"],
            text=text,
            distance=distance,
        )
        for chunk_id, text, meta, distance in zip(
            retrieved["ids"], retrieved["documents"], retrieved["metadatas"], retrieved["distances"]
        )
    ]


@app.post("/ask")
@limiter.limit(ASK_RATE_LIMIT)
def ask(request: Request, body: AskRequest) -> AskResponse:
    last_error: str | None = None
    attempts: list[AttemptResult] = []
    total_tokens_used = 0
    total_prompt_tokens = 0
    total_completion_tokens = 0
    # Display fallback for the force_bad synthetic-success edge case, where
    # no real provider call is ever made — no provider/model to attribute
    # a served response to in that path.
    served_provider: str | None = None
    served_model = body.model or DEFAULT_MODEL
    if body.model is not None and body.provider not in (None, "openai"):
        raise HTTPException(
            status_code=400,
            detail="model= already implies openai; provider= must be 'openai' or omitted alongside it.",
        )
    _validate_forced_provider(body.provider, require_structured=True)

    # RAG retrieval — always-on (2.21's design: no client-facing flag),
    # except for force_bad's deterministic guardrail demo, which is
    # orthogonal to this feature and stays exactly as it was. Runs once,
    # before the retry loop below, since it's deterministic and doesn't
    # need that loop's validation-retry semantics.
    grounded_messages: list[dict] | None = None
    retrieved_ids: list[str] = []
    embedding_cost_usd: float | None = None
    if not body.force_bad:
        try:
            query_embedding, embed_tokens = embed_query(_get_embedding_client(), body.question)
            embedding_cost_usd = compute_embedding_cost(embed_tokens)
            # Overfetch beyond what actually reaches the generator (see
            # rag_service.OVERFETCH_K's docstring) so select_context_chunks
            # has real alternatives to backfill from when applying its
            # per-document diversity cap — retrieving and grounding-on the
            # same fixed top-5 would leave nothing to substitute in.
            candidates = query_store(get_collection(), query_embedding, top_k=OVERFETCH_K)
            if candidates["distances"] and candidates["distances"][0] <= RAG_RELEVANCE_THRESHOLD:
                retrieved = select_context_chunks(candidates)
                retrieved_ids = retrieved["ids"]
                grounded_messages = build_grounded_messages(body.question, retrieved)
            record_event("retrieval", {
                "request_id": getattr(request.state, "operational_request_id", None),
                "question": body.question, "candidate_ids": candidates["ids"],
                "distances": candidates["distances"], "selected_ids": retrieved_ids,
                "collection_revision": get_collection().revision(),
                "embedding_tokens": embed_tokens, "embedding_cost_usd": embedding_cost_usd,
            })
        except Exception as exc:
            record_event("retrieval_error", {"request_id": getattr(request.state, "operational_request_id", None), "error_type": type(exc).__name__})
            # Retrieval must never be the reason /ask fails outright — a
            # vector-store or embedding-call problem degrades to a direct,
            # non-RAG answer (status stays "not_applicable"), same
            # never-500-if-avoidable philosophy as compute_cost_breakdown's
            # pricing-gap handling.
            logger.exception("RAG retrieval failed — falling back to a direct, non-grounded answer")

    start = time.perf_counter()

    for attempt in range(2):
        try:
            if body.force_bad and attempt == 0:
                # Free, deterministic guardrail demo — no OpenAI call, no
                # tokens spent (see ask_service.synthetic_malformed_json).
                raw = synthetic_malformed_json()

                try:
                    answer = Answer.model_validate_json(raw)
                except ValidationError as exc:
                    last_error = str(exc)
                    attempts.append(
                        AttemptResult(
                            attempt=attempt + 1,
                            step="forced_bad_json",
                            ok=False,
                            message="Validation failed, so the endpoint retries with structured output.",
                            raw_output=raw,
                            validation_error=str(exc),
                        )
                    )
                    continue

                attempts.append(
                    AttemptResult(
                        attempt=attempt + 1,
                        step="forced_bad_json",
                        ok=True,
                        message="Unexpectedly passed validation.",
                        raw_output=raw,
                    )
                )
            else:
                if body.model is not None:
                    _require_valid_key()

                if grounded_messages is not None:
                    answer, tokens_used, prompt_tokens, completion_tokens, served_provider, served_model = (
                        call_structured(body.model, grounded_messages, Answer, body.provider)
                    )
                else:
                    answer, tokens_used, prompt_tokens, completion_tokens, served_provider, served_model = (
                        call_structured_model(body.question, body.model, body.provider)
                    )
                total_tokens_used += tokens_used
                total_prompt_tokens += prompt_tokens
                total_completion_tokens += completion_tokens
                attempts.append(
                    AttemptResult(
                        attempt=attempt + 1,
                        step="structured_output",
                        ok=True,
                        message="Structured output matched the Answer schema.",
                    )
                )

            latency_ms = int((time.perf_counter() - start) * 1000)
            input_cost_usd, output_cost_usd = compute_cost_breakdown(
                served_model, total_prompt_tokens, total_completion_tokens, provider=served_provider
            )
            cost_usd = (
                None
                if input_cost_usd is None
                else input_cost_usd + output_cost_usd + (embedding_cost_usd or 0.0)
            )
            # grounded_messages is None whenever retrieval didn't run
            # (force_bad) or the relevance gate judged the question
            # off-topic — both cases stay "not_applicable". Otherwise the
            # gate judged it topically relevant, and GROUNDED_PROMPT's own
            # refusal instruction (via the existing Answer.sources_needed
            # field, not a new schema field) decides supported vs
            # insufficient — see rag_service.GROUNDED_PROMPT.
            if grounded_messages is None:
                rag_status: Literal["supported", "insufficient", "not_applicable"] = "not_applicable"
                citations: list[str] = []
            elif answer.sources_needed:
                rag_status = "insufficient"
                citations = []
            else:
                rag_status = "supported"
                # Citation precision fix, 2026-09-18: used_passage_numbers
                # is the model's own claim about which numbered passages
                # (see build_grounded_messages) its answer actually drew
                # on, mapped back to real chunk ids by 1-based position.
                # Replaces the previous "cite everything retrieved"
                # behavior, which over-attributed — a supported answer
                # that only used 2 of 5 retrieved chunks was citing all 5.
                # Deduped/sorted since the model isn't asked to dedup or
                # order them itself, and out-of-range numbers (a
                # hallucinated passage index) are silently dropped rather
                # than raising, matching this project's fail-open stance on
                # imperfect model output elsewhere in this function.
                cited = sorted(
                    {n for n in answer.used_passage_numbers if 1 <= n <= len(retrieved_ids)}
                )
                citations = [retrieved_ids[n - 1] for n in cited]
                if not citations:
                    # The model marked the answer supported but didn't
                    # name any passage it used (empty/all-invalid
                    # used_passage_numbers) -- an instruction-following
                    # miss, not evidence the answer is actually
                    # unsupported (sources_needed already covers that
                    # case). Falling back to the full retrieved set here
                    # keeps a "supported" answer from citing nothing,
                    # rather than silently under-citing a real grounded
                    # answer because of one imperfectly-followed field.
                    citations = retrieved_ids
            return AskResponse(
                answer=answer,
                tokens_used=total_tokens_used,
                prompt_tokens=total_prompt_tokens,
                completion_tokens=total_completion_tokens,
                model=f"{served_provider}:{served_model}" if served_provider else served_model,
                latency_ms=latency_ms,
                cost_usd=round(cost_usd, 6) if cost_usd is not None else None,
                input_cost_usd=round(input_cost_usd, 6) if input_cost_usd is not None else None,
                output_cost_usd=round(output_cost_usd, 6) if output_cost_usd is not None else None,
                free_tier_note=free_tier_note(served_provider),
                attempts=attempts,
                citations=citations,
                status=rag_status,
                embedding_cost_usd=round(embedding_cost_usd, 6) if embedding_cost_usd is not None else None,
            )
        except (ValidationError, ValueError) as exc:
            last_error = str(exc)
            attempts.append(
                AttemptResult(
                    attempt=attempt + 1,
                    step="structured_output",
                    ok=False,
                    message="Structured output failed validation.",
                    validation_error=str(exc),
                )
            )
        except AuthenticationError as exc:
            raise HTTPException(
                status_code=401,
                detail=(
                    "OpenAI rejected the API key. Check that OPENAI_API_KEY in .env is "
                    "current and belongs to an active account."
                ),
            ) from exc
        except RateLimitError as exc:
            quota_markers = {"insufficient_quota", "credit_balance_exhausted"}
            if getattr(exc, "code", None) in quota_markers or getattr(exc, "type", None) in quota_markers:
                raise HTTPException(
                    status_code=402,
                    detail=(
                        "This OpenAI account has no remaining credit. Add a payment method or "
                        "credits at https://platform.openai.com/settings/organization/billing/ and retry."
                    ),
                ) from exc
            raise HTTPException(
                status_code=429,
                detail="OpenAI rate limit hit (too many requests). Wait a few seconds and retry.",
            ) from exc
        except OpenAIError as exc:
            raise HTTPException(status_code=502, detail=f"OpenAI request failed: {exc}") from exc

    raise HTTPException(
        status_code=502,
        detail=f"Model response failed schema validation after retry: {last_error}",
    )
