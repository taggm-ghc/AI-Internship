"""Which backend actually serves an /ask-family call: loads the configured
provider chain (config/model-selection.json's `provider_chain`, via
pricing_config.ProviderConfig), builds a client per provider, and runs the
free-tier-first fallback trial. Owns all client construction — including the
OpenAI terminal entry — so nothing here imports from ask_service.py; the
dependency direction is main.py -> ask_service.py -> providers.py -> SDK,
never the reverse.

Added 2026-09-13. Design corrected mid-review (see
p3m3/week1-module1-checklist.md for the full account) after an external
review of the first draft caught several real bugs before any code was
written:
  - a free-tier allowance is not the same fact as a $0 canonical price
    (see pricing_config.ProviderConfig's docstring) — this module never
    invents pricing, it only decides which provider/model serves a call;
  - not every provider error means "try the next provider" — a malformed
    request (400-class) is either our own bug or a capability mismatch that
    should have been filtered out before the attempt, not silently papered
    over by burning through providers until the paid one accepts it;
  - a streaming fallback is only safe *before* the first chunk reaches the
    caller — once real bytes are out, a different provider can't take over
    a response the client has already started receiving;
  - retrying a provider that just told us it's rate-limited, on every
    subsequent request, wastes latency for no benefit.
"""
import json
import logging
import os
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

from openai import (
    APIConnectionError,
    APITimeoutError,
    AuthenticationError,
    InternalServerError,
    OpenAI,
    OpenAIError,
    RateLimitError,
)
from pydantic import BaseModel

from pricing_config import ProviderConfig, latest_pricing_for, load_model_pricing, load_model_selection

logger = logging.getLogger(__name__)

# LAN-local inference server(s) (e.g. llama.cpp/opencode-style OpenAI-
# compatible servers reachable on the local network) — added 2026-09-13,
# extended 2026-09-14 to allow more than one such server (e.g. two
# different LAN machines each hosting their own local model) rather than
# a single fixed host:port. "Local" here means LAN-local vs. a hyperscaler
# cloud API, not necessarily the same machine this process runs on.
#
# Configuration is LOCAL_INFERENCE_SERVERS, a comma-separated list of
# host:port pairs (e.g. "127.0.0.1:8010,192.168.1.42:8011"). If that var is
# unset, falls back to the single-server pair LOCAL_INFERENCE_HOST /
# LOCAL_INFERENCE_PORT — but only if at least one of those two is actually
# set. This is deliberately opt-in, not opinionated-default: no
# LOCAL_INFERENCE_* var set at all means LAN-local is skipped entirely
# (local_inference_servers() returns []), not silently probed at some
# conventional host:port. That matters because a real LAN-local server
# happening to be reachable at a common default (e.g. 127.0.0.1:8010) would
# otherwise win the frontier ranking over every cloud provider — including
# OpenAI — on every plain /ask call, with no way to tell from the request
# itself that this had happened (see p3m3/week1-module1-checklist.md,
# 2026-09-14 entry, for the live instance of this that motivated the fix).
# 127.0.0.1 / 8010 remain the fallback values for whichever of the pair is
# left unset once the other is, matching the existing "host with no :port
# reuses LOCAL_INFERENCE_PORT" convention for the multi-server form below.
#
# Host/port are plain env vars, not a config file: unlike the cloud
# provider_chain (committed, provenance-tracked data — this repo is a
# graded, portfolio-facing artifact), a LAN server's connection details
# have no business being either hardcoded or committed, and its *models*
# are discovered live from its own OpenAI-compatible /models endpoint
# (client.models.list()) rather than named anywhere in this codebase at
# all — so no model name, however it's branded upstream, ever needs to
# appear in code, config, or git history here. A server that isn't
# reachable (the common case anywhere off that LAN, including any deployed
# instance) simply contributes no candidates; nothing else needs to know
# or care that it exists.
LOCAL_INFERENCE_PROVIDER_NAME = "local-inference"
# Most LAN-local OpenAI-compatible servers (llama.cpp included) don't
# actually validate this key — setdefault so _is_configured()'s ordinary
# "is there a non-empty credential" check passes without requiring the user
# to set anything, while still respecting a real override if they do set
# one. Shared across every configured LAN-local server (a scope choice, not
# an oversight): if a specific server ever genuinely needs its own distinct
# key, that's an additive extension — a per-server api_key_env — not a
# redesign of this default.
os.environ.setdefault("LOCAL_INFERENCE_API_KEY", "local-inference")
# Short and no retries: this is a liveness probe for a server that may
# simply not be reachable right now, not a real request — a slow timeout
# here would make every single default-path request pay that cost whenever
# a LAN server is down or unreachable, which is the common case off that
# particular network. With multiple servers configured, this cost is paid
# once per unreachable server, probed sequentially (see _local_provider_
# entries) — an acceptable latency for an opt-in LAN scale-out; revisit
# (e.g. concurrent probes) only if the configured server count grows enough
# for that to matter in practice.
LOCAL_DISCOVERY_TIMEOUT_SECONDS = 2.0


# Concrete walkthrough of the multi-server config format and how it's used,
# in one place (the per-function docstrings below cover each step's own
# rationale but don't spell out the end-to-end flow):
#
# Recording (.env): a single comma-separated variable, LOCAL_INFERENCE_SERVERS,
# one host:port pair per server:
#
#   LOCAL_INFERENCE_SERVERS=127.0.0.1:8010,192.168.1.42:8011
#
# This takes priority over the single-server LOCAL_INFERENCE_HOST/
# LOCAL_INFERENCE_PORT pair whenever it's set. A bare host with no :port
# (e.g. 192.168.1.42) reuses LOCAL_INFERENCE_PORT, or 8010 if that's also
# unset. If none of LOCAL_INFERENCE_SERVERS/HOST/PORT are set, LAN-local is
# skipped entirely (see the opt-in rationale above).
#
# Usage:
#   1. local_inference_servers() splits that string on commas, then each
#      entry on its first ":" (partition(":")) into (host, port) — a plain
#      list of tuples, e.g. [("127.0.0.1", "8010"), ("192.168.1.42", "8011")].
#   2. _local_provider_entries() loops over that list. For each pair it
#      builds a distinct provider identity — _local_provider_name(host, port)
#      -> "local-inference@127.0.0.1:8010", "local-inference@192.168.1.42:8011"
#      — and a matching base URL (_local_base_url), then calls
#      discover_local_models(base_url, provider_name) against that specific
#      server's own /v1/models endpoint.
#   3. Every model that server currently reports (after the
#      _EXCLUDED_NAME_SUBSTRINGS filter) becomes its own ProviderConfig entry
#      tagged with that server's distinct provider name.
#   4. Those entries feed into
#      _rank_by_frontier(_local_provider_entries() + load_provider_chain())
#      alongside the cloud providers — so with two LAN servers configured,
#      both contribute separate $0 candidates, and the frontier sort picks
#      among all of them (plus cloud) by the same rule.
#
# The distinct per-server name (host:port suffix, not a shared
# "local-inference" string) exists specifically to keep them independent
# downstream: cooldown state (_unavailable_until), the availability/
# retirement log, and /providers/status all key off provider.provider, so
# one server being unreachable never shadows or gets confused with another
# that's fine.
def local_inference_servers() -> list[tuple[str, str]]:
    """Parses LOCAL_INFERENCE_SERVERS ("host:port,host:port,...") into a
    list of (host, port) pairs, one per configured LAN-local server. Falls
    back to the single LOCAL_INFERENCE_HOST/LOCAL_INFERENCE_PORT pair when
    that var is unset and at least one of the pair is itself set, so an
    existing single-server .env keeps working unchanged. If nothing
    LOCAL_INFERENCE_* is configured at all, returns [] — LAN-local is
    opt-in, never probed at a conventional default just because no one
    said otherwise (see the module-level comment above this pair's
    definition for why that distinction matters). A bare "host" entry with
    no ":port" reuses LOCAL_INFERENCE_PORT (or its own 8010 fallback).

    Reads os.environ fresh on every call rather than caching into
    module-level constants at import time — main.py imports this module
    before calling load_dotenv() (so a value from .env, as opposed to one
    already in the shell's real environment, would not exist yet at
    providers.py's own import time), and freezing these at import time
    silently meant LAN-local could never actually activate through the
    real app at all, no matter what .env said. Caught 2026-09-14 by seeing
    zero LAN-local activity in the server log across several /ask and
    /ask/stream calls where it should have won the frontier ranking."""
    local_host = os.getenv("LOCAL_INFERENCE_HOST")
    local_port = os.getenv("LOCAL_INFERENCE_PORT")
    raw = os.getenv("LOCAL_INFERENCE_SERVERS", "").strip()
    if raw:
        servers = []
        for entry in raw.split(","):
            entry = entry.strip()
            if not entry:
                continue
            host, _, port = entry.partition(":")
            servers.append((host, port or local_port or "8010"))
        return servers
    if local_host or local_port:
        return [(local_host or "127.0.0.1", local_port or "8010")]
    return []


def _local_provider_name(host: str, port: str) -> str:
    """A distinct provider id per LAN-local server (e.g.
    "local-inference@192.168.1.42:8011") — needed once more than one such
    server can be configured, so per-server cooldown state
    (_unavailable_until), the availability log, and /providers/status each
    correctly distinguish "this server is unreachable" from "that other one
    is fine." Carries only host:port, never a model name — LAN connection
    detail, not the thing this project has specifically avoided committing
    or logging."""
    return f"{LOCAL_INFERENCE_PROVIDER_NAME}@{host}:{port}"


def _local_base_url(host: str, port: str) -> str:
    return f"http://{host}:{port}/v1"


def _is_local(provider: ProviderConfig) -> bool:
    """True for any LAN-local server's entry, regardless of which
    host:port it's actually running at — provider.provider is now
    "local-inference@host:port" (see _local_provider_name), not the bare
    LOCAL_INFERENCE_PROVIDER_NAME constant, once more than one server can
    be configured."""
    return provider.provider.startswith(f"{LOCAL_INFERENCE_PROVIDER_NAME}@")

# Append-only, timestamped log of "this provider/model was actually reached"
# observations — added 2026-09-13, per explicit direction to flag a model
# unseen for 180+ days as likely retired/deprecated. Deliberately a
# gitignored dotfile at the project root (matching .faststream-local-url's
# convention), not anywhere under config/: this file WILL contain LAN-local
# model names once they're actually used, and config/ is committed,
# provenance-tracked data meant for this repo's portfolio-facing history —
# the whole reason discovery replaced a static file for local models in the
# first place. Only positive ("seen") observations are ever recorded; a
# model's absence from recent observations *is* the staleness signal (see
# is_retired) — there's no need to separately log every failed attempt to
# know something hasn't been seen in a while.
AVAILABILITY_LOG_PATH = Path(__file__).resolve().parent / ".model-availability.json"
RETIREMENT_THRESHOLD_DAYS = 180


def _load_availability_log() -> list[dict]:
    if not AVAILABILITY_LOG_PATH.exists():
        return []
    try:
        return json.loads(AVAILABILITY_LOG_PATH.read_text())
    except Exception:
        logger.exception("Failed to load or parse %s — treating as empty.", AVAILABILITY_LOG_PATH)
        return []


def record_seen(provider: str, model: str) -> None:
    """Appends a timestamped observation that `provider`/`model` was just
    successfully reached. Called from the trial loops on a successful call
    and from discover_local_models() for each model a LAN-local server
    currently reports — never on a failure, see module note above."""
    log = _load_availability_log()
    log.append({"provider": provider, "model": model, "observed_at": datetime.now(timezone.utc).isoformat()})
    AVAILABILITY_LOG_PATH.write_text(json.dumps(log, indent=2) + "\n")


def is_retired(provider: str, model: str, threshold_days: int = RETIREMENT_THRESHOLD_DAYS) -> bool:
    """True only for a provider/model with *some* observation history whose
    most recent "seen" record is older than `threshold_days` — a model
    never observed at all is new/unknown, not retired (there's nothing
    stale about a model this project simply hasn't tried yet). In practice
    this stays False for everything for a long time: the log starts empty
    today, so nothing can be 180 days stale until 180 real days of
    observations have actually accumulated — this is the correctly-inert
    state for brand-new data, not a bug."""
    observations = [
        o for o in _load_availability_log() if o["provider"] == provider and o["model"] == model
    ]
    if not observations:
        return False
    last_seen = max(datetime.fromisoformat(o["observed_at"]) for o in observations)
    return (datetime.now(timezone.utc) - last_seen) > timedelta(days=threshold_days)


# No SDK-internal retries and a short timeout: the trial loop *across*
# providers is the retry mechanism now. Letting each individual client also
# retry internally would compound latency (N providers x internal retries x
# backoff) instead of bounding it (N providers x one short timeout each).
PROVIDER_TIMEOUT_SECONDS = 8.0
PROVIDER_MAX_RETRIES = 0

# Bounds worst-case *output*-token cost per call, across every provider in
# the chain — moved here 2026-09-13 from ask_service.py when this module
# took over making the actual completion calls (see the cost audit note in
# ask_service.py for why this cap exists at all: max_length already bounds
# input tokens, but nothing capped output before this).
MAX_COMPLETION_TOKENS = 1000

# How long to skip a provider after it reports a rate limit/quota problem,
# when the response doesn't give us a more precise Retry-After to use
# instead. Not a real circuit breaker — just enough to stop re-probing
# something already known to be exhausted on every single request.
DEFAULT_COOLDOWN_SECONDS = 60.0

# Exception types that mean "this provider isn't available right now, try
# the next one" — auth failure, rate limit, connection/timeout, or a
# provider-side (5xx) problem. Deliberately does NOT include the SDK's
# BadRequestError/UnprocessableEntityError: those mean our request itself
# was malformed or asked for something this provider/model can't do, which
# is either an application bug or a capability mismatch that should have
# been filtered out by ProviderConfig.structured_output before the attempt
# — not something to hide by trying five more providers and quietly
# spending the paid key to mask it.
_RETRYABLE_EXCEPTIONS = (AuthenticationError, RateLimitError, APIConnectionError, APITimeoutError, InternalServerError)

_unavailable_until: dict[str, datetime] = {}

# How many days out an unexpired key counts as "expiring_soon" rather than
# plain "ok" — see key_expiry_status().
KEY_EXPIRY_WARNING_DAYS = 14


def key_expiry_status(provider: ProviderConfig) -> tuple[str | None, str]:
    """Reads an optional "{api_key_env}_EXPIRES" companion env var (e.g.
    GROQ_API_KEY_EXPIRES=2026-12-13, an ISO 8601 date) recording when that
    *specific* credential expires — added 2026-09-14 after a real Groq key
    was created with a Dec 13 2026 expiry, a fact this project previously
    had no way to track at all (an expired key just fails as an ordinary
    AuthenticationError, indistinguishable from any other bad-key case,
    until this function is checked ahead of time).

    Deliberately a companion env var, not a provider_chain field: expiry is
    a fact about one specific secret value someone generated, not about
    the provider/model itself — it belongs alongside the credential in
    .env (gitignored, per-environment, per-user), never in the committed
    config/model-selection.json, which every environment shares.

    Returns (expiry_date_iso_or_None, status): status is "unknown" (no
    _EXPIRES var set — most providers here don't even expose an expiring
    key, and that's the common, unremarkable case, not a problem to flag),
    "ok", "expiring_soon" (within KEY_EXPIRY_WARNING_DAYS), or "expired"."""
    raw = os.getenv(f"{provider.api_key_env}_EXPIRES")
    if not raw:
        return None, "unknown"
    try:
        expiry = date.fromisoformat(raw)
    except ValueError:
        logger.warning(
            "%s_EXPIRES=%r is not a valid ISO date (YYYY-MM-DD) — ignoring.",
            provider.api_key_env, raw,
        )
        return None, "unknown"
    today = date.today()
    if expiry < today:
        return expiry.isoformat(), "expired"
    if (expiry - today).days <= KEY_EXPIRY_WARNING_DAYS:
        return expiry.isoformat(), "expiring_soon"
    return expiry.isoformat(), "ok"


def _is_configured(provider: ProviderConfig) -> bool:
    """Deliberately just "is a non-empty value present," uniform across
    every provider including OpenAI — considered and rejected using
    openai_key_check.classify_openai_api_key to fast-reject an
    obviously-malformed OpenAI key locally (the way _require_valid_key()
    used to, unconditionally, before that check was scoped to only the
    explicit-override path). That would save a network round trip in the
    common case, but it also means a malformed OpenAI key — if it's the
    chain's only candidate — gets silently excluded as "unconfigured,"
    surfacing as a vague "no provider available" 502 instead of the
    specific, actionable 401 a real (failed) call to OpenAI's own API
    already produces via the existing AuthenticationError handling. A
    slower but clearer error beats a faster, vaguer one; there's no dollar
    cost to the wasted round trip, only latency."""
    key = os.getenv(provider.api_key_env)
    if not key:
        return False
    for env_name in (provider.extra_env or {}).values():
        if not os.getenv(env_name):
            return False
    return True


def _is_cooling_down(provider: ProviderConfig) -> bool:
    until = _unavailable_until.get(provider.provider)
    return until is not None and datetime.now(timezone.utc) < until


def _mark_cooling_down(provider: ProviderConfig, exc: Exception) -> None:
    retry_after = getattr(getattr(exc, "response", None), "headers", {}).get("retry-after") if isinstance(exc, RateLimitError) else None
    try:
        seconds = float(retry_after) if retry_after is not None else DEFAULT_COOLDOWN_SECONDS
    except (TypeError, ValueError):
        seconds = DEFAULT_COOLDOWN_SECONDS
    _unavailable_until[provider.provider] = datetime.now(timezone.utc) + timedelta(seconds=seconds)


@lru_cache(maxsize=None)
def _client_for(provider_name: str, api_key: str, base_url: str | None) -> OpenAI:
    """Cached per (provider_name, api_key, base_url) so a changed .env value
    during a long-running process doesn't stick with a stale client — the
    cache key includes the actual credential, not just the provider name."""
    return OpenAI(
        api_key=api_key,
        base_url=base_url,
        timeout=PROVIDER_TIMEOUT_SECONDS,
        max_retries=PROVIDER_MAX_RETRIES,
    )


def _resolve_base_url(provider: ProviderConfig) -> str | None:
    """Substitutes `{ENV_VAR_NAME}` placeholders in base_url with that env
    var's value — needed for Cloudflare Workers AI, whose endpoint embeds
    the caller's account ID (config/model-selection.json's `extra_env`
    entry names which env var holds it). A no-op for every other provider,
    none of which template their base_url."""
    base_url = provider.base_url
    if base_url is None:
        return None
    for env_name in (provider.extra_env or {}).values():
        base_url = base_url.replace(f"{{{env_name}}}", os.getenv(env_name, ""))
    return base_url


def _build_client(provider: ProviderConfig) -> OpenAI:
    return _client_for(provider.provider, os.getenv(provider.api_key_env, ""), _resolve_base_url(provider))


def load_provider_chain() -> list[ProviderConfig]:
    selection = load_model_selection()
    return selection.provider_chain if selection else []


def _known_cost_per_million_input_tokens(provider: ProviderConfig) -> float | None:
    """The one axis of an economic utility frontier this project can
    honestly populate today: a real, committed per-token price — never a
    guessed one. Returns None for a free-tier candidate with no priced
    record yet (Mistral/OpenRouter/SambaNova/Cloudflare Workers AI today),
    which _frontier_key treats as a distinct, unranked-among-themselves
    tier rather than assuming they're cheaper or pricier than a known
    quantity. Uses the input-token rate as the representative figure —
    the cost paid regardless of how long a response turns out to be,
    unlike output cost, which depends on model behavior this function
    can't observe in advance."""
    if _is_local(provider):
        return 0.0
    record = latest_pricing_for(provider.model, load_model_pricing(), provider=provider.provider)
    return record.input if record is not None else None


def _frontier_key(provider: ProviderConfig) -> tuple[int, float]:
    """Cost-and-tier half of an effective economic utility frontier for
    provider/model selection (2026-09-13, per explicit direction to frame
    selection this way): a true frontier needs two axes, cost and quality,
    and this project has real data for exactly one of them — no per-model
    quality score exists anywhere here, and fabricating one just to
    complete a two-axis ranking would repeat the exact mistake this
    project has refused to make with pricing and capability data all
    session (see ProviderConfig's docstring on free-tier-as-fake-$0, and
    the structured_output "strict"/"unverified" split). So this ranks
    candidates on the one honestly-known axis instead of guessing the
    other:
      tier 0 — a LAN-local server (any of them, if more than one is
               configured): $0, real and known, the frontier's origin
               point.
      tier 1 — free-tier AND has a real priced record: ranked by that
               known cost, ascending (currently Groq, then Gemini).
      tier 2 — free-tier but no priced record yet: no data to rank these
               against each other, so they're tried in their configured
               order, but always ahead of anything known to be paid-only.
      tier 3 — no free-tier allowance at all (OpenAI): the paid fallback
               of last resort, ranked by known cost if more than one such
               entry ever exists.
    Once 1.D1's capability testing produces a real per-model quality
    score, it plugs in as a second sort component within tier 1 (and
    tier 2, once those entries get priced) rather than requiring this
    function to be redesigned."""
    if _is_local(provider):
        return (0, 0.0)
    cost = _known_cost_per_million_input_tokens(provider)
    is_free_tier = provider.free_entitlement_unit is not None
    if is_free_tier and cost is not None:
        return (1, cost)
    if is_free_tier:
        return (2, 0.0)
    return (3, cost if cost is not None else float("inf"))


def _rank_by_frontier(candidates: list[ProviderConfig]) -> list[ProviderConfig]:
    """Stable sort by _frontier_key — "stable" matters for tier 2 (equal
    keys), where it preserves config/model-selection.json's authored
    relative order among candidates this project has no data to rank
    against each other."""
    return sorted(candidates, key=_frontier_key)


# Case-insensitive substring denylist, checked against discovered model ids
# — added 2026-09-13 after live-testing discover_local_models() against a
# real reachable server turned up 8 models, not the 4 shown earlier in
# conversation, including several ("...-absolute-heresy", "...-Nymphaea-RP-
# heretic...") never previously seen or approved. Unfiltered dynamic
# discovery would silently re-admit exactly what was asked to be excluded
# the moment the server happens to be reachable — this is the enforcement
# point for that exclusion, not a one-time hardcoded model list (which
# would need updating by hand every time the server's loaded set changes).
# Trusting "whatever the LAN operator's own server serves" was the original
# design; that trust turned out to be wrong the first time it was actually
# tested against a live server, not merely assumed safe.
_EXCLUDED_NAME_SUBSTRINGS = ("heretic", "heresy", "uncensored")


def discover_local_models(base_url: str, provider_name: str) -> list[str]:
    """Queries one LAN-local server's own OpenAI-compatible model-list
    endpoint (GET {base_url}/models, via the SDK's client.models.list()) to
    find out what's currently loaded — rather than hardcoding a model name
    anywhere, so nothing about what's running on that server, however it's
    branded upstream, ever needs to appear in this codebase. Returns [] on
    any failure (server not running, unreachable, wrong host/port) — that's
    the expected, common state everywhere except that one LAN, not an
    error worth raising. Filters out anything matching
    _EXCLUDED_NAME_SUBSTRINGS before returning. `provider_name` (this
    server's distinct id, from _local_provider_name) is only used to
    attribute record_seen observations to the right server."""
    try:
        client = OpenAI(
            api_key=os.environ["LOCAL_INFERENCE_API_KEY"],
            base_url=base_url,
            timeout=LOCAL_DISCOVERY_TIMEOUT_SECONDS,
            max_retries=0,
        )
        all_ids = [m.id for m in client.models.list()]
    except Exception as exc:
        logger.info(
            "LAN-local inference server not reachable at %s (%s) — skipping.",
            base_url, type(exc).__name__,
        )
        return []

    excluded = [m for m in all_ids if any(s in m.lower() for s in _EXCLUDED_NAME_SUBSTRINGS)]
    if excluded:
        logger.info(
            "Excluded %d LAN-local model(s) matching a denylisted name pattern from %s.",
            len(excluded), base_url,
        )
    seen = [m for m in all_ids if m not in excluded]
    for model_id in seen:
        record_seen(provider_name, model_id)
    return seen


def _local_provider_entries() -> list[ProviderConfig]:
    """Builds ephemeral ProviderConfig entries for whatever each configured
    LAN-local server (local_inference_servers()) currently reports — probed
    sequentially, never persisted beyond a runtime model-id string, and not
    part of provider_chain (which stays committed, cloud-only config).
    structured_output defaults to "unverified", same capability-gating rule
    as every other entry: discovery proves a model is loaded, not that it
    reliably honors response_format — see ProviderConfig.structured_output's
    docstring. Each server's entries carry that server's own distinct
    provider id (_local_provider_name) so an unreachable server never
    shadows a reachable one at a different host:port."""
    entries = []
    for host, port in local_inference_servers():
        provider_name = _local_provider_name(host, port)
        base_url = _local_base_url(host, port)
        for model_id in discover_local_models(base_url, provider_name):
            entries.append(
                ProviderConfig(
                    provider=provider_name,
                    model=model_id,
                    api_key_env="LOCAL_INFERENCE_API_KEY",
                    base_url=base_url,
                    compatible_with="openai",
                    structured_output="unverified",
                    supports_streaming=True,
                    rationale="Discovered live from this LAN-local inference server's own /models endpoint.",
                    source_url=base_url,
                )
            )
    return entries


def get_provider_status() -> list[dict]:
    """Reports each configured chain entry's current state, in the same
    frontier-ranked order the trial loop actually uses (see _frontier_key)
    — never logs or returns credential values, only whether one is
    present. Local entries include a live discovery probe, so this call
    can take up to LOCAL_DISCOVERY_TIMEOUT_SECONDS longer when that server
    isn't reachable — acceptable for a low-frequency diagnostic endpoint."""
    status = []
    for provider in _rank_by_frontier(_local_provider_entries() + load_provider_chain()):
        key_expires, key_expiry = key_expiry_status(provider)
        if is_retired(provider.provider, provider.model):
            state = f"retired: not seen in {RETIREMENT_THRESHOLD_DAYS}+ days"
        elif key_expiry == "expired":
            # Checked ahead of _is_configured: the key IS present, just no
            # longer valid — "credential_missing" would misreport why this
            # provider is unusable.
            state = f"unavailable: key_expired_{key_expires}"
        elif not _is_configured(provider):
            state = "unavailable: credential_missing"
        elif _is_cooling_down(provider):
            until = _unavailable_until[provider.provider].isoformat()
            state = f"unavailable: cooling_down_until={until}"
        elif key_expiry == "expiring_soon":
            state = f"configured: key_expiring_{key_expires}"
        else:
            state = "configured"
        status.append(
            {
                "provider": provider.provider,
                "model": provider.model,
                # As-stored, not _resolve_base_url()'d: for an entry needing
                # extra_env (Cloudflare), this deliberately still shows the
                # raw "{CLOUDFLARE_ACCOUNT_ID}" placeholder rather than a
                # resolved URL with an empty-string gap when unconfigured.
                # None here means "OpenAI SDK's own default endpoint" (see
                # ProviderConfig.base_url), not "unknown."
                "base_url": provider.base_url,
                "compatible_with": provider.compatible_with,
                "structured_output": provider.structured_output,
                "supports_streaming": provider.supports_streaming,
                "key_expires": key_expires,
                # All three None for a provider with no free-tier allowance
                # at all (e.g. openai, LAN-local) — added 2026-09-14, same
                # day as main.py's free_tier_note, so this same "cost_usd
                # may not be actually billed" fact is visible here too, not
                # just attached to a live /ask response.
                "free_entitlement_unit": provider.free_entitlement_unit,
                "free_entitlement_amount": provider.free_entitlement_amount,
                "free_entitlement_period": provider.free_entitlement_period,
                "status": state,
            }
        )
    return status


def call_structured_with_fallback(
    messages: list[dict], response_format: type[BaseModel], forced_provider: str | None = None
) -> tuple[BaseModel, int, int, int, str, str]:
    """Tries each "strict"-structured-output-capable provider in chain
    order, skipping unconfigured or cooling-down ones, advancing only on a
    retryable failure. Returns (parsed, total_tokens, prompt_tokens,
    completion_tokens, provider_name, model_name).

    Only ever called for the no-override-*model* default path — an
    explicit per-request `model=` override is handled entirely inside
    ask_service.py, via its own already-tested `_get_client()`, and never
    reaches this function at all. That keeps this module single-purpose
    (the fallback chain, nothing else) and avoids two different timeout/
    retry configurations for what's nominally "the same" OpenAI call
    depending on which code path reached it.

    `forced_provider` (added 2026-09-14, for the Streamlit demo's provider
    selector): restricts the already-eligible candidate list to that one
    provider instead of trying the whole chain — still subject to every
    existing safety check below (credential presence, cooldown, key
    expiry). main.py validates `forced_provider` names a real,
    structured-output-eligible provider *before* calling this, so an
    unknown or "unverified" provider never reaches here at all — the only
    way this can still end up empty is the forced provider being
    momentarily unconfigured/cooling-down/expired, handled below with a
    clearer message than the generic chain-exhausted one.

    Candidate order is the cost/tier half of an economic utility frontier
    (_frontier_key/_rank_by_frontier) — LAN-local first ($0, known), then
    free-tier-with-known-price ascending by cost, then free-tier-unpriced,
    then paid-only last — not just "local, then whatever order the config
    file happens to list." In practice this rarely changes anything for
    structured calls today: discovery always starts entries at
    structured_output="unverified", so they're filtered out below the same
    way any other unverified cloud entry would be, until a model is
    actually confirmed reliable and promoted."""
    ranked = _rank_by_frontier(_local_provider_entries() + load_provider_chain())
    candidates = [
        p for p in ranked if p.structured_output == "strict" and not is_retired(p.provider, p.model)
    ]
    if forced_provider is not None:
        candidates = [p for p in candidates if p.provider == forced_provider]
    last_exc: Exception | None = None
    for provider in candidates:
        if not _is_configured(provider) or _is_cooling_down(provider):
            continue
        if key_expiry_status(provider)[1] == "expired":
            # A present-but-expired key would otherwise reach the API and
            # fail as an ordinary AuthenticationError anyway (still
            # correctly triggers fallback via _RETRYABLE_EXCEPTIONS below) —
            # skipped here purely to avoid the wasted round trip when it's
            # already known ahead of time.
            continue
        client = _build_client(provider)
        try:
            completion = client.chat.completions.parse(
                model=provider.model,
                messages=messages,
                response_format=response_format,
                max_completion_tokens=MAX_COMPLETION_TOKENS,
            )
        except _RETRYABLE_EXCEPTIONS as exc:
            logger.warning("Provider %s unavailable (%s), trying next.", provider.provider, type(exc).__name__)
            if isinstance(exc, RateLimitError):
                _mark_cooling_down(provider, exc)
            last_exc = exc
            continue

        parsed = completion.choices[0].message.parsed
        if parsed is None:
            raise ValueError("Model returned no parseable structured output")
        usage = completion.usage
        total, prompt, comp = (usage.total_tokens, usage.prompt_tokens, usage.completion_tokens) if usage else (0, 0, 0)
        record_seen(provider.provider, provider.model)
        return parsed, total, prompt, comp, provider.provider, provider.model

    if last_exc is not None:
        raise last_exc
    if forced_provider is not None:
        raise OpenAIError(
            f"Provider {forced_provider!r} is not currently available (missing "
            "credential, cooling down, or an expired key — see GET /providers/status)."
        )
    raise OpenAIError("No configured provider was available to serve this request.")


def stream_with_fallback(messages: list[dict], forced_provider: str | None = None):
    """Tries each streaming-capable provider in chain order. Fallback is
    only attempted for a failure that happens *before* the first chunk is
    read from a given provider's stream — once content has been yielded to
    our own caller, a later mid-stream failure ends the stream; it can't be
    silently retried on a different provider without the client seeing a
    response spliced from two different models. Returns
    (chunk_iterator, provider_name, model_name).

    Like call_structured_with_fallback, only ever called for the
    no-override-*model* default path — see that function's docstring,
    including for the frontier-ranked candidate order and `forced_provider`
    semantics, both shared here. Unlike the structured path, there's no
    structured_output-eligibility gate to validate against — any
    supports_streaming provider is a legal forced choice on this endpoint."""
    ranked = _rank_by_frontier(_local_provider_entries() + load_provider_chain())
    candidates = [
        p for p in ranked if p.supports_streaming and not is_retired(p.provider, p.model)
    ]
    if forced_provider is not None:
        candidates = [p for p in candidates if p.provider == forced_provider]
    last_exc: Exception | None = None
    for provider in candidates:
        if not _is_configured(provider) or _is_cooling_down(provider):
            continue
        if key_expiry_status(provider)[1] == "expired":
            # A present-but-expired key would otherwise reach the API and
            # fail as an ordinary AuthenticationError anyway (still
            # correctly triggers fallback via _RETRYABLE_EXCEPTIONS below) —
            # skipped here purely to avoid the wasted round trip when it's
            # already known ahead of time.
            continue
        client = _build_client(provider)
        try:
            stream = client.chat.completions.create(
                model=provider.model,
                messages=messages,
                stream=True,
                max_completion_tokens=MAX_COMPLETION_TOKENS,
            )
            first_chunk = next(stream)
        except StopIteration:
            first_chunk = None
        except _RETRYABLE_EXCEPTIONS as exc:
            logger.warning("Provider %s unavailable (%s), trying next.", provider.provider, type(exc).__name__)
            if isinstance(exc, RateLimitError):
                _mark_cooling_down(provider, exc)
            last_exc = exc
            continue

        record_seen(provider.provider, provider.model)

        def _generate(first, rest):
            if first is not None:
                yield first
            # Anything that fails from here on has already sent content to
            # the caller — it ends the stream, it does not fall back.
            yield from rest

        return _generate(first_chunk, stream), provider.provider, provider.model

    if last_exc is not None:
        raise last_exc
    if forced_provider is not None:
        raise OpenAIError(
            f"Provider {forced_provider!r} is not currently available (missing "
            "credential, cooling down, or an expired key — see GET /providers/status)."
        )
    raise OpenAIError("No configured provider was available to serve this request.")
