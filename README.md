# Gemini PR Code Review Action

Automated Pull Request code reviews on GitHub using the official Google Gen AI SDK (`google-genai`), Pydantic Structured Outputs, and Gemini.

This is a custom-built, highly resilient GitHub Action that replaces the deprecated `run-gemini-cli`. It is designed to perform precise, context-aware reviews on your pull requests while avoiding common failure points such as binary file encoding issues or malformed response formats.

---

## How It Works

1. **Change Discovery**: The action scans the Pull Request diff. It uses a robust extension and path exclusion list to automatically filter out binary, encrypted, or locked files (like `.png`, `.enc`, `uv.lock`, `.env`, etc.).
2. **Context Enrichment**: For every modified text file in the diff, the script reads the *full file contents* from the local workspace. This provides Gemini with complete file context surrounding the diff hunks, allowing it to perform much higher quality reviews.
3. **Structured Review Generation**: The action sends the diff hunks and file contexts to Gemini (using `gemini-3.5-flash` by default). It uses Gemini's native **Structured Outputs** (`response_schema`) to force the model to respond in a strict JSON format.
4. **Interactive suggestions**: Code recommendations are wrapped in native GitHub ` ```suggestion ` blocks, allowing reviewers to apply the changes directly on the PR with one click.
5. **Resilient Comment Posting**: The review is posted atomically via the GitHub Pull Request Review API. If the API call fails (e.g. if the model hallucinates an invalid line number in the diff), the script catches the error and falls back to posting comments individually, ensuring your CI status check stays green while still delivering all valid feedback.

---

## Setup & Configuration

Add this workflow to your repository under `.github/workflows/gemini-review.yml`:

```yaml
name: '🔎 Gemini Code Review'

on:
  pull_request:
    branches:
      - main
    # Optional: restrict paths to trigger reviews only on relevant files
    paths:
      - 'src/**'
      - 'tests/**'
      - 'pyproject.toml'
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

---

## Customising the Prompt

You can customize the instructions given to the code review bot on a repository-by-repository basis. 

To define repository-specific review criteria, create a file at `.github/commands/gemini-review.toml` in your calling repository. 

### Custom Prompt Schema
The file must contain a `prompt` key enclosing your system instructions in markdown format:

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

The action parses the TOML file and dynamically substitutes the following legacy expressions:
- `!{echo $REPOSITORY}`: Replaced with the current repository name (e.g. `derailed-dash/my-repo`).
- `!{echo $PULL_REQUEST_NUMBER}`: Replaced with the number of the Pull Request being triaged.
- `!{echo $ADDITIONAL_CONTEXT}`: Replaced with empty space or triggering comment arguments.

If `.github/commands/gemini-review.toml` is not present in the calling repository, the action will automatically fall back to an embedded, high-quality general-purpose review prompt.

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

