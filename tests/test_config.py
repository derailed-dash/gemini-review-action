"""
Description: Unit tests for configuration and model resolution.
Verifies that DEFAULT_MODEL is defined, get_default_model resolves
explicit arguments, environment variables (GEMINI_MODEL, MODEL),
and standard defaults in the expected order of precedence.
"""

from gemini_review.config import DEFAULT_MODEL, DEFAULT_TIMEOUT, get_default_model, load_config


class TestConfigAndModelResolution:
    def test_default_model_is_gemini_4_hydrogen(self):
        """Verify the repository default model is gemini-4-hydrogen."""
        assert DEFAULT_MODEL == "gemini-4-hydrogen"

    def test_default_timeout_is_positive(self):
        """Verify the default timeout constant is set."""
        assert DEFAULT_TIMEOUT == 60

    def test_get_default_model_fallback(self, monkeypatch):
        """When no argument or environment variables are set, fallback to DEFAULT_MODEL."""
        monkeypatch.delenv("GEMINI_MODEL", raising=False)
        monkeypatch.delenv("MODEL", raising=False)
        assert get_default_model() == DEFAULT_MODEL

    def test_get_default_model_explicit_arg_takes_highest_precedence(self, monkeypatch):
        """An explicitly supplied model name should override environment variables and default."""
        monkeypatch.setenv("GEMINI_MODEL", "gemini-custom-env")
        monkeypatch.setenv("MODEL", "gemini-legacy-env")
        assert get_default_model("gemini-explicit") == "gemini-explicit"

    def test_get_default_model_gemini_model_env(self, monkeypatch):
        """GEMINI_MODEL environment variable takes precedence over MODEL and DEFAULT_MODEL."""
        monkeypatch.setenv("GEMINI_MODEL", "gemini-3.7-flash")
        monkeypatch.setenv("MODEL", "gemini-legacy")
        assert get_default_model() == "gemini-3.7-flash"

    def test_get_default_model_model_env_fallback(self, monkeypatch):
        """MODEL environment variable is used if GEMINI_MODEL is unset."""
        monkeypatch.delenv("GEMINI_MODEL", raising=False)
        monkeypatch.setenv("MODEL", "gemini-legacy-env")
        assert get_default_model() == "gemini-legacy-env"

    def test_get_default_model_strips_whitespace(self):
        """Surrounding whitespace in explicit model argument should be trimmed."""
        assert get_default_model("  gemini-custom  ") == "gemini-custom"

    def test_get_default_model_whitespace_only_falls_back(self, monkeypatch):
        """Whitespace-only argument or environment variable should fall back to DEFAULT_MODEL."""
        monkeypatch.delenv("GEMINI_MODEL", raising=False)
        monkeypatch.delenv("MODEL", raising=False)
        assert get_default_model("   ") == DEFAULT_MODEL

        monkeypatch.setenv("GEMINI_MODEL", "   ")
        assert get_default_model() == DEFAULT_MODEL

        monkeypatch.delenv("GEMINI_MODEL", raising=False)
        monkeypatch.setenv("MODEL", "   \t\n ")
        assert get_default_model() == DEFAULT_MODEL

    def test_load_config_returns_dict(self):
        """load_config should return a dictionary from existing TOML or defaults."""
        config = load_config()
        assert isinstance(config, dict)

    def test_get_diff_exclude_patterns_parsing_and_normalisation(self, monkeypatch):
        """get_diff_exclude_patterns, get_context_exclude_patterns, and get_extra_context_files normalise cleanly."""
        from gemini_review.config import (
            get_context_exclude_patterns,
            get_diff_exclude_patterns,
            get_extra_context_files,
        )

        monkeypatch.delenv("GEMINI_DIFF_EXCLUDE_PATTERNS", raising=False)
        assert get_diff_exclude_patterns() == []

        monkeypatch.setenv("GEMINI_DIFF_EXCLUDE_PATTERNS", "  ./starter-examples\\skills/** , \n generated/** , ")
        assert get_diff_exclude_patterns() == ["starter-examples/skills/**", "generated/**"]
        assert get_diff_exclude_patterns("") == []
        assert get_diff_exclude_patterns("./") == []
        assert get_diff_exclude_patterns(["  ./dir/** ", "  ", "./", "/root/gen/**"]) == [
            "dir/**",
            "root/gen/**",
        ]

        assert get_context_exclude_patterns({"context_exclude_patterns": ["./", "  /vendor/**  "]}) == ["vendor/**"]
        assert get_extra_context_files({"extra_context_files": ["/tmp/abs.md", "  ./rel.md "]}) == [
            "/tmp/abs.md",
            "rel.md",
        ]

    def test_get_attach_markdown_images_defaults_and_validation(self, monkeypatch, capsys):
        """get_attach_markdown_images defaults to 'all', honours env/config, and validates choices."""
        from gemini_review.config import get_attach_markdown_images

        monkeypatch.delenv("GEMINI_ATTACH_MARKDOWN_IMAGES", raising=False)
        assert get_attach_markdown_images() == "all"
        assert get_attach_markdown_images({"attach_markdown_images": "CORE"}) == "core"

        monkeypatch.setenv("GEMINI_ATTACH_MARKDOWN_IMAGES", "  DiFf ")
        assert get_attach_markdown_images({"attach_markdown_images": "all"}) == "diff"

        monkeypatch.setenv("GEMINI_ATTACH_MARKDOWN_IMAGES", "none")
        assert get_attach_markdown_images() == "none"

        monkeypatch.setenv("GEMINI_ATTACH_MARKDOWN_IMAGES", "bogus")
        assert get_attach_markdown_images() == "all"
        err = capsys.readouterr().err
        assert "GEMINI_ATTACH_MARKDOWN_IMAGES" in err or "attach_markdown_images" in err

    def test_get_context_mode_defaults_and_validation(self, monkeypatch, capsys):
        """get_context_mode defaults to 'auto', honours env/config, and validates choices."""
        from gemini_review.config import get_context_mode

        monkeypatch.delenv("GEMINI_CONTEXT_MODE", raising=False)
        assert get_context_mode() == "auto"
        assert get_context_mode({"context_mode": "Sparse"}) == "sparse"

        monkeypatch.setenv("GEMINI_CONTEXT_MODE", " DIFF_ONLY ")
        assert get_context_mode({"context_mode": "full"}) == "diff_only"

        monkeypatch.setenv("GEMINI_CONTEXT_MODE", "full")
        assert get_context_mode() == "full"

        monkeypatch.setenv("GEMINI_CONTEXT_MODE", "invalid_mode")
        assert get_context_mode() == "auto"
        err = capsys.readouterr().err
        assert "invalid_mode" in err

    def test_get_max_context_bytes_defaults_and_overrides(self, monkeypatch):
        """get_max_context_bytes defaults to 1536000 and safely falls back on non-positive values."""
        from gemini_review.config import DEFAULT_MAX_CONTEXT_BYTES, get_max_context_bytes

        monkeypatch.delenv("GEMINI_MAX_CONTEXT_BYTES", raising=False)
        assert DEFAULT_MAX_CONTEXT_BYTES == 1500 * 1024
        assert get_max_context_bytes() == 1536000
        assert get_max_context_bytes({"max_context_bytes": "512000"}) == 512000

        monkeypatch.setenv("GEMINI_MAX_CONTEXT_BYTES", "2048000")
        assert get_max_context_bytes({"max_context_bytes": 100}) == 2048000

        monkeypatch.setenv("GEMINI_MAX_CONTEXT_BYTES", "0")
        assert get_max_context_bytes() == DEFAULT_MAX_CONTEXT_BYTES

        monkeypatch.setenv("GEMINI_MAX_CONTEXT_BYTES", "not-an-int")
        assert get_max_context_bytes() == DEFAULT_MAX_CONTEXT_BYTES

    def test_get_service_tier_defaults_and_validation(self, monkeypatch, capsys):
        """get_service_tier defaults to 'standard', accepts 'standard'|'flex', and validates invalid values."""
        from gemini_review.config import VALID_SERVICE_TIERS, get_service_tier

        assert VALID_SERVICE_TIERS == ("standard", "flex")
        monkeypatch.delenv("GEMINI_SERVICE_TIER", raising=False)
        assert get_service_tier() == "standard"
        assert get_service_tier({"service_tier": "FLEX"}) == "flex"

        monkeypatch.setenv("GEMINI_SERVICE_TIER", "  fLeX ")
        assert get_service_tier({"service_tier": "standard"}) == "flex"

        monkeypatch.setenv("GEMINI_SERVICE_TIER", "turbo")
        assert get_service_tier() == "standard"
        assert "turbo" in capsys.readouterr().err

    def test_get_service_tier_fallback_defaults_and_bool_parsing(self, monkeypatch):
        """get_service_tier_fallback defaults to True and respects env/config booleans."""
        from gemini_review.config import get_service_tier_fallback

        monkeypatch.delenv("GEMINI_SERVICE_TIER_FALLBACK", raising=False)
        assert get_service_tier_fallback() is True
        assert get_service_tier_fallback({"service_tier_fallback": False}) is False

        monkeypatch.setenv("GEMINI_SERVICE_TIER_FALLBACK", "false")
        assert get_service_tier_fallback({"service_tier_fallback": True}) is False

        monkeypatch.setenv("GEMINI_SERVICE_TIER_FALLBACK", "0")
        assert get_service_tier_fallback() is False

        monkeypatch.setenv("GEMINI_SERVICE_TIER_FALLBACK", "YES")
        assert get_service_tier_fallback({"service_tier_fallback": False}) is True

        monkeypatch.delenv("GEMINI_SERVICE_TIER_FALLBACK", raising=False)
        assert get_service_tier_fallback({"service_tier_fallback": None}) is True
        assert get_service_tier_fallback({"service_tier_fallback": "maybe"}) is True

    def test_get_media_resolution_defaults_env_and_validation(self, monkeypatch, capsys):
        """Media resolution resolvers default safely ('medium'/'high'), honour env > config, and forbid 'low'."""
        from gemini_review.config import (
            VALID_MEDIA_RESOLUTIONS,
            get_context_media_resolution,
            get_triage_media_resolution,
            get_visual_diff_media_resolution,
        )

        assert VALID_MEDIA_RESOLUTIONS == ("medium", "high")
        for var in (
            "GEMINI_CONTEXT_MEDIA_RESOLUTION",
            "GEMINI_VISUAL_DIFF_MEDIA_RESOLUTION",
            "GEMINI_TRIAGE_MEDIA_RESOLUTION",
        ):
            monkeypatch.delenv(var, raising=False)

        assert get_context_media_resolution() == "medium"
        assert get_visual_diff_media_resolution() == "high"
        assert get_triage_media_resolution() == "medium"

        assert get_context_media_resolution({"context_media_resolution": "HIGH"}) == "high"
        assert get_visual_diff_media_resolution({"visual_diff_media_resolution": "Medium"}) == "medium"
        assert get_triage_media_resolution({"triage_media_resolution": "HIGH"}) == "high"

        monkeypatch.setenv("GEMINI_CONTEXT_MEDIA_RESOLUTION", "  HiGh ")
        assert get_context_media_resolution({"context_media_resolution": "medium"}) == "high"

        monkeypatch.setenv("GEMINI_VISUAL_DIFF_MEDIA_RESOLUTION", " medium ")
        assert get_visual_diff_media_resolution({"visual_diff_media_resolution": "high"}) == "medium"

        monkeypatch.setenv("GEMINI_TRIAGE_MEDIA_RESOLUTION", " HIGH ")
        assert get_triage_media_resolution({"triage_media_resolution": "medium"}) == "high"

        # 'low', 'ultra_high', and unknown strings must warn and fall back to safe defaults (never 'low')
        for bad in ("low", "ultra_high", "bogus"):
            monkeypatch.setenv("GEMINI_CONTEXT_MEDIA_RESOLUTION", bad)
            monkeypatch.setenv("GEMINI_VISUAL_DIFF_MEDIA_RESOLUTION", bad)
            monkeypatch.setenv("GEMINI_TRIAGE_MEDIA_RESOLUTION", bad)
            assert get_context_media_resolution() == "medium"
            assert get_visual_diff_media_resolution() == "high"
            assert get_triage_media_resolution() == "medium"
            err = capsys.readouterr().err
            assert bad in err

    def test_action_yml_media_resolution_defaults_match_config(self):
        """Verify action.yml media_resolution input defaults stay aligned with gemini_review.config constants."""
        import re
        from pathlib import Path

        from gemini_review.config import (
            DEFAULT_CONTEXT_MEDIA_RESOLUTION,
            DEFAULT_TRIAGE_MEDIA_RESOLUTION,
            DEFAULT_VISUAL_DIFF_MEDIA_RESOLUTION,
        )

        action_yml = (Path(__file__).resolve().parent.parent / "action.yml").read_text(encoding="utf-8")
        expected = {
            "context_media_resolution": DEFAULT_CONTEXT_MEDIA_RESOLUTION,
            "visual_diff_media_resolution": DEFAULT_VISUAL_DIFF_MEDIA_RESOLUTION,
            "triage_media_resolution": DEFAULT_TRIAGE_MEDIA_RESOLUTION,
        }
        for key, default_val in expected.items():
            match = re.search(
                rf"^  {key}:\s*\n(?:    [^\n]+\n)*?    default:\s*['\"]?([^'\"\n]+)['\"]?\s*$",
                action_yml,
                re.MULTILINE,
            )
            assert match is not None, f"Missing default for {key} in action.yml"
            assert match.group(1).strip() == default_val

    def test_get_triage_engine_and_threshold(self, monkeypatch, capsys):
        """Triage engine and threshold resolvers validate inputs, honour env > config, and match action.yml."""
        import re
        from pathlib import Path

        import pytest

        from gemini_review.config import (
            DEFAULT_TRIAGE_ENGINE,
            DEFAULT_TRIAGE_THRESHOLD,
            VALID_TRIAGE_ENGINES,
            get_triage_engine,
            get_triage_threshold,
        )

        assert DEFAULT_TRIAGE_ENGINE == "auto"
        assert DEFAULT_TRIAGE_THRESHOLD == pytest.approx(0.70)
        assert VALID_TRIAGE_ENGINES == ("auto", "decisions", "generate_content")

        monkeypatch.delenv("GEMINI_TRIAGE_ENGINE", raising=False)
        monkeypatch.delenv("GEMINI_TRIAGE_THRESHOLD", raising=False)

        assert get_triage_engine() == "auto"
        assert get_triage_threshold() == pytest.approx(0.70)

        assert get_triage_engine({"triage_engine": "DECISIONS"}) == "decisions"
        assert get_triage_engine({"triage_engine": "generate_content"}) == "generate_content"
        assert get_triage_threshold({"triage_threshold": 0.85}) == pytest.approx(0.85)

        monkeypatch.setenv("GEMINI_TRIAGE_ENGINE", " decisions ")
        assert get_triage_engine({"triage_engine": "generate_content"}) == "decisions"

        monkeypatch.setenv("GEMINI_TRIAGE_THRESHOLD", " 0.80 ")
        assert get_triage_threshold({"triage_threshold": 0.50}) == pytest.approx(0.80)

        # Upper boundary 1.0 is valid; whitespace-only env falls through to config
        monkeypatch.setenv("GEMINI_TRIAGE_THRESHOLD", "   ")
        assert get_triage_threshold({"triage_threshold": 1.0}) == pytest.approx(1.0)

        monkeypatch.setenv("GEMINI_TRIAGE_ENGINE", "invalid_engine")
        assert get_triage_engine() == "auto"
        assert "invalid_engine" in capsys.readouterr().err

        for bad_thresh in ("0", "0.0", "-0.1", "1.5", "nan", "inf", "-inf", "abc"):
            monkeypatch.setenv("GEMINI_TRIAGE_THRESHOLD", bad_thresh)
            assert get_triage_threshold() == pytest.approx(0.70)
            assert bad_thresh in capsys.readouterr().err

        monkeypatch.delenv("GEMINI_TRIAGE_THRESHOLD", raising=False)
        for bool_thresh in (True, False):
            assert get_triage_threshold({"triage_threshold": bool_thresh}) == pytest.approx(0.70)
            assert str(bool_thresh) in capsys.readouterr().err

        action_yml = (Path(__file__).resolve().parent.parent / "action.yml").read_text(encoding="utf-8")
        for key, expected_default in (
            ("triage_engine", DEFAULT_TRIAGE_ENGINE),
            ("triage_threshold", f"{DEFAULT_TRIAGE_THRESHOLD:.2f}"),
        ):
            match = re.search(
                rf"^  {key}:\s*\n(?:    [^\n]+\n)*?    default:\s*['\"]?([^'\"\n]+)['\"]?\s*$",
                action_yml,
                re.MULTILINE,
            )
            assert match is not None, f"Missing default for {key} in action.yml"
            assert match.group(1).strip() == expected_default

    def test_get_enable_preflight_router_and_threshold(self, monkeypatch, capsys):
        """Pre-flight router toggle and threshold validate inputs and match action.yml."""
        import re
        from pathlib import Path

        import pytest

        from gemini_review.config import (
            DEFAULT_ENABLE_PREFLIGHT_ROUTER,
            DEFAULT_PREFLIGHT_CONFIDENCE_THRESHOLD,
            build_thinking_config,
            get_enable_preflight_router,
            get_preflight_confidence_threshold,
        )

        assert DEFAULT_ENABLE_PREFLIGHT_ROUTER is True
        assert DEFAULT_PREFLIGHT_CONFIDENCE_THRESHOLD == pytest.approx(0.75)

        monkeypatch.delenv("GEMINI_ENABLE_PREFLIGHT_ROUTER", raising=False)
        monkeypatch.delenv("GEMINI_PREFLIGHT_CONFIDENCE_THRESHOLD", raising=False)

        assert get_enable_preflight_router() is True
        assert get_enable_preflight_router({"enable_preflight_router": False}) is False
        monkeypatch.setenv("GEMINI_ENABLE_PREFLIGHT_ROUTER", "false")
        assert get_enable_preflight_router({"enable_preflight_router": True}) is False
        monkeypatch.setenv("GEMINI_ENABLE_PREFLIGHT_ROUTER", "1")
        assert get_enable_preflight_router() is True
        monkeypatch.setenv("GEMINI_ENABLE_PREFLIGHT_ROUTER", "maybe")
        assert get_enable_preflight_router() is True
        assert "maybe" in capsys.readouterr().err

        assert get_preflight_confidence_threshold() == pytest.approx(0.75)
        assert get_preflight_confidence_threshold({"preflight_confidence_threshold": 0.85}) == pytest.approx(0.85)
        monkeypatch.setenv("GEMINI_PREFLIGHT_CONFIDENCE_THRESHOLD", " 0.80 ")
        assert get_preflight_confidence_threshold({"preflight_confidence_threshold": 0.60}) == pytest.approx(0.80)

        for bad in ("0", "0.0", "-0.1", "1.5", "nan", "inf", "abc"):
            monkeypatch.setenv("GEMINI_PREFLIGHT_CONFIDENCE_THRESHOLD", bad)
            assert get_preflight_confidence_threshold() == pytest.approx(0.75)
            assert bad in capsys.readouterr().err

        monkeypatch.delenv("GEMINI_PREFLIGHT_CONFIDENCE_THRESHOLD", raising=False)
        assert get_preflight_confidence_threshold({"preflight_confidence_threshold": True}) == pytest.approx(0.75)

        # 'auto' thinking_level returns None (model default) when router does not resolve it
        assert build_thinking_config("auto", model="gemini-4-hydrogen") is None
        assert build_thinking_config("AUTO", model="gemini-4-neon") is None

        # get_thinking_level honours env > config and preserves falsy '0' / False
        from gemini_review.config import get_thinking_level

        monkeypatch.delenv("GEMINI_THINKING_LEVEL", raising=False)
        assert get_thinking_level() == ""
        assert get_thinking_level({"thinking_level": 0}) == "0"
        assert get_thinking_level({"thinking_level": False}) == "False"
        assert get_thinking_level({"thinking_level": "medium"}) == "medium"
        monkeypatch.setenv("GEMINI_THINKING_LEVEL", "auto")
        assert get_thinking_level({"thinking_level": "low"}) == "auto"

        action_yml = (Path(__file__).resolve().parent.parent / "action.yml").read_text(encoding="utf-8")
        for key, expected_default in (
            ("enable_preflight_router", "true"),
            ("preflight_confidence_threshold", f"{DEFAULT_PREFLIGHT_CONFIDENCE_THRESHOLD:.2f}"),
        ):
            match = re.search(
                rf"^  {key}:\s*\n(?:    [^\n]+\n)*?    default:\s*['\"]?([^'\"\n]+)['\"]?\s*$",
                action_yml,
                re.MULTILINE,
            )
            assert match is not None, f"Missing default for {key} in action.yml"
            assert match.group(1).strip() == expected_default
