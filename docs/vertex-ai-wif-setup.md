# Vertex AI Workload Identity Federation (WIF) Setup Guide

This guide walks step-by-step through configuring keyless **Google Cloud Workload Identity Federation (WIF)** for `gemini-review-action` on **Vertex AI**. Every CLI command below has been validated directly against `gcloud` and GitHub Actions.

---

## Why Use Vertex AI with WIF Instead of `GEMINI_API_KEY`?

1. **Keyless OIDC security**: GitHub Actions exchanges a short-lived OIDC token for temporary Google Cloud credentials — no long-lived API keys or JSON service-account keys stored in secrets.
2. **80%–95% Durable Implicit Caching on Gemini 4 (`eu` / `us`)**: When `GOOGLE_CLOUD_LOCATION` is set to a multi-region endpoint such as **`eu`** or **`us`**, Vertex AI enables Durable Implicit Caching (up to 24h TTL) across successive pushes and concurrent PRs sharing the invariant `base_sha` prefix introduced in `v1.12.0` (billed at `0.05×` cached read rates, with a `10%` regional SKU uplift automatically reflected in PR telemetry).
3. **Cloud Billing cost attribution**: `gemini-review-action` automatically attaches `component=gemini-review-action` and `repo=<owner_repo>` billing labels on Vertex AI calls, so multiple repositories can safely share one automation project while still separating costs in Cloud Billing.
4. **ADC support for Developer Knowledge search**: When `GEMINI_API_KEY` is omitted, the action's Google Developer Knowledge search tools authenticate automatically via Application Default Credentials (ADC).

---

## Prerequisites

- A billing-enabled Google Cloud project (`YOUR_PROJECT_ID` / numeric `YOUR_PROJECT_NUMBER`).
- Permissions on that project to enable APIs, create Workload Identity Pools/Providers, create Service Accounts, and assign IAM roles (`roles/owner` or `roles/iam.workloadIdentityPoolAdmin` + `roles/iam.serviceAccountAdmin` + `roles/resourcemanager.projectIamAdmin` + `roles/serviceusage.serviceUsageAdmin`).
- Local [Google Cloud CLI (`gcloud`)](https://cloud.google.com/sdk/docs/install) authenticated (`gcloud auth login`), and optionally [GitHub CLI (`gh`)](https://cli.github.com/) authenticated (`gh auth login`).

Set convenience variables in your local shell before running the commands below (never commit real project IDs or numbers to tracked repository files):

```bash
export PROJECT_ID="YOUR_PROJECT_ID"
export PROJECT_NUMBER="$(gcloud projects describe "${PROJECT_ID}" --format='value(projectNumber)')"
export GITHUB_OWNER="YOUR_GITHUB_OWNER"       # GitHub username or organisation
export GITHUB_REPO="YOUR_REPO"                # Only needed if choosing single-repo scope
export POOL_ID="github-actions-pool"
export PROVIDER_ID="github-oidc"
export SA_NAME="gemini-review-bot"
export SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
```

---

## Step 1: Enable Required Google Cloud APIs

Enable Vertex AI, IAM, IAM Service Account Credentials, and Security Token Service (STS):

```bash
gcloud services enable \
  aiplatform.googleapis.com \
  iam.googleapis.com \
  iamcredentials.googleapis.com \
  sts.googleapis.com \
  --project="${PROJECT_ID}" \
  --quiet
```

---

## Step 2: Create the Workload Identity Pool & GitHub OIDC Provider

1. **Create the Workload Identity Pool**:

   ```bash
   gcloud iam workload-identity-pools create "${POOL_ID}" \
     --location=global \
     --display-name="GitHub Actions Pool" \
     --project="${PROJECT_ID}" \
     --quiet
   ```

2. **Create the GitHub OIDC Provider** (enforcing an attribute condition locked to your GitHub owner so external repositories or forks cannot exchange tokens against your pool):

   ```bash
   gcloud iam workload-identity-pools providers create-oidc "${PROVIDER_ID}" \
     --location=global \
     --workload-identity-pool="${POOL_ID}" \
     --display-name="GitHub OIDC Provider" \
     --issuer-uri="https://token.actions.githubusercontent.com" \
     --attribute-mapping="google.subject=assertion.sub,attribute.actor=assertion.actor,attribute.repository=assertion.repository,attribute.repository_owner=assertion.repository_owner" \
     --attribute-condition="assertion.repository_owner == '${GITHUB_OWNER}'" \
     --project="${PROJECT_ID}" \
     --quiet
   ```

3. **Verify the provider is `ACTIVE`**:

   ```bash
   gcloud iam workload-identity-pools providers list \
     --location=global \
     --workload-identity-pool="${POOL_ID}" \
     --project="${PROJECT_ID}" \
     --limit=5 \
     --format="json(name,state,attributeCondition)" \
     --quiet
   ```

---

## Step 3: Create Service Account & Assign Minimal Permissions

1. **Create a dedicated least-privilege service account**:

   ```bash
   gcloud iam service-accounts create "${SA_NAME}" \
     --display-name="Gemini Review GitHub Action" \
     --project="${PROJECT_ID}" \
     --quiet
   ```

2. **Grant Vertex AI User (`roles/aiplatform.user`) on the project**:

   ```bash
   gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
     --member="serviceAccount:${SA_EMAIL}" \
     --role="roles/aiplatform.user" \
     --quiet
   ```

3. **Allow GitHub Actions OIDC identities to impersonate the service account (`roles/iam.workloadIdentityUser`)** — choose **Option A** (shared across all your repositories) or **Option B** (restricted to one repository):

   - **Option A — All repositories under `YOUR_GITHUB_OWNER` *(recommended for a shared GitHub automation project)***:

     ```bash
     gcloud iam service-accounts add-iam-policy-binding "${SA_EMAIL}" \
       --role="roles/iam.workloadIdentityUser" \
       --member="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL_ID}/attribute.repository_owner/${GITHUB_OWNER}" \
       --project="${PROJECT_ID}" \
       --quiet
     ```

   - **Option B — Single repository strictly (`YOUR_GITHUB_OWNER/YOUR_REPO`)**:

     ```bash
     gcloud iam service-accounts add-iam-policy-binding "${SA_EMAIL}" \
       --role="roles/iam.workloadIdentityUser" \
       --member="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL_ID}/attribute.repository/${GITHUB_OWNER}/${GITHUB_REPO}" \
       --project="${PROJECT_ID}" \
       --quiet
     ```

   *(Even under Option A, keep fork-PR guards in your workflow `if:` conditions so untrusted pull requests from external forks never trigger the job or access OIDC federation.)*

---

## Step 4: Configure GitHub Repository Secrets

Store all GCP identifiers in GitHub Actions secrets rather than hardcoding them in workflow YAML files. Always pass secret values over stdin rather than CLI arguments:

```bash
printf '%s' "${PROJECT_ID}" | gh secret set GCP_PROJECT_ID --repo "${GITHUB_OWNER}/${GITHUB_REPO}"
printf '%s' "${PROJECT_NUMBER}" | gh secret set GCP_PROJECT_NUMBER --repo "${GITHUB_OWNER}/${GITHUB_REPO}"
printf '%s' "${SA_EMAIL}" | gh secret set GCP_SERVICE_ACCOUNT --repo "${GITHUB_OWNER}/${GITHUB_REPO}"
printf '%s' "${POOL_ID}" | gh secret set WIF_POOL_ID --repo "${GITHUB_OWNER}/${GITHUB_REPO}"
printf '%s' "${PROVIDER_ID}" | gh secret set WIF_PROVIDER_ID --repo "${GITHUB_OWNER}/${GITHUB_REPO}"
```

Verify the secrets are present:

```bash
gh secret list --repo "${GITHUB_OWNER}/${GITHUB_REPO}"
```

*(Note: Once both PR review and issue triage workflows use WIF, `GEMINI_API_KEY` is no longer required unless you also run Google Developer Knowledge search with an API key.)*

---

## Step 5: Configure Your Workflows

Note the critical requirements in both workflows:
- `permissions` MUST include **`id-token: write`** so GitHub Actions can mint the OIDC JWT.
- Pin `google-github-actions/auth` to a full 40-character commit SHA (`7c6bc770dae815cd3e89ee6cdf493a5fab2cc093` = `v3.0.0`) because the job holds write permissions against PRs/issues.
- Pass `GOOGLE_GENAI_USE_VERTEXAI: 'True'`, `GOOGLE_CLOUD_PROJECT: ${{ secrets.GCP_PROJECT_ID }}`, and `GOOGLE_CLOUD_LOCATION: 'eu'` (or `'us'`) under `env:` on the `gemini-review-action` step, and omit `gemini_api_key`.

### PR Code Review (`.github/workflows/gemini-review.yml`)

```yaml
name: "🔎 Dazbo's Gemini Code Review"

on:
  pull_request:
    branches:
      - main
  issue_comment:
    types: [created]

jobs:
  review:
    if: |
      (github.event_name == 'pull_request' && github.event.pull_request.head.repo.full_name == github.repository) ||
      (
        github.event_name == 'issue_comment' &&
        github.event.issue.pull_request &&
        startsWith(github.event.comment.body, '/gemini-review') &&
        contains(fromJSON('["OWNER", "MEMBER", "COLLABORATOR"]'), github.event.comment.author_association)
      )
    runs-on: ubuntu-latest
    concurrency:
      group: gemini-review-${{ github.event.pull_request.number || github.event.issue.number }}
      cancel-in-progress: true
    permissions:
      contents: read
      pull-requests: write
      issues: write
      statuses: write
      checks: write
      id-token: write

    steps:
      - name: Checkout repository
        uses: actions/checkout@v7
        with:
          ref: ${{ github.event.pull_request.head.sha || format('refs/pull/{0}/head', github.event.issue.number) }}

      - name: Authenticate to Google Cloud via WIF
        uses: google-github-actions/auth@7c6bc770dae815cd3e89ee6cdf493a5fab2cc093 # v3.0.0
        with:
          workload_identity_provider: 'projects/${{ secrets.GCP_PROJECT_NUMBER }}/locations/global/workloadIdentityPools/${{ secrets.WIF_POOL_ID }}/providers/${{ secrets.WIF_PROVIDER_ID }}'
          service_account: '${{ secrets.GCP_SERVICE_ACCOUNT }}'

      - name: Run Gemini Review Action
        uses: derailed-dash/gemini-review-action@v1
        env:
          GOOGLE_GENAI_USE_VERTEXAI: 'True'
          GOOGLE_CLOUD_PROJECT: ${{ secrets.GCP_PROJECT_ID }}
          GOOGLE_CLOUD_LOCATION: 'eu'
        with:
          github_token: ${{ secrets.GITHUB_TOKEN }}
          gemini_model: 'gemini-4-hydrogen'
          thinking_level: 'low'
          language: 'English (UK)'
```

### Issue Triage (`.github/workflows/gemini-triage.yml`)

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
      id-token: write

    steps:
      - name: Checkout repository
        uses: actions/checkout@v7

      - name: Authenticate to Google Cloud via WIF
        uses: google-github-actions/auth@7c6bc770dae815cd3e89ee6cdf493a5fab2cc093 # v3.0.0
        with:
          workload_identity_provider: 'projects/${{ secrets.GCP_PROJECT_NUMBER }}/locations/global/workloadIdentityPools/${{ secrets.WIF_POOL_ID }}/providers/${{ secrets.WIF_PROVIDER_ID }}'
          service_account: '${{ secrets.GCP_SERVICE_ACCOUNT }}'

      - name: Run Gemini Triage Action
        uses: derailed-dash/gemini-review-action@v1
        env:
          GOOGLE_GENAI_USE_VERTEXAI: 'True'
          GOOGLE_CLOUD_PROJECT: ${{ secrets.GCP_PROJECT_ID }}
          GOOGLE_CLOUD_LOCATION: 'eu'
        with:
          command: 'triage'
          github_token: ${{ secrets.GITHUB_TOKEN }}
          gemini_model: 'gemini-4-hydrogen'
          thinking_level: 'low'
          language: 'English (UK)'
```

---

## Step 6: Verification & Expected Telemetry

On your first PR using Vertex AI WIF:

1. **Workflow logs** should confirm:
   - `Authenticate to Google Cloud via WIF` succeeds cleanly.
   - `Initialising GenAI Client (Model: gemini-4-hydrogen) using Vertex AI authentication...`
   - `Billing labels: {'component': 'gemini-review-action', 'repo': '<owner>_<repo>'}`
   - `Codebase context: running in Invariant Baseline Full Context Mode (...)`
2. **PR review telemetry (`📊 Token Usage & Cost Efficiency`)**:
   - Displays `| **Endpoint** | Vertex AI (eu, +10% regional) |`.
   - On the **first run** for a `base_sha` prefix, `Input Tokens (cached)` seeds the cache (`0` or partial hits).
   - On **subsequent commits or parallel PRs** against the same `main` `base_sha`, `Input Tokens (cached)` typically hits **80%–95%** (`⚡ XX.X% cached` billed at `0.05×` input price).

---

## Troubleshooting

| Symptom | Cause | Fix |
| :--- | :--- | :--- |
| `Error: Input required and not supplied: gemini_api_key` | Consumer pinned to a version before optional `gemini_api_key` | Upgrade to `v1.12.1+` / `@v1` once updated, or pass `gemini_api_key: ''`. |
| `Unable to generate an OIDC token` / `id-token` error | Missing OIDC permission on the workflow job | Add `id-token: write` to the job's `permissions:` block. |
| `Error acquiring credentials` / `unauthorized_client` during WIF exchange | OIDC `--attribute-condition` or `roles/iam.workloadIdentityUser` `--member` mismatch | Verify `attribute.repository_owner` / `attribute.repository` matches your repo and wait ~60s for new IAM bindings to propagate. |
| `PermissionDenied` calling Vertex AI | Missing `roles/aiplatform.user` or `aiplatform.googleapis.com` disabled | Re-run Step 1 and Step 3 (`roles/aiplatform.user`). |
| `Input Tokens (cached)` stays `0` across commits on Gemini 4 | `GOOGLE_CLOUD_LOCATION` set to `'global'`, or `context_mode` mutated prefix | Set `GOOGLE_CLOUD_LOCATION: 'eu'` (or `'us'`) for Durable Implicit Caching and verify `Invariant Baseline Full Context Mode` appears in runner logs. |
