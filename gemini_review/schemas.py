"""
Description: Pydantic schemas for structured Gemini PR review responses.
Provides Pydantic data schemas for line-specific inline comments and top-level review summaries.
"""

from pydantic import BaseModel, Field, field_validator


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
            " IMPORTANT: Whenever code_suggestion replaces or includes multiple lines of existing code, start_line MUST"
            " be provided so that [start_line, line] covers ALL original lines being replaced."
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
            " line(s) WITHOUT line numbers, line prefixes (e.g. '105 | '), or markdown fences. CRITICAL:"
            " code_suggestion must correspond EXACTLY to the target line range [start_line..line]. If start_line is"
            " omitted (single-line comment), code_suggestion MUST only modify or replace that single line. If"
            " code_suggestion replaces multiple existing lines, start_line MUST be set to the first line and line to"
            " the last line of the replaced range to prevent GitHub line duplication."
        ),
    )


class ResolvedItem(BaseModel):
    """A prior review finding the model judges to have been addressed in this update.

    `path` and `line` are optional on purpose. They are what allows the corresponding GitHub
    review thread to be resolved automatically, but a model that cannot confidently attribute a
    resolved item to a specific location should omit them rather than guess: the item is still
    reported in the summary, and its thread is simply left open. Leaving a thread open is a
    small annoyance, whereas resolving the wrong one hides feedback that is still outstanding.
    """

    description: str = Field(description="What was resolved, in the requested language.")
    path: str | None = Field(
        default=None,
        description=(
            "Relative file path of the ORIGINAL comment being resolved. Provide this only when you are"
            " certain which prior comment this refers to. Omit it if unsure."
        ),
    )
    line: int | None = Field(
        default=None,
        description=(
            "Line number the ORIGINAL comment was attached to, as shown in the prior review context."
            " Provide only alongside path, and only when certain. Omit if unsure."
        ),
    )


class ReviewResult(BaseModel):
    """Represents the structured review results returned by the Gemini model."""

    summary: str = Field(
        description="A brief, high-level assessment of the Pull Request's objective and quality (2-3 sentences)."
    )
    resolved_items: list[ResolvedItem] = Field(
        default_factory=list,
        description=(
            "Previously raised review comments/threads that have been resolved or addressed in this PR update."
            " Include path and line ONLY when you are certain which prior comment each refers to."
        ),
    )

    @field_validator("resolved_items", mode="before")
    @classmethod
    def _accept_plain_strings(cls, value: object) -> object:
        """Coerce a bare string into a ResolvedItem.

        `resolved_items` used to be a list of strings. The schema sent to the model is now the
        structured form only, which is the point — the model must supply path and line for a
        thread to be resolvable. But a replayed, cached, or hand-built response using the old
        shape should still render rather than raise, since the alternative is losing a review
        that has already been paid for over a formatting detail.
        """
        if isinstance(value, list):
            return [{"description": v} if isinstance(v, str) else v for v in value]
        return value

    general_feedback: list[str] = Field(
        description="General feedback items, positive observations, or non-line-specific feedback."
    )
    comments: list[InlineComment] = Field(description="Line-specific code review comments and suggestions.")


class DynamicContextSelection(BaseModel):
    """Represents the structured file selection returned by Gemini for dynamic repo context."""

    selected_files: list[str] = Field(
        default_factory=list,
        description="List of relative file paths from candidate files selected to provide relevant context for the PR.",
    )
    reasoning: str = Field(
        default="",
        description="Short justification explaining why these specific files were selected to understand the changes.",
    )
