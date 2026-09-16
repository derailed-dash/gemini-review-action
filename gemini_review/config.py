"""
Description: Configuration loader for Gemini PR review action.
Loads settings from gemini-review.toml or starter default configurations.
"""

import os
import sys
import tomllib

from google.genai import types

DEFAULT_TIMEOUT = 60
DEFAULT_MODEL = "gemini-3.8-flash"


def get_default_model(explicit_model: str | None = None) -> str:
    """Return the default Gemini model configured via argument, environment, or fallback."""
    model = explicit_model or os.environ.get("GEMINI_MODEL") or os.environ.get("MODEL")
    return model.strip() if model and model.strip() else DEFAULT_MODEL


def load_config() -> dict:
    """Load configuration from gemini-review.toml."""
    path = ".github/commands/gemini-review.toml"
    if not os.path.exists(path):
        action_default_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "starter-examples", "gemini-review.toml"
        )
        if os.path.exists(action_default_path):
            path = action_default_path
        else:
            return {}

    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except Exception as e:
        print(f"Warning: Failed to load config from {path}: {e}", file=sys.stderr)
        return {}


def build_thinking_config(thinking_level: str | int | None = None) -> types.ThinkingConfig | None:
    """Build a ThinkingConfig from a string level (e.g. 'low', 'medium', 'high') or integer budget."""
    val = None
    if thinking_level is not None:
        if isinstance(thinking_level, str):
            val = thinking_level.strip()
        else:
            val = str(thinking_level).strip()

    if not val:
        val = os.environ.get("GEMINI_THINKING_LEVEL", "").strip()

    if not val:
        budget_env = os.environ.get("GEMINI_THINKING_BUDGET", "").strip()
        if budget_env:
            val = budget_env
        else:
            return None

    # Check if value is a numeric token budget
    if val.isdigit() or (val.startswith("-") and val[1:].isdigit()):
        try:
            budget = int(val)
            return types.ThinkingConfig(thinking_budget=budget)
        except ValueError:
            pass

    # Treat as thinking level string (e.g. 'minimal', 'low', 'medium', 'high')
    level_str = val.lower()
    try:
        return types.ThinkingConfig(thinking_level=level_str)
    except Exception as e:
        print(f"Warning: Failed to construct ThinkingConfig with thinking_level='{val}': {e}", file=sys.stderr)
        return None


def get_max_candidate_files(config: dict | None = None) -> int:
    """Maximum candidate files to consider during dynamic context selection (default: 500)."""
    default_val = 500
    env_val = os.environ.get("GEMINI_MAX_CANDIDATE_FILES")
    if env_val:
        try:
            return int(env_val)
        except ValueError:
            pass
    if config:
        cfg_val = config.get("max_candidate_files")
        if cfg_val is not None:
            try:
                return int(cfg_val)
            except ValueError:
                pass
    return default_val


def get_extra_context_files(config: dict | None = None) -> list[str]:
    """Return explicit list of static extra context files from env or config."""
    raw = os.environ.get("GEMINI_EXTRA_CONTEXT_FILES")
    if not raw and config:
        raw = config.get("extra_context_files")

    if not raw:
        return []

    if isinstance(raw, list):
        items = raw
    else:
        # Split by comma or newline
        items = [p.strip() for line in str(raw).splitlines() for p in line.split(",") if p.strip()]

    return [item.replace("\\", "/").removeprefix("./") for item in items if item]


def get_context_diff_directories_only(config: dict | None = None) -> bool:
    """Whether dynamic context candidates must be restricted to directories touched in diff."""
    env_val = os.environ.get("GEMINI_CONTEXT_DIFF_DIRECTORIES_ONLY", "").strip().lower()
    if env_val in ("true", "1", "yes"):
        return True
    if env_val in ("false", "0", "no"):
        return False
    if config:
        return bool(config.get("context_diff_directories_only", False))
    return False


def get_context_exclude_patterns(config: dict | None = None) -> list[str]:
    """Return exclusion glob patterns for dynamic context candidates."""
    raw = os.environ.get("GEMINI_CONTEXT_EXCLUDE_PATTERNS")
    if not raw and config:
        raw = config.get("context_exclude_patterns")

    if not raw:
        return []

    if isinstance(raw, list):
        items = raw
    else:
        items = [p.strip() for line in str(raw).splitlines() for p in line.split(",") if p.strip()]

    return [item.replace("\\", "/").removeprefix("./") for item in items if item]
