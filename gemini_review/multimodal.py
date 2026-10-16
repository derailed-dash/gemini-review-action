"""Multimodal image and document processing utilities.

This module provides detection, MIME resolution, deterministic Pillow downscaling,
local Markdown image reference extraction, and remote screenshot extraction/fetching
for visual assets (PNG, JPEG, WebP, GIF, SVG) and document formats (PDF).
"""

import html
import io
import ipaddress
import mimetypes
import os
import re
import sys
import urllib.parse
from collections.abc import Sequence
from typing import Any

import requests
from google.genai import types
from PIL import Image

from gemini_review.config import (
    get_image_target_bytes,
    get_image_trigger_bytes,
    get_max_triage_images,
)

SUPPORTED_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg"}
SUPPORTED_MULTIMODAL_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg", ".pdf"}

DEFAULT_MAX_TRIAGE_IMAGES = 5
DEFAULT_MAX_REMOTE_IMAGE_BYTES = 20 * 1024 * 1024  # 20 MB cap per remote image download


_MIME_TO_EXTENSION = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/svg+xml": ".svg",
}


def _strip_query_and_fragment(filename: str) -> str:
    """Strip query string and fragment components from a file path or URL."""
    return filename.split("?")[0].split("#")[0]


def is_supported_image(filename: str) -> bool:
    """Check if the given filename is a supported image format."""
    _, ext = os.path.splitext(_strip_query_and_fragment(filename).lower())
    return ext in SUPPORTED_IMAGE_EXTENSIONS


def is_supported_multimodal_file(filename: str) -> bool:
    """Check if the given filename is a supported multimodal file (image or PDF)."""
    _, ext = os.path.splitext(_strip_query_and_fragment(filename).lower())
    return ext in SUPPORTED_MULTIMODAL_EXTENSIONS


def get_mime_type(filename: str) -> str:
    """Determine MIME type for a file with reliable overrides for modern image types."""
    clean_name = _strip_query_and_fragment(filename)
    _, ext = os.path.splitext(clean_name.lower())
    overrides = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".gif": "image/gif",
        ".svg": "image/svg+xml",
        ".pdf": "application/pdf",
    }
    if ext in overrides:
        return overrides[ext]
    mime, _ = mimetypes.guess_type(clean_name)
    return mime or "application/octet-stream"


def _detect_image_mime_from_bytes(data: bytes) -> str | None:
    """Inspect binary header or Pillow image metadata to identify image MIME type."""
    if not data:
        return None
    head = data[:256].lstrip()
    if head.startswith((b"<svg", b"<?xml")) and b"<svg" in data[:1024].lower():
        return "image/svg+xml"
    try:
        with Image.open(io.BytesIO(data)) as img:
            fmt = (img.format or "").upper()
            fmt_map = {
                "PNG": "image/png",
                "JPEG": "image/jpeg",
                "JPG": "image/jpeg",
                "WEBP": "image/webp",
                "GIF": "image/gif",
            }
            if fmt in fmt_map:
                return fmt_map[fmt]
    except Exception:
        pass
    return None


def optimize_image_bytes(
    data: bytes,
    filename: str,
    trigger_bytes: int = 614400,
    target_bytes: int = 307200,
) -> tuple[bytes, str]:
    """Deterministically downscale and compress raster images exceeding trigger_bytes (~600KB).

    Leaves vector SVGs, PDFs, and images under trigger_bytes intact.
    """
    clean_name = _strip_query_and_fragment(filename)
    mime = get_mime_type(clean_name)
    if mime == "application/octet-stream":
        detected = _detect_image_mime_from_bytes(data)
        if detected:
            mime = detected
    _, ext = os.path.splitext(clean_name.lower())

    if len(data) <= trigger_bytes or ext in (".svg", ".pdf") or mime in ("image/svg+xml", "application/pdf"):
        return data, mime

    try:
        with Image.open(io.BytesIO(data)) as img:
            orig_format = img.format or "PNG"
            orig_width, orig_height = img.size

            # Estimate reduction factor needed to reach target_bytes
            scale_factor = (target_bytes / len(data)) ** 0.5
            # Bound scale factor between 0.2 and 0.85 to avoid excessive degradation
            scale_factor = max(0.2, min(scale_factor, 0.85))

            new_width = max(1, int(orig_width * scale_factor))
            new_height = max(1, int(orig_height * scale_factor))

            # Maintain transparency / color mode
            resample_mode = Image.Resampling.LANCZOS
            resized = img.resize((new_width, new_height), resample=resample_mode)

            out_buf = io.BytesIO()
            save_kwargs = {}
            if orig_format.upper() in ("JPEG", "JPG"):
                save_kwargs = {"quality": 82, "optimize": True}
                if resized.mode in ("RGBA", "P"):
                    resized = resized.convert("RGB")
            elif orig_format.upper() == "PNG":
                save_kwargs = {"optimize": True}
            elif orig_format.upper() == "WEBP":
                save_kwargs = {"quality": 82}

            resized.save(out_buf, format=orig_format, **save_kwargs)
            out_bytes = out_buf.getvalue()

            print(
                f"Optimised large image '{filename}' ({len(data):,} bytes -> {len(out_bytes):,} bytes, "
                f"{orig_width}x{orig_height} -> {new_width}x{new_height}).",
                file=sys.stderr,
            )
            return out_bytes, mime
    except Exception as e:
        print(f"Warning: Failed to downscale image '{filename}': {e}. Using original bytes.", file=sys.stderr)
        return data, mime


def extract_markdown_image_references(
    md_content: str,
    md_file_path: str,
    base_dir: str = ".",
    repo_root: str | None = None,
) -> list[str]:
    """Extract local image and PDF file references embedded within a markdown document.

    Resolves both standard markdown images `![alt](path)` and HTML `<img src="path">`.
    Filters out external URLs (http/https/data) and verifies path traversal security
    using os.path.commonpath.
    """
    if repo_root is not None:
        base_dir = repo_root
    if not md_content:
        return []

    found_refs: list[str] = []
    seen_refs: set[str] = set()

    # Match standard markdown ![alt](path) or HTML <img src="path"> in document order
    combined_pattern = re.compile(
        r"""!\[.*?\]\(\s*([^\s\)\"\']+)(?:\s+[\"\'].*?[\"\'])?\s*\)|<img\b[^>]*?\bsrc\s*=\s*["']([^"']+)["']""",
        re.IGNORECASE,
    )
    for match in combined_pattern.finditer(md_content):
        ref = (match.group(1) or match.group(2) or "").strip()
        if ref and ref not in seen_refs:
            seen_refs.add(ref)
            found_refs.append(ref)

    base_abs = os.path.abspath(base_dir)
    md_dir = os.path.dirname(os.path.abspath(os.path.join(base_dir, md_file_path)))

    valid_images: list[str] = []

    for ref in found_refs:
        # Ignore external or inline data URLs
        if ref.startswith(("http://", "https://", "data:", "ftp://", "#")):
            continue

        clean_ref = ref.split("?")[0].split("#")[0]
        if not clean_ref:
            continue

        # If reference starts with '/', interpret relative to repository root
        if clean_ref.startswith("/"):
            candidate_abs = os.path.abspath(os.path.join(base_abs, clean_ref.lstrip("/")))
        else:
            candidate_abs = os.path.abspath(os.path.join(md_dir, clean_ref))

        # Security check: prevent path traversal outside repository root
        try:
            if os.path.commonpath([base_abs, candidate_abs]) != base_abs:
                continue
        except ValueError:
            continue

        norm_rel_path = os.path.relpath(candidate_abs, base_abs).replace("\\", "/")

        if is_supported_multimodal_file(norm_rel_path) and os.path.isfile(candidate_abs):
            if norm_rel_path not in valid_images:
                valid_images.append(norm_rel_path)

    return valid_images


def _is_github_host(hostname: str) -> bool:
    """Return True if the hostname belongs to github.com or githubusercontent.com."""
    host = (hostname or "").lower().strip()
    return (
        host == "github.com"
        or host.endswith(".github.com")
        or host == "githubusercontent.com"
        or host.endswith(".githubusercontent.com")
    )


def _is_unsafe_or_local_host(hostname: str) -> bool:
    """Return True if the hostname targets localhost, loopback, link-local metadata, or private IPs."""
    host = (hostname or "").lower().strip().strip("[]")
    if not host:
        return True
    if host in ("localhost", "metadata.google.internal") or host.endswith((".local", ".internal", ".localhost")):
        return True
    try:
        ip = ipaddress.ip_address(host)
        return (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        )
    except ValueError:
        pass
    if host.startswith(("127.", "10.", "192.168.", "169.254.")):
        return True
    return False


def extract_remote_image_urls(
    md_content: str | None,
    comments: Sequence[dict[str, Any] | str] | None = None,
) -> list[str]:
    """Extract remote HTTPS image URLs from issue markdown bodies and discussion comments.

    Supports both standard Markdown `![alt](https://...)` and HTML `<img src="https://...">`,
    including extensionless GitHub user-uploaded asset URLs (`github.com/user-attachments/assets/...`,
    `githubusercontent.com/...`). Preserves document order and deduplicates URLs.
    """
    text_segments: list[str] = []
    if md_content:
        text_segments.append(md_content)
    for c in comments or []:
        if isinstance(c, dict):
            body = c.get("body")
            if isinstance(body, str) and body:
                text_segments.append(body)
        elif isinstance(c, str) and c:
            text_segments.append(c)

    if not text_segments:
        return []

    combined_pattern = re.compile(
        r"""!\[.*?\]\(\s*<?([^\s\)\"\'>]+)>?(?:\s+[\"\'].*?[\"\'])?\s*\)|<img\b[^>]*?\bsrc\s*=\s*["']([^"']+)["']""",
        re.IGNORECASE,
    )

    urls: list[str] = []
    seen: set[str] = set()

    for segment in text_segments:
        for match in combined_pattern.finditer(segment):
            raw_ref = (match.group(1) or match.group(2) or "").strip()
            if not raw_ref:
                continue
            ref = html.unescape(raw_ref)
            if not ref.lower().startswith("https://"):
                continue

            try:
                parsed = urllib.parse.urlparse(ref)
            except Exception:
                continue

            if parsed.scheme.lower() != "https" or _is_unsafe_or_local_host(parsed.hostname or ""):
                continue

            # Allow URLs with a supported image extension OR extensionless URLs
            # (such as github.com/user-attachments/assets/<uuid> or camo.githubusercontent.com/<hash>).
            _, ext = os.path.splitext(parsed.path.lower())
            if ext and ext not in SUPPORTED_IMAGE_EXTENSIONS:
                continue

            if ref not in seen:
                seen.add(ref)
                urls.append(ref)

    return urls


def _extract_bearer_token(github_token: str | None, headers: dict[str, str] | None) -> str | None:
    """Resolve a raw GitHub token string from explicit argument, headers dict, or environment."""
    raw = github_token
    if not raw and headers:
        raw = headers.get("Authorization") or headers.get("authorization")
    if not raw:
        raw = os.environ.get("GITHUB_TOKEN")
    if not raw or not isinstance(raw, str):
        return None
    token = raw.strip()
    for prefix in ("Bearer ", "bearer ", "token ", "Token "):
        if token.startswith(prefix):
            token = token[len(prefix) :].strip()
            break
    return token or None


def fetch_remote_image(
    url: str,
    github_token: str | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 60,
    max_bytes: int = DEFAULT_MAX_REMOTE_IMAGE_BYTES,
) -> tuple[bytes, str] | None:
    """Fetch remote image bytes using streaming with size limits and GitHub authentication.

    For GitHub-hosted assets (`github.com`, `githubusercontent.com`), attaches
    `Authorization: Bearer <GITHUB_TOKEN>` so private repository screenshots can be retrieved.
    Never attaches credentials to third-party hosts.
    Returns `(image_bytes, mime_type)` or `None` if download fails, times out, or exceeds `max_bytes`.
    """
    if not url or not isinstance(url, str):
        return None

    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        return None

    hostname = parsed.hostname or ""
    if parsed.scheme.lower() != "https" or _is_unsafe_or_local_host(hostname):
        print(f"Warning: Skipping unsafe or non-HTTPS remote image URL '{url}'.", file=sys.stderr)
        return None

    req_headers: dict[str, str] = {"Accept": "image/*, */*;q=0.8"}
    token = _extract_bearer_token(github_token, headers)
    if token and _is_github_host(hostname):
        req_headers["Authorization"] = f"Bearer {token}"

    res = None
    try:
        res = requests.get(url, headers=req_headers, timeout=timeout, stream=True)
        # Pre-signed githubusercontent.com URLs can reject requests that also carry an Authorization header
        if (
            res.status_code in (400, 403)
            and "Authorization" in req_headers
            and hostname.lower().endswith("githubusercontent.com")
        ):
            if hasattr(res, "close"):
                res.close()
            no_auth_headers = {k: v for k, v in req_headers.items() if k != "Authorization"}
            res = requests.get(url, headers=no_auth_headers, timeout=timeout, stream=True)

        if res.status_code != 200:
            print(
                f"Warning: Failed to download remote screenshot '{url}' (HTTP {res.status_code}).",
                file=sys.stderr,
            )
            return None

        resp_headers = getattr(res, "headers", None) or {}
        if isinstance(resp_headers, dict) or hasattr(resp_headers, "get"):
            content_length_raw = resp_headers.get("Content-Length") or resp_headers.get("content-length")
            if content_length_raw is not None:
                try:
                    if int(content_length_raw) > max_bytes:
                        print(
                            f"Warning: Remote screenshot '{url}' exceeds max size limit "
                            f"({content_length_raw} > {max_bytes} bytes). Skipping.",
                            file=sys.stderr,
                        )
                        return None
                except (ValueError, TypeError):
                    pass

            content_type_raw = resp_headers.get("Content-Type") or resp_headers.get("content-type") or ""
        else:
            content_type_raw = ""

        ct = content_type_raw.split(";")[0].strip().lower() if isinstance(content_type_raw, str) else ""
        if ct and (ct.startswith(("text/", "application/json", "application/xml")) and ct != "image/svg+xml"):
            print(
                f"Warning: Remote screenshot URL '{url}' returned non-image Content-Type '{ct}'. Skipping.",
                file=sys.stderr,
            )
            return None

        buf = bytearray()
        try:
            chunks = res.iter_content(chunk_size=65536)
            for chunk in chunks:
                if not chunk:
                    continue
                buf.extend(chunk)
                if len(buf) > max_bytes:
                    print(
                        f"Warning: Remote screenshot '{url}' exceeded {max_bytes:,} bytes during download. Skipping.",
                        file=sys.stderr,
                    )
                    return None
        except TypeError:
            # Fallback if a unit test mock did not configure iter_content as an iterable
            raw_content = getattr(res, "content", b"")
            if isinstance(raw_content, bytes):
                if len(raw_content) > max_bytes:
                    return None
                buf.extend(raw_content)

        data = bytes(buf)
        if not data:
            return None

        # Resolve MIME type from Content-Type header, URL extension, or binary inspection
        if ct == "image/jpg":
            ct = "image/jpeg"
        if ct in _MIME_TO_EXTENSION:
            mime = ct
        else:
            url_mime = get_mime_type(url)
            if url_mime in _MIME_TO_EXTENSION:
                mime = url_mime
            else:
                detected_mime = _detect_image_mime_from_bytes(data)
                if not detected_mime:
                    print(
                        f"Warning: Remote file '{url}' is not a recognised image format. Skipping.",
                        file=sys.stderr,
                    )
                    return None
                mime = detected_mime

        return data, mime
    except Exception as e:
        print(f"Warning: Error downloading remote screenshot '{url}': {e}. Continuing without it.", file=sys.stderr)
        return None
    finally:
        if res is not None and hasattr(res, "close"):
            try:
                res.close()
            except Exception:
                pass


def build_triage_multimodal_parts(
    issue_body: str | None,
    issue_comments: Sequence[dict[str, Any] | str] | None = None,
    github_token: str | None = None,
    headers: dict[str, str] | None = None,
    config: dict | None = None,
    timeout: int = 60,
    max_images: int | None = None,
) -> list[Any]:
    """Extract, download, downscale, and assemble remote issue screenshots into Gemini Part objects.

    Caps attached screenshots at `max_images` (default: 5 via `get_max_triage_images`),
    downscales large raster images via `optimize_image_bytes`, and gracefully skips any
    screenshots that fail or time out.
    """
    effective_max = max_images if max_images is not None else get_max_triage_images(config)
    if effective_max <= 0:
        return []

    urls = extract_remote_image_urls(issue_body, issue_comments)
    if not urls:
        return []

    trigger_bytes = get_image_trigger_bytes(config)
    target_bytes = get_image_target_bytes(config)
    parts: list[Any] = []

    for url in urls:
        if len(parts) >= effective_max:
            print(
                f"Triage multimodal context: reached maximum triage screenshot cap ({effective_max}). "
                f"Skipping remaining {len(urls) - len(parts)} image URL(s).",
                file=sys.stderr,
            )
            break

        fetched = fetch_remote_image(
            url,
            github_token=github_token,
            headers=headers,
            timeout=timeout,
        )
        if fetched is None:
            continue

        raw_bytes, fetched_mime = fetched
        # Ensure optimize_image_bytes has a filename extension hint for extensionless GitHub asset URLs
        filename_hint = _strip_query_and_fragment(url)
        if get_mime_type(filename_hint) == "application/octet-stream":
            ext_hint = _MIME_TO_EXTENSION.get(fetched_mime, ".png")
            filename_hint = f"{filename_hint}{ext_hint}"

        opt_bytes, final_mime = optimize_image_bytes(
            raw_bytes,
            filename_hint,
            trigger_bytes=trigger_bytes,
            target_bytes=target_bytes,
        )
        if final_mime == "application/octet-stream":
            final_mime = fetched_mime

        part = types.Part.from_bytes(data=opt_bytes, mime_type=final_mime)
        parts.append(part)
        print(
            f"Triage multimodal context: attached remote screenshot '{url}' ({len(opt_bytes):,} bytes, {final_mime}).",
            file=sys.stderr,
        )

    return parts
