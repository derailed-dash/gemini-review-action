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
