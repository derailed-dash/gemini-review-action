"""Decisions API Wave 2 Pre-Flight PR Router (gemini-decision-1).

Classifies each Pull Request in a single calibrated prefill call before the main
review begins — without downloading or re-reading the full repository baseline.
It bundles three routing concerns into one sub-second ``decisions.create`` request:

1. **Dynamic ``thinking_level`` routing**: Evaluates PR complexity (``trivial``,
   ``standard``, ``complex``) alongside risk flags (security/auth, concurrency/state,
   architecture/schema) with confidence-safe upward fallback.
2. **Pre-flight skill selection**: Scores discovered action and workspace skills
   so top deduplicated matches are injected directly into Turn 2 (after static
   ``codebase_context``), eliminating multi-turn discovery round-trips without
   busting Gemini 4's prefix cache.
3. **Developer Knowledge tool gating**: Determines whether Google Developer
   Knowledge search tools are needed for the PR, leaving tools attached whenever
   uncertain.
"""

from __future__ import annotations

import math
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from gemini_review.budget import cap_file_content
from gemini_review.config import (
    DEFAULT_PREFLIGHT_CONFIDENCE_THRESHOLD,
    DEFAULT_PREFLIGHT_MAX_SKILLS,
)
from gemini_review.decisions import (
    DECISIONS_INPUT_TOKEN_LIMIT,
    MAX_DECISIONS_QUESTIONS,
    DecisionsBlockedError,
    build_decision_input_blocks,
    call_decisions,
    estimate_decision_tokens,
)
from gemini_review.skills import read_skill_instructions

DEFAULT_PREFLIGHT_DIFF_LINES_PER_FILE = 40
DEFAULT_PREFLIGHT_MAX_CHARS_PER_FILE = 4_000
DEFAULT_PREFLIGHT_MAX_SUMMARY_CHARS = 80_000
DEFAULT_PREFLIGHT_MAX_SKILL_BYTES = 32 * 1024

PR_COMPLEXITY_QUESTION_ID = "pr_complexity"
RISK_QUESTION_IDS: tuple[str, ...] = (
    "touches_security_or_auth",
    "touches_concurrency_or_state",
    "architectural_or_schema_change",
)
DEV_DOCS_QUESTION_ID = "needs_google_dev_docs"
SKILL_QUESTION_PREFIX = "skill_"

_RISK_QUESTION_SPECS: tuple[tuple[str, str, str, str], ...] = (
    (
        "touches_security_or_auth",
        "Does this Pull Request touch security boundaries, authentication, authorisation, secrets, "
        "token handling, path validation, sanitisation, or SSRF/injection defences?",
        "Modifies auth, tokens, permissions, secrets, sanitisation, URL validation, or path traversal guards.",
        "Does not alter security-sensitive code, authentication, authorisation, or input/path boundaries.",
    ),
    (
        "touches_concurrency_or_state",
        "Does this Pull Request touch concurrency, locks, threads, async workflows, retries, caching, "
        "or persistent/shared mutable state?",
        "Modifies locking, async/threading, retry/backoff loops, caches, or shared state transitions.",
        "Purely stateless, sequential, or isolated logic/documentation changes.",
    ),
    (
        "architectural_or_schema_change",
        "Does this Pull Request introduce a significant architectural change, new module, public API/action input, "
        "or data/schema/configuration contract change?",
        "Adds or modifies schemas, core architecture, API contracts, action inputs, or module boundaries.",
        "Limited to local refactors, bug fixes, tests, comments, or documentation without contract changes.",
    ),
)


@dataclass(frozen=True)
class PreflightSkillMatch:
    """A matched agent skill selected by the pre-flight router."""

    id: str
    name: str
    description: str
    probability: float


@dataclass(frozen=True)
class PreflightRoutingResult:
    """Structured decisions and telemetry from one pre-flight PR routing call."""

    complexity: str
    complexity_confidence: float
    risk_flags: dict[str, float]
    triggered_risk_flags: list[str]
    upward_fallback_applied: bool
    thinking_level: str
    include_dev_knowledge: bool
    dev_docs_probability: float | None
    matched_skills: list[PreflightSkillMatch]
    input_tokens: int
    total_tokens: int
    cost_usd: float
    elapsed_ms: float


def _field(obj: Any, name: str) -> Any:
    """Read an answer field safely from either a Mapping or an attribute-based SDK object."""
    if obj is None:
        return None
    if isinstance(obj, Mapping):
        return obj.get(name)
    return getattr(obj, name, None)


def _finite_prob(value: Any) -> float | None:
    """Return finite probability float in [0.0, 1.0], rejecting bools and NaN/inf."""
    if isinstance(value, bool):
        return None
    if not isinstance(value, (int, float)):
        return None
    val = float(value)
    if not math.isfinite(val):
        return None
    return min(1.0, max(0.0, val))


def build_preflight_diff_summary(
    text_files: Sequence[Mapping[str, Any]],
    image_files: Sequence[Mapping[str, Any]] | None = None,
    *,
    max_lines_per_file: int = DEFAULT_PREFLIGHT_DIFF_LINES_PER_FILE,
    max_total_chars: int = DEFAULT_PREFLIGHT_MAX_SUMMARY_CHARS,
    max_chars_per_file: int = DEFAULT_PREFLIGHT_MAX_CHARS_PER_FILE,
) -> str:
    """Build a compact PR metadata and truncated diff summary for pre-flight classification."""
    imgs = image_files or ()
    if not text_files and not imgs:
        return ""

    lines: list[str] = [f"Pull Request Pre-Flight Summary: {len(text_files)} text file(s), {len(imgs)} image file(s)."]

    if text_files:
        manifest = [f"- {f.get('filename', 'unknown')} ({f.get('status', 'modified')})" for f in text_files[:100]]
        if len(text_files) > 100:
            manifest.append(f"- ... (+{len(text_files) - 100} more text files)")
        lines.append("Changed Text Files:\n" + "\n".join(manifest))

    if imgs:
        img_entries = [f"- {img.get('filename', 'unknown')} ({img.get('status', 'modified')})" for img in imgs[:20]]
        if len(imgs) > 20:
            img_entries.append(f"- ... (+{len(imgs) - 20} more image files)")
        lines.append("Changed Images:\n" + "\n".join(img_entries))

    line_cap = max(1, max_lines_per_file)
    char_cap = max(256, max_chars_per_file)
    for file_info in text_files:
        filename = str(file_info.get("filename", "unknown"))
        status = str(file_info.get("status", "modified"))
        additions = file_info.get("additions")
        deletions = file_info.get("deletions")
        stat_str = (
            f" (+{additions}/-{deletions})"
            if isinstance(additions, int)
            and not isinstance(additions, bool)
            and isinstance(deletions, int)
            and not isinstance(deletions, bool)
            else ""
        )
        header = f"\n--- File: {filename} [{status}{stat_str}] ---"
        patch = file_info.get("patch")
        if isinstance(patch, str) and patch.strip():
            patch_lines = patch.splitlines()
            snippet = "\n".join(patch_lines[:line_cap])
            if len(snippet) > char_cap:
                snippet = snippet[:char_cap] + "\n... (file snippet character-capped)"
            elif len(patch_lines) > line_cap:
                snippet += f"\n... (truncated {len(patch_lines) - line_cap} remaining lines)"
            lines.append(f"{header}\n{snippet}")
        else:
            lines.append(f"{header}\n(no text patch attached)")

    summary = "\n".join(lines).strip()
    if max_total_chars > 0 and len(summary) > max_total_chars:
        suffix = "\n... (summary truncated for pre-flight budget)"
        keep = max(0, max_total_chars - len(suffix))
        summary = (summary[:keep] + suffix)[:max_total_chars]
    return summary


def build_preflight_questions(
    skills: Sequence[Mapping[str, str]] | None = None,
    *,
    include_dev_knowledge: bool = True,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, str]]]:
    """Construct the bundled Decisions API question dict and collision-safe skill index map."""
    questions: dict[str, dict[str, Any]] = {
        PR_COMPLEXITY_QUESTION_ID: {
            "type": "choice",
            "instructions": (
                "Classify the overall review complexity of this Pull Request diff based on code risk, "
                "logic density, cross-file impact, and subtle bug surface."
            ),
            "criteria": {
                "trivial": (
                    "Purely mechanical or low-risk changes: documentation/Markdown prose, typos, comments, "
                    "formatting, simple renames, or tiny isolated configuration/version tweaks."
                ),
                "standard": (
                    "Typical feature, bug-fix, refactor, or test changes within clear module boundaries "
                    "without deep concurrency, security, or cross-cutting architectural risk."
                ),
                "complex": (
                    "Large, multi-module, subtle, stateful, concurrent, security-sensitive, performance-critical, "
                    "or architectural/schema changes requiring deep multi-step reasoning."
                ),
            },
        }
    }

    for q_id, instructions, true_desc, false_desc in _RISK_QUESTION_SPECS:
        questions[q_id] = {
            "type": "noul",
            "instructions": instructions,
            "criteria": {
                "true_description": true_desc,
                "false_description": false_desc,
            },
        }

    if include_dev_knowledge:
        questions[DEV_DOCS_QUESTION_ID] = {
            "type": "noul",
            "instructions": (
                "Does reviewing this Pull Request benefit from consulting official Google Developer documentation "
                "(Google Cloud, Gemini API / google-genai SDK, ADK, Firebase, BigQuery, Cloud Run, GKE, or Android)?"
            ),
            "criteria": {
                "true_description": (
                    "PR uses, configures, or modifies Google Cloud, Gemini API, ADK, Firebase, or Google SDK code/docs."
                ),
                "false_description": (
                    "PR is unrelated to Google Cloud, Gemini APIs, or Google developer platforms/SDKs."
                ),
            },
        }

    skill_map: dict[str, dict[str, str]] = {}
    for idx, skill in enumerate(skills or ()):
        skill_id = str(skill.get("id", "")).strip()
        skill_name = str(skill.get("name") or "").strip() or skill_id
        skill_desc = str(skill.get("description", "")).strip()
        if not skill_id:
            continue
        q_id = f"{SKILL_QUESTION_PREFIX}{idx}"
        desc_text = skill_desc or skill_name
        questions[q_id] = {
            "type": "noul",
            "instructions": (
                f"Is the agent skill '{skill_name}' directly relevant to reviewing the files, technologies, "
                f"or tasks in this Pull Request diff? Skill summary: {desc_text}"
            ),
            "criteria": {
                "true_description": f"The PR directly involves domains or workflows covered by '{skill_name}'.",
                "false_description": f"The PR does not involve '{skill_name}'.",
            },
        }
        skill_map[q_id] = {"id": skill_id, "name": skill_name, "description": skill_desc}

    return questions, skill_map


def resolve_thinking_level_from_answers(
    answers: Mapping[str, Any],
    *,
    threshold: float = DEFAULT_PREFLIGHT_CONFIDENCE_THRESHOLD,
) -> tuple[str, bool, list[str]]:
    """Map complexity + risk answers to a confidence-safe ``thinking_level`` with upward fallback.

    Returns ``(thinking_level, upward_fallback_applied, triggered_risk_flags)``.
    Never routes downward to ``'low'`` when either complexity or any risk flag is uncertain,
    and escalates upward to ``'high'`` whenever any risk flag is missing or leans ``True``.
    """
    low_ceiling = min(0.5, max(0.0, 1.0 - threshold))
    risk_escalate_floor = min(threshold, 0.5)
    triggered_risks: list[str] = []
    leaning_or_missing_risk = False
    uncertain_risk = False

    for risk_id in RISK_QUESTION_IDS:
        answer = answers.get(risk_id)
        prob = _finite_prob(_field(answer, "noul"))
        if prob is None:
            uncertain_risk = True
            leaning_or_missing_risk = True
            continue
        if prob >= threshold:
            triggered_risks.append(risk_id)
        elif prob >= risk_escalate_floor:
            uncertain_risk = True
            leaning_or_missing_risk = True
        elif prob > low_ceiling:
            uncertain_risk = True

    if triggered_risks:
        return "high", False, triggered_risks
    if leaning_or_missing_risk:
        return "high", True, triggered_risks

    comp_ans = answers.get(PR_COMPLEXITY_QUESTION_ID)
    raw_choice = _field(comp_ans, "choice")
    complexity = raw_choice.strip().lower() if isinstance(raw_choice, str) else ""
    comp_conf = _finite_prob(_field(comp_ans, "confidence"))
    raw_probs = _field(comp_ans, "probabilities")
    complex_prob = (
        _finite_prob(raw_probs.get("complex")) if isinstance(raw_probs, Mapping) and "complex" in raw_probs else None
    )

    if complexity not in ("trivial", "standard", "complex") or comp_conf is None:
        return "high", True, triggered_risks

    if complexity == "complex":
        return "high", False, triggered_risks

    if complexity == "standard":
        if comp_conf < threshold and (complex_prob is None or complex_prob >= low_ceiling):
            return "high", True, triggered_risks
        return "medium", False, triggered_risks

    # complexity == "trivial": only allow 'low' when BOTH complexity and every risk flag are confident
    if comp_conf >= threshold and not uncertain_risk:
        return "low", False, triggered_risks

    if complex_prob is not None and complex_prob >= low_ceiling:
        return "high", True, triggered_risks
    return "medium", True, triggered_risks


def format_preloaded_skills_section(
    matched_skills: Sequence[PreflightSkillMatch],
    *,
    read_skill_fn: Callable[[str], str] = read_skill_instructions,
    max_skill_bytes: int = DEFAULT_PREFLIGHT_MAX_SKILL_BYTES,
) -> tuple[str, list[PreflightSkillMatch]]:
    """Load and size-cap matched skill instructions for Turn 2 injection, skipping unreadable/empty entries."""
    if not matched_skills:
        return "", []

    blocks: list[str] = []
    injected: list[PreflightSkillMatch] = []
    for match in matched_skills:
        content = read_skill_fn(match.id)
        if not content or not content.strip() or content.startswith("Error:"):
            print(
                f"Notice: Skipping pre-loaded skill '{match.id}' ({content.strip() if content else 'empty content'}).",
                file=sys.stderr,
            )
            continue
        bounded_content, was_capped = cap_file_content(content.strip(), match.id, max_skill_bytes)
        if was_capped:
            print(
                f"Notice: Pre-loaded skill '{match.id}' capped to {max_skill_bytes:,} bytes for Turn 2 budget safety.",
                file=sys.stderr,
            )
        blocks.append(f"#### Skill: `{match.name}` (`{match.id}`, match={match.probability:.2f})\n{bounded_content}")
        injected.append(match)

    if not blocks:
        return "", []

    header = (
        "### Pre-Loaded Matched Skills (Turn 2 Dynamic Injection)\n"
        "The following relevant skill instructions were pre-loaded for this PR by the pre-flight router. "
        "Apply them directly without re-calling `list_available_skills` or `load_skill_instructions` for these skills "
        "(only call those tools if you discover an additional unattached skill domain)."
    )
    return f"{header}\n\n" + "\n\n".join(blocks), injected


def run_preflight_router(
    client: Any,
    *,
    text_files: Sequence[Mapping[str, Any]],
    image_files: Sequence[Mapping[str, Any]] | None = None,
    skills: Sequence[Mapping[str, str]] | None = None,
    include_dev_knowledge: bool = True,
    threshold: float = DEFAULT_PREFLIGHT_CONFIDENCE_THRESHOLD,
    max_skills: int = DEFAULT_PREFLIGHT_MAX_SKILLS,
    use_vertexai: bool = False,
) -> PreflightRoutingResult | None:
    """Execute one Decisions API pre-flight classification call and return routing decisions.

    Returns ``None`` on Vertex AI / Enterprise clients, quota/token limit breaches, empty diffs,
    or API/safety errors so callers cleanly fall back to baseline behaviour.
    """
    if use_vertexai:
        print(
            "Pre-flight router: skipped on Vertex AI / Enterprise (gemini-decision-1 requires Gemini Developer API).",
            file=sys.stderr,
        )
        return None

    diff_summary = build_preflight_diff_summary(text_files, image_files)
    if not diff_summary:
        return None

    questions, skill_map = build_preflight_questions(
        skills=skills,
        include_dev_knowledge=include_dev_knowledge,
    )
    if len(questions) > MAX_DECISIONS_QUESTIONS:
        print(
            f"Warning: Pre-flight router question count ({len(questions):,}) exceeds limit "
            f"({MAX_DECISIONS_QUESTIONS:,}). Falling back to default settings.",
            file=sys.stderr,
        )
        return None

    input_blocks = build_decision_input_blocks(diff_summary)
    estimated_tokens = estimate_decision_tokens(input_blocks, questions)
    if estimated_tokens > DECISIONS_INPUT_TOKEN_LIMIT:
        print(
            f"Warning: Pre-flight router estimated input ({estimated_tokens:,} tokens) exceeds "
            f"limit ({DECISIONS_INPUT_TOKEN_LIMIT:,}). Falling back to default settings.",
            file=sys.stderr,
        )
        return None

    try:
        dec_result = call_decisions(client, input_blocks=input_blocks, questions=questions)
    except DecisionsBlockedError as blocked_err:
        print(
            f"Warning: Pre-flight router blocked by Decisions API ({blocked_err}). Using default settings.",
            file=sys.stderr,
        )
        return None
    except Exception as err:
        print(
            f"Warning: Pre-flight router failed ({err}). Using default settings.",
            file=sys.stderr,
        )
        return None

    answers = dec_result.answers
    thinking_level, upward_fallback, triggered_risks = resolve_thinking_level_from_answers(
        answers,
        threshold=threshold,
    )

    comp_ans = answers.get(PR_COMPLEXITY_QUESTION_ID)
    raw_choice = _field(comp_ans, "choice")
    complexity = raw_choice.strip().lower() if isinstance(raw_choice, str) and raw_choice.strip() else "unknown"
    comp_conf = _finite_prob(_field(comp_ans, "confidence"))

    risk_flags: dict[str, float] = {}
    for risk_id in RISK_QUESTION_IDS:
        answer = answers.get(risk_id)
        prob = _finite_prob(_field(answer, "noul"))
        if prob is not None:
            risk_flags[risk_id] = prob

    # Safe upward fallback for Dev Knowledge tools: only gate off when confidently false (< 0.5 and <= 1 - threshold)
    routed_dev_docs = include_dev_knowledge
    dev_docs_prob: float | None = None
    if include_dev_knowledge:
        dev_ans = answers.get(DEV_DOCS_QUESTION_ID)
        dev_docs_prob = _finite_prob(_field(dev_ans, "noul"))
        dev_conf = _finite_prob(_field(dev_ans, "confidence"))
        low_ceiling = min(0.5, max(0.0, 1.0 - threshold))
        if (
            dev_docs_prob is not None
            and dev_docs_prob < 0.5
            and dev_docs_prob <= low_ceiling
            and (dev_conf is None or dev_conf >= threshold)
        ):
            routed_dev_docs = False

    candidate_skills: list[PreflightSkillMatch] = []
    for q_id, meta in skill_map.items():
        answer = answers.get(q_id)
        prob = _finite_prob(_field(answer, "noul"))
        if prob is not None and prob >= threshold:
            candidate_skills.append(
                PreflightSkillMatch(
                    id=meta["id"],
                    name=meta["name"],
                    description=meta.get("description", ""),
                    probability=prob,
                )
            )
    # Sort descending by probability; prefer workspace (.agents/skills/) over builtin: on ties, then deduplicate by name
    candidate_skills.sort(key=lambda item: (-item.probability, item.id.startswith("builtin:"), item.id))
    deduped_skills: list[PreflightSkillMatch] = []
    seen_skill_names: set[str] = set()
    for skill_match in candidate_skills:
        norm_name = skill_match.name.strip().lower() or skill_match.id.lower()
        if norm_name in seen_skill_names:
            continue
        seen_skill_names.add(norm_name)
        deduped_skills.append(skill_match)
    matched_skills = deduped_skills[: max(0, max_skills)]

    skill_desc = ", ".join(f"{s.name} ({s.probability:.2f})" for s in matched_skills) if matched_skills else "none"
    risk_desc = ", ".join(triggered_risks) if triggered_risks else "none"
    dev_desc = (
        f"{'attached' if routed_dev_docs else 'gated-off'}"
        f"{f' (P={dev_docs_prob:.2f})' if dev_docs_prob is not None else ''}"
    )
    fallback_note = " [upward-fallback]" if upward_fallback else ""
    print(
        f"Pre-flight router (gemini-decision-1): complexity='{complexity}' "
        f"(conf={comp_conf if comp_conf is not None else 'n/a'}), "
        f"thinking_level='{thinking_level}'{fallback_note}, risks=[{risk_desc}], "
        f"dev_docs={dev_desc}, matched_skills=[{skill_desc}], "
        f"tokens={dec_result.input_tokens:,}, cost=${dec_result.cost_usd:.6f}, "
        f"latency={dec_result.elapsed_ms:.1f}ms.",
        file=sys.stderr,
    )

    return PreflightRoutingResult(
        complexity=complexity,
        complexity_confidence=comp_conf if comp_conf is not None else 0.0,
        risk_flags=risk_flags,
        triggered_risk_flags=triggered_risks,
        upward_fallback_applied=upward_fallback,
        thinking_level=thinking_level,
        include_dev_knowledge=routed_dev_docs,
        dev_docs_probability=dev_docs_prob,
        matched_skills=matched_skills,
        input_tokens=dec_result.input_tokens,
        total_tokens=dec_result.total_tokens,
        cost_usd=dec_result.cost_usd,
        elapsed_ms=dec_result.elapsed_ms,
    )
