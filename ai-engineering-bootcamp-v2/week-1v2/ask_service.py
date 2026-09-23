"""OpenAI request handling for /ask: the completion call and token-usage
extraction. OpenAI SDK exceptions intentionally propagate to main.py's
routing layer, where they're translated into HTTP responses — this module
has no knowledge of HTTP or FastAPI.

Ported 2026-09-13 from ../ai-eng-bootcamp.vera/vera/ask_service.py. Adapted:
that project bundles cost calculation into this layer too, since its /ask
always answers with exactly one call at one fixed, pre-selected model. This
project supports a per-request model override and a validation-retry loop
that accumulates tokens across up to two calls, so cost calculation stays in
main.py (compute_cost_breakdown), where the retry-accumulated totals and the model
actually used are already in scope — folding it in here would mean passing
that state back out anyway.

The force_bad guardrail demo is also adapted, not copied verbatim: the
source project never calls OpenAI for it at all (a synthetic bad payload is
validated directly against the response schema, and the request ends there).
This project's force_bad demonstrates the fuller lesson — the guardrail
catches a bad response *and* the retry recovers to a real answer — so only
the *first*, deliberately-bad attempt is replaced with the source project's
free/deterministic approach (synthetic_malformed_json, below); the retry
still makes a real call via call_structured_model. That keeps the demo
free and reproducible on its failing half without losing the "recovers to a
real answer" half, which the source project's version doesn't demonstrate.
"""
from functools import lru_cache
from typing import Literal

from openai import OpenAI
from pydantic import BaseModel, Field

import providers


class Answer(BaseModel):
    """The model output shape we want every caller to receive.

    used_passage_numbers (added 2026-09-18, Week 2 citation-precision fix):
    only meaningful on the RAG-grounded /ask path, where rag_service's
    GROUNDED_PROMPT numbers each retrieved passage [1]..[N] and asks the
    model which ones it actually drew on. main.py only reads this field when
    grounded_messages is not None -- on every other path (ungrounded /ask,
    /summarize's and /analyze-sentiment's own schemas don't use Answer at
    all, force_bad's synthetic first attempt) there are no numbered passages
    in the prompt for the model to reference, so whatever it returns here is
    simply never consulted. Kept on the one shared Answer model rather than
    a second RAG-only response schema, matching this field's siblings
    (confidence, sources_needed) that are also only fully meaningful in
    some call paths -- splitting the schema per path would mean
    call_structured/call_structured_model no longer sharing one contract,
    a bigger change than this fix calls for. OpenAI's structured-output
    strict mode requires every property regardless of this default, so the
    model always populates it; the default only matters for the
    force_bad/synthetic_malformed_json path below, which pydantic parses
    directly rather than through the API's strict-mode schema.
    """

    answer: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    sources_needed: bool
    used_passage_numbers: list[int] = Field(default_factory=list)


class Summary(BaseModel):
    """Structured output for /summarize."""

    summary: str = Field(min_length=1)
    key_points: list[str] = Field(min_length=1)


class Sentiment(BaseModel):
    """Structured output for /analyze-sentiment."""

    sentiment: Literal["positive", "negative", "neutral"]
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str = Field(min_length=1)


@lru_cache(maxsize=1)
def _get_client() -> OpenAI:
    """Construct the OpenAI client on first use, not at module import time.

    timeout/max_retries match the pattern already proven in
    ../ai-engineering-bootcamp/week1-fastapi-intro/main.py: a slow/hanging
    call fails on a known 20s clock instead of whatever the host happens to
    enforce, and the SDK itself retries transient (5xx/connection) failures
    up to 3 times before that clock runs out — the "timeout" and "retry"
    halves of the homework's "every endpoint: timeout, retry, graceful
    fallback" requirement. The "graceful fallback" half is main.py's job
    (mapping whatever's left over to a clean HTTP response, never a bare
    500).

    Used only for an explicit per-request `model=` override (2026-09-13):
    the no-override default path now goes through providers.py's fallback
    chain instead, which builds its own shorter-timeout/no-retry clients
    per provider (see providers.py's module docstring for why). Deliberately
    left untouched here rather than folded into providers.py, so this
    already-tested, single-call path keeps its own tested config exactly as
    it was."""
    return OpenAI(timeout=20.0, max_retries=3)


def usage_counts(completion) -> tuple[int, int, int]:
    usage = completion.usage
    if usage is None:
        return 0, 0, 0
    return usage.total_tokens, usage.prompt_tokens, usage.completion_tokens


def call_structured(
    explicit_model: str | None, messages: list[dict], response_format: type[BaseModel],
    forced_provider: str | None = None,
) -> tuple[BaseModel, int, int, int, str, str]:
    """General structured-output call, generalized 2026-09-13 out of what was
    call_structured_model's Answer-only body, so /summarize and
    /analyze-sentiment can reuse the same call-and-validate mechanics (and
    the same OpenAI-error propagation contract) as /ask instead of
    duplicating them against a different schema.

    Returns (parsed, total_tokens, prompt_tokens, completion_tokens,
    provider_name, model_name) — extended 2026-09-13 for the multi-provider
    fallback chain. `explicit_model=None` (no per-request override given)
    routes through providers.py's chain — locally-discovered models first
    (see providers.discover_local_models), then the free-tier cloud
    providers, then paid OpenAI last. A caller-supplied `explicit_model`
    routes straight to OpenAI via this module's own client, exactly as
    before the fallback chain existed — that path is untouched, not rewired
    onto providers.py, to avoid re-testing an already-verified single-call
    contract for a cosmetic unification. (Explicitly selecting one specific
    locally-discovered model by name isn't supported yet — the discovered
    set changes at runtime, so it can't be validated against a static
    per-request schema the way OpenAI's fixed model list can; only the
    automatic chain uses discovery today.)

    `forced_provider` (added 2026-09-14, for the Streamlit demo's provider
    selector) is only meaningful on the no-`explicit_model` path — it's
    ignored whenever `explicit_model` is given, since that already forces
    OpenAI specifically via this function's own client; main.py itself
    never sends both (its own validation rejects that combination
    earlier)."""
    if explicit_model is not None:
        completion = _get_client().chat.completions.parse(
            model=explicit_model,
            messages=messages,
            response_format=response_format,
            max_completion_tokens=providers.MAX_COMPLETION_TOKENS,
            # Pinned 2026-09-19 (golden_eval.py remediation, Gap 5) -- was
            # unset (API default 1.0), the confirmed root cause of this
            # session's observed golden_eval.py generation-score flakiness
            # (10/10 -> 9/10 -> 8/10 across identical fresh runs on an
            # unchanged, deterministic retrieved context). Only fixes the
            # explicit-model path (what golden_eval.py always exercises,
            # passing model="gpt-4.1-nano") -- the no-explicit-model
            # fallback chain (providers.call_structured_with_fallback)
            # is untouched, a scoped decision, not an oversight.
            temperature=0,
        )
        parsed = completion.choices[0].message.parsed
        if parsed is None:
            # Also covers a completion truncated by max_completion_tokens
            # before finishing valid JSON — main.py's existing
            # retry-on-ValueError path handles that the same as any other
            # malformed/incomplete output.
            raise ValueError("Model returned no parseable structured output")
        total_tokens, prompt_tokens, completion_tokens = usage_counts(completion)
        return parsed, total_tokens, prompt_tokens, completion_tokens, "openai", explicit_model

    return providers.call_structured_with_fallback(messages, response_format, forced_provider)


# p3m3 item #19, fixed 2026-09-23 -- root cause of the live "not_applicable
# fabrication" gap found 2026-09-22: this path (every case where main.py's
# grounded_messages is None -- the relevance gate judging a question too
# far off-topic, rag_mode="no_rag", or force_bad's retry) sent the bare
# question with zero system framing, no different from asking a
# general-purpose assistant. Nothing told the model it had no verified
# source material, so a question resembling what the corpus *should* cover
# (e.g. a specific company's HR policy) got answered with confident,
# specific, fabricated details instead of an honest "I don't have verified
# information for that." rag_service.GROUNDED_PROMPT already solves this
# for the grounded path; this is the same fix for the ungrounded one.
UNGROUNDED_HONESTY_PROMPT = """{question}

(No retrieved source documents were available for this question -- answer \
from general knowledge only. If the question asks about specific facts, \
figures, policies, or details belonging to a particular named \
organization, product, or document you cannot verify, say so honestly \
rather than inventing plausible-sounding specifics, and set confidence \
low. General knowledge you're genuinely confident is broadly true is fine \
to state as such.)"""


def call_structured_model(
    question: str, explicit_model: str | None, forced_provider: str | None = None
) -> tuple[Answer, int, int, int, str, str]:
    prompt = UNGROUNDED_HONESTY_PROMPT.format(question=question)
    return call_structured(explicit_model, [{"role": "user", "content": prompt}], Answer, forced_provider)


def stream_answer(explicit_model: str | None, messages: list[dict], forced_provider: str | None = None):
    """Returns (lazy_chunk_iterator, provider_name, model_name) for
    /ask/stream.

    The SDK opens the HTTP connection and receives the initial response
    (headers + status) synchronously inside this call — only the response
    *body* (the token chunks) is read lazily as the caller iterates the
    returned object. That means an auth/rate-limit/provider error surfaces
    as an exception raised *here*, before any bytes have been sent to our
    caller, so main.py can still map it to a proper HTTP status code the
    same way it does for /ask. Errors that instead occur mid-stream (a
    connection drop after some tokens have already been sent) can't change
    a status code that's already gone out — that gap is a known, documented
    limitation, not something this function can fix.

    `explicit_model=None` routes through providers.py's fallback chain
    (locally-discovered models first, then free-tier cloud, then paid
    OpenAI last — same before-first-chunk-only fallback boundary applies
    throughout); a caller-supplied `explicit_model` routes straight to
    OpenAI via this module's own client, unchanged. `forced_provider` is
    only meaningful on the no-`explicit_model` path, same as
    call_structured's."""
    if explicit_model is not None:
        stream = _get_client().chat.completions.create(
            model=explicit_model,
            messages=messages,
            stream=True,
            max_completion_tokens=providers.MAX_COMPLETION_TOKENS,
        )
        return stream, "openai", explicit_model

    return providers.stream_with_fallback(messages, forced_provider)


def synthetic_malformed_json() -> str:
    """Deterministic, free stand-in for a malformed model response: no OpenAI
    call, no tokens spent, and no dependence on the model actually complying
    with an instruction to misbehave (the old approach — asking the model to
    set confidence to a string — could in principle just answer correctly
    instead). Produces the same fault shape (confidence as a string, not a
    number) so it fails Answer validation identically every time."""
    return '{"answer": "placeholder", "confidence": "very high", "sources_needed": false}'
