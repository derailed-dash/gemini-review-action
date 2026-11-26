"""Tests for cost estimation."""

from datetime import date

import pytest

from gemini_review.pricing import (
    RATES,
    Promo,
    Rate,
    effective_rate,
    estimate_cost,
    usd,
)


def usage(fresh=0, cached=0, output=0, history=0):
    return {
        "fresh_tokens": fresh,
        "cached_tokens": cached,
        "candidates_tokens": output,
        "comment_history_tokens": history,
    }


class TestEffectiveRate:
    def test_no_promo_returns_rate_unchanged(self):
        rate = Rate(input=1.5, output=7.5, label="Test")
        applied, note = effective_rate(rate, today=date(2026, 8, 17))
        assert applied is rate
        assert note is None

    def test_promo_applies_on_and_before_end_date(self):
        rate = Rate(input=1.5, output=7.5, label="Test", promo=Promo(0.75, 3.75, "2026-12-31"))
        applied, note = effective_rate(rate, today=date(2026, 12, 31))
        assert (applied.input, applied.output) == (0.75, 3.75)
        assert "introductory rate" in note
        assert "2026-12-31" in note

    def test_standard_rate_the_day_after_the_promo_ends(self):
        rate = Rate(input=1.5, output=7.5, label="Test", promo=Promo(0.75, 3.75, "2026-12-31"))
        applied, note = effective_rate(rate, today=date(2027, 1, 1))
        assert (applied.input, applied.output) == (1.5, 7.5)
        assert "ended" in note


class TestEstimateCost:
    def test_unknown_model_reports_tokens_only(self):
        cost = estimate_cost(usage(fresh=1000, output=100), "some-future-model")
        assert cost.rate is None
        assert cost.total == 0.0
        assert "No rate entry" in cost.caveats[0]

    def test_known_model_prices_each_bucket(self):
        # 100k uncached in, 900k cached in, 1k out, at the promo rate $0.75/$3.75.
        cost = estimate_cost(
            usage(fresh=100_000, cached=900_000, output=1_000),
            "gemini-3.7-flash",
            today=date(2026, 8, 17),
        )
        assert cost.uncached_input == pytest.approx(0.075)
        assert cost.cached_input == pytest.approx(0.0675)  # 0.1x input rate
        assert cost.output == pytest.approx(0.00375)
        assert cost.total == pytest.approx(0.14625)

    def test_comment_history_bills_at_full_input_rate(self):
        """`fresh_tokens` excludes comment history, but it is ordinary billed input."""
        without = estimate_cost(usage(fresh=100_000), "gemini-3.7-flash", today=date(2026, 8, 17))
        with_history = estimate_cost(usage(fresh=100_000, history=100_000), "gemini-3.7-flash", today=date(2026, 8, 17))
        assert with_history.total == pytest.approx(without.total * 2)

    def test_model_id_is_normalised(self):
        bare = estimate_cost(usage(fresh=1_000_000), "gemini-3.7-flash", today=date(2026, 8, 17))
        prefixed = estimate_cost(
            usage(fresh=1_000_000),
            "publishers/google/models/GEMINI-3.7-FLASH",
            today=date(2026, 8, 17),
        )
        assert prefixed.total == bare.total

    def test_cache_storage_caveat_only_when_cache_was_used(self):
        with_cache = estimate_cost(usage(fresh=10, cached=10), "gemini-3.6-flash")
        without_cache = estimate_cost(usage(fresh=10), "gemini-3.6-flash")
        assert any("STORAGE" in c for c in with_cache.caveats)
        assert not any("STORAGE" in c for c in without_cache.caveats)

    def test_negative_counts_cannot_produce_a_negative_charge(self):
        cost = estimate_cost(usage(fresh=-5000, cached=-1), "gemini-3.7-flash")
        assert cost.total == 0.0

    def test_missing_keys_are_treated_as_zero(self):
        cost = estimate_cost({}, "gemini-3.7-flash", today=date(2026, 8, 17))
        assert cost.total == 0.0
        assert cost.rate is not None


class TestRateOverride:
    def test_config_override_wins_over_the_table(self):
        cost = estimate_cost(
            usage(fresh=1_000_000),
            "gemini-3.7-flash",
            config={"rate_input": 10.0, "rate_output": 20.0},
        )
        assert cost.total == pytest.approx(10.0)
        assert cost.rate.label == "configured rate"

    def test_env_override_wins_over_config(self, monkeypatch):
        monkeypatch.setenv("GEMINI_RATE_INPUT", "2.0")
        monkeypatch.setenv("GEMINI_RATE_OUTPUT", "4.0")
        cost = estimate_cost(
            usage(fresh=1_000_000),
            "gemini-3.7-flash",
            config={"rate_input": 10.0, "rate_output": 20.0},
        )
        assert cost.total == pytest.approx(2.0)

    def test_override_prices_an_otherwise_unknown_model(self, monkeypatch):
        monkeypatch.setenv("GEMINI_RATE_INPUT", "1.0")
        monkeypatch.setenv("GEMINI_RATE_OUTPUT", "2.0")
        cost = estimate_cost(usage(fresh=1_000_000), "some-future-model")
        assert cost.total == pytest.approx(1.0)

    def test_invalid_override_falls_back_to_the_table(self, monkeypatch):
        monkeypatch.setenv("GEMINI_RATE_INPUT", "not-a-number")
        monkeypatch.setenv("GEMINI_RATE_OUTPUT", "4.0")
        cost = estimate_cost(usage(fresh=1_000_000), "gemini-3.7-flash", today=date(2026, 8, 17))
        assert cost.total == pytest.approx(0.75)


class TestUsd:
    @pytest.mark.parametrize(
        "amount,expected",
        [(0.0, "$0.00"), (0.0198, "$0.0198"), (0.9999, "$0.9999"), (1.0, "$1.00"), (1234.5, "$1,234.50")],
    )
    def test_formatting(self, amount, expected):
        assert usd(amount) == expected


class TestGemini38Flash:
    """3.8 shipped priced identically to 3.7, including the same promo end date."""

    def test_it_is_priced_and_not_reported_as_unknown(self):
        cost = estimate_cost(usage(fresh=1_000_000), "gemini-3.8-flash", today=date(2026, 8, 29))
        assert cost.rate is not None, "an unpriced model reports tokens and no cost"
        assert cost.total == pytest.approx(0.75)

    def test_the_promo_expires_with_the_year(self):
        after = estimate_cost(usage(fresh=1_000_000), "gemini-3.8-flash", today=date(2027, 1, 1))
        assert after.total == pytest.approx(1.50)


class TestGemini4Hydrogen:
    """Hydrogen has flat pricing ($1.50/$7.50) and 0.05x cached input (95% discount)."""

    def test_a_usage_record_is_priced_correctly(self):
        # 100k uncached in ($0.15), 900k cached in at 0.05x = $0.075/1M ($0.0675),
        # 2k response + 3k thinking out at $7.50/1M ($0.015 + $0.0225 = $0.0375) -> $0.255.
        record = {**usage(fresh=100_000, cached=900_000, output=2_000), "thoughts_tokens": 3_000}
        cost = estimate_cost(record, "gemini-4-hydrogen", today=date(2026, 11, 14))
        assert cost.rate is not None, "an unpriced model reports tokens and no cost"
        assert cost.uncached_input == pytest.approx(0.15)
        assert cost.cached_input == pytest.approx(0.0675)
        assert cost.thinking == pytest.approx(0.0225)
        assert cost.candidates_output == pytest.approx(0.015)
        assert cost.output == pytest.approx(0.0375)
        assert cost.total == pytest.approx(0.255)

    def test_has_no_introductory_promo_and_is_flat_across_dates(self):
        early = estimate_cost(usage(fresh=1_000_000, cached=1_000_000), "gemini-4-hydrogen", today=date(2026, 11, 14))
        later = estimate_cost(usage(fresh=1_000_000, cached=1_000_000), "gemini-4-hydrogen", today=date(2027, 2, 1))
        assert early.total == pytest.approx(1.50 + 0.075)
        assert later.total == pytest.approx(early.total)
        assert not any("introductory rate" in c for c in early.caveats)

    def test_vertex_model_path_resolves(self):
        vertex_id = "publishers/google/models/gemini-4-hydrogen"
        cost = estimate_cost(usage(output=1_000_000), vertex_id, today=date(2026, 11, 14))
        assert cost.total == pytest.approx(7.50)


class TestGemini4Argon:
    """Argon's promo ends a month before the Flash one, and its cache reads are 0.05x, not 0.1x."""

    def test_a_usage_record_is_priced_at_the_promo_rate(self):
        # 100k uncached in, 900k cached in, 2k response + 3k thinking out, at $2.00/$10.00.
        record = {**usage(fresh=100_000, cached=900_000, output=2_000), "thoughts_tokens": 3_000}
        cost = estimate_cost(record, "gemini-4-argon", today=date(2026, 10, 28))
        assert cost.rate is not None, "an unpriced model reports tokens and no cost"
        assert cost.uncached_input == pytest.approx(0.20)
        assert cost.cached_input == pytest.approx(0.09)  # 900k x $0.10, the published cached rate
        assert cost.thinking == pytest.approx(0.03)  # reasoning bills at the output rate
        assert cost.output == pytest.approx(0.05)
        assert cost.total == pytest.approx(0.34)

    def test_the_promo_ends_with_november(self):
        on = estimate_cost(usage(fresh=1_000_000, cached=1_000_000), "gemini-4-argon", today=date(2026, 11, 30))
        after = estimate_cost(usage(fresh=1_000_000, cached=1_000_000), "gemini-4-argon", today=date(2026, 12, 1))
        assert on.total == pytest.approx(2.00 + 0.10)
        assert after.total == pytest.approx(4.00 + 0.20)

    def test_vertex_model_path_resolves(self):
        vertex_id = "publishers/google/models/gemini-4-argon"
        cost = estimate_cost(usage(output=1_000_000), vertex_id, today=date(2026, 12, 1))
        assert cost.total == pytest.approx(20.00)


class TestGemini4Neon:
    """Neon has flat pricing ($3.00/$15.00) and 0.05x cached input (95% discount)."""

    def test_a_usage_record_is_priced_correctly(self):
        # 100k uncached in ($0.30), 900k cached in at 0.05x = $0.15/1M ($0.135),
        # 2k response + 3k thinking out at $15.00/1M ($0.03 + $0.045 = $0.075) -> $0.51.
        record = {**usage(fresh=100_000, cached=900_000, output=2_000), "thoughts_tokens": 3_000}
        cost = estimate_cost(record, "gemini-4-neon", today=date(2026, 11, 24))
        assert cost.rate is not None, "an unpriced model reports tokens and no cost"
        assert cost.uncached_input == pytest.approx(0.30)
        assert cost.cached_input == pytest.approx(0.135)
        assert cost.thinking == pytest.approx(0.045)
        assert cost.candidates_output == pytest.approx(0.03)
        assert cost.output == pytest.approx(0.075)
        assert cost.total == pytest.approx(0.51)

    def test_has_no_introductory_promo_and_is_flat_across_dates(self):
        early = estimate_cost(usage(fresh=1_000_000, cached=1_000_000), "gemini-4-neon", today=date(2026, 11, 24))
        later = estimate_cost(usage(fresh=1_000_000, cached=1_000_000), "gemini-4-neon", today=date(2027, 2, 1))
        assert early.total == pytest.approx(3.00 + 0.15)
        assert later.total == pytest.approx(early.total)
        assert not any("introductory rate" in c for c in early.caveats)

    def test_vertex_model_path_resolves(self):
        vertex_id = "publishers/google/models/gemini-4-neon"
        cost = estimate_cost(usage(output=1_000_000), vertex_id, today=date(2026, 11, 24))
        assert cost.total == pytest.approx(15.00)


class TestGemini4Carbon:
    """Carbon has promo ($2.00/$10.00) through Nov 30, 2026, then $4.00/$20.00, with 0.05x cached input."""

    def test_a_usage_record_is_priced_at_the_promo_rate(self):
        # 100k uncached in ($0.20), 900k cached in at 0.05x = $0.10/1M ($0.09),
        # 2k response + 3k thinking out at $10.00/1M ($0.02 + $0.03 = $0.05) -> $0.34.
        record = {**usage(fresh=100_000, cached=900_000, output=2_000), "thoughts_tokens": 3_000}
        cost = estimate_cost(record, "gemini-4-carbon", today=date(2026, 11, 24))
        assert cost.rate is not None, "an unpriced model reports tokens and no cost"
        assert cost.uncached_input == pytest.approx(0.20)
        assert cost.cached_input == pytest.approx(0.09)
        assert cost.thinking == pytest.approx(0.03)
        assert cost.candidates_output == pytest.approx(0.02)
        assert cost.output == pytest.approx(0.05)
        assert cost.total == pytest.approx(0.34)

    def test_the_promo_ends_with_november(self):
        on = estimate_cost(usage(fresh=1_000_000, cached=1_000_000), "gemini-4-carbon", today=date(2026, 11, 30))
        after = estimate_cost(usage(fresh=1_000_000, cached=1_000_000), "gemini-4-carbon", today=date(2026, 12, 1))
        assert on.total == pytest.approx(2.00 + 0.10)
        assert after.total == pytest.approx(4.00 + 0.20)

    def test_vertex_model_path_resolves(self):
        vertex_id = "publishers/google/models/gemini-4-carbon"
        cost = estimate_cost(usage(output=1_000_000), vertex_id, today=date(2026, 12, 1))
        assert cost.total == pytest.approx(20.00)


class TestGeminiDecision1:
    """Decision 1 bills $0.10/1M input tokens, $0.00 output tokens, and does not support caching."""

    def test_a_usage_record_is_priced_correctly(self):
        cost = estimate_cost(usage(fresh=1_000_000, output=500), "gemini-decision-1", today=date(2026, 11, 24))
        assert cost.rate is not None, "an unpriced model reports tokens and no cost"
        assert cost.uncached_input == pytest.approx(0.10)
        assert cost.output == pytest.approx(0.0)
        assert cost.total == pytest.approx(0.10)
        assert any("context caching not supported" in c.lower() for c in cost.caveats)

    def test_cached_multiplier_is_full_price_if_ever_passed(self):
        cost = estimate_cost(usage(cached=1_000_000), "gemini-decision-1", today=date(2026, 11, 24))
        assert cost.cached_input == pytest.approx(0.10)


class TestRateTable:
    def test_every_promo_is_cheaper_than_its_standard_rate(self):
        """A promo dearer than the standard rate would mean the two were swapped."""
        for key, rate in RATES.items():
            if rate.promo:
                assert rate.promo.input <= rate.input, key
                assert rate.promo.output <= rate.output, key

    def test_keys_are_already_normalised(self):
        for key in RATES:
            assert key == key.lower()
            assert "models/" not in key


class TestCacheReadMultiplier:
    """A rate that sets cache_read=0.0 means free cache reads, not "unset"."""

    def test_zero_cache_read_is_honoured_not_treated_as_missing(self, monkeypatch):
        from gemini_review import pricing

        free = Rate(input=1.0, output=2.0, label="Free cache reads", cache_read=0.0)
        monkeypatch.setitem(pricing.RATES, "free-cache-model", free)
        cost = estimate_cost(usage(cached=1_000_000), "free-cache-model")
        assert cost.cached_input == 0.0
        assert cost.total == 0.0

    def test_unset_cache_read_falls_back_to_the_default_multiple(self, monkeypatch):
        from gemini_review import pricing

        plain = Rate(input=1.0, output=2.0, label="No cache_read set")
        monkeypatch.setitem(pricing.RATES, "plain-model", plain)
        cost = estimate_cost(usage(cached=1_000_000), "plain-model")
        assert cost.cached_input == pytest.approx(0.1)

    def test_storage_caveat_is_hedged_not_asserted(self):
        """Some model families have a per-token-hour storage SKU and some do not."""
        cost = estimate_cost(usage(fresh=10, cached=10), "gemini-3.6-flash")
        storage = [c for c in cost.caveats if "STORAGE" in c]
        assert storage, "expected a storage caveat when cache was used"
        assert "where the model charges for it" in storage[0]
        assert "is billed per token-hour" not in storage[0]


class TestServiceTierAndVertexRegionalPricing:
    """Verify Flex 50% discount, fallback caveats, Vertex regional 1.10x uplift, and override precedence."""

    def test_flex_tier_halves_all_cost_components_on_hydrogen(self):
        std = estimate_cost(
            {
                **usage(fresh=100_000, cached=200_000, output=1_000),
                "thoughts_tokens": 2_000,
                "context_selection_prompt_tokens": 50_000,
                "context_selection_output_tokens": 500,
            },
            "gemini-4-hydrogen",
            today=date(2026, 11, 26),
        )
        flex = estimate_cost(
            {
                **usage(fresh=100_000, cached=200_000, output=1_000),
                "thoughts_tokens": 2_000,
                "context_selection_prompt_tokens": 50_000,
                "context_selection_output_tokens": 500,
                "service_tier": "flex",
            },
            "gemini-4-hydrogen",
            today=date(2026, 11, 26),
        )
        assert flex.uncached_input == pytest.approx(std.uncached_input * 0.5)
        assert flex.cached_input == pytest.approx(std.cached_input * 0.5)
        assert flex.output == pytest.approx(std.output * 0.5)
        assert flex.thinking == pytest.approx(std.thinking * 0.5)
        assert flex.candidates_output == pytest.approx(std.candidates_output * 0.5)
        assert flex.context_selection == pytest.approx(std.context_selection * 0.5)
        assert flex.total == pytest.approx(std.total * 0.5)
        assert any("50% discount" in c for c in flex.caveats)

    def test_flex_fallback_reports_standard_pricing_and_caveat(self):
        cost = estimate_cost(
            {**usage(fresh=100_000, output=1_000), "service_tier": "standard", "flex_fallback": True},
            "gemini-4-hydrogen",
            today=date(2026, 11, 26),
        )
        assert cost.uncached_input == pytest.approx(0.15)
        assert any("fell back to Standard tier" in c for c in cost.caveats)

    def test_vertex_regional_endpoint_applies_10_percent_uplift_while_global_stays_base(self):
        global_cost = estimate_cost(
            {**usage(fresh=1_000_000, cached=1_000_000, output=1_000_000), "vertex_location": "global"},
            "gemini-4-hydrogen",
            today=date(2026, 11, 26),
        )
        eu_cost = estimate_cost(
            {**usage(fresh=1_000_000, cached=1_000_000, output=1_000_000), "vertex_location": "eu"},
            "gemini-4-hydrogen",
            today=date(2026, 11, 26),
        )
        assert global_cost.total == pytest.approx(1.50 + 0.075 + 7.50)
        assert eu_cost.uncached_input == pytest.approx(global_cost.uncached_input * 1.10)
        assert eu_cost.cached_input == pytest.approx(global_cost.cached_input * 1.10)
        assert eu_cost.output == pytest.approx(global_cost.output * 1.10)
        assert eu_cost.total == pytest.approx(global_cost.total * 1.10)
        assert any("10% regional surcharge" in c and "eu" in c for c in eu_cost.caveats)

    def test_custom_rate_override_is_never_multiplied_by_flex_or_regional(self):
        cost = estimate_cost(
            {**usage(fresh=1_000_000), "service_tier": "flex", "vertex_location": "eu"},
            "gemini-4-hydrogen",
            config={"rate_input": 10.0, "rate_output": 20.0},
        )
        assert cost.total == pytest.approx(10.0)
