# Agent Runtime Infrastructure

> **Assumes `/google-agents-cli-scaffold` scaffolding.** If your project isn't scaffolded yet, see `/google-agents-cli-scaffold` first.

## Deployment Architecture

Agent Runtime uses **container-based deployment**: `agents-cli deploy` packages your project and Agent Engine builds a container image from your project's `Dockerfile` (required at the project root; scaffolded projects ship one).

File selection honors the project-root `.gcloudignore`, else the project-root `.gitignore` (nested `.gitignore` files are not consulted).

**App object:** the container runs `uvicorn app.fast_api_app:app`, the same
entrypoint as Cloud Run and GKE — there is no top-level `AgentEngineApp`/`AdkApp`
deployment entrypoint anymore, the container serves HTTP directly. Which routes
that app exposes depends on the framework; check `app/fast_api_app.py`.
`agents-cli deploy` always labels the deployment `agent_framework = "google-adk"`
(see `service.tf`) — that label picks the Console playground, it does not
constrain the container.

> **ADK projects.** `fast_api_app.py` builds the FastAPI `app` via
> `get_fast_api_app(web=True, lifespan=...)`. The lifespan builds one `Runner`
> from the shared session/artifact services (`app_utils/services.py`) and mounts
> A2A routes (`attach_a2a_routes`); `attach_reasoning_engine_routes(app)` adds
> the reasoning_engine contract routes (the adapter constructs an `AdkApp`
> internally to dispatch the native `:streamQuery`/`:query` contract). So the
> container serves the ADK HTTP surface (`/run_sse`, `/apps/...`), the A2A routes
> under `/a2a/{app_name}` (JSON-RPC + agent card), and the reasoning_engine
> adapter routes `/api/reasoning_engine` + `/api/stream_reasoning_engine` (used
> by the Console Playground and Gemini Enterprise ADK registration).

### The `/api` HTTP passthrough

Agent Engine exposes the container's HTTP routes externally under an
`/api` prefix, so deployed agents are reachable without a public Cloud Run URL:

```
https://{location}-aiplatform.googleapis.com/reasoningEngines/v1/{resource}/api/{container_path}
```

where `{resource}` is the full `projects/.../reasoningEngines/...` name. For
example, the A2A agent card (container route `/a2a/{agent_directory}/.well-known/agent-card.json`)
is reachable at:

```
https://{location}-aiplatform.googleapis.com/reasoningEngines/v1/{resource}/api/a2a/{agent_directory}/.well-known/agent-card.json
```

`{agent_directory}` is the app name (the project's `agent_directory`, recorded in
`deployment_metadata.json`). This is the exact URL `deploy` advertises on
success and `run` constructs for `--mode a2a` against an Agent Runtime URL — both
authenticate with your Google credentials. On Agent Runtime, `publish` defaults
to **ADK** registration (`:streamQuery` against the reasoning-engine resource
name) rather than this card URL; pass `--registration-type a2a` if your container
serves only A2A.

## Deploying

Deploy with `agents-cli deploy` (run `agents-cli deploy --help` for the full flag reference). CI/CD pipelines invoke the same command.

**Deployment flow:**
1. `agents-cli deploy` packages the project files (honoring `.gcloudignore`/`.gitignore`)
2. Agent Engine builds the container image and creates/updates the Agent Runtime instance
3. Writes `deployment_metadata.json` with the engine resource ID

## Terraform Resource

Agent Runtime uses `google_vertex_ai_reasoning_engine` in `deployment/terraform/single-project/service.tf` (and the `cicd/service.tf` variant for CI/CD-managed deployments). Check those files for current scaling, concurrency, and resource limit settings.

Key difference from Cloud Run: the `lifecycle.ignore_changes` (covering `container_spec`, `source_code_spec`, and `deployment_spec`) is critical — the image and source are updated by `agents-cli deploy` / CI/CD, not Terraform.

## deployment_metadata.json

Written by `agents-cli deploy` after a successful deployment:

```json
{
  "remote_agent_runtime_id": "projects/PROJECT/locations/LOCATION/reasoningEngines/ENGINE_ID",
  "deployment_target": "agent_runtime",
  "is_a2a": true,
  "agent_directory": "app",
  "deployment_timestamp": "2025-02-25T10:30:00.000+00:00"
}
```

Used by: subsequent deploys (update vs create), `agents-cli run --url`, and `agents-cli publish` (reads the runtime ID for the default ADK registration on Agent Runtime, and constructs the A2A card URL only when A2A registration is explicitly chosen). Cloud Run does not use this file.

If deployment times out but the engine was created, manually populate this file with the engine resource ID.

## CI/CD Differences from Cloud Run

| Aspect | Agent Runtime | Cloud Run |
|--------|-------------|-----------|
| **Build** | Dockerfile → image (built by Agent Engine) | Dockerfile → image (`gcloud builds`) |
| **Deploy command** | `agents-cli deploy` | `gcloud run deploy --image ...` |
| **Artifact** | Container image | Container image in Artifact Registry |
| **Python version** | Configurable in Dockerfile | Configurable in Dockerfile |
| **Load testing** | Via `locust` against Agent Runtime endpoint | Direct HTTP to Cloud Run URL |

## Playground & Remote Testing

```bash
# Local mode (uses local agent instance)
agents-cli playground

# Query your deployed Agent Runtime remotely (ADK projects; use --mode a2a otherwise)
agents-cli run --url https://LOCATION-aiplatform.googleapis.com/v1/projects/PROJECT/locations/LOCATION/reasoningEngines/ID --mode adk "Hello, what can you do?"
```

`--mode` is required with `--url`: use `adk` for the ADK streaming API (`:streamQuery`) or `a2a` for the A2A protocol. Add `-v` for full JSON event payloads. Auth is auto-detected via Google Cloud credentials.

To query Agent Runtime programmatically:

```python
import agentplatform

client = agentplatform.Client(location="us-east1")
agent = client.agent_engines.get(name="projects/PROJECT/locations/LOCATION/reasoningEngines/ENGINE_ID")

async for event in agent.async_stream_query(message="Hello!", user_id="test"):
    print(event)
```

## Session & Artifact Services

> **ADK projects.** The two paragraphs below are the ADK scaffold's session and
> artifact wiring. The environment-variable sources at the end of this section
> apply to any framework.

Agent Runtime always uses in-memory sessions at scaffold time; at runtime `app_utils/services.py` upgrades to `VertexAiSessionService` when Agent Engine injects `GOOGLE_CLOUD_AGENT_ENGINE_ID`. `get_fast_api_app` receives the `shared://session` URI, resolved by `services.py`.

Artifacts use `GcsArtifactService` when `LOGS_BUCKET_NAME` is set, otherwise `InMemoryArtifactService`.

Environment variables set during deployment come from `agents-cli deploy` (the CLI's `deploy/agent_runtime.py`) for SDK deploys, and from `deployment/terraform/single-project/service.tf` (or the `cicd/` variant) for Terraform-managed deploys. Check those for current values.

### Memory Bank

To enable cross-session memory on Agent Runtime, configure `memory_bank_config` via `context_spec`. See the ADK [`cross-session-memory` recipe](https://github.com/google/adk-samples/tree/main/core/python/cross-session-memory) for the full pattern.

## Networking (PSC Interface)

Agent Runtime cannot reach your VPC by default. To enable private connectivity, create a [network attachment](https://cloud.google.com/vpc/docs/create-manage-network-attachments) and deploy with `--network-attachment`. Add `--dns-peering-domain`, `--dns-peering-project`, and `--dns-peering-network` if you need private DNS resolution. PSC config is immutable after deployment — delete and redeploy to change it. See the [GCP docs](https://cloud.google.com/gemini-enterprise-agent-platform/scale/runtime/private-service-connect-interface) for prerequisites and setup.
