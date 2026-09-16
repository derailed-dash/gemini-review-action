"""Multimodal image and document processing utilities.

This module provides detection, MIME resolution, deterministic Pillow downscaling,
and Markdown image reference extraction for visual assets (PNG, JPEG, WebP, GIF, SVG)
and document formats (PDF).
"""

import io
import mimetypes
import os
import re
import sys

from PIL import Image

SUPPORTED_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg"}
SUPPORTED_MULTIMODAL_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg", ".pdf"}


def is_supported_image(filename: str) -> bool:
    """Check if the given filename is a supported image format."""
    _, ext = os.path.splitext(filename.lower())
    return ext in SUPPORTED_IMAGE_EXTENSIONS


def is_supported_multimodal_file(filename: str) -> bool:
    """Check if the given filename is a supported multimodal file (image or PDF)."""
    _, ext = os.path.splitext(filename.lower())
    return ext in SUPPORTED_MULTIMODAL_EXTENSIONS


def get_mime_type(filename: str) -> str:
    """Determine MIME type for a file with reliable overrides for modern image types."""
    _, ext = os.path.splitext(filename.lower())
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
    mime, _ = mimetypes.guess_type(filename)
    return mime or "application/octet-stream"


def optimize_image_bytes(
    data: bytes,
    filename: str,
    trigger_bytes: int = 614400,
    target_bytes: int = 307200,
) -> tuple[bytes, str]:
    """Deterministically downscale and compress raster images exceeding trigger_bytes (~600KB).

    Leaves vector SVGs, PDFs, and images under trigger_bytes intact.
    """
    mime = get_mime_type(filename)
    _, ext = os.path.splitext(filename.lower())

    if len(data) <= trigger_bytes or ext in (".svg", ".pdf"):
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
