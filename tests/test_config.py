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
