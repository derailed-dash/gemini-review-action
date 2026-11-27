"""Unit tests for the Decisions API Wave 2 Pre-Flight PR Router (gemini_review/preflight.py)."""

from __future__ import annotations

import math

import pytest

from gemini_review.decisions import (
    DECISIONS_INPUT_TOKEN_LIMIT,
    DECISIONS_MODEL,
    MAX_DECISIONS_QUESTIONS,
    DecisionsBlockedError,
    build_decision_input_blocks,
)
from gemini_review.preflight import (
    DEFAULT_PREFLIGHT_MAX_SKILLS,
    DEV_DOCS_QUESTION_ID,
    PR_COMPLEXITY_QUESTION_ID,
    RISK_QUESTION_IDS,
    SKILL_QUESTION_PREFIX,
    PreflightRoutingResult,
    PreflightSkillMatch,
    build_preflight_diff_summary,
    build_preflight_questions,
    format_preloaded_skills_section,
    resolve_thinking_level_from_answers,
    run_preflight_router,
)


def _sample_files() -> tuple[list[dict], list[dict]]:
    patch_lines = "\n".join(f"+line {i}" for i in range(1, 61))
    text_files = [
        {
            "filename": "gemini_review/preflight.py",
            "status": "added",
            "additions": 60,
            "deletions": 0,
            "patch": f"@@ -0,0 +1,60 @@\n{patch_lines}",
        },
        {
            "filename": "README.md",
            "status": "modified",
            "additions": 3,
            "deletions": 1,
            "patch": "@@ -1,3 +1,5 @@\n-old\n+new",
        },
    ]
    image_files = [
        {"filename": "docs/diagram.png", "status": "modified"},
    ]
    return text_files, image_files


def _choice_answer(choice: str, confidence: float, probs: dict[str, float] | None = None):
    class _Ans:
        pass

    answer = _Ans()
    answer.type = "choice"
    answer.choice = choice
    answer.confidence = confidence
    answer.probabilities = probs if probs is not None else {choice: confidence}
    return answer


def _noul_answer(prob_true: float, confidence: float | None = None):
    class _Ans:
        pass

    answer = _Ans()
    answer.type = "noul"
    answer.noul = prob_true
    answer.confidence = confidence if confidence is not None else max(prob_true, 1.0 - prob_true)
    return answer


def test_build_preflight_diff_summary_truncates_and_caps() -> None:
    text_files, image_files = _sample_files()
    summary = build_preflight_diff_summary(
        text_files,
        image_files,
        max_lines_per_file=10,
    )
    assert "gemini_review/preflight.py" in summary
    assert "+60/-0" in summary
    assert "+line 9" in summary
    assert "+line 10" not in summary
    assert "truncated" in summary.lower()
    assert "docs/diagram.png" in summary

    massive_files = [
        {"filename": f"file_{i}.py", "status": "modified", "patch": "@@ -1 +1 @@\n+" + ("x" * 200)} for i in range(20)
    ]
    capped = build_preflight_diff_summary(massive_files, max_lines_per_file=10, max_total_chars=600)
    assert len(capped) <= 600

    # Empty inputs return "" (making run_preflight_router empty guard reachable)
    assert build_preflight_diff_summary([], []) == ""

    # Changed Text Files manifest + per-file char cap keeps later critical filenames visible
    wide_first = {"filename": "huge_line.json", "status": "modified", "patch": "+" + ("z" * 10_000)}
    critical_second = {"filename": "gemini_review/auth.py", "status": "modified", "patch": "+verify_jwt()"}
    wide_summary = build_preflight_diff_summary([wide_first, critical_second], max_chars_per_file=500)
    assert "Changed Text Files:\n- huge_line.json" in wide_summary
    assert "gemini_review/auth.py" in wide_summary
    assert "+verify_jwt()" in wide_summary
    assert "character-capped" in wide_summary


def test_build_preflight_questions_handles_whitespace_skill_name() -> None:
    questions, skill_map = build_preflight_questions(
        skills=[{"id": "custom/rule.md", "name": "   ", "description": ""}],
        include_dev_knowledge=False,
    )
    assert skill_map[f"{SKILL_QUESTION_PREFIX}0"]["name"] == "custom/rule.md"
    assert "custom/rule.md" in questions[f"{SKILL_QUESTION_PREFIX}0"]["instructions"]


def test_build_preflight_questions_matches_installed_sdk_schema() -> None:
    from google.genai._gaos.types.decisions import CreateDecisionRequest

    skills = [
        {"id": "builtin:bigquery-basics/SKILL.md", "name": "bigquery-basics", "description": "BigQuery SQL & jobs"},
        {"id": "custom/SKILL.md", "name": "custom-auth", "description": "Custom auth rules"},
    ]
    questions, skill_map = build_preflight_questions(skills=skills, include_dev_knowledge=True)

    assert PR_COMPLEXITY_QUESTION_ID in questions
    for risk_id in RISK_QUESTION_IDS:
        assert risk_id in questions
    assert DEV_DOCS_QUESTION_ID in questions
    assert f"{SKILL_QUESTION_PREFIX}0" in questions
    assert f"{SKILL_QUESTION_PREFIX}1" in questions
    assert skill_map[f"{SKILL_QUESTION_PREFIX}0"]["id"] == "builtin:bigquery-basics/SKILL.md"
    assert skill_map[f"{SKILL_QUESTION_PREFIX}1"]["name"] == "custom-auth"

    summary = build_preflight_diff_summary(*_sample_files())
    blocks = build_decision_input_blocks(summary)
    validated = CreateDecisionRequest.model_validate(
        {
            "model": DECISIONS_MODEL,
            "input": blocks,
            "questions": questions,
        }
    )
    assert PR_COMPLEXITY_QUESTION_ID in validated.questions
    assert validated.questions[PR_COMPLEXITY_QUESTION_ID].type == "choice"
    assert validated.questions[DEV_DOCS_QUESTION_ID].type == "noul"


def test_build_preflight_questions_omits_dev_docs_when_disabled() -> None:
    questions, skill_map = build_preflight_questions(skills=[], include_dev_knowledge=False)
    assert DEV_DOCS_QUESTION_ID not in questions
    assert skill_map == {}


def test_resolve_thinking_level_confidence_safe_matrix() -> None:
    # 1. Confident trivial + all risk flags confidently false (<= 0.25) -> low
    answers_trivial = {
        PR_COMPLEXITY_QUESTION_ID: _choice_answer(
            "trivial", 0.90, {"trivial": 0.90, "standard": 0.08, "complex": 0.02}
        ),
        RISK_QUESTION_IDS[0]: _noul_answer(0.05),
        RISK_QUESTION_IDS[1]: _noul_answer(0.10),
        RISK_QUESTION_IDS[2]: _noul_answer(0.12),
    }
    level, fallback, reasons = resolve_thinking_level_from_answers(answers_trivial, threshold=0.75)
    assert level == "low"
    assert fallback is False
    assert reasons == []

    # 2. Low-confidence trivial -> safe upward fallback to medium
    answers_uncertain_trivial = {
        PR_COMPLEXITY_QUESTION_ID: _choice_answer(
            "trivial", 0.60, {"trivial": 0.60, "standard": 0.35, "complex": 0.05}
        ),
        RISK_QUESTION_IDS[0]: _noul_answer(0.05),
        RISK_QUESTION_IDS[1]: _noul_answer(0.05),
        RISK_QUESTION_IDS[2]: _noul_answer(0.05),
    }
    level, fallback, _ = resolve_thinking_level_from_answers(answers_uncertain_trivial, threshold=0.75)
    assert level == "medium"
    assert fallback is True

    # 3. Trivial PR with an UNCERTAIN risk flag (0.40) -> cannot route to low; falls back upward
    answers_uncertain_risk = {
        PR_COMPLEXITY_QUESTION_ID: _choice_answer(
            "trivial", 0.88, {"trivial": 0.88, "standard": 0.10, "complex": 0.02}
        ),
        RISK_QUESTION_IDS[0]: _noul_answer(0.40),
        RISK_QUESTION_IDS[1]: _noul_answer(0.05),
        RISK_QUESTION_IDS[2]: _noul_answer(0.05),
    }
    level, fallback, _ = resolve_thinking_level_from_answers(answers_uncertain_risk, threshold=0.75)
    assert level == "medium"
    assert fallback is True

    # 4. Triggered risk flag overrides even trivial/standard PR complexity -> high
    answers_risky = {
        PR_COMPLEXITY_QUESTION_ID: _choice_answer(
            "standard", 0.85, {"trivial": 0.05, "standard": 0.85, "complex": 0.10}
        ),
        RISK_QUESTION_IDS[0]: _noul_answer(0.82),
        RISK_QUESTION_IDS[1]: _noul_answer(0.05),
        RISK_QUESTION_IDS[2]: _noul_answer(0.10),
    }
    level, fallback, reasons = resolve_thinking_level_from_answers(answers_risky, threshold=0.75)
    assert level == "high"
    assert fallback is False
    assert reasons == [RISK_QUESTION_IDS[0]]

    # 5. Confident standard -> medium
    answers_standard = {
        PR_COMPLEXITY_QUESTION_ID: _choice_answer(
            "standard", 0.85, {"trivial": 0.05, "standard": 0.85, "complex": 0.10}
        ),
        RISK_QUESTION_IDS[0]: _noul_answer(0.10),
        RISK_QUESTION_IDS[1]: _noul_answer(0.10),
        RISK_QUESTION_IDS[2]: _noul_answer(0.10),
    }
    level, fallback, _ = resolve_thinking_level_from_answers(answers_standard, threshold=0.75)
    assert level == "medium"
    assert fallback is False

    # 6. Uncertain standard leaning complex (probs['complex'] >= 1 - threshold = 0.25) -> upward to high
    answers_leaning_complex = {
        PR_COMPLEXITY_QUESTION_ID: _choice_answer(
            "standard", 0.62, {"trivial": 0.02, "standard": 0.62, "complex": 0.36}
        ),
        RISK_QUESTION_IDS[0]: _noul_answer(0.10),
        RISK_QUESTION_IDS[1]: _noul_answer(0.10),
        RISK_QUESTION_IDS[2]: _noul_answer(0.10),
    }
    level, fallback, _ = resolve_thinking_level_from_answers(answers_leaning_complex, threshold=0.75)
    assert level == "high"
    assert fallback is True

    # 7. Malformed / NaN / bool values fail safe upward to high
    bad_answers = {
        PR_COMPLEXITY_QUESTION_ID: _choice_answer("bogus", float("nan"), {}),
        RISK_QUESTION_IDS[0]: _noul_answer(True),  # type: ignore[arg-type]
    }
    level, fallback, _ = resolve_thinking_level_from_answers(bad_answers, threshold=0.75)
    assert level == "high"
    assert fallback is True

    # 8. Complex PR -> high, upward_fallback=False
    answers_complex = {
        PR_COMPLEXITY_QUESTION_ID: _choice_answer(
            "complex", 0.90, {"trivial": 0.02, "standard": 0.08, "complex": 0.90}
        ),
        RISK_QUESTION_IDS[0]: _noul_answer(0.05),
        RISK_QUESTION_IDS[1]: _noul_answer(0.05),
        RISK_QUESTION_IDS[2]: _noul_answer(0.05),
    }
    assert resolve_thinking_level_from_answers(answers_complex, threshold=0.75) == ("high", False, [])

    # 9. Standard PR with leaning risk (0.70) or missing risk answer must escalate upward to high
    #    even when threshold is raised to 0.90 (no threshold inversion!)
    for thresh in (0.75, 0.90):
        ans_std_risky = {
            PR_COMPLEXITY_QUESTION_ID: _choice_answer(
                "standard", 0.95, {"trivial": 0.01, "standard": 0.95, "complex": 0.04}
            ),
            RISK_QUESTION_IDS[0]: _noul_answer(0.70),
            RISK_QUESTION_IDS[1]: _noul_answer(0.05),
            RISK_QUESTION_IDS[2]: _noul_answer(0.05),
        }
        assert resolve_thinking_level_from_answers(ans_std_risky, threshold=thresh) == ("high", True, [])

    ans_std_missing_risk = {
        PR_COMPLEXITY_QUESTION_ID: _choice_answer(
            "standard", 0.90, {"trivial": 0.05, "standard": 0.90, "complex": 0.05}
        ),
    }
    assert resolve_thinking_level_from_answers(ans_std_missing_risk, threshold=0.75) == ("high", True, [])

    # 10. Uncertain trivial leaning complex (complex_prob >= 0.25) -> high, upward_fallback=True
    #     Also test Mapping-shaped answer payloads work identically to attribute objects
    ans_mapping_trivial_complex = {
        PR_COMPLEXITY_QUESTION_ID: {
            "choice": "trivial",
            "confidence": 0.55,
            "probabilities": {"trivial": 0.55, "standard": 0.15, "complex": 0.30},
        },
        RISK_QUESTION_IDS[0]: {"noul": 0.05},
        RISK_QUESTION_IDS[1]: {"noul": 0.05},
        RISK_QUESTION_IDS[2]: {"noul": 0.05},
    }
    assert resolve_thinking_level_from_answers(ans_mapping_trivial_complex, threshold=0.75) == ("high", True, [])

    # 11. Uncertain standard NOT leaning complex (complex_prob < 0.25) stays medium
    ans_std_uncertain_noncomplex = {
        PR_COMPLEXITY_QUESTION_ID: _choice_answer(
            "standard", 0.65, {"trivial": 0.30, "standard": 0.65, "complex": 0.05}
        ),
        RISK_QUESTION_IDS[0]: _noul_answer(0.05),
        RISK_QUESTION_IDS[1]: _noul_answer(0.05),
        RISK_QUESTION_IDS[2]: _noul_answer(0.05),
    }
    assert resolve_thinking_level_from_answers(ans_std_uncertain_noncomplex, threshold=0.75) == ("medium", False, [])


def test_format_preloaded_skills_section_skips_unreadable_empty_and_caps_large_skills() -> None:
    empty_section, empty_injected = format_preloaded_skills_section([])
    assert empty_section == ""
    assert empty_injected == []

    matches = [
        PreflightSkillMatch(id="builtin:a/SKILL.md", name="skill-a", description="A", probability=0.92),
        PreflightSkillMatch(id="bad/SKILL.md", name="skill-bad", description="Bad", probability=0.81),
        PreflightSkillMatch(id="blank/SKILL.md", name="skill-blank", description="Blank", probability=0.80),
    ]

    def _fake_read(skill_id: str) -> str:
        if skill_id == "bad/SKILL.md":
            return "Error: Skill 'bad/SKILL.md' not found."
        if skill_id == "blank/SKILL.md":
            return "   \n\t  "
        return "Rule line\n" * 400

    section, injected = format_preloaded_skills_section(matches, read_skill_fn=_fake_read, max_skill_bytes=200)
    assert len(injected) == 1
    assert injected[0].name == "skill-a"
    assert "### Pre-Loaded Matched Skills (Turn 2 Dynamic Injection)" in section
    assert "TRUNCATED by gemini-review" in section
    assert "skill-bad" not in section
    assert "skill-blank" not in section


def test_run_preflight_router_happy_path_gates_and_caps_skills(mocker, capsys) -> None:
    mock_client = mocker.Mock()
    skills = [{"id": f"builtin:s{i}/SKILL.md", "name": f"skill-{i}", "description": f"Skill {i}"} for i in range(5)]
    answers = {
        PR_COMPLEXITY_QUESTION_ID: _choice_answer(
            "standard", 0.88, {"trivial": 0.05, "standard": 0.88, "complex": 0.07}
        ),
        RISK_QUESTION_IDS[0]: _noul_answer(0.05),
        RISK_QUESTION_IDS[1]: _noul_answer(0.10),
        RISK_QUESTION_IDS[2]: _noul_answer(0.15),
        DEV_DOCS_QUESTION_ID: _noul_answer(0.08, confidence=0.92),
        f"{SKILL_QUESTION_PREFIX}0": _noul_answer(0.91),
        f"{SKILL_QUESTION_PREFIX}1": _noul_answer(0.76),
        f"{SKILL_QUESTION_PREFIX}2": _noul_answer(0.84),
        f"{SKILL_QUESTION_PREFIX}3": _noul_answer(0.95),
        f"{SKILL_QUESTION_PREFIX}4": _noul_answer(0.40),
    }
    mock_client.decisions.create.return_value = mocker.Mock(
        answers=answers,
        prompt_feedback=None,
        usage=mocker.Mock(total_input_tokens=620, total_tokens=620),
    )

    result = run_preflight_router(
        mock_client,
        text_files=_sample_files()[0],
        image_files=_sample_files()[1],
        skills=skills,
        include_dev_knowledge=True,
        threshold=0.75,
        use_vertexai=False,
    )

    assert isinstance(result, PreflightRoutingResult)
    assert result.complexity == "standard"
    assert result.thinking_level == "medium"
    assert result.upward_fallback_applied is False
    assert result.include_dev_knowledge is False
    assert math.isclose(result.dev_docs_probability or 0.0, 0.08)
    assert len(result.matched_skills) == DEFAULT_PREFLIGHT_MAX_SKILLS
    assert [s.name for s in result.matched_skills] == ["skill-3", "skill-0", "skill-2"]
    assert result.input_tokens == 620
    assert result.cost_usd == pytest.approx(620 / 1e6 * 0.10)

    err = capsys.readouterr().err
    assert "Pre-flight router" in err
    assert "thinking_level='medium'" in err


def test_run_preflight_router_keeps_dev_docs_when_uncertain(mocker) -> None:
    mock_client = mocker.Mock()
    mock_client.decisions.create.return_value = mocker.Mock(
        answers={
            PR_COMPLEXITY_QUESTION_ID: _choice_answer(
                "trivial", 0.90, {"trivial": 0.90, "standard": 0.08, "complex": 0.02}
            ),
            RISK_QUESTION_IDS[0]: _noul_answer(0.05),
            RISK_QUESTION_IDS[1]: _noul_answer(0.05),
            RISK_QUESTION_IDS[2]: _noul_answer(0.05),
            DEV_DOCS_QUESTION_ID: _noul_answer(0.42, confidence=0.58),
        },
        prompt_feedback=None,
        usage=mocker.Mock(total_input_tokens=400, total_tokens=400),
    )

    result = run_preflight_router(
        mock_client,
        text_files=_sample_files()[0],
        skills=[],
        include_dev_knowledge=True,
        threshold=0.75,
        use_vertexai=False,
    )
    assert result is not None
    assert result.include_dev_knowledge is True
    assert result.thinking_level == "low"


def test_run_preflight_router_bypasses_and_handles_failures(mocker, capsys) -> None:
    mock_client = mocker.Mock()

    # 0. Empty text & image files -> returns None cleanly with zero API calls
    assert run_preflight_router(mock_client, text_files=[], image_files=[], use_vertexai=False) is None
    mock_client.decisions.create.assert_not_called()

    # 1. Vertex AI bypass -> zero API calls
    res_vertex = run_preflight_router(
        mock_client,
        text_files=_sample_files()[0],
        skills=[],
        use_vertexai=True,
    )
    assert res_vertex is None
    mock_client.decisions.create.assert_not_called()
    assert "Vertex AI" in capsys.readouterr().err

    # 2. > MAX_DECISIONS_QUESTIONS guard
    many_skills = [{"id": f"s{i}", "name": f"s{i}", "description": "desc"} for i in range(MAX_DECISIONS_QUESTIONS)]
    res_q = run_preflight_router(
        mock_client,
        text_files=_sample_files()[0],
        skills=many_skills,
        include_dev_knowledge=True,
        use_vertexai=False,
    )
    assert res_q is None
    mock_client.decisions.create.assert_not_called()

    # 3. > DECISIONS_INPUT_TOKEN_LIMIT guard
    mocker.patch("gemini_review.preflight.estimate_decision_tokens", return_value=DECISIONS_INPUT_TOKEN_LIMIT + 1)
    res_tok = run_preflight_router(
        mock_client,
        text_files=_sample_files()[0],
        skills=[],
        use_vertexai=False,
    )
    assert res_tok is None
    mock_client.decisions.create.assert_not_called()
    mocker.stopall()

    # 4. DecisionsBlockedError & generic RuntimeError fall back to None cleanly
    mocker.patch("gemini_review.preflight.call_decisions", side_effect=DecisionsBlockedError("safety"))
    assert run_preflight_router(mock_client, text_files=_sample_files()[0], skills=[], use_vertexai=False) is None
    mocker.patch("gemini_review.preflight.call_decisions", side_effect=RuntimeError("boom"))
    assert run_preflight_router(mock_client, text_files=_sample_files()[0], skills=[], use_vertexai=False) is None


def test_run_preflight_router_dedupes_skills_honours_zero_max_skills_and_dev_docs_guards(mocker) -> None:
    mock_client = mocker.Mock()
    dup_skills = [
        {"id": "builtin:bq/SKILL.md", "name": "bigquery-basics", "description": "BQ"},
        {"id": "bq/SKILL.md", "name": "bigquery-basics", "description": "BQ"},
        {"id": "builtin:api/SKILL.md", "name": "gemini-api-dev", "description": "API"},
    ]
    mock_client.decisions.create.return_value = mocker.Mock(
        answers={
            PR_COMPLEXITY_QUESTION_ID: _choice_answer(
                "trivial", 0.90, {"trivial": 0.90, "standard": 0.08, "complex": 0.02}
            ),
            RISK_QUESTION_IDS[0]: _noul_answer(0.05),
            RISK_QUESTION_IDS[1]: _noul_answer(0.05),
            RISK_QUESTION_IDS[2]: _noul_answer(0.05),
            DEV_DOCS_QUESTION_ID: _noul_answer(0.55, confidence=0.55),
            f"{SKILL_QUESTION_PREFIX}0": _noul_answer(0.90),
            f"{SKILL_QUESTION_PREFIX}1": _noul_answer(0.90),
            f"{SKILL_QUESTION_PREFIX}2": _noul_answer(0.85),
        },
        prompt_feedback=None,
        usage=mocker.Mock(total_input_tokens=500, total_tokens=500),
    )

    # Deduplicates builtin+workspace skill names (prefers workspace bq/SKILL.md), and threshold=0.40 does NOT
    # gate off dev_docs when dev_docs_prob=0.55 (>= 0.5)
    res_dedup = run_preflight_router(
        mock_client,
        text_files=_sample_files()[0],
        skills=dup_skills,
        include_dev_knowledge=True,
        threshold=0.40,
        max_skills=3,
        use_vertexai=False,
    )
    assert res_dedup is not None
    assert res_dedup.include_dev_knowledge is True
    assert [s.id for s in res_dedup.matched_skills] == ["bq/SKILL.md", "builtin:api/SKILL.md"]

    # max_skills=0 returns [] rather than failing open
    res_zero = run_preflight_router(
        mock_client,
        text_files=_sample_files()[0],
        skills=dup_skills,
        include_dev_knowledge=False,
        threshold=0.75,
        max_skills=0,
        use_vertexai=False,
    )
    assert res_zero is not None
    assert res_zero.matched_skills == []
    assert res_zero.include_dev_knowledge is False
