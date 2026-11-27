"""Unit tests for the shared Gemini Decisions API helper (gemini_review/decisions.py)."""

from __future__ import annotations

import base64
from enum import Enum

import pytest
from google.genai import types

from gemini_review.decisions import (
    DECISIONS_API_VERSION,
    DECISIONS_MODEL,
    MAX_DECISIONS_QUESTIONS,
    DecisionsBlockedError,
    DecisionsResult,
    build_decision_input_blocks,
    call_decisions,
    estimate_decision_tokens,
    extract_decision_usage,
)


def test_build_decision_input_blocks_text_only() -> None:
    blocks = build_decision_input_blocks("Issue title and body")
    assert blocks == [{"type": "text", "text": "Issue title and body"}]


def test_build_decision_input_blocks_with_images_and_resolution(capsys) -> None:
    png_part = types.Part.from_bytes(data=b"png-bytes", mime_type="image/png")
    jpeg_part = types.Part.from_bytes(data=b"jpeg-bytes", mime_type="image/jpeg")

    med_blocks = build_decision_input_blocks("See screenshots", [png_part, jpeg_part])
    assert len(med_blocks) == 3
    assert med_blocks[0] == {"type": "text", "text": "See screenshots"}
    assert med_blocks[1] == {
        "type": "image",
        "data": base64.b64encode(b"png-bytes").decode("ascii"),
        "mime_type": "image/png",
        "resolution": "medium",
    }
    assert med_blocks[2] == {
        "type": "image",
        "data": base64.b64encode(b"jpeg-bytes").decode("ascii"),
        "mime_type": "image/jpeg",
        "resolution": "medium",
    }

    high_blocks = build_decision_input_blocks("Hi-res", [png_part], image_resolution="high")
    assert high_blocks[1]["resolution"] == "high"

    # Requesting 'low' or invalid resolution must warn and clamp to 'medium' (never emit 'low')
    for bad_res in ("low", "ultra_high", "invalid"):
        clamped = build_decision_input_blocks("Safe", [png_part], image_resolution=bad_res)
        assert clamped[1]["resolution"] == "medium"
        err = capsys.readouterr().err
        assert bad_res in err


def test_build_decision_input_blocks_skips_unsupported_mime_parts(capsys) -> None:
    pdf_part = types.Part.from_bytes(data=b"%PDF-1.4", mime_type="application/pdf")
    svg_part = types.Part.from_bytes(data=b"<svg></svg>", mime_type="image/svg+xml")
    webp_part = types.Part.from_bytes(data=b"webp-bytes", mime_type="image/webp")

    blocks = build_decision_input_blocks("Mixed", [pdf_part, svg_part, webp_part])
    assert len(blocks) == 2
    assert blocks[1]["mime_type"] == "image/webp"
    err = capsys.readouterr().err
    assert "application/pdf" in err
    assert "image/svg+xml" in err


def test_estimate_decision_tokens_counts_text_images_and_questions() -> None:
    blocks = [
        {"type": "text", "text": "a" * 400},  # 100 tokens
        {"type": "image", "data": "abc", "mime_type": "image/png", "resolution": "medium"},  # 560 tokens
        {"type": "image", "data": "def", "mime_type": "image/png", "resolution": "high"},  # 1120 tokens
    ]
    questions = {
        "q1": {
            "type": "noul",
            "instructions": "b" * 80,  # 20 tokens
            "criteria": {"true_description": "c" * 40, "false_description": "d" * 40},  # 4+10 + 5+10 = 29 tokens
        }
    }
    est = estimate_decision_tokens(blocks, questions)
    assert est == 100 + 560 + 1120 + 20 + 29


def test_estimate_decision_tokens_handles_choice_score_structured_instructions_and_short_text() -> None:
    # Plain string input (2 chars -> ceiling 1 token)
    # ChoiceQuestion: instructions dict + dict criteria with empty descriptions (keys still counted)
    # ScoreQuestion: list[dict] instructions + list[str] rubric criteria
    questions = {
        "router": {
            "type": "choice",
            "instructions": {"type": "text", "text": "Pick one"},  # 8 chars -> 2 tokens
            "criteria": {"low": "", "high": "Complex diff"},  # 3->1, 4->1, 12->3 => 5 tokens
        },
        "rubric": {
            "type": "score",
            "instructions": [{"type": "text", "text": "Rate"}, {"type": "text", "text": "risk"}],  # 1 + 1 = 2 tokens
            "criteria": ["none", "minor", "severe"],  # 1 + 2 + 2 = 5 tokens
        },
    }
    est = estimate_decision_tokens("hi", questions)
    assert est == 1 + (2 + 5) + (2 + 5)


def test_build_decision_input_blocks_handles_str_data_empty_bytes_and_empty_text(capsys) -> None:
    str_part = types.Part(inline_data=types.Blob(data=b"abc", mime_type="image/png"))
    # Simulate a custom object whose inline_data.data is already base64 string or empty bytes
    empty_part = types.Part.from_bytes(data=b"", mime_type="image/png")

    class _StrDataPart:
        class inline_data:  # noqa: N801
            data = "YWJj"
            mime_type = "image/jpeg"

    blocks = build_decision_input_blocks("", [str_part, empty_part, _StrDataPart()])
    assert len(blocks) == 2
    assert blocks[0]["mime_type"] == "image/png"
    assert blocks[1] == {
        "type": "image",
        "data": "YWJj",
        "mime_type": "image/jpeg",
        "resolution": "medium",
    }
    assert "empty payload" in capsys.readouterr().err


def test_extract_decision_usage_fallbacks_and_type_guards(mocker) -> None:
    assert extract_decision_usage(None) == (0, 0)
    assert extract_decision_usage(mocker.Mock(usage=None)) == (0, 0)

    # total_input_tokens=0 falls back to total_tokens
    resp_fallback = mocker.Mock(usage=mocker.Mock(total_input_tokens=0, total_tokens=420))
    assert extract_decision_usage(resp_fallback) == (420, 420)

    # bool, negative, and mock attributes are rejected safely
    resp_bad = mocker.Mock(usage=mocker.Mock(total_input_tokens=True, total_tokens=-5))
    assert extract_decision_usage(resp_bad) == (0, 0)
    resp_unconfigured = mocker.Mock(usage=mocker.Mock())
    assert extract_decision_usage(resp_unconfigured) == (0, 0)


def test_call_decisions_happy_path_and_mock_prompt_feedback(mocker) -> None:
    mock_client = mocker.Mock()
    # Intentionally leave prompt_feedback as an unconfigured Mock attribute to ensure it doesn't false-trigger
    mock_ans = mocker.Mock(noul=0.92, confidence=0.92, type="noul")
    mock_usage = mocker.Mock(total_input_tokens=1500, total_tokens=1500)
    mock_resp = mocker.Mock(answers={"label_0": mock_ans}, usage=mock_usage)
    mock_client.decisions.create.return_value = mock_resp

    input_blocks = [{"type": "text", "text": "Bug report"}]
    questions = {"label_0": {"type": "noul", "instructions": "Is this a bug?"}}

    result = call_decisions(mock_client, input_blocks=input_blocks, questions=questions)

    assert isinstance(result, DecisionsResult)
    mock_client.decisions.create.assert_called_once_with(
        model=DECISIONS_MODEL,
        api_version=DECISIONS_API_VERSION,
        input=input_blocks,
        questions=questions,
    )
    assert result.answers == {"label_0": mock_ans}
    assert result.input_tokens == 1500
    assert result.total_tokens == 1500
    assert result.cost_usd == pytest.approx(1500 / 1e6 * 0.10)
    assert result.elapsed_ms >= 0.0
    assert result.model == DECISIONS_MODEL

    # Explicit prompt_feedback=None should also succeed cleanly
    mock_resp_none = mocker.Mock(answers={"label_0": mock_ans}, prompt_feedback=None, usage=mock_usage)
    mock_client.decisions.create.return_value = mock_resp_none
    result2 = call_decisions(mock_client, input_blocks=input_blocks, questions=questions)
    assert result2.input_tokens == 1500


def test_call_decisions_raises_on_blocked_prompt_feedback(mocker) -> None:
    class _FakeBlockReason(Enum):
        SAFETY = "safety"

    mock_client = mocker.Mock()
    for reason in ("safety", _FakeBlockReason.SAFETY):
        mock_resp = mocker.Mock(
            answers=None,
            prompt_feedback=mocker.Mock(block_reason=reason, block_reason_message="Blocked by safety"),
        )
        mock_client.decisions.create.return_value = mock_resp
        with pytest.raises(DecisionsBlockedError, match="safety"):
            call_decisions(
                mock_client,
                input_blocks=[{"type": "text", "text": "bad"}],
                questions={"q1": {"type": "noul", "instructions": "test"}},
            )


def test_call_decisions_validates_inputs_questions_and_empty_answers(mocker) -> None:
    mock_client = mocker.Mock()

    for bad_input in ("", "   ", []):
        with pytest.raises(ValueError, match="non-empty input"):
            call_decisions(
                mock_client,
                input_blocks=bad_input,
                questions={"q1": {"type": "noul", "instructions": "x"}},
            )

    with pytest.raises(ValueError, match="at least 1"):
        call_decisions(mock_client, input_blocks="hi", questions={})

    too_many = {f"q_{i}": {"type": "noul", "instructions": "x"} for i in range(MAX_DECISIONS_QUESTIONS + 1)}
    with pytest.raises(ValueError, match="1000|1,000"):
        call_decisions(mock_client, input_blocks="hi", questions=too_many)

    # Exact MAX_DECISIONS_QUESTIONS (1,000) boundary must succeed
    at_limit = {f"q_{i}": {"type": "noul", "instructions": "x"} for i in range(MAX_DECISIONS_QUESTIONS)}
    mock_client.decisions.create.return_value = mocker.Mock(
        answers={"q_0": mocker.Mock(noul=0.8, confidence=0.8)},
        prompt_feedback=None,
        usage=mocker.Mock(total_input_tokens=100, total_tokens=100),
    )
    res = call_decisions(mock_client, input_blocks="hi", questions=at_limit)
    assert res.input_tokens == 100

    for empty_answers in (None, {}):
        mock_client.decisions.create.return_value = mocker.Mock(answers=empty_answers, prompt_feedback=None)
        with pytest.raises(RuntimeError, match="no answers|empty"):
            call_decisions(
                mock_client,
                input_blocks="hi",
                questions={"q1": {"type": "noul", "instructions": "x"}},
            )
