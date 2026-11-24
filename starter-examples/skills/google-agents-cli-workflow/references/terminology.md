# Product Name Mapping

The platform is now called **Gemini platform**; users may still call it Vertex AI, Agent Platform, Gemini Enterprise Agent Platform, or GEAP. Map the product names users give to the correct CLI values:

| User may say | CLI value |
|-------------|-----------|
| Agent Runtime, Agent Engine, Vertex AI Agent Engine | `--deployment-target agent_runtime` |
| Sessions in Gemini platform, managed sessions, Agent Engine sessions, Agent Platform Sessions | `--session-type agent_platform_sessions` |
| Search or Vector Search in Gemini platform, Vertex AI Search, Vertex AI Vector Search, RAG | clone-and-study recipe, not a flag (see `/google-agents-cli-adk-code` → `references/recipes.md`) |
| Gemini, Gemini Enterprise (publish destination) | `agents-cli publish gemini` |

`google-cloud-aiplatform`, ships two importable packages: the older `vertexai` and the newer
`agentplatform`. New code should import `agentplatform`.
