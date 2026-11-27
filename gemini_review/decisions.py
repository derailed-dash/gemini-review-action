"""Shared Gemini Decisions API helper for calibrated prefill classification.

This module wraps ``client.decisions.create`` (``gemini-decision-1`` on ``v1beta``)
for fast, zero-output-token multi-question scoring over shared text and image inputs.
It enforces safe multimodal resolution floors (``medium`` or ``high``, never ``low``),
pre-flight token ceiling estimates (262,144 tokens), safety-block checks, and
cost/latency telemetry so both issue triage (Wave 1) and PR routing (Wave 2) can
share one well-tested implementation.
"""

from __future__ import annotations

import base64
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any

from gemini_review.pricing import estimate_cost

DECISIONS_MODEL = "gemini-decision-1"
DECISIONS_API_VERSION = "v1beta"
MAX_DECISIONS_QUESTIONS = 1000
MAX_TRIAGE_DECISIONS_LABELS = 990
DECISIONS_INPUT_TOKEN_LIMIT = 262_144

DECISIONS_SUPPORTED_IMAGE_MIMES = frozenset(
    {
        "image/png",
        "image/jpeg",
        "image/webp",
        "image/gif",
        "image/heic",
        "image/heif",
    }
)


class DecisionsBlockedError(RuntimeError):
    """Raised when the Decisions API blocks an input via prompt_feedback."""


@dataclass(frozen=True)
class DecisionsResult:
    """Structured result and telemetry from a single ``decisions.create`` call."""

    answers: dict[str, Any]
    input_tokens: int
    total_tokens: int
    cost_usd: float
    elapsed_ms: float
    model: str
    raw_response: Any


def _normalise_decision_image_resolution(resolution: str | None) -> str:
    """Normalise Decisions image resolution to 'medium' or 'high' (never 'low')."""
    cleaned = (resolution or "medium").strip().lower()
    if cleaned in ("medium", "high"):
        return cleaned
    print(
        f"Warning: Unsupported Decisions image resolution '{resolution}' (only 'medium' and 'high' are allowed). "
        "Using 'medium'.",
        file=sys.stderr,
    )
    return "medium"


def build_decision_input_blocks(
    text: str,
    multimodal_parts: Sequence[Any] | None = None,
    *,
    image_resolution: str = "medium",
) -> list[dict[str, Any]]:
    """Build Decisions API ``input`` blocks from text and multimodal ``types.Part`` images.

    Only supported image blocks (``ImageContent``) are attached by this helper; PDFs
    (which use ``DocumentContent`` in the Decisions schema) and unsupported formats
    (e.g. ``image/svg+xml``) are skipped with a warning. Image resolution is strictly
    constrained to ``medium`` or ``high`` (never ``low``).
    """
    safe_resolution = _normalise_decision_image_resolution(image_resolution)
    blocks: list[dict[str, Any]] = []
    if text:
        blocks.append({"type": "text", "text": text})

    for part in multimodal_parts or ():
        inline_data = getattr(part, "inline_data", None)
        if inline_data is None:
            continue
        raw_data = getattr(inline_data, "data", None)
        mime_type = (getattr(inline_data, "mime_type", None) or "").strip().lower()
        if not raw_data or mime_type not in DECISIONS_SUPPORTED_IMAGE_MIMES:
            print(
                f"Warning: Skipping Decisions multimodal part with unsupported or empty payload "
                f"(mime='{mime_type or 'unknown'}').",
                file=sys.stderr,
            )
            continue

        if isinstance(raw_data, (bytes, bytearray)):
            b64_data = base64.b64encode(bytes(raw_data)).decode("ascii")
        elif isinstance(raw_data, str):
            b64_data = raw_data
        else:
            continue

        if not b64_data:
            continue

        blocks.append(
            {
                "type": "image",
                "data": b64_data,
                "mime_type": mime_type,
                "resolution": safe_resolution,
            }
        )

    return blocks


def _approx_text_tokens(text: str) -> int:
    """Return ceiling token estimate (~4 chars/token) for a non-empty string."""
    return (len(text) + 3) // 4 if text else 0


def _approx_instructions_tokens(instructions: Any) -> int:
    """Estimate tokens for Decisions ``instructions`` (str, content block, or sequence)."""
    if isinstance(instructions, str):
        return _approx_text_tokens(instructions)
    if isinstance(instructions, Mapping):
        text = instructions.get("text")
        return _approx_text_tokens(text) if isinstance(text, str) else 0
    if isinstance(instructions, Sequence) and not isinstance(instructions, (str, bytes, bytearray)):
        return sum(_approx_instructions_tokens(item) for item in instructions)
    return 0


def estimate_decision_tokens(
    input_blocks: Sequence[Mapping[str, Any]] | str,
    questions: Mapping[str, Mapping[str, Any]],
) -> int:
    """Estimate combined input + question tokens for a Decisions API request."""
    total = 0

    if isinstance(input_blocks, str):
        total += _approx_text_tokens(input_blocks)
    else:
        for block in input_blocks:
            btype = str(block.get("type", "")).lower()
            if btype == "text":
                total += _approx_text_tokens(str(block.get("text", "")))
            elif btype == "image":
                res = str(block.get("resolution", "medium")).lower()
                total += 1120 if res == "high" else 560

    for q in questions.values():
        total += _approx_instructions_tokens(q.get("instructions"))
        criteria = q.get("criteria")
        if isinstance(criteria, Mapping):
            for c_key, c_val in criteria.items():
                if isinstance(c_key, str):
                    total += _approx_text_tokens(c_key)
                if isinstance(c_val, str):
                    total += _approx_text_tokens(c_val)
        elif isinstance(criteria, Sequence) and not isinstance(criteria, (str, bytes, bytearray)):
            for c_item in criteria:
                if isinstance(c_item, str):
                    total += _approx_text_tokens(c_item)

    return max(1, total)


def _safe_int(value: Any) -> int:
    """Return non-negative integer value or 0, rejecting bools and mock objects."""
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return 0


def extract_decision_usage(response: Any) -> tuple[int, int]:
    """Extract ``(input_tokens, total_tokens)`` safely from a ``CreateDecisionResponse``."""
    usage = getattr(response, "usage", None) if response is not None else None
    if usage is None:
        return 0, 0
    total_input = _safe_int(getattr(usage, "total_input_tokens", None))
    total_tokens = _safe_int(getattr(usage, "total_tokens", None))
    if total_input == 0 and total_tokens > 0:
        total_input = total_tokens
    return total_input, total_tokens


def _extract_block_reason(response: Any) -> tuple[str | None, str | None]:
    """Return ``(block_reason, block_reason_message)`` only when explicitly populated."""
    pf = getattr(response, "prompt_feedback", None) if response is not None else None
    if pf is None:
        return None, None

    raw_reason = getattr(pf, "block_reason", None)
    reason_str: str | None = None
    if isinstance(raw_reason, Enum):
        if isinstance(raw_reason.value, str) and raw_reason.value.strip():
            reason_str = raw_reason.value.strip()
    elif isinstance(raw_reason, str) and raw_reason.strip():
        reason_str = raw_reason.strip()

    if reason_str is None:
        return None, None

    raw_msg = getattr(pf, "block_reason_message", None)
    msg_str = raw_msg.strip() if isinstance(raw_msg, str) and raw_msg.strip() else None
    return reason_str, msg_str


def call_decisions(
    client: Any,
    *,
    input_blocks: list[dict[str, Any]] | str,
    questions: dict[str, dict[str, Any]],
    model: str = DECISIONS_MODEL,
    api_version: str = DECISIONS_API_VERSION,
) -> DecisionsResult:
    """Execute ``client.decisions.create`` and return calibrated answers with telemetry."""
    if not input_blocks or (isinstance(input_blocks, str) and not input_blocks.strip()):
        raise ValueError("Decisions API requires non-empty input.")
    if not questions:
        raise ValueError("Decisions API requires at least 1 question.")
    if len(questions) > MAX_DECISIONS_QUESTIONS:
        raise ValueError(
            f"Decisions API supports at most {MAX_DECISIONS_QUESTIONS:,} questions per call (got {len(questions):,})."
        )

    started = time.perf_counter()
    response = client.decisions.create(
        model=model,
        api_version=api_version,
        input=input_blocks,
        questions=questions,
    )
    elapsed_ms = (time.perf_counter() - started) * 1000.0

    block_reason, block_msg = _extract_block_reason(response)
    if block_reason is not None:
        detail = f": {block_msg}" if block_msg else ""
        raise DecisionsBlockedError(f"Decisions input blocked (block_reason={block_reason}{detail})")

    raw_answers = getattr(response, "answers", None)
    if not isinstance(raw_answers, Mapping) or len(raw_answers) == 0:
        raise RuntimeError("Decisions API returned no answers.")

    input_tokens, total_tokens = extract_decision_usage(response)
    cost = estimate_cost(
        {"fresh_tokens": input_tokens, "candidates_tokens": 0, "thoughts_tokens": 0},
        model=model,
    )

    return DecisionsResult(
        answers=dict(raw_answers),
        input_tokens=input_tokens,
        total_tokens=total_tokens,
        cost_usd=cost.total,
        elapsed_ms=elapsed_ms,
        model=model,
        raw_response=response,
    )
