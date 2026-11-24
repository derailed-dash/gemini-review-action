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
