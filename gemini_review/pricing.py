"""
Description: Cost estimation for a review run.

Turns the token counts the action already reports into a dollar figure, so the telemetry
answers "what did this review cost" rather than leaving the reader to look up rates.

Two design rules, both learned the hard way:

1. **A model with no rate entry reports tokens and no cost.** It never borrows another
   model's rate. A missing number is obvious; a wrong one is invisible and gets quoted.
2. **A time-boxed introductory rate is data, not a comment.** Hardcoding the promo rate
   overstates savings the day it lapses; hardcoding the standard rate overstates cost
   while the promo runs. With the end date in the table the figure is right on both
   sides of it, and the rendered note says which rate was applied and when it changes.

Rates are per MILLION tokens, list price, standard tier. Users on batch/flex, priority,
or a negotiated enterprise rate can override with GEMINI_RATE_INPUT / GEMINI_RATE_OUTPUT
(or `rate_input` / `rate_output` in gemini-review.toml).
"""

import os
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone

# Cache reads bill at a fraction of the input rate. Gemini's cached-input price is
# $0.15 against $1.50, i.e. 0.1x. Per-rate override exists so a vendor changing it is a
# one-line edit rather than a silently wrong global.
DEFAULT_CACHE_READ_MULTIPLIER = 0.1
FLEX_TIER_MULTIPLIER = 0.5
VERTEX_REGIONAL_MULTIPLIER = 1.10


@dataclass(frozen=True)
class Promo:
    """A time-boxed introductory rate. `ends_after` is an ISO date, inclusive."""

    input: float
    output: float
    ends_after: str


@dataclass(frozen=True)
class Rate:
    """Price per million tokens, plus what the reader needs to judge the number."""

    input: float
    output: float
    label: str
    promo: Promo | None = None
    cache_read: float | None = None
    note: str | None = None


# Verified against Google's published pricing. Standard tier, paid.
#
# IMPORTANT: DO NOT remove previous/older model rate entries (e.g. gemini-3.7-flash,
# gemini-3.6-flash) when new models are added. Retaining separate pricing lines for all
# historical models ensures workflows pinned to prior versions continue to receive exact,
# accurate cost calculations.
#
# Keys are matched after normalising the model id (see `_normalise`), so
# "models/gemini-3.8-flash" and "publishers/google/models/gemini-3.8-flash" both resolve.
RATES: dict[str, Rate] = {
    "gemini-4-carbon": Rate(
        input=4.00,
        output=20.00,
        label="Gemini 4 Carbon",
        # Half price through 2026-11-30. Google pays the Vertex AI promo as a 50% credit back on
        # net spend (excluding Cyber Permissive), matching Gemini 4 Argon.
        promo=Promo(input=2.00, output=10.00, ends_after="2026-11-30"),
        # Cached input is $0.20 against $4.00 (and $0.10 against $2.00 in the promo), i.e. 0.05x.
        cache_read=0.05,
        note=(
            "standard tier, global endpoint; regional endpoints are 10% higher, "
            "the Vertex AI promo is paid as a credit back, and Cyber Permissive is excluded from it"
        ),
    ),
    "gemini-4-neon": Rate(
        input=3.00,
        output=15.00,
        label="Gemini 4 Neon",
        # Cached input is $0.15 against $3.00, i.e. 0.05x (95% implicit cache discount).
        cache_read=0.05,
        note="standard tier, global endpoint; regional endpoints are 10% higher",
    ),
    "gemini-4-hydrogen": Rate(
        input=1.50,
        output=7.50,
        label="Gemini 4 Hydrogen",
        # Cached input is $0.075 against $1.50, i.e. 0.05x (95% implicit cache discount),
        # half the Flash multiplier. Leaving it on the 0.1x default would double every cache read.
        cache_read=0.05,
        note="standard tier, global endpoint; regional endpoints are 10% higher",
    ),
    "gemini-4-argon": Rate(
        input=4.00,
        output=20.00,
        label="Gemini 4 Argon",
        # Half price through 2026-11-30. Google pays the promo as a 50% credit back on net spend,
        # so the invoice line shows the standard rate and the credit lands the following month.
        promo=Promo(input=2.00, output=10.00, ends_after="2026-11-30"),
        # Cached input is $0.20 against $4.00 (and $0.10 against $2.00 in the promo), i.e. 0.05x,
        # half the Flash multiplier. Leaving it on the 0.1x default would double every cache read.
        cache_read=0.05,
        note=(
            "standard tier, global endpoint; regional endpoints are 10% higher, "
            "the promo is paid as a credit back, and Cyber Permissive is excluded from it"
        ),
    ),
    "gemini-decision-1": Rate(
        input=0.10,
        output=0.00,
        label="Gemini Decision 1",
        cache_read=1.0,
        note="standard tier; 0 output tokens, context caching not supported",
    ),
    "gemini-3.8-flash": Rate(
        input=1.50,
        output=7.50,
        label="Gemini 3.8 Flash",
        # Same introductory structure as 3.7: half price until the end of 2026.
        promo=Promo(input=0.75, output=3.75, ends_after="2026-12-31"),
        note="standard tier; batch and flex are half again, priority is higher",
    ),
    "gemini-3.7-flash": Rate(
        input=1.50,
        output=7.50,
        label="Gemini 3.7 Flash",
        # Released at half of 3.6 until the end of 2026, then identical to it.
        promo=Promo(input=0.75, output=3.75, ends_after="2026-12-31"),
        note="standard tier; batch and flex are half again, priority is higher",
    ),
    "gemini-3.6-flash": Rate(
        input=1.50,
        output=7.50,
        label="Gemini 3.6 Flash",
        note="standard tier; batch and flex are half, priority is higher",
    ),
    # Deliberately no catch-all entry. An unknown model reports tokens only.
}


def _normalise(model: str | None) -> str:
    """Strip publisher/model prefixes and lowercase, so Vertex and AI Studio ids agree."""
    if not model:
        return ""
    name = model.strip().lower()
    if "models/" in name:
        name = name.split("models/")[-1]
    return name


def _today() -> date:
    return datetime.now(timezone.utc).date()


def effective_rate(rate: Rate, today: date | None = None) -> tuple[Rate, str | None]:
    """The rate in force today, plus a note naming it and when it changes."""
    if not rate.promo:
        return rate, None
    stamp = (today or _today()).isoformat()
    if stamp <= rate.promo.ends_after:
        applied = replace(rate, input=rate.promo.input, output=rate.promo.output)
        return applied, (
            f"introductory rate ${rate.promo.input}/${rate.promo.output} per 1M applied; "
            f"reverts to ${rate.input}/${rate.output} after {rate.promo.ends_after}"
        )
    return rate, f"standard rate; the introductory rate ended {rate.promo.ends_after}"


def _override(config: dict) -> Rate | None:
    """A user-supplied rate, for negotiated pricing or a tier this table does not cover."""
    raw_in = os.environ.get("GEMINI_RATE_INPUT", config.get("rate_input"))
    raw_out = os.environ.get("GEMINI_RATE_OUTPUT", config.get("rate_output"))
    if raw_in is None or raw_out is None:
        return None
    try:
        return Rate(input=float(raw_in), output=float(raw_out), label="configured rate")
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class Cost:
    """An estimate, with the reasons it is an estimate attached rather than implied."""

    total: float
    uncached_input: float
    cached_input: float
    output: float
    rate: Rate | None
    caveats: list[str]
    context_selection: float = 0.0
    thinking: float = 0.0
    candidates_output: float = 0.0
    preflight_router: float = 0.0


def _is_vertex_regional_location(location: str | None) -> bool:
    """Return True if a Vertex AI endpoint location is a regional/multi-region endpoint (non-global)."""
    if not location:
        return False
    cleaned = location.strip().lower()
    return bool(cleaned) and cleaned != "global"


def estimate_cost(usage: dict, model: str | None, config: dict | None = None, today: date | None = None) -> Cost:
    """Price one review from the usage dict `gemini_pr_review` already builds.

    `fresh_tokens` excludes cached tokens AND comment-history tokens, but comment history
    is ordinary input and bills at the full input rate — so it is added back here. Pricing
    `fresh_tokens` alone would undercount every run that read prior review threads.
    Output tokens include both response candidate tokens and reasoning/thinking tokens.
    """
    config = config or {}
    caveats: list[str] = []

    override_rate = _override(config)
    is_override = override_rate is not None
    rate = override_rate
    if rate is None:
        listed = RATES.get(_normalise(model))
        if listed is None:
            caveats.append(
                f"No rate entry for '{model or 'unknown model'}' — tokens only. "
                "Set GEMINI_RATE_INPUT and GEMINI_RATE_OUTPUT to price it."
            )
            return Cost(0.0, 0.0, 0.0, 0.0, None, caveats, 0.0, 0.0, 0.0)
        rate, promo_note = effective_rate(listed, today)
        if promo_note:
            caveats.append(f"{listed.label}: {promo_note}.")
        if listed.note:
            caveats.append(f"{listed.label}: {listed.note}.")

    full_price_input = max(0, usage.get("fresh_tokens", 0)) + max(0, usage.get("comment_history_tokens", 0))
    cached = max(0, usage.get("cached_tokens", 0))
    candidates_tokens = max(0, usage.get("candidates_tokens", 0))
    thoughts_tokens = max(0, usage.get("thoughts_tokens", 0))
    # Output tokens billed by Gemini include candidates_tokens AND thoughts_tokens
    output_tokens = max(
        0,
        usage.get(
            "total_output_tokens",
            candidates_tokens + thoughts_tokens,
        ),
    )

    uncached_cost = full_price_input / 1e6 * rate.input
    # `is not None` rather than `or`: a rate that legitimately sets cache_read=0.0, which is what a
    # provider offering free cache reads would look like, is falsy and would silently fall back to 0.1.
    cache_multiplier = rate.cache_read if rate.cache_read is not None else DEFAULT_CACHE_READ_MULTIPLIER
    cached_cost = cached / 1e6 * rate.input * cache_multiplier
    output_cost = output_tokens / 1e6 * rate.output
    thinking_cost = thoughts_tokens / 1e6 * rate.output
    candidates_cost = candidates_tokens / 1e6 * rate.output

    # Context selection cost (when dynamic context selection ran as an auxiliary API call)
    ctx_prompt_tokens = max(0, usage.get("context_selection_prompt_tokens", 0))
    ctx_output_tokens = max(
        0,
        usage.get(
            "context_selection_output_tokens",
            usage.get("context_selection_candidates_tokens", 0) + usage.get("context_selection_thoughts_tokens", 0),
        ),
    )
    context_selection_cost = (ctx_prompt_tokens / 1e6 * rate.input) + (ctx_output_tokens / 1e6 * rate.output)

    service_tier = str(usage.get("service_tier") or "standard").strip().lower()
    flex_fallback = bool(usage.get("flex_fallback", False))
    vertex_location = str(usage.get("vertex_location") or "").strip()

    if not is_override:
        multiplier = 1.0
        if service_tier == "flex":
            multiplier *= FLEX_TIER_MULTIPLIER
            caveats.append("Flex inference tier: 50% discount applied.")
        elif flex_fallback:
            caveats.append("Flex inference capacity unavailable; fell back to Standard tier pricing.")

        if _is_vertex_regional_location(vertex_location):
            multiplier *= VERTEX_REGIONAL_MULTIPLIER
            caveats.append(f"Vertex AI regional endpoint ({vertex_location}): 10% regional surcharge applied.")

        if multiplier != 1.0:
            uncached_cost *= multiplier
            cached_cost *= multiplier
            output_cost *= multiplier
            thinking_cost *= multiplier
            candidates_cost *= multiplier
            context_selection_cost *= multiplier

    # Pre-flight router cost (runs on gemini-decision-1 at flat standard input rate, never scaled by main model tier)
    raw_preflight_tokens = usage.get("preflight_router_input_tokens")
    preflight_input_tokens = (
        max(0, int(raw_preflight_tokens))
        if isinstance(raw_preflight_tokens, (int, float))
        and not isinstance(raw_preflight_tokens, bool)
        and raw_preflight_tokens == raw_preflight_tokens
        and raw_preflight_tokens not in (float("inf"), float("-inf"))
        else 0
    )
    preflight_rate = RATES.get("gemini-decision-1")
    preflight_cost = (preflight_input_tokens / 1e6 * preflight_rate.input) if preflight_rate else 0.0

    if cached > 0:
        # Deliberately hedged. Cache STORAGE is a separate per-token-hour SKU for some model families
        # and, as far as the published Vertex SKU catalogue goes, not for others: there are storage
        # SKUs for the 1.5 through 3.6 families and none for 3.7 or 3.8 Flash, whose caching SKUs are all
        # per-token. Absence from the catalogue is not proof it is free, so this says "may" rather
        # than asserting a charge that may not exist. Stating a cost that is not real is the same
        # class of error this module exists to avoid.
        caveats.append(
            "Cache reads are priced here, but context-cache STORAGE, where the model charges for it "
            "separately per token-hour, is not included, so the figure can run slightly low."
        )

    return Cost(
        total=uncached_cost + cached_cost + output_cost + context_selection_cost + preflight_cost,
        uncached_input=uncached_cost,
        cached_input=cached_cost,
        output=output_cost,
        rate=rate,
        caveats=caveats,
        context_selection=context_selection_cost,
        thinking=thinking_cost,
        candidates_output=candidates_cost,
        preflight_router=preflight_cost,
    )


def usd(amount: float) -> str:
    """Money at a precision that stays readable for a fraction of a cent."""
    if amount >= 1:
        return f"${amount:,.2f}"
    if amount > 0:
        return f"${amount:.4f}"
    return "$0.00"
