"""
Tests for thinking_level configuration, dynamic context candidate bounding,
static extra_context_files bypass, and comprehensive token/cost telemetry.
"""

from unittest.mock import MagicMock

import pytest

from gemini_review.config import build_thinking_config
from gemini_review.pricing import estimate_cost
from gemini_review.prompts import build_codebase_context, select_dynamic_context_files
from gemini_review.utils import (
    extract_import_references,
    is_text_file,
    rank_and_bound_candidates,
)

# ==============================================================================
# 1. Thinking Configuration Tests
# ==============================================================================


def test_build_thinking_config_empty():
    assert build_thinking_config(None) is None
    assert build_thinking_config("") is None
    assert build_thinking_config("   ") is None


def test_build_thinking_config_string_levels():
    cfg_low = build_thinking_config("low")
    assert cfg_low is not None
    assert str(cfg_low.thinking_level).upper().endswith("LOW")

    cfg_high = build_thinking_config("HIGH")
    assert cfg_high is not None
    assert str(cfg_high.thinking_level).upper().endswith("HIGH")

    cfg_minimal = build_thinking_config("minimal")
    assert cfg_minimal is not None
    assert str(cfg_minimal.thinking_level).upper().endswith("MINIMAL")


def test_build_thinking_config_numeric_budget():
    cfg_budget = build_thinking_config("2048")
    assert cfg_budget is not None
    assert cfg_budget.thinking_budget == 2048
    assert cfg_budget.thinking_level is None

    cfg_zero = build_thinking_config("0")
    assert cfg_zero is not None
    assert cfg_zero.thinking_budget == 0

    cfg_off = build_thinking_config("off")
    assert cfg_off is not None
    assert cfg_off.thinking_budget == 0
    assert cfg_off.thinking_level is None

    cfg_false = build_thinking_config("false")
    assert cfg_false is not None
    assert cfg_false.thinking_budget == 0

    cfg_disabled = build_thinking_config("disabled")
    assert cfg_disabled is not None
    assert cfg_disabled.thinking_budget == 0

    assert build_thinking_config("none") is None
    assert build_thinking_config("default") is None


# ==============================================================================
# 2. Expanded File Exclusions Tests
# ==============================================================================


@pytest.mark.parametrize(
    "filename",
    [
        "package-lock.json",
        "uv.lock",
        "pnpm-lock.yaml",
        "yarn.lock",
        "bun.lockb",
        "bun.lock",
        "Cargo.lock",
        "poetry.lock",
        "Pipfile.lock",
        "composer.lock",
        "Gemfile.lock",
        "go.sum",
        "packages.lock.json",
        "flake.lock",
        "pdm.lock",
        ".DS_Store",
        "Thumbs.db",
        "bundle.min.js",
        "styles.min.css",
        "app.js.map",
        "component.snap",
        "test.snapshot",
        "data.parquet",
        "weights.onnx",
        "model.pt",
        "font.otf",
        "video.mp4",
        "audio.mp3",
    ],
)
def test_is_text_file_excludes_lockfiles_and_artifacts(filename):
    assert is_text_file(filename) is False


def test_is_text_file_keeps_normal_source_files():
    assert is_text_file("src/main.py") is True
    assert is_text_file("index.ts") is True
    assert is_text_file("Cargo.toml") is True
    assert is_text_file("package.json") is True
    assert is_text_file("README.md") is True


# ==============================================================================
# 3. Static Import Extraction Tests
# ==============================================================================


def test_extract_import_references():
    files = [
        {
            "filename": "src/app.py",
            "patch": """
+import os
+import sys
+from services.auth import AuthService
+from .models import User
+import utils.helpers
""",
        },
        {
            "filename": "web/index.ts",
            "patch": """
+import { Button } from './components/Button';
+import api from '../utils/api';
+const config = require('./config.json');
""",
        },
        {
            "filename": "cmd/main.go",
            "patch": """
+import (
+    "fmt"
+    "github.com/myorg/myrepo/pkg/storage"
+)
""",
        },
        {
            "filename": "native/render.cpp",
            "patch": """
+#include "engine/renderer.h"
""",
        },
    ]

    refs = extract_import_references(files)
    # Check that key modules and file basenames were extracted
    assert any("AuthService" in r or "auth" in r for r in refs)
    assert any("models" in r or "User" in r for r in refs)
    assert any("Button" in r for r in refs)
    assert any("components/Button" in r for r in refs)
    assert any("api" in r for r in refs)
    assert any("utils/api" in r for r in refs)
    assert any("storage" in r for r in refs)
    assert any("renderer" in r for r in refs)
    assert any("engine/renderer" in r for r in refs)


# ==============================================================================
# 4. Candidate Ranking & Bounding Tests
# ==============================================================================


def test_rank_and_bound_candidates_bounds_limit():
    modified_files = [{"filename": "src/auth/login.py", "patch": "+from src.auth.tokens import create_token\n"}]
    # 100 candidate files in various dirs
    candidates = [f"src/other/file_{i}.py" for i in range(80)]
    candidates.extend([f"src/auth/file_{i}.py" for i in range(20)])
    candidates.append("src/auth/tokens.py")  # import match

    bounded = rank_and_bound_candidates(
        candidates,
        modified_files,
        max_candidates=10,
    )

    assert len(bounded) == 10
    # tokens.py (import match) and src/auth/* (same directory) must be prioritised
    assert "src/auth/tokens.py" in bounded
    assert any(f.startswith("src/auth/") for f in bounded)


def test_rank_and_bound_candidates_diff_dirs_only():
    modified_files = [{"filename": "src/billing/invoice.py", "patch": ""}]
    candidates = [
        "src/billing/calculator.py",
        "src/billing/tax.py",
        "src/unrelated/worker.py",
        "docs/readme.md",
    ]

    bounded = rank_and_bound_candidates(
        candidates,
        modified_files,
        max_candidates=50,
        diff_dirs_only=True,
    )

    assert "src/billing/calculator.py" in bounded
    assert "src/billing/tax.py" in bounded
    assert "src/unrelated/worker.py" not in bounded
    assert "docs/readme.md" not in bounded


# ==============================================================================
# 5. Static extra_context_files Bypass Tests
# ==============================================================================


def test_build_codebase_context_with_extra_context_files(mocker, tmp_path):
    # Create static files
    f1 = tmp_path / "extra_docs.md"
    f1.write_text("Detailed architecture notes.")
    f2 = tmp_path / "extra_schema.json"
    f2.write_text('{"type": "object"}')

    mocker.patch("gemini_review.prompts.get_all_repo_files", return_value=["main.py", "core.md", "helper.py"])
    mocker.patch("os.path.getsize", return_value=100000)
    mocker.patch("gemini_review.prompts.is_core_file", side_effect=lambda f, pats: f.endswith(".md"))

    mock_client = MagicMock()
    mock_select = mocker.patch("gemini_review.prompts.select_dynamic_context_files")

    config = {
        "context_files": [],
        "extra_context_files": f"{f1},{f2}",
        "max_context_bytes": 1000,
    }

    files = [{"filename": "main.py", "status": "modified", "patch": "diff"}]
    context = build_codebase_context(
        files,
        config,
        client=mock_client,
        model="gemini-3.7-flash",
    )

    # Dynamic selector should NOT be called when extra_context_files are provided
    mock_select.assert_not_called()
    assert "extra_docs.md" in context
    assert "Detailed architecture notes." in context
    assert "extra_schema.json" in context
    assert '{"type": "object"}' in context


def test_build_codebase_context_includes_agents_md_as_project_context(mocker):
    """AGENTS.md and GEMINI.md should always be included as project context when looking at diffs."""
    mocker.patch(
        "gemini_review.get_all_repo_files",
        return_value=["main.py", "AGENTS.md", "README.md", "GEMINI.md", "service.py"],
    )
    mocker.patch("os.path.getsize", return_value=5000)
    mocker.patch("gemini_review.get_file_content", side_effect=lambda f: f"# Content of {f}")
    mocker.patch("gemini_review.select_dynamic_context_files", return_value=([], ""))

    mock_client = MagicMock()
    files = [{"filename": "main.py", "status": "modified", "patch": "diff"}]

    # In full context mode
    context_full = build_codebase_context(
        files,
        {"max_context_bytes": 1000000},
        client=mock_client,
        model="gemini-3.7-flash",
    )
    assert "AGENTS.md" in context_full
    assert "# Content of AGENTS.md" in context_full
    assert "GEMINI.md" in context_full

    # In sparse context mode
    context_sparse = build_codebase_context(
        files,
        {"max_context_bytes": 1000, "max_core_context_bytes": 50000},
        client=mock_client,
        model="gemini-3.7-flash",
    )
    assert "AGENTS.md" in context_sparse
    assert "# Content of AGENTS.md" in context_sparse
    assert "GEMINI.md" in context_sparse


# ==============================================================================
# 6. Telemetry and Cost Calculation Tests (Thinking + Context Selection)
# ==============================================================================


def test_estimate_cost_includes_thinking_tokens():
    # Model gemini-3.7-flash: promo input=$0.75/1M, output=$3.75/1M
    usage = {
        "fresh_tokens": 100_000,
        "cached_tokens": 500_000,
        "candidates_tokens": 1_000,
        "thoughts_tokens": 9_000,  # 9k thinking tokens
    }

    cost = estimate_cost(usage, "gemini-3.7-flash")

    # Output tokens should be candidates (1,000) + thoughts (9,000) = 10,000 tokens
    # Output cost: 10,000 / 1e6 * 3.75 = 0.0375
    assert pytest.approx(cost.output, 0.0001) == 0.0375
    # Total cost should reflect the full output cost
    expected_total = cost.uncached_input + cost.cached_input + 0.0375
    assert pytest.approx(cost.total, 0.0001) == expected_total


def test_estimate_cost_includes_context_selection_tokens():
    usage = {
        "fresh_tokens": 10_000,
        "cached_tokens": 0,
        "candidates_tokens": 1_000,
        "thoughts_tokens": 0,
        "context_selection_tokens": 5_000,
        "context_selection_prompt_tokens": 4_500,
        "context_selection_candidates_tokens": 500,
        "context_selection_thoughts_tokens": 0,
    }

    cost = estimate_cost(usage, "gemini-3.7-flash")

    # Context selection input: 4,500 / 1e6 * 0.75 = 0.003375
    # Context selection output: 500 / 1e6 * 3.75 = 0.001875
    # Context selection cost = 0.00525
    assert hasattr(cost, "context_selection")
    assert pytest.approx(cost.context_selection, 0.0001) == 0.00525
    assert pytest.approx(cost.total, 0.0001) == (
        cost.uncached_input + cost.cached_input + cost.output + cost.context_selection
    )


# ==============================================================================
# 7. Resilient Thinking Fallback Tests
# ==============================================================================


def test_select_dynamic_context_files_retries_on_unsupported_thinking(mocker):
    """When a model rejects thinking_config, retry without thinking_config."""
    mock_client = MagicMock()

    # First call raises an exception indicating unsupported thinking
    error_resp = Exception("400 INVALID_ARGUMENT: thinking_level is not supported for this model")
    success_resp = MagicMock()
    success_resp.text = '{"selected_files": ["src/main.py"], "reasoning": "Core entrypoint"}'
    success_resp.usage_metadata.prompt_token_count = 100
    success_resp.usage_metadata.candidates_token_count = 20
    success_resp.usage_metadata.thoughts_token_count = 0
    success_resp.usage_metadata.total_token_count = 120

    mock_client.models.generate_content.side_effect = [error_resp, success_resp]

    files = [{"filename": "src/app.py", "patch": ""}]
    candidates = ["src/main.py", "src/helper.py"]

    selected, reasoning, usage_dict = select_dynamic_context_files(
        mock_client,
        "unsupported-model",
        files,
        candidates,
        thinking_level="low",
    )

    assert selected == ["src/main.py"]
    assert mock_client.models.generate_content.call_count == 2
    # Verify second call had thinking_config=None
    second_call_config = mock_client.models.generate_content.call_args_list[1].kwargs["config"]
    assert second_call_config.thinking_config is None
    assert usage_dict["prompt_tokens"] == 100


# ==============================================================================
# 8. Huge Monorepo Simulation Test
# ==============================================================================


def test_huge_monorepo_simulation(mocker, monkeypatch):
    """Simulate a large monorepo with 10,000 files across 50 packages.

    Verifies:
    1. Built-in exclusions automatically prune lockfiles, snapshots, and binaries.
    2. Candidate pool of thousands is efficiently bounded to max_candidate_files (500).
    3. Import references and directory proximity are prioritised at the top of the pool.
    4. Execution finishes in milliseconds without memory or token bloat.
    5. Diff-directories-only mode scopes candidates strictly to touched package directories.
    """
    import time

    # Generate 10,000 candidate repository paths across 50 packages
    total_repo_files = []
    for pkg_idx in range(50):
        pkg_dir = f"packages/pkg_{pkg_idx}"
        # Core docs per package
        total_repo_files.append(f"{pkg_dir}/README.md")
        total_repo_files.append(f"{pkg_dir}/package.json")
        # Standard noise / artifacts that must be filtered out by is_text_file
        total_repo_files.append(f"{pkg_dir}/pnpm-lock.yaml")
        total_repo_files.append(f"{pkg_dir}/dist/bundle.min.js")
        total_repo_files.append(f"{pkg_dir}/dist/bundle.js.map")
        total_repo_files.append(f"{pkg_dir}/tests/__snapshots__/suite.snap")
        total_repo_files.append(f"{pkg_dir}/models/weights.onnx")

        # Regular source files (193 per package -> 50 * 200 = 10,000 total)
        for f_idx in range(193):
            total_repo_files.append(f"{pkg_dir}/src/module_{f_idx}.ts")

    # Add specific files to test priority ranking
    total_repo_files.append("packages/pkg_12/src/service.ts")
    total_repo_files.append("packages/pkg_12/src/test_handler.ts")
    total_repo_files.append("packages/pkg_5/src/client.ts")

    # Modified files in the PR (diff touches pkg_12, imports service and client)
    modified_files = [
        {
            "filename": "packages/pkg_12/src/handler.ts",
            "patch": """
+import { Service } from './service';
+import { Client } from '../../pkg_5/src/client';
+export class Handler {}
""",
        }
    ]

    # Mock git / file system helpers
    mocker.patch("gemini_review.get_all_repo_files", return_value=total_repo_files)
    # Return file sizes such that total > 1.5MB to trigger Sparse Mode
    mocker.patch("os.path.getsize", return_value=5000)
    mocker.patch("os.path.isfile", return_value=True)
    mocker.patch("gemini_review.get_file_content", return_value="// mock content")

    captured_candidates = []

    def mock_select(client, model, files, candidate_files, **kwargs):
        captured_candidates.extend(candidate_files)
        return (
            ["packages/pkg_12/src/service.ts", "packages/pkg_5/src/client.ts"],
            "Selected imported dependencies.",
            {"prompt_tokens": 1200, "candidates_tokens": 40, "total_tokens": 1240},
        )

    mocker.patch("gemini_review.select_dynamic_context_files", side_effect=mock_select)

    mock_client = MagicMock()
    start_time = time.perf_counter()

    # 1. Run simulation with standard default bounds (max_candidate_files=500)
    monkeypatch.setenv("GEMINI_MAX_CANDIDATE_FILES", "500")
    telemetry = {}
    context = build_codebase_context(
        files=modified_files,
        config={"max_context_bytes": 1000},
        client=mock_client,
        model="gemini-3.8-flash",
        context_telemetry=telemetry,
    )

    elapsed_ms = (time.perf_counter() - start_time) * 1000

    # Ensure 10,000 files processed swiftly through full pipeline (tree generation + bounding < 3s)
    assert elapsed_ms < 3000, f"Monorepo processing took too long: {elapsed_ms:.2f}ms"

    # Also directly verify rank_and_bound_candidates on 10,000 files is sub-second (< 250ms)
    rank_start = time.perf_counter()
    sample_bounded = rank_and_bound_candidates(
        total_repo_files,
        modified_files,
        max_candidates=500,
    )
    rank_elapsed_ms = (time.perf_counter() - rank_start) * 1000
    assert rank_elapsed_ms < 250, f"Ranking took too long: {rank_elapsed_ms:.2f}ms"
    assert len(sample_bounded) == 500

    # Verify candidate list was bounded to exactly 500
    assert len(captured_candidates) == 500

    # Verify exclusions: lockfiles, minified files, snapshots, maps, binaries were excluded
    assert not any("pnpm-lock.yaml" in f for f in captured_candidates)
    assert not any(".min.js" in f for f in captured_candidates)
    assert not any(".js.map" in f for f in captured_candidates)
    assert not any(".snap" in f for f in captured_candidates)
    assert not any(".onnx" in f for f in captured_candidates)

    # Verify high-priority candidates are included in the bounded pool of 500
    assert "packages/pkg_12/src/service.ts" in captured_candidates
    assert "packages/pkg_12/src/test_handler.ts" in captured_candidates
    assert "packages/pkg_5/src/client.ts" in captured_candidates

    # Verify telemetry captures dynamic context selection tokens
    assert telemetry["prompt_tokens"] == 1200
    assert telemetry["candidates_tokens"] == 40
    assert "packages/pkg_12/src/service.ts" in context

    # 2. Test context_diff_directories_only=True on the monorepo
    captured_candidates.clear()
    monkeypatch.setenv("GEMINI_CONTEXT_DIFF_DIRECTORIES_ONLY", "true")
    telemetry = {}

    context = build_codebase_context(
        files=modified_files,
        config={"max_context_bytes": 1000},
        client=mock_client,
        model="gemini-3.8-flash",
        context_telemetry=telemetry,
    )

    # In diff_dirs_only mode, only files in packages/pkg_12/src should be candidates
    assert len(captured_candidates) > 0
    assert all(f.startswith("packages/pkg_12/src/") for f in captured_candidates)
    assert "packages/pkg_5/src/client.ts" not in captured_candidates
