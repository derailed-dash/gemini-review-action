"""
Description: Google Developer Knowledge MCP and API integration module.
Handles authentication via GEMINI_API_KEY or Application Default Credentials (ADC)
and executes RPC calls to search and fetch official Google developer documentation.
"""

import json
import os
import sys

import requests

from gemini_review.utils import _get_pr_review_func


def get_google_auth_headers() -> dict:
    """Generate authentication headers for calling the Google Developer Knowledge API."""
    headers = {"Content-Type": "application/json"}

    # 1. Use GEMINI_API_KEY if present
    gemini_api_key = os.environ.get("GEMINI_API_KEY")
    if gemini_api_key:
        headers["X-Goog-Api-Key"] = gemini_api_key
        return headers

    # 2. Try falling back to Google Cloud Application Default Credentials (ADC)
    try:
        import google.auth
        import google.auth.transport.requests

        credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        auth_req = google.auth.transport.requests.Request()
        credentials.refresh(auth_req)
        if credentials.token:
            headers["Authorization"] = f"Bearer {credentials.token}"
            return headers
        return {}
    except Exception as e:
        print(f"Warning: Failed to fetch Application Default Credentials for Developer Knowledge: {e}", file=sys.stderr)
        return {}


def search_google_developer_knowledge(query: str) -> str:
    """Searches official Google developer documentation for APIs, best practices, guides, and troubleshooting.

    Args:
        query: The search query, e.g. 'How to configure Google Cloud Run with custom domains'.
    """
    fn_get_google_auth_headers = _get_pr_review_func("get_google_auth_headers", get_google_auth_headers)
    print(f"Tool Call: search_google_developer_knowledge(query='{query}') invoked by agent.", file=sys.stderr)
    headers = fn_get_google_auth_headers()
    if not headers or ("X-Goog-Api-Key" not in headers and "Authorization" not in headers):
        return (
            "Error: No API key or Application Default Credentials found. Google Developer Knowledge Search is"
            " unavailable."
        )

    url = "https://developerknowledge.googleapis.com/mcp"
    payload = {
        "jsonrpc": "2.0",
        "method": "tools/call",
        "params": {"name": "search_documents", "arguments": {"query": query}},
        "id": 1,
    }

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=15)
        if response.status_code != 200:
            return f"Error from Google Developer Knowledge API: {response.status_code} - {response.text}"

        data = response.json()
        if "error" in data:
            return f"API Error: {json.dumps(data['error'])}"

        result = data.get("result", {})
        content_list = result.get("content", [])

        text_outputs = []
        for item in content_list:
            if item.get("type") == "text":
                text_outputs.append(item.get("text", ""))

        if not text_outputs:
            return "No matching documentation found."

        return "\n\n".join(text_outputs)
    except Exception as e:
        return f"Error calling Google Developer Knowledge API: {e}"


def get_google_developer_documents(names: list[str]) -> str:
    """Retrieves the full content of one or more documents from the Google developer documentation.

    Args:
        names: A list of document names/URIs returned by search_google_developer_knowledge.
               Format of each name: 'documents/docs.cloud.google.com/...'
    """
    fn_get_google_auth_headers = _get_pr_review_func("get_google_auth_headers", get_google_auth_headers)
    print(f"Tool Call: get_google_developer_documents(names={names}) invoked by agent.", file=sys.stderr)
    headers = fn_get_google_auth_headers()
    if not headers or ("X-Goog-Api-Key" not in headers and "Authorization" not in headers):
        return "Error: No API key or Application Default Credentials found. Document retrieval is unavailable."

    url = "https://developerknowledge.googleapis.com/mcp"
    payload = {
        "jsonrpc": "2.0",
        "method": "tools/call",
        "params": {"name": "get_documents", "arguments": {"names": names}},
        "id": 1,
    }

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=15)
        if response.status_code != 200:
            return f"Error from Google Developer Knowledge API: {response.status_code} - {response.text}"

        data = response.json()
        if "error" in data:
            return f"API Error: {json.dumps(data['error'])}"

        result = data.get("result", {})

        content_list = result.get("content", [])

        text_outputs = []
        for item in content_list:
            if item.get("type") == "text":
                text_outputs.append(item.get("text", ""))

        if not text_outputs:
            return "Document content is empty or not found."

        return "\n\n".join(text_outputs)
    except Exception as e:
        return f"Error calling Google Developer Knowledge API: {e}"
