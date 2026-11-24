"""
Description: Configuration loader for Gemini PR review action.
Loads settings from gemini-review.toml or starter default configurations.
"""

import os
import sys
import tomllib

from google.genai import types

DEFAULT_TIMEOUT = 60
DEFAULT_MODEL = "gemini-4-hydrogen"


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


def _is_gemini_4(model: str | None) -> bool:
    """Return True if the normalised model identifier targets the Gemini 4 family."""
    if not model:
        return False
    return model.strip().lower().split("models/")[-1].startswith("gemini-4")


def build_thinking_config(
    thinking_level: str | int | None = None,
    model: str | None = None,
) -> types.ThinkingConfig | None:
    """Build a ThinkingConfig from a string level (e.g. 'low', 'medium', 'high', 'xhigh') or integer budget."""
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

    gemini_4 = _is_gemini_4(model)

    # Check if value is a numeric token budget
    if val.isdigit() or (val.startswith("-") and val[1:].isdigit()):
        try:
            budget = int(val)
            if budget == 0 and gemini_4:
                print(
                    f"Warning: thinking_level='{val}' is not supported by Gemini 4 model '{model}'; clamping to 'low'.",
                    file=sys.stderr,
                )
                return types.ThinkingConfig(thinking_level=types.ThinkingLevel.LOW)
            return types.ThinkingConfig(thinking_budget=budget)
        except ValueError:
            pass

    # Treat as thinking level string (e.g. 'minimal', 'low', 'medium', 'high', 'xhigh', 'off')
    level_str = val.lower()
    if level_str in ("off", "false", "disabled"):
        if gemini_4:
            print(
                f"Warning: thinking_level='{val}' is not supported by Gemini 4 model '{model}'; clamping to 'low'.",
                file=sys.stderr,
            )
            level_str = "low"
        else:
            return types.ThinkingConfig(thinking_budget=0)
    if level_str in ("none", "default"):
        return None
    if level_str == "minimal" and gemini_4:
        print(
            f"Warning: thinking_level='{val}' is not supported by Gemini 4 model '{model}'; clamping to 'low'.",
            file=sys.stderr,
        )
        level_str = "low"

    try:
        level_enum = getattr(types.ThinkingLevel, level_str.upper(), None)
        if level_enum is not None:
            return types.ThinkingConfig(thinking_level=level_enum)
        return types.ThinkingConfig(thinking_level=level_str)  # type: ignore[arg-type]
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


def _parse_path_list(raw: object, *, strip_leading_slash: bool = False) -> list[str]:
    """Parse a comma- or newline-separated path/glob setting into normalised POSIX strings."""
    if not raw:
        return []
    if isinstance(raw, (list, tuple)):
        items = [str(p).strip() for p in raw]
    else:
        items = [p.strip() for line in str(raw).splitlines() for p in line.split(",")]

    result: list[str] = []
    for item in items:
        norm = item.replace("\\", "/")
        while norm.startswith("./"):
            norm = norm[2:]
        if strip_leading_slash:
            norm = norm.lstrip("/")
            while norm.startswith("./"):
                norm = norm[2:]
        if norm:
            result.append(norm)
    return result


def get_extra_context_files(config: dict | None = None) -> list[str]:
    """Return explicit list of static extra context files from env or config."""
    raw = os.environ.get("GEMINI_EXTRA_CONTEXT_FILES")
    if not raw and config:
        raw = config.get("extra_context_files")
    return _parse_path_list(raw, strip_leading_slash=False)


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
    """Return exclusion glob patterns for codebase context filtering."""
    raw = os.environ.get("GEMINI_CONTEXT_EXCLUDE_PATTERNS")
    if not raw and config:
        raw = config.get("context_exclude_patterns")
    return _parse_path_list(raw, strip_leading_slash=True)


def get_diff_exclude_patterns(raw: str | list[str] | None = None) -> list[str]:
    """Return comma/newline-separated glob patterns excluded from PR diff and visual diff review."""
    value: object = os.environ.get("GEMINI_DIFF_EXCLUDE_PATTERNS") if raw is None else raw
    return _parse_path_list(value, strip_leading_slash=True)


def get_max_multimodal_images(config: dict | None = None) -> int:
    """Return maximum number of multimodal images to attach in review context."""
    default_val = 20
    env_val = os.environ.get("GEMINI_MAX_MULTIMODAL_IMAGES")
    if env_val:
        try:
            return int(env_val)
        except ValueError:
            pass
    if config:
        cfg_val = config.get("max_multimodal_images")
        if cfg_val is not None:
            try:
                return int(cfg_val)
            except ValueError:
                pass
    return default_val


def get_max_triage_images(config: dict | None = None) -> int:
    """Return maximum number of remote screenshots to attach during issue triage (default 5)."""
    default_val = 5
    env_val = os.environ.get("GEMINI_MAX_TRIAGE_IMAGES")
    if env_val:
        try:
            return int(env_val)
        except ValueError:
            pass
    if config:
        cfg_val = config.get("max_triage_images")
        if cfg_val is not None:
            try:
                return int(cfg_val)
            except ValueError:
                pass
    return default_val


def get_image_trigger_bytes(config: dict | None = None) -> int:
    """Return byte size threshold above which images are downscaled (default 600 KB)."""
    default_val = 614400
    env_val = os.environ.get("GEMINI_IMAGE_TRIGGER_BYTES")
    if env_val:
        try:
            return int(env_val)
        except ValueError:
            pass
    if config:
        cfg_val = config.get("image_trigger_bytes")
        if cfg_val is not None:
            try:
                return int(cfg_val)
            except ValueError:
                pass
    return default_val


def get_image_target_bytes(config: dict | None = None) -> int:
    """Return target byte size for downscaling large images (default 300 KB)."""
    default_val = 307200
    env_val = os.environ.get("GEMINI_IMAGE_TARGET_BYTES")
    if env_val:
        try:
            return int(env_val)
        except ValueError:
            pass
    if config:
        cfg_val = config.get("image_target_bytes")
        if cfg_val is not None:
            try:
                return int(cfg_val)
            except ValueError:
                pass
    return default_val
