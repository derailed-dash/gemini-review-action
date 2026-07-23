"""
Description: Pydantic schemas for structured Gemini PR review responses.
Provides Pydantic data schemas for line-specific inline comments and top-level review summaries.
"""

from pydantic import BaseModel, Field


class InlineComment(BaseModel):
    """Represents a single inline comment to be posted on a file in the Pull Request."""

    path: str = Field(description="The relative file path being reviewed.")
    line: int = Field(
        description=(
            "The line number in the RIGHT (new/modified) or LEFT (deleted) version of the file where the comment"
            " applies. If start_line is specified, line is the end line number of the multi-line range."
        )
    )
    start_line: int | None = Field(
        default=None,
        description=(
            "Optional start line number for multi-line comments. If provided, must be <= line and in the same file."
        ),
    )
    side: str = Field(
        default="RIGHT", description="Must be 'RIGHT' for additions/modifications or 'LEFT' for deletions."
    )
    severity: str = Field(description="Severity icon: 🔴 (Critical), 🟠 (High), 🟡 (Medium), 🟢 (Low)")
    comment_text: str = Field(
        description="Constructive feedback explaining the issue. Write the feedback comments in the requested language."
    )
    code_suggestion: str | None = Field(
        None,
        description=(
            "Optional drop-in replacement code. Must match the exact code structure and indentation of the replaced"
            " line(s) WITHOUT line number prefixes or markdown fences."
        ),
    )


class ReviewResult(BaseModel):
    """Represents the structured review results returned by the Gemini model."""

    summary: str = Field(
        description="A brief, high-level assessment of the Pull Request's objective and quality (2-3 sentences)."
    )
    resolved_items: list[str] = Field(
        default_factory=list,
        description=(
            "List of previously raised review comments/threads that have been resolved or addressed in this PR update."
        ),
    )
    general_feedback: list[str] = Field(
        description="General feedback items, positive observations, or non-line-specific feedback."
    )
    comments: list[InlineComment] = Field(description="Line-specific code review comments and suggestions.")
