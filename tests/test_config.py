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
