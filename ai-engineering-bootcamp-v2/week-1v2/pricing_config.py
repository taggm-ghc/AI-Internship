"""Model selection and pricing configuration: shared schema and loaders used by
main.py and the scripts/ pricing-maintenance tools, so the schema is defined
once instead of duplicated across files.

Ported from the sibling VERA project (../ai-eng-bootcamp.vera/vera/pricing/config.py)
2026-09-13. That project's own scraper for OpenAI's pricing page proved
unreliable enough to be deprecated as a documented anti-pattern before this
schema/loader design replaced it — see that project's
pricing-scraper-design-prv.md for the full failure analysis (table-walk +
regex parsing of developers.openai.com/api/docs/pricing substring-collided on
model names and confused cached-input for output). The lesson that traveled
with this port: treat pricing as versioned, provenance-tracked, human-reviewed
data (see config/model-pricing.json's `source_url`/`retrieved_at`/`notes`
fields on every record), not something scraped fresh on every request.

Config is version-controlled JSON, not a database — the same reasoning
applies here as in the source project: a host's filesystem may be ephemeral
across redeploys, while Git-tracked JSON is restored every time, diffable,
and human-reviewable.
"""
import json
import logging
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator

logger = logging.getLogger(__name__)

# This file lives at <week-1v2>/pricing_config.py, so CONFIG_DIR is a direct
# sibling directory (unlike the source project, where the equivalent module
# lives three levels below its repo root).
CONFIG_DIR = Path(__file__).resolve().parent / "config"
MODEL_SELECTION_PATH = CONFIG_DIR / "model-selection.json"
MODEL_PRICING_PATH = CONFIG_DIR / "model-pricing.json"
WEB_SEARCH_PRICING_PATH = CONFIG_DIR / "web-search-tool-pricing.json"


class ProviderConfig(BaseModel):
    """One entry in ModelSelection.provider_chain — a provider/model this
    project can call, plus what it takes to call it and what it's known to
    support. Deliberately does NOT carry a price: a free-tier allowance
    (free_entitlement_*, below) is a different fact from a model's canonical
    per-token price, and collapsing them into a fake `$0.00` PricingRecord
    would be factually wrong (Groq and Cloudflare, among others, publish
    real per-token/per-request prices *and separately* grant a free
    allowance) and would contradict this project's own "unknown is not the
    same as zero" pricing philosophy. A provider's real canonical price, if
    it has one worth tracking, belongs in config/model-pricing.json like
    every other PricingRecord."""

    provider: str
    model: str
    api_key_env: str
    # Cloudflare Workers AI needs an account ID alongside its API token —
    # this covers that case (and any future provider needing more than one
    # credential) without a provider-specific field.
    extra_env: dict[str, str] | None = None
    base_url: str | None = None  # None = OpenAI's own default endpoint
    # Every entry in this project — cloud config here and LAN-local's
    # runtime-discovered entries alike — is called through one client
    # construction (providers._build_client(), a plain OpenAI SDK client
    # pointed at base_url): that's what "openai" records here. It's been an
    # unstated precondition of membership in this chain rather than a
    # recorded fact until 2026-09-14 — added explicitly (and surfaced in
    # GET /providers/status) after a request to make LAN-local's config
    # carry the same base_url/compatibility visibility cloud entries do.
    # Currently always "openai" for every entry; a provider using a
    # genuinely different protocol would need its own client construction
    # (not just a new provider_chain row) before this field could honestly
    # say otherwise.
    compatible_with: str = "openai"
    # "strict" only for a provider/model independently confirmed (from that
    # provider's own docs, not an aggregator) to support schema-constrained
    # structured output the way this project's /ask-family endpoints need.
    # Structured-output support is a per-model fact, not a provider-wide
    # one — confirmed the hard way for Groq, whose strict JSON-schema mode
    # is limited to a handful of models, not "every model" as an early,
    # since-corrected pass at this research assumed from a secondary
    # source. "unverified" entries are still usable by /ask/stream (no
    # schema requirement there) but excluded from the structured chain
    # until promoted.
    structured_output: Literal["strict", "unverified"] = "unverified"
    supports_streaming: bool = True
    free_entitlement_unit: str | None = None  # e.g. "requests" | "tokens" | "neurons"
    free_entitlement_amount: float | None = None
    free_entitlement_period: str | None = None  # e.g. "minute" | "day" | "month"
    rationale: str
    source_url: str
    notes: str = ""


class ModelSelection(BaseModel):
    """`supported_models` added 2026-09-13: previously main.py hardcoded its
    own separate Literal of allowed model names, which had to be kept in
    sync with this file by hand (and briefly drifted while adding
    gpt-4.1-nano — the immediate trigger for this fix). This list is now the
    single source of truth main.py, MVP_Layered_Ask.py, and the startup pricing
    check all read from, instead of each hardcoding their own copy."""

    selected_model: str
    selected_at: str
    rationale: str
    supported_models: list[str] = Field(min_length=1)
    # provider_chain added 2026-09-13: a *separate* concern from
    # selected_model/supported_models above. Those describe which OpenAI
    # model a caller can explicitly force via a per-request `model=`
    # override (unchanged, still OpenAI-only). provider_chain instead
    # describes the *default*, no-override path: an ordered list of
    # providers/models to try — free ones first — before spending against
    # the paid OpenAI key, which is always the chain's last entry. See
    # providers.py for the trial logic and ProviderConfig's own docstring
    # for why free tiers are never represented as a $0 price here.
    provider_chain: list[ProviderConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def _selected_model_is_supported(self) -> "ModelSelection":
        if self.selected_model not in self.supported_models:
            raise ValueError(
                f"selected_model {self.selected_model!r} is not in supported_models {self.supported_models!r}"
            )
        return self


class PricingRecord(BaseModel):
    provider: str
    model: str
    service_tier: str = "standard"
    # None means the model has a single price regardless of context length
    # (e.g. legacy models like gpt-4o-mini); "short"/"long" for models that
    # publish separate short- and long-context rates.
    context_length: str | None = None
    currency: str = "USD"
    unit: str = "per_1m_tokens"
    input: float
    cached_input: float | None = None
    cache_writes: float | None = None
    output: float
    source_url: str
    retrieved_at: str  # when this record was scraped/entered (ISO datetime)
    effective_from: str  # date the price is believed to have taken effect
    notes: str = ""


class ModelPricing(BaseModel):
    records: list[PricingRecord]


class WebSearchToolPricing(BaseModel):
    """OpenAI's built-in web-search tool is billed per call, not per token —
    a different shape from PricingRecord (input/output per-1M-tokens), so it
    gets its own model/file rather than being shoehorned into PricingRecord
    (e.g. reusing service_tier for "reasoning vs non-reasoning" would collide
    with that field's existing standard/flex meaning for token pricing, and
    latest_pricing_for()'s model+service_tier lookup doesn't fit a per-call
    price anyway). Added 2026-09-13: reference data only — no endpoint in
    this project currently calls OpenAI's web_search tool, so nothing reads
    this yet. Added on request, verbatim from a user-supplied screenshot of
    https://developers.openai.com/api/docs/pricing's "Tools" table (not an
    automated scrape or an AI-summarized fetch — two different automated
    reads of that same page disagreed with each other on these exact numbers
    before the user supplied the verbatim table, which is exactly the
    scraping-unreliability failure mode config/model-pricing.json's own
    design already exists to avoid)."""

    tool: str  # stable identifier, e.g. "web_search", "web_search_preview_reasoning"
    details: str  # human-readable label as shown on the pricing page
    price_per_1k_calls: float
    currency: str = "USD"
    # "billed_at_model_rates" | "free" — whether search-content tokens fed to
    # the model alongside the prompt are billed separately at that model's
    # normal per-token rate, or included at no extra charge.
    search_content_tokens: str
    source_url: str
    retrieved_at: str
    effective_from: str
    notes: str = ""


class WebSearchPricing(BaseModel):
    records: list[WebSearchToolPricing]


def _load_json_config(path: Path, model_cls):
    try:
        return model_cls(**json.loads(path.read_text()))
    except Exception:
        logger.exception("Failed to load or validate %s", path)
        return None


def load_model_selection() -> ModelSelection | None:
    return _load_json_config(MODEL_SELECTION_PATH, ModelSelection)


def load_model_pricing() -> ModelPricing | None:
    return _load_json_config(MODEL_PRICING_PATH, ModelPricing)


def load_web_search_pricing() -> WebSearchPricing | None:
    return _load_json_config(WEB_SEARCH_PRICING_PATH, WebSearchPricing)




def latest_pricing_for(
    model: str,
    pricing: ModelPricing | None,
    service_tier: str = "standard",
    provider: str | None = None,
) -> PricingRecord | None:
    """Defaults to service_tier="standard" — fixed 2026-09-13, ahead of the
    source project (VERA), which found but explicitly deferred this: some
    models (e.g. gpt-5-nano, gpt-5.4-nano, gpt-5-mini in
    config/model-pricing.json) have both a "standard" and a "flex" price
    record sharing the same effective_from date. Picking "latest by date"
    without also filtering by tier makes the choice between those two
    records arbitrary (whichever happens to sort last), which could silently
    apply flex pricing to a call actually billed at the standard rate.
    ask_service.py never requests OpenAI's flex processing tier, so
    "standard" is the only tier this project's calls are ever actually
    billed at. Currently unreachable in practice — none of week-1v2's
    supported models have a tier-ambiguous record — but fixed proactively
    rather than left as a latent, ported bug.

    `provider` (optional, added 2026-09-13 for the multi-provider fallback
    chain): PricingRecord already had this field but nothing filtered on it
    until now — with only OpenAI models in play, a bare model-name match was
    unambiguous. Once other providers' models can be looked up too, scoping
    by provider as well prevents a same-named model string from a different
    provider silently matching the wrong record. Omitting it preserves the
    exact previous match-by-model-name-only behavior for every existing
    caller."""
    if pricing is None:
        return None
    matches = [
        r
        for r in pricing.records
        if r.model == model
        and r.service_tier == service_tier
        and (provider is None or r.provider == provider)
    ]
    return max(matches, key=lambda r: r.effective_from) if matches else None


def append_pricing_record(record: PricingRecord, path: Path = MODEL_PRICING_PATH) -> bool:
    """Append a validated pricing record unless a byte-identical one already
    exists. Never edits or removes an existing record — pricing history stays
    auditable, and main.py always resolves to the most recent record by
    effective_from."""
    data = json.loads(path.read_text()) if path.exists() else {"records": []}
    record_dict = record.model_dump()
    if record_dict in data["records"]:
        return False
    data["records"].append(record_dict)
    path.write_text(json.dumps(data, indent=2) + "\n")
    return True
