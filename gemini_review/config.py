"""
Description: Configuration loader for Gemini PR review action.
Loads settings from gemini-review.toml or starter default configurations.
"""

import os
import sys
import tomllib

DEFAULT_TIMEOUT = 60


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
