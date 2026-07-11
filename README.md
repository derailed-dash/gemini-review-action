# Gemini-Review-Action

- Automated Pull Request code reviews and Issue Triaging on GitHub
- Leverages Google Gemini
- Can be used in your GitHub Actions-based workflows and pipelines
- Can be used as a drop-in replacement for the deprecated `run-gemini-cli`
- Supports use of API keys and Workload Identity Federation

## Features Overview

- **AI-Powered Code Reviews**: Automated, constructive line-specific feedback on Pull Requests using Google Gemini models.
- **Automated Issue Triage**: Dynamically labels, prioritises, and triages incoming issues.
- **Structured Outputs**: Error-free JSON response formatting using Pydantic schema validation.
- **Context-Enriched Analysis**: Surrounds diff patches with the full contents of modified files from the local workspace to allow deeper, more context-aware reviews.
- **Interactive Suggestions**: Formats code recommendations inside native GitHub ` ```suggestion ` blocks for one-click merge applications.
- **Fast-Execution Composite Action**: Avoids containerisation build/pull latency (no slow `docker build` on every execution) by running as a native composite action powered by Astral `uv`.
- **Cross-Platform Support**: Runs natively on Linux, macOS, and Windows runners (both GitHub-hosted and self-hosted).
- **Modern SDK Execution**: Leverages the official Google GenAI SDK (`google-genai`), rather than older APIs and SDKs.
- **Enterprise-Grade Security**: Flexible authentication via either Google Gemini API Keys or Google Cloud Workload Identity Federation (WIF).
- **Customisable Prompts**: Supports repository-specific overrides for both reviews and triaging via simple TOML config files.

## How It Works

1. **Change Discovery**: The action scans the Pull Request diff. It uses a robust extension and path exclusion list to automatically filter out binary, encrypted, or locked files (like `.png`, `.enc`, `uv.lock`, `.env`, etc.).
2. **Context Enrichment**: For every modified text file in the diff, the script reads the *full file contents* from the local workspace. This provides Gemini with complete file context surrounding the diff hunks, allowing it to perform much higher quality reviews.
3. **Structured Review Generation**: The action sends the diff hunks and file contexts to Gemini (using `gemini-3.5-flash` by default). It uses Gemini's native **Structured Outputs** (`response_schema`) to force the model to respond in a strict JSON format.
4. **Interactive suggestions**: Code recommendations are wrapped in native GitHub ` ```suggestion ` blocks, allowing reviewers to apply the changes directly on the PR with one click.
5. **Resilient Comment Posting**: The review is posted atomically via the GitHub Pull Request Review API. If the API call fails (e.g. if the model hallucinates an invalid line number in the diff), the script catches the error and falls back to posting comments individually, ensuring your CI status check stays green while still delivering all valid feedback.

## The Step-by-Step Flow

1. **Trigger Event**: A developer opens or pushes an update to a Pull Request on GitHub.
2. **Workflow Run**: GitHub Actions detects the event and starts a runner, checking out the pull request ref and executing the review action.
3. **Context Preparation**:
   - The action retrieves the PR diff files via pagination from the GitHub API.
   - It filters out non-text files and blacklisted paths (like lock files or binaries).
   - It reads the full text of the remaining modified files from the local workspace filesystem.
4. **Gemini API Call**: The action merges the diff hunks, full-file surrounding context, and system instructions (from [gemini-review.toml](gemini-review.toml)) into a unified prompt payload and posts it to the Google Gemini API.
5. **Structured Assessment**: The Gemini model analyses the files and changes, and generates a structured code review response guaranteed to follow the Pydantic schema constraints ([ReviewResult](gemini_pr_review.py#L35-L39)).
6. **Publish Feedback**: The action parses the structured JSON response and submits line-specific inline comments containing severity markers and interactive suggestions back to the Pull Request. If a comment contains a line range mismatch, the resilient handler falls back to publishing comments individually so that valid reviews are not lost and the workflow status stays green.

---

## Setup & Configuration

Add this workflow to your repository using a custom action like this: `.github/workflows/gemini-review.yml`

```yaml
name: '🔎 Gemini Code Review'

on:
  pull_request:
    branches:
      - main
    # Optional: restrict paths to trigger reviews only on relevant files
    # paths:
    #   - 'src/**'
    #   - 'tests/**'
    #   - 'pyproject.toml'
  issue_comment:
    types: [created]

jobs:
  review:
    # Run on PR updates, OR on issue comment starting with /gemini-review by repo owners/members
    if: |
      github.event_name == 'pull_request' ||
      (
        github.event_name == 'issue_comment' &&
        github.event.issue.pull_request &&
        startsWith(github.event.comment.body, '/gemini-review') &&
        contains(fromJSON('["OWNER", "MEMBER", "COLLABORATOR"]'), github.event.comment.author_association)
      )
    runs-on: ubuntu-latest
    permissions:
      contents: read
      pull-requests: write
      issues: write

    steps:
      - name: Checkout repository
        uses: actions/checkout@v6
        with:
          # Automatically checks out the head ref for PR events,
          # and pulls the PR head branch for comment-based triggers
          ref: ${{ github.event.pull_request.head.sha || format('refs/pull/{0}/head', github.event.issue.number) }}

      - name: Run Gemini Review Action
        uses: derailed-dash/gemini-review-action@v1
        with:
          gemini_api_key: ${{ secrets.GEMINI_API_KEY }}
          github_token: ${{ secrets.GITHUB_TOKEN }}
          gemini_model: 'gemini-3.5-flash'
```

### Triggering Reviews via Comments

If the workflow is configured with the `issue_comment` trigger as shown in the example above, you can trigger a code review manually at any time by posting a comment on the Pull Request:

* Simply comment `/gemini-review` on the PR.
* **Security & Access Control:** To prevent unauthorised runs and control costs, the action will only trigger if the commenter is an `OWNER`, `MEMBER`, or `COLLABORATOR` of the repository.

### Action Inputs

| Input | Description | Required | Default |
| :--- | :--- | :--- | :--- |
| `gemini_api_key` | Your Gemini Developer API Key (from Google AI Studio). | **Yes** | N/A |
| `github_token` | Repository GITHUB_TOKEN (needed to post comments). | **Yes** | N/A |
| `gemini_model` | The Gemini model version to target. | No | `gemini-3.5-flash` |

### Environment Variables & Authentication

By default, this action uses your AI Studio API key (`gemini_api_key`).
If you prefer to authenticate using **Google Cloud Workload Identity Federation (WIF)** and Vertex AI, you can omit `gemini_api_key` and configure the following environment variables in your calling step:

- `GOOGLE_GENAI_USE_VERTEXAI: "True"`
- `GOOGLE_CLOUD_PROJECT: "your-project-id"`
- `GOOGLE_CLOUD_LOCATION: "global"`

Ensure you have run the `google-github-actions/auth` step prior to running this action to configure Application Default Credentials.

## Customising the Prompt

This action bundles high-quality default prompt configurations for both review and triage:
* **Default Review Prompt:** [gemini-review.toml](file:///home/dazbo/localdev/gemini-review-action/gemini-review.toml)
* **Default Triage Prompt:** [gemini-triage.toml](file:///home/dazbo/localdev/gemini-review-action/gemini-triage.toml)

### Overriding the Default Prompt

You can customize or completely override the prompt instructions given to the review or triage bots on a repository-by-repository basis:

* **To override the Code Review prompt:** Create a file at `.github/commands/gemini-review.toml` in your calling repository.
* **To override the Issue Triage prompt:** Create a file at `.github/commands/gemini-triage.toml` in your calling repository.

### Custom Prompt Schema

Your custom TOML file must contain a `prompt` key enclosing your system instructions in markdown format:

```toml
description = "Reviews a pull request with Gemini"
prompt = """
## Role
You are a world-class code review assistant.

## Primary Directive
Review the diff and full file contents to identify performance, security, and logic bugs.

## Rules
- Focus comments strictly on lines added or modified (lines starting with `+` or `-`).
- Use English (UK) spelling for all review feedback text.
- Do not make comments on formatting unless it is an egregious style guide violation.

- **GitHub Repository**: !{echo $REPOSITORY}
- **Pull Request Number**: !{echo $PULL_REQUEST_NUMBER}
"""
```

### Prompt Placeholders

The action parses the TOML files and dynamically substitutes the following expressions:

* **Code Review placeholders:**
  * `!{echo $REPOSITORY}`: Replaced with the current repository name (e.g. `derailed-dash/my-repo`).
  * `!{echo $PULL_REQUEST_NUMBER}`: Replaced with the number of the Pull Request being triaged.
  * `!{echo $ADDITIONAL_CONTEXT}`: Replaced with empty space or triggering comment arguments.

* **Issue Triage placeholders:**
  * `!{echo $AVAILABLE_LABELS}`: Replaced with a comma-separated list of available labels.
  * `!{echo $ISSUE_TITLE}`: Replaced with the title of the issue.
  * `!{echo $ISSUE_BODY}`: Replaced with the body text of the issue.
  * `!{echo $GITHUB_ENV}`: Replaced with the file path to append output environment variables.

## Review Response Format

Gemini uses a strict Pydantic schema to generate reviews. Every submitted review contains:
1. **📋 Review Summary**: A 2-3 sentence assessment of the pull request's overall objective and quality.
2. **🔍 General Feedback**: A bulleted list of high-level observations and positive highlights.
3. **💬 Inline Comments**: Line-specific comments containing:
   - Severity icons: `🔴` (Critical), `🟠` (High), `🟡` (Medium), `🟢` (Low).
   - Constructive explanation written in English (UK) spelling.
   - Interactive code replacement suggestion blocks (optional).

---

## Author

Developed and maintained by **Darren 'Dazbo' Lester** (GitHub: [@derailed-dash](https://github.com/derailed-dash)).

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

