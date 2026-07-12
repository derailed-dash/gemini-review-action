# Gemini PR Review & Triage Action

![Dazbo's Gemini Review & Triage Banner](assets/gemini_review_banner.png)

**Automated, Google Gemini-based Pull Request reviews and Issue Triaging for all your GitHub repositories and CI/CD pipelines.**

## Features Overview

- **AI-Powered Code Reviews**: Automated, constructive line-specific feedback on Pull Requests using Google Gemini models (Gemini 3.5 Flash by default).
- **Automated Issue Triage**: Dynamically labels, prioritises, and triages incoming issues.
- **Drop-in Migration**: Fully compatible as a direct, drop-in replacement for the deprecated `run-gemini-cli` action.
- **Structured Outputs**: Error-free JSON response formatting using Pydantic schema validation.
- **Context-Enriched Analysis**: Surrounds diffs with the full contents of modified files for context-aware reviews.
- **Interactive Suggestions**: Formats code recommendations inside native GitHub ` ```suggestion ` blocks for one-click merge applications.
- **Triggers**: The action triggers automatically in response to PR events. It can also be triggered by posting a comment in the PR starting with `/gemini-review`.
- **Fast-Execution Composite Action**: Avoids containerisation build/pull latency (no slow `docker build` on every execution) by running as a native composite action.
- **Cross-Platform Support**: Runs natively on Linux, macOS, and Windows runners (both GitHub-hosted and self-hosted).
- **Modern SDK Execution**: Leverages the modern Google GenAI SDK (`google-genai`).
- **Enterprise-Grade Security**: Authentication via either Google Gemini API Keys or Google Cloud Workload Identity Federation (WIF).
- **Customisable Prompts**: Supports repository-specific overrides for both reviews and triaging via simple TOML config files.

## How It Works

1. **Change Discovery**: The action scans the Pull Request diff. It uses a robust extension and path exclusion list to automatically filter out binary, encrypted, or locked files (like `.png`, `.enc`, `uv.lock`, `.env`, etc.).
2. **Context Enrichment**: For every modified text file in the diff, the script reads the *full file contents* from the local workspace. This provides Gemini with complete file context surrounding the diff hunks, allowing it to perform much higher quality reviews.
3. **Structured Review Generation**: The action sends the diff and file contexts to Gemini. It uses Gemini's native **Structured Outputs** (`response_schema`) to force the model to respond in a strict JSON format.
4. **Interactive suggestions**: Change recommendations are wrapped in native GitHub ` ```suggestion ` blocks, allowing reviewers to apply the changes directly on the PR with one click.
5. **Resilient Comment Posting**: The review is posted atomically via the GitHub Pull Request Review API. If the API call fails (e.g. if the model hallucinates an invalid line number in the diff), the script catches the error and falls back to posting comments individually, ensuring your CI status check stays green while still delivering all valid feedback.

## Setup & Use

### Authentication with Gemini API Key

This one-time setup (per repo) is required to allow the action to authenticate to Google Gemini.

By default, this action uses a repository secret called `gemini_api_key`. You can create this key, for example, in [Google AI Studio](https://aistudio.google.com/). 

Add this variable to your repo:

1. Navigate to **Settings** > **Secrets and variables** > **Actions**.
2. Click **New repository secret**.
3. Name the secret `GEMINI_API_KEY` and paste your API key as the value.
4. Reference it in your workflow file as `${{ secrets.GEMINI_API_KEY }}`.

> [!NOTE]
> The `secrets.GITHUB_TOKEN` is automatically created and populated by GitHub for every workflow run. You do not need to add it to your repository secrets manually. You only need to ensure the correct `permissions` are defined in the workflow file, as shown in the examples.

If you prefer to authenticate using a combination of **Google Cloud Workload Identity Federation (WIF)** and **Application Default Credentials (ADC)** with Gemini Enterprise Agent Platform (formerly known as Vertex AI), you can omit `gemini_api_key`. This allows the action to authenticate securely with Google Cloud without storing a long-lived service account key JSON file in your repository.

Alternatively, we can use WIF and ADC to authenticate. In this approach, we do not use persistent Gemini API key. This will be shown later.

### PR Review Action Definition

One-time step: add this GitHub Action to your repository, by copying the example workflow below to `.github/workflows/gemini-review.yml` in your repo.

```yaml
name: "🔎 Dazbo's Gemini Code Review"

on:
  pull_request:
    branches:
      - main
    # Optional: restrict trigger paths (supports inclusions & exclusions)
    # paths:
    #   - 'src/**'
    #   - '!src/generated/**'  # Exclude generated files
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

### Seeing It In Action

1. Create a PR in the repository:

    ![Create the PR](./assets/create-pr.png)

2. Watch the workflow run. The action will automatically start a review process:

    ![Automated PR Review Starts](./assets/pr-running.png)

3. Once complete, you will see a review comment with recommendations:

    ![Automated PR Review Completes](./assets/pr-review-summary.png)


#### Filtering Trigger Paths (Inclusions & Exclusions)

By default, GitHub Actions will trigger the code review workflow on a `pull_request` event for changes to *any* files in the repository. You can restrict which files trigger the workflow by configuring `paths` or `paths-ignore` under the trigger configuration.

To only review files under certain directories or with specific file extensions, use the `paths` key:

```yaml
on:
  pull_request:
    paths:
      - 'src/**'
      - 'tests/**'
      - 'pyproject.toml'
```

You can define exclusions in two ways:

1.  **Exclusions within inclusions (`!` prefix):**
    If you want to review a directory but exclude specific subdirectories or file types, prefix the pattern with `!`. Note that negative patterns must follow at least one positive pattern.
    ```yaml
    on:
      pull_request:
        paths:
          - 'src/**'
          - '!src/generated/**'  # Exclude generated files
    ```

2.  **Excluding paths globally (`paths-ignore`):**
    If you want to run the review for all files except for certain folders (like documentation or configurations), use `paths-ignore`:
    ```yaml
    on:
      pull_request:
        paths-ignore:
          - 'docs/**'
          - '**.md'
    ```

> [!NOTE]
> Regardless of your workflow's `paths` trigger, the underlying Python script automatically filters out binary, locked, and encrypted files (such as `.png`, `.enc`, `package-lock.json`, and `uv.lock`) before sending the code context to Google Gemini.

#### Triggering Reviews via Comments

If the workflow is configured to allow triggering via comments (e.g. with the `issue_comment` trigger as shown in the example above), you can trigger a code review manually at any time by posting a comment on the Pull Request:

* Simply comment `/gemini-review` on the PR.
* **Security & Access Control:** To prevent unauthorised runs and control costs, the action will only trigger if the commenter is an `OWNER`, `MEMBER`, or `COLLABORATOR` of the repository.

### Issues Triage Action Definition

Add this GitHub Action to your repository, by copying the example workflow below to `.github/workflows/gemini-issue.yml` in your repo.

```yaml
name: "🏷️ Dazbo's Gemini Issue Triage"

on:
  issues:
    types: [opened, reopened]

jobs:
  triage:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      issues: write

    steps:
      - name: Checkout repository
        uses: actions/checkout@v6

      - name: Run Gemini Triage Action
        uses: derailed-dash/gemini-review-action@v1
        with:
          command: 'triage'        
          gemini_api_key: ${{ secrets.GEMINI_API_KEY }}
          github_token: ${{ secrets.GITHUB_TOKEN }}
          gemini_model: 'gemini-3.5-flash'
```

## Configuration

### Action Inputs

| Input | Description | Required | Default |
| :--- | :--- | :--- | :--- |
| `gemini_api_key` | Your Gemini Developer API Key (from Google AI Studio). | **Yes** (unless using WIF) | N/A |
| `github_token` | Repository `GITHUB_TOKEN` (automatically provided by GitHub; no manual secret creation required). | **Yes** | N/A |
| `gemini_model` | The Gemini model version to target. | No | `gemini-3.5-flash` |
| `command` | The mode/command to run: `review` (for PR reviews) or `triage` (for issue triaging). | No | `review` |
| `language` | The language to use for the review comments (e.g. `English (UK)`, `English (US)`, `French`, `Spanish`). | No | `English (UK)` |
| `timeout` | Timeout for API requests in seconds. | No | `60` |

### Custom Prompts / Instructions

This action bundles high-quality default prompt configurations for both review and triage:
* **Default Review Prompt:** [gemini-review.toml](file:///home/dazbo/localdev/gemini-review-action/gemini-review.toml)
* **Default Triage Prompt:** [gemini-triage.toml](file:///home/dazbo/localdev/gemini-review-action/gemini-triage.toml)

You can customize or completely override the prompt instructions given to the review or triage reviewers on a repository-by-repository basis:

* **To override the Code Review prompt:** Create a file at `.github/commands/gemini-review.toml` in your calling repository.
* **To override the Issue Triage prompt:** Create a file at `.github/commands/gemini-triage.toml` in your calling repository.

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

## How It Works

![Gemini PR Review & Triage Pipeline Flowchart](assets/gemini_review_flow.png)

### Step-by-Step

1. **Trigger**: A developer opens or pushes an update to a Pull Request, or opens/reopens an Issue. GitHub Actions detects this webhook event and starts a runner to execute the review or triage action.
2. **Context Gathering**:
   - The action retrieves the Pull Request diff from the GitHub API.
   - It performs exclusion filtering to automatically ignore non-text files and configured paths (like lock files or binaries).
   - For all remaining modified files, it reads the full text from the local workspace filesystem to provide surrounding file context.
3. **Gemini AI Analysis**: The action packs the diff, the full-file surrounding context, and the system prompt instructions (loaded from [gemini-review.toml](gemini-review.toml) or [gemini-triage.toml](gemini-triage.toml)) into a payload and sends it to the Google Gemini API. The model performs in-depth analysis (assessing code quality, identifying bugs or improvements, and scanning for security concerns).
4. **Automated Feedback**:
   - The model generates a structured assessment guaranteed to follow the Pydantic schema constraints (such as [ReviewResult](gemini_pr_review.py#L35-L39)).
   - The action parses this structured response and automatically posts comments (including severity markers and interactive suggestions) or labels back to the GitHub PR or Issue.
   - **Resilience:** If a PR comment contains a line range mismatch, the resilient handler falls back to publishing comments individually so that valid reviews are not lost and the workflow status stays green.


### Review Response Format

Gemini uses a strict Pydantic schema to generate reviews. Every submitted review contains:

1. **📋 Review Summary**: A 2-3 sentence assessment of the pull request's overall objective and quality.
2. **🔍 General Feedback**: A bulleted list of high-level observations and positive highlights.
3. **💬 Inline Comments**: Line-specific comments containing:
   - Severity icons: `🔴` (Critical), `🟠` (High), `🟡` (Medium), `🟢` (Low).
   - Constructive explanation written in English (UK) spelling.
   - Interactive code replacement suggestion blocks (optional).

### Logging & Troubleshooting

All standard output (`stdout`) and error logs (`stderr`) produced by the action's execution (such as API call progress, validation warnings, or error details) are printed directly to the console. 

You can view these logs by opening the specific workflow run in the **Actions** tab of your GitHub repository, selecting the active job (e.g. `review` or `triage`), and expanding the **Run Script** step.

## Author

Developed and maintained by **Darren 'Dazbo' Lester** (GitHub: [@derailed-dash](https://github.com/derailed-dash)).

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## Alternative Authentication

**Using Google Workload Identity Federation (WIF) and Application Default Credentials (ADC)**

For this authentication approach, you must configure the following in your calling workflow:

1. **OIDC Permissions:** Grant the workflow job `id-token: write` permission so GitHub can request OIDC credentials.
2. **Authenticate Step:** Run the `google-github-actions/auth` step prior to running this action to exchange GitHub's OIDC token for Google Cloud credentials.
3. **Environment Variables:** Pass the required Google Cloud environment variables (`GOOGLE_GENAI_USE_VERTEXAI`, `GOOGLE_CLOUD_PROJECT`, and `GOOGLE_CLOUD_LOCATION`) to the review action.

To keep your infrastructure details private, you should save the following as GitHub repository secrets:
* `GCP_SERVICE_ACCOUNT`: The email address of your Google Cloud service account.
* `WIF_POOL_ID`: The ID of your Workload Identity Pool (e.g. `my-pool`).
* `WIF_PROVIDER_ID`: The ID of your Workload Identity Provider (e.g. `my-provider`).

Example workflow job configured to run the review action with WIF authentication:

```yaml
jobs:
  review:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      pull-requests: write
      id-token: write # Required for WIF OIDC token exchange

    steps:
      - name: Checkout repository
        uses: actions/checkout@v6
        with:
          ref: ${{ github.event.pull_request.head.sha || format('refs/pull/{0}/head', github.event.issue.number) }}

      - name: Authenticate to Google Cloud via WIF
        uses: google-github-actions/auth@v2
        with:
          workload_identity_provider: 'projects/123456789012/locations/global/workloadIdentityPools/${{ secrets.WIF_POOL_ID }}/providers/${{ secrets.WIF_PROVIDER_ID }}'
          service_account: '${{ secrets.GCP_SERVICE_ACCOUNT }}'

      - name: Run Gemini Review Action
        uses: derailed-dash/gemini-review-action@v1
        env:
          GOOGLE_GENAI_USE_VERTEXAI: "True"
          GOOGLE_CLOUD_PROJECT: "my-project-id"
          GOOGLE_CLOUD_LOCATION: "global" # Or your preferred model endpoint region
        with:
          github_token: ${{ secrets.GITHUB_TOKEN }}
          gemini_model: 'gemini-3.5-flash'
```

> [!NOTE]
> When `GOOGLE_GENAI_USE_VERTEXAI` is set to `"True"`, the underlying `google-genai` SDK uses Application Default Credentials (ADC) to automatically locate and use the short-lived credentials configured on the runner by the `google-github-actions/auth` step.


## Development & Releases

### Local Development & Testing

#### Repository Structure

Here is an overview of the directory tree and the purpose of each file:

```text
.                                   
├── assets/                  # Documentation assets (banners, images)
├── tests/                   # Unit tests
├── action.yml               # GitHub Action definition (inputs, environment, and steps)
├── CONTRIBUTING.md          # Collaboration guidelines for developers
├── gemini-review.toml       # Default prompt template for PR reviews
├── gemini-triage.toml       # Default prompt template for issue triage
├── gemini_issue_triage.py   # Python script to triage and label incoming issues
├── gemini_pr_review.py      # Python script to review PRs
├── pyproject.toml           # Python project config, dependencies
└── README.md                # Project documentation, setup guide, and usage examples
```

This project uses `uv` for python environment and dependency management. To configure your local environment and run the test suite:

1. **Install Dependencies:**
   Run `uv sync` to create a virtual environment (`.venv`) and install all runtime and development packages.
   ```bash
   uv sync
   ```

2. **Linting & Code Formatting:**
   We enforce strict syntax, formatting, and spelling checks using `ruff` and `codespell`. Run these from the project root:
   ```bash
   uvx codespell@latest -s
   uvx ruff@latest check --fix .
   ```

3. **Running the Unit Tests:**
   Run the unit test suite with `pytest`:
   ```bash
   uv run pytest
   ```

### Deploying & Publishing Updates

GitHub Actions are versioned by Git tags. When other repositories consume this action, they reference a specific tag (e.g. `uses: derailed-dash/gemini-review-action@v1`). To release new changes so that they are picked up by consuming repositories, you need to publish a release and update the major version tag.

Follow this step-by-step workflow:

#### Step 1. Commit and Push Your Changes

Ensure all your local tests pass successfully, then commit and push your changes to the main branch:

```bash
git add .
git commit -m "feat: do some stuff"
git push origin main
```

#### Step 2. Update the Git Tags Locally

To let users lock to specific versions (like `v1.1.0`) while still allowing others to automatically receive updates via the major version tag (`v1`), create or move these tags on your local machine:

1. **Create the minor/patch tag** (e.g. `v1.1.0`):
   ```bash
   git tag -fa v1.1.0 -m "Release version v1.1.0"
   ```
2. **Move the major version tag** (`v1`) to point to this new release:
   ```bash
   git tag -fa v1 -m "Update v1 tag to point to v1.1.0"
   ```
3. **Push the tags to GitHub** (you must use the `--force` flag to update the existing `v1` tag on the remote server):
   ```bash
   git push origin v1.1.0
   git push origin v1 --force
   ```

#### Step 3. Draft and Publish the Release on GitHub

To make the new version officially available and visible on the GitHub Marketplace:

1. Open the repository on GitHub.
2. In the right-hand sidebar, locate the **Releases** section and click **Draft a new release** (or click the gear icon and select *Create a release*).
3. Click the **Choose a tag** dropdown:
   * Type in the version you just pushed (e.g. `v1.1.0`).
   * Select it from the dropdown.
4. Under **Release title**, enter a title for the version (e.g. `v1.1.0 - Configurable language & testing suite`).
5. **Publish to the Marketplace:**
   * Tick the checkbox next to **Publish this Action to the GitHub Marketplace**.
   * *If this is your first time publishing this action:* Accept the GitHub Developer Agreement, select a primary category (e.g. `Code quality` or `Utilities`), and customise the colour and icon for the marketplace listing card.
6. Write a summary of changes in the description box, or click **Generate release notes** to automatically construct them from your commit logs.
7. Click **Publish release**.

#### Example: Releasing Version `v1.1.0`

Here is a full example of checking tag status and releasing version `v1.1.0`:

1. **Check current tag status:**
   Find the closest tag and see how many commits the branch is ahead by:
   ```bash
   git describe --tags
   # Example output: v1-4-gb824f0f (4 commits ahead of v1)
   ```

2. **Create the minor/patch tag and move the major version tag locally:**
   ```bash
   git tag -fa v1.1.0 -m "Release version v1.1.0"
   git tag -fa v1 -m "Update v1 tag to point to v1.1.0"
   ```

3. **Push tags to remote (using `--force` to update the existing `v1` tag on GitHub):**
   ```bash
   git push origin v1.1.0
   git push origin v1 --force
   ```

