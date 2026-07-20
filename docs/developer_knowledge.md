# Google Developer Knowledge MCP Integration

This action supports the **Google Developer Knowledge Model Context Protocol (MCP)**. When enabled, it empowers the Gemini code reviewer to query official, up-to-date documentation across Google platforms (such as Google Cloud, Firebase, Android, and Flutter) to verify your PR against real-time best practices.

---

## How It Works

The runner checks if your environment has the credentials necessary to authenticate against the Google Developer Knowledge endpoint at `https://developerknowledge.googleapis.com/mcp`. 

By default, the script looks for:
1.  Your `GEMINI_API_KEY` (typically from Google AI Studio).
2.  Standard Google Cloud Application Default Credentials (ADC) if running in a Google Cloud environment or with a Vertex AI backend.

If either is detected and has access, the reviewer dynamically registers the `search_google_developer_knowledge` and `get_google_developer_documents` tools.

---

## Configuration & Prerequisites

To empower the PR reviewer with this API, follow these setup steps:

### 1. Enable the API

You must enable the **Developer Knowledge API** in the Google Cloud Project associated with your API key or credentials:

```bash
gcloud services enable developerknowledge.googleapis.com
```

### 2. Enable the MCP Service

You must also enable the MCP service specifically on the target project:

```bash
gcloud beta services mcp enable developerknowledge.googleapis.com
```

### 3. Ensure API Key Access

Ensure that the API key provided in the `GEMINI_API_KEY` environment variable is not restricted from calling the Developer Knowledge API. If you have API key restrictions configured in the Google Cloud Console, make sure to add `Developer Knowledge API` to the list of allowed APIs.

---

## More Information

For further setup details, credential options, and documentation coverage, please visit the official documentation page:
*   [Google Developer Knowledge MCP Setup Guide](https://developers.google.com/knowledge/mcp)
