"""
Description: Tests for multimodal context (Images & PDFs), Markdown image reference extraction,
Pillow deterministic downscaling, and visual PR diffing.
"""

import io
import os

from PIL import Image

from gemini_review import (
    extract_markdown_image_references,
    get_image_target_bytes,
    get_image_trigger_bytes,
    get_max_multimodal_images,
    get_mime_type,
    is_supported_image,
    is_supported_multimodal_file,
    optimize_image_bytes,
)


def test_is_supported_image():
    assert is_supported_image("diagram.png") is True
    assert is_supported_image("photo.jpg") is True
    assert is_supported_image("photo.jpeg") is True
    assert is_supported_image("graphic.webp") is True
    assert is_supported_image("animation.gif") is True
    assert is_supported_image("vector.svg") is True
    assert is_supported_image("DOC.PNG") is True

    assert is_supported_image("spec.pdf") is False
    assert is_supported_image("code.py") is False
    assert is_supported_image("data.json") is False
    assert is_supported_image("archive.zip") is False


def test_is_supported_multimodal_file():
    assert is_supported_multimodal_file("diagram.png") is True
    assert is_supported_multimodal_file("spec.pdf") is True
    assert is_supported_multimodal_file("vector.svg") is True
    assert is_supported_multimodal_file("code.py") is False
    assert is_supported_multimodal_file("archive.zip") is False


def test_get_mime_type():
    assert get_mime_type("img.png") == "image/png"
    assert get_mime_type("img.jpg") == "image/jpeg"
    assert get_mime_type("img.jpeg") == "image/jpeg"
    assert get_mime_type("img.webp") == "image/webp"
    assert get_mime_type("img.gif") == "image/gif"
    assert get_mime_type("img.svg") == "image/svg+xml"
    assert get_mime_type("doc.pdf") == "application/pdf"
    assert get_mime_type("unknown.unknownext") == "application/octet-stream"


def test_multimodal_config_defaults_and_env(monkeypatch):
    assert get_max_multimodal_images({}) == 20
    assert get_image_trigger_bytes({}) == 614400
    assert get_image_target_bytes({}) == 307200

    monkeypatch.setenv("GEMINI_MAX_MULTIMODAL_IMAGES", "10")
    monkeypatch.setenv("GEMINI_IMAGE_TRIGGER_BYTES", "500000")
    monkeypatch.setenv("GEMINI_IMAGE_TARGET_BYTES", "200000")

    assert get_max_multimodal_images({}) == 10
    assert get_image_trigger_bytes({}) == 500000
    assert get_image_target_bytes({}) == 200000


def test_extract_markdown_image_references(tmp_path):
    repo_root = str(tmp_path)
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    img_dir = docs_dir / "images"
    img_dir.mkdir()

    arch_img = img_dir / "arch.png"
    arch_img.write_bytes(b"fake_png_data")

    flow_img = docs_dir / "flow.webp"
    flow_img.write_bytes(b"fake_webp_data")

    md_file = docs_dir / "architecture.md"
    md_content = """# Architecture Overview
Here is the system architecture diagram:
![System Architecture](images/arch.png "Architecture")

And here is the data flow:
<img src="./flow.webp" alt="Data flow" width="500" />

External link should be ignored:
![External](https://example.com/logo.png)

Path traversal attempt should be safely ignored:
![Secret](../../outside.png)

Non-existent file should be ignored:
![Missing](images/nonexistent.png)

Duplicate reference:
![Arch Again](images/arch.png)
"""
    md_file.write_text(md_content, encoding="utf-8")

    refs = extract_markdown_image_references(
        md_content=md_content,
        md_file_path=os.path.join("docs", "architecture.md"),
        repo_root=repo_root,
    )

    expected = [
        os.path.join("docs", "images", "arch.png"),
        os.path.join("docs", "flow.webp"),
    ]
    assert refs == expected


def test_optimize_image_bytes_small():
    # Small image (under 600KB) should remain untouched
    img = Image.new("RGB", (100, 100), color="red")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    raw_bytes = buf.getvalue()

    optimized_bytes, mime = optimize_image_bytes(
        data=raw_bytes,
        filename="test.png",
        trigger_bytes=614400,
        target_bytes=307200,
    )
    assert optimized_bytes == raw_bytes
    assert mime == "image/png"


def test_optimize_image_bytes_large():
    # Generate a large synthetic image that exceeds 600KB
    img = Image.new("RGB", (3000, 2000), color="blue")
    buf = io.BytesIO()
    # Save as uncompressed BMP or large PNG to exceed 600KB
    img.save(buf, format="PNG", compress_level=0)
    raw_bytes = buf.getvalue()
    assert len(raw_bytes) > 614400

    optimized_bytes, mime = optimize_image_bytes(
        data=raw_bytes,
        filename="large.png",
        trigger_bytes=614400,
        target_bytes=307200,
    )

    assert len(optimized_bytes) < len(raw_bytes)
    assert len(optimized_bytes) <= 450000  # Well within target range
    assert mime == "image/png"

    # Verify that the optimised image is valid and readable
    with Image.open(io.BytesIO(optimized_bytes)) as opt_img:
        assert opt_img.size[0] < 3000
        assert opt_img.size[1] < 2000


def test_optimize_image_bytes_passthrough_pdf_and_rasterize_svg():
    from gemini_review import is_gemini_supported_mime, rasterize_svg_bytes

    pdf_bytes = b"%PDF-1.4 large fake pdf bytes " * 30000
    assert len(pdf_bytes) > 614400
    out, mime = optimize_image_bytes(pdf_bytes, "doc.pdf")
    assert out == pdf_bytes
    assert mime == "application/pdf"

    valid_svg = (
        b'<svg xmlns="http://www.w3.org/2000/svg" width="120" height="80">'
        b'<rect width="120" height="80" fill="#2563eb"/>'
        b"</svg>"
    )
    out_svg, mime_svg = optimize_image_bytes(valid_svg, "vector.svg")
    assert mime_svg == "image/png"
    assert is_gemini_supported_mime(mime_svg) is True
    assert is_gemini_supported_mime("image/svg+xml") is False
    assert out_svg.startswith(b"\x89PNG\r\n\x1a\n")
    with Image.open(io.BytesIO(out_svg)) as png_img:
        assert png_img.size == (120, 80)

    assert rasterize_svg_bytes(b"") is None
    assert rasterize_svg_bytes(b"<html>not an svg</html>") is None


def test_build_codebase_context_rasterizes_svg_and_skips_corrupt_svg(tmp_path, monkeypatch):
    from gemini_review import build_codebase_context

    monkeypatch.chdir(tmp_path)
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    valid_svg = docs_dir / "flow.svg"
    valid_svg.write_bytes(
        b'<svg xmlns="http://www.w3.org/2000/svg" width="100" height="60">'
        b'<rect width="100" height="60" fill="green"/></svg>'
    )
    corrupt_svg = docs_dir / "corrupt.svg"
    corrupt_svg.write_bytes(b"not valid xml or svg at all")

    md_text = "# Flow\n![Flow](docs/flow.svg)\n![Corrupt](docs/corrupt.svg)\n"
    arch_doc = tmp_path / "ARCHITECTURE.md"
    arch_doc.write_text(md_text, encoding="utf-8")

    monkeypatch.setattr(
        "gemini_review.prompts.get_all_repo_files",
        lambda: ["ARCHITECTURE.md", "docs/flow.svg", "docs/corrupt.svg"],
    )
    monkeypatch.setattr(
        "gemini_review.prompts.get_file_content",
        lambda f: md_text if f == "ARCHITECTURE.md" else "",
    )

    multimodal_parts = []
    build_codebase_context(
        files=[{"filename": "src/main.py", "status": "modified"}],
        config={"max_context_bytes": 10000},
        multimodal_parts=multimodal_parts,
    )

    # Only the valid SVG should be attached (rasterised to image/png); corrupt.svg must be skipped
    assert len(multimodal_parts) == 2
    assert "docs/flow.svg" in multimodal_parts[0]
    assert multimodal_parts[1].inline_data.mime_type == "image/png"
    assert multimodal_parts[1].inline_data.data.startswith(b"\x89PNG\r\n\x1a\n")


def test_count_text_tokens_multimodal():
    from google.genai import types

    from gemini_review import count_text_tokens

    # Text only (100 chars -> 25 tokens)
    text_sample = "a" * 100
    assert count_text_tokens(None, "gemini-3.8-flash", text_sample) == 25

    # Heterogeneous list with a text string and a Part (~25 tokens + 258 tokens = 283)
    part = types.Part.from_bytes(data=b"fake_image_bytes", mime_type="image/png")
    contents = [text_sample, part]
    assert count_text_tokens(None, "gemini-3.8-flash", contents) == 283


def test_build_codebase_context_with_markdown_image(tmp_path, monkeypatch):
    from gemini_review import build_codebase_context

    monkeypatch.chdir(tmp_path)
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    img = docs_dir / "diagram.png"
    img.write_bytes(b"png_bytes_content")

    arch_doc = tmp_path / "ARCHITECTURE.md"
    arch_doc.write_text("# Arch\n![Diagram](docs/diagram.png)\n", encoding="utf-8")

    monkeypatch.setattr("gemini_review.prompts.get_all_repo_files", lambda: ["ARCHITECTURE.md", "docs/diagram.png"])
    monkeypatch.setattr(
        "gemini_review.prompts.get_file_content",
        lambda f: "# Arch\n![Diagram](docs/diagram.png)\n" if f == "ARCHITECTURE.md" else "",
    )

    multimodal_parts = []
    context = build_codebase_context(
        files=[{"filename": "src/main.py", "status": "modified"}],
        config={"max_context_bytes": 1000},
        multimodal_parts=multimodal_parts,
    )

    assert "ARCHITECTURE.md" in context
    assert len(multimodal_parts) == 2
    assert "Visual Context" in multimodal_parts[0]
    assert hasattr(multimodal_parts[1], "inline_data")
    assert multimodal_parts[1].inline_data.data == b"png_bytes_content"


def test_build_codebase_context_with_extra_multimodal_files(tmp_path, monkeypatch):
    from gemini_review import build_codebase_context

    monkeypatch.chdir(tmp_path)
    pdf_file = tmp_path / "spec.pdf"
    pdf_file.write_bytes(b"%PDF-1.4 fake_pdf_data")

    monkeypatch.setattr("gemini_review.prompts.get_all_repo_files", lambda: ["README.md", "spec.pdf"])
    monkeypatch.setattr("gemini_review.prompts.get_file_content", lambda f: "# Readme" if f == "README.md" else "")

    multimodal_parts = []
    config = {
        "max_context_bytes": 1000,
        "extra_context_files": "spec.pdf",
    }
    context = build_codebase_context(
        files=[{"filename": "src/main.py", "status": "modified"}],
        config=config,
        multimodal_parts=multimodal_parts,
    )

    assert "Multimodal File: spec.pdf" in context
    assert len(multimodal_parts) == 2
    assert "Extra Context File: spec.pdf" in multimodal_parts[0]
    assert multimodal_parts[1].inline_data.mime_type == "application/pdf"
    assert multimodal_parts[1].inline_data.data == b"%PDF-1.4 fake_pdf_data"


def test_build_visual_diff_parts_variant_images(tmp_path, monkeypatch):
    from PIL import ImageDraw

    from gemini_review import build_visual_diff_parts

    monkeypatch.chdir(tmp_path)

    # 1. Create Base Image (Red square)
    base_img = Image.new("RGB", (200, 200), color="red")
    base_buf = io.BytesIO()
    base_img.save(base_buf, format="PNG")
    base_bytes = base_buf.getvalue()

    # 2. Create Variant Head Image (Blue square with green circle)
    head_img = Image.new("RGB", (200, 200), color="blue")
    draw = ImageDraw.Draw(head_img)
    draw.ellipse((50, 50, 150, 150), fill="green")
    head_buf = io.BytesIO()
    head_img.save(head_buf, format="PNG")
    head_bytes = head_buf.getvalue()

    # Write head image to disk
    img_path = tmp_path / "ui_component.png"
    img_path.write_bytes(head_bytes)

    # Mock git blob retrieval for base image
    monkeypatch.setattr(
        "gemini_review.get_git_blob",
        lambda path, ref: base_bytes if ref == "main" and path == "ui_component.png" else None,
    )

    # Test modified image file
    image_files = [{"filename": "ui_component.png", "status": "modified"}]
    summary, parts = build_visual_diff_parts(
        image_files=image_files,
        base_sha="main",
        repository="test-repo",
        headers={},
        config={},
    )

    assert "Modified image: ui_component.png" in summary
    assert len(parts) == 5
    assert "=== Visual Diff: Modified Image 'ui_component.png' ===" in parts[0]
    assert "[Visual Diff] Base (prior) version" in parts[1]
    assert parts[2].inline_data.data == base_bytes
    assert "[Visual Diff] Head (new) version" in parts[3]
    assert parts[4].inline_data.data == head_bytes

    # Test newly added image file
    added_img = tmp_path / "new_logo.png"
    added_img.write_bytes(head_bytes)
    summary_add, parts_add = build_visual_diff_parts(
        image_files=[{"filename": "new_logo.png", "status": "added"}],
        base_sha="main",
    )
    assert "Newly added image: new_logo.png" in summary_add
    assert len(parts_add) == 2
    assert parts_add[1].inline_data.data == head_bytes

    # Test removed image file
    monkeypatch.setattr(
        "gemini_review.get_git_blob",
        lambda path, ref: base_bytes if ref == "main" and path == "deleted.png" else None,
    )
    summary_rem, parts_rem = build_visual_diff_parts(
        image_files=[{"filename": "deleted.png", "status": "removed"}],
        base_sha="main",
    )
    assert "Removed image: deleted.png" in summary_rem
    assert len(parts_rem) == 2
    assert parts_rem[1].inline_data.data == base_bytes


def test_extract_remote_image_urls():
    from gemini_review import extract_remote_image_urls

    issue_body = """
    ### Bug Description
    The modal dialog overflows on mobile screens:
    ![Mobile overflow](https://github.com/user-attachments/assets/550e8400-e29b-41d4-a716-446655440000)

    Console stack trace screenshot:
    <img width="800" alt="Console Error" src="https://user-images.githubusercontent.com/12345/98765432-abcdef.png" />

    Signed private image:
    ![Private](https://private-user-images.githubusercontent.com/123/456.jpg?jwt=eyJhbGci&amp;foo=bar "Title")

    Ignored links:
    - Local image: ![Local](docs/local.png)
    - Insecure HTTP: ![Insecure](http://example.com/insecure.png)
    - SSRF localhost: ![Localhost](https://localhost/secret.png)
    - SSRF metadata: ![Metadata](https://169.254.169.254/latest/meta-data/)
    - SSRF RFC1918 172.16: ![Private172](https://172.16.0.1/internal.png)
    - SSRF RFC1918 172.31: ![Private172End](https://172.31.255.255/internal.png)
    - SSRF IPv6 loopback: ![IPv6Loopback](https://[::1]/secret.png)
    - SSRF internal domain: ![Internal](https://corp.internal/secret.png)
    - Non-image video asset: ![Video](https://example.com/demo.mp4)
    - Duplicate link: ![Dup](https://github.com/user-attachments/assets/550e8400-e29b-41d4-a716-446655440000)
    """

    comments = [
        {"body": "Here is another screenshot from Safari:\n![Safari](https://example.com/safari-bug.webp)"},
        {"body": 'And duplicate:\n<img src="https://example.com/safari-bug.webp">'},
    ]

    urls = extract_remote_image_urls(issue_body, comments)
    assert urls == [
        "https://github.com/user-attachments/assets/550e8400-e29b-41d4-a716-446655440000",
        "https://user-images.githubusercontent.com/12345/98765432-abcdef.png",
        "https://private-user-images.githubusercontent.com/123/456.jpg?jwt=eyJhbGci&foo=bar",
        "https://example.com/safari-bug.webp",
    ]


def test_fetch_remote_image_auth_and_headers(mocker):
    from gemini_review import fetch_remote_image

    mock_get = mocker.patch("gemini_review.multimodal.requests.get")

    img = Image.new("RGB", (50, 50), color="green")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    png_bytes = buf.getvalue()

    mock_resp = mocker.Mock()
    mock_resp.status_code = 200
    mock_resp.headers = {"Content-Type": "image/png", "Content-Length": str(len(png_bytes))}
    mock_resp.iter_content.return_value = [png_bytes]
    mock_get.return_value = mock_resp

    # 1. GitHub user-attachment URL should receive Authorization: Bearer <token>
    gh_url = "https://github.com/user-attachments/assets/550e8400-e29b-41d4-a716-446655440000"
    res = fetch_remote_image(gh_url, github_token="ghp_secret123", timeout=30)
    assert res is not None
    data, mime = res
    assert data == png_bytes
    assert mime == "image/png"

    called_headers = mock_get.call_args[1]["headers"]
    assert called_headers.get("Authorization") == "Bearer ghp_secret123"
    assert mock_get.call_args[1]["stream"] is True
    assert mock_get.call_args[1]["timeout"] == 30

    # 2. Third-party URL must NOT receive GitHub Authorization header
    mock_get.reset_mock()
    ext_url = "https://example.com/screenshot.png"
    res_ext = fetch_remote_image(ext_url, github_token="ghp_secret123", timeout=30)
    assert res_ext is not None
    ext_headers = mock_get.call_args[1]["headers"]
    assert "Authorization" not in ext_headers


def test_fetch_remote_image_size_limit_and_errors(mocker):
    from gemini_review import fetch_remote_image

    mock_get = mocker.patch("gemini_review.multimodal.requests.get")

    # 1. Exceeds max_bytes via Content-Length
    mock_resp_large = mocker.Mock()
    mock_resp_large.status_code = 200
    mock_resp_large.headers = {"Content-Type": "image/png", "Content-Length": "50000000"}
    mock_get.return_value = mock_resp_large
    assert fetch_remote_image("https://github.com/user-attachments/assets/123", max_bytes=1024) is None

    # 2. Exceeds max_bytes during streaming chunks
    mock_resp_stream = mocker.Mock()
    mock_resp_stream.status_code = 200
    mock_resp_stream.headers = {"Content-Type": "image/png"}
    mock_resp_stream.iter_content.return_value = [b"x" * 600, b"y" * 600]
    mock_get.return_value = mock_resp_stream
    assert fetch_remote_image("https://github.com/user-attachments/assets/123", max_bytes=1000) is None

    # 3. Non-image Content-Type (e.g. HTML login page)
    mock_resp_html = mocker.Mock()
    mock_resp_html.status_code = 200
    mock_resp_html.headers = {"Content-Type": "text/html; charset=utf-8"}
    mock_resp_html.iter_content.return_value = [b"<html>Sign in</html>"]
    mock_get.return_value = mock_resp_html
    assert fetch_remote_image("https://github.com/user-attachments/assets/123") is None

    # 4. Unrecognised binary data (e.g. ZIP archive at extensionless URL with application/octet-stream)
    mock_resp_zip = mocker.Mock()
    mock_resp_zip.status_code = 200
    mock_resp_zip.headers = {"Content-Type": "application/octet-stream"}
    mock_resp_zip.iter_content.return_value = [b"PK\x03\x04not_an_image_payload"]
    mock_get.return_value = mock_resp_zip
    assert fetch_remote_image("https://github.com/user-attachments/assets/123") is None

    # 5. Network exception / timeout handled gracefully
    mock_get.side_effect = Exception("Connection timed out")
    assert fetch_remote_image("https://github.com/user-attachments/assets/123") is None


def test_build_triage_multimodal_parts_downscaling_and_cap(mocker, monkeypatch):
    from gemini_review import build_triage_multimodal_parts, get_max_triage_images

    assert get_max_triage_images({}) == 5
    monkeypatch.setenv("GEMINI_MAX_TRIAGE_IMAGES", "2")
    assert get_max_triage_images({}) == 2

    # Create a large synthetic image (> 600 KB) to verify Pillow downscaling
    large_img = Image.new("RGB", (1000, 1000), color="purple")
    buf = io.BytesIO()
    large_img.save(buf, format="PNG", compress_level=0)
    large_bytes = buf.getvalue()
    assert len(large_bytes) > 614400

    mock_get = mocker.patch("gemini_review.multimodal.requests.get")

    def _side_effect(url, **kwargs):
        if "fail" in url:
            raise RuntimeError("Download failed")
        resp = mocker.Mock()
        resp.status_code = 200
        resp.headers = {"Content-Type": "image/png"}
        resp.iter_content.return_value = [large_bytes]
        return resp

    mock_get.side_effect = _side_effect

    issue_body = """
    ![Fail](https://github.com/user-attachments/assets/fail-1)
    ![Img1](https://github.com/user-attachments/assets/valid-1)
    ![Img2](https://github.com/user-attachments/assets/valid-2)
    ![Img3](https://github.com/user-attachments/assets/valid-3)
    """

    parts = build_triage_multimodal_parts(
        issue_body=issue_body,
        github_token="ghp_test",
    )

    # Capped at 2 valid images (since GEMINI_MAX_TRIAGE_IMAGES=2, and fail-1 was skipped gracefully)
    assert len(parts) == 2
    for part in parts:
        assert hasattr(part, "inline_data")
        assert part.inline_data.mime_type == "image/png"
        assert len(part.inline_data.data) < len(large_bytes)


def _minimal_png_bytes() -> bytes:
    img = Image.new("RGB", (24, 24), color="blue")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_attach_markdown_images_modes_and_prefix_tail_separation(tmp_path, monkeypatch):
    import subprocess

    from gemini_review.context import build_codebase_context

    repo = tmp_path / "repo"
    repo.mkdir()
    assets = repo / "assets"
    assets.mkdir()
    docs = repo / "docs"
    docs.mkdir()

    base_png = _minimal_png_bytes()
    head_new_png = _minimal_png_bytes()
    (assets / "banner.png").write_bytes(base_png)
    (assets / "arch.png").write_bytes(base_png)
    (assets / "new_pr_shot.png").write_bytes(head_new_png)
    (repo / "README.md").write_text("# Readme\n![Banner](assets/banner.png)\n", encoding="utf-8")
    (docs / "architecture.md").write_text("# Arch\n![Arch](../assets/arch.png)\n", encoding="utf-8")

    for cmd in (
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "init", "-b", "main"],
        ["git", "add", "."],
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "base"],
    ):
        subprocess.run(cmd, cwd=repo, check=True, capture_output=True)
    base_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()

    # PR edits README.md to add new_pr_shot.png in diff, while existing banner stays untouched in base_sha
    (repo / "README.md").write_text(
        "# Readme\n![Banner](assets/banner.png)\n![New](assets/new_pr_shot.png)\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(repo)
    pr_files = [
        {
            "filename": "README.md",
            "status": "modified",
            "patch": "@@ -1,2 +1,3 @@\n # Readme\n ![Banner](assets/banner.png)\n+![New](assets/new_pr_shot.png)",
        }
    ]

    # 1. 'all' (default): baseline docs/images go to static prefix, PR-added image goes to dynamic tail
    static_all: list = []
    dynamic_all: list = []
    build_codebase_context(
        files=pr_files,
        config={"attach_markdown_images": "all"},
        multimodal_parts=static_all,
        base_sha=base_sha,
        dynamic_multimodal_parts=dynamic_all,
    )
    static_labels_all = [p for p in static_all if isinstance(p, str)]
    dynamic_labels_all = [p for p in dynamic_all if isinstance(p, str)]
    assert any("banner.png" in lbl for lbl in static_labels_all)
    assert any("arch.png" in lbl for lbl in static_labels_all)
    assert not any("new_pr_shot.png" in lbl for lbl in static_labels_all)
    assert any("new_pr_shot.png" in lbl for lbl in dynamic_labels_all)

    # 2. 'core': attaches architecture diagram in prefix + PR diff '+' line in tail, skips untouched README banner
    static_core: list = []
    dynamic_core: list = []
    build_codebase_context(
        files=pr_files,
        config={"attach_markdown_images": "core"},
        multimodal_parts=static_core,
        base_sha=base_sha,
        dynamic_multimodal_parts=dynamic_core,
    )
    static_labels_core = [p for p in static_core if isinstance(p, str)]
    dynamic_labels_core = [p for p in dynamic_core if isinstance(p, str)]
    assert any("arch.png" in lbl for lbl in static_labels_core)
    assert not any("banner.png" in lbl for lbl in static_labels_core)
    assert any("new_pr_shot.png" in lbl for lbl in dynamic_labels_core)

    # 3. 'diff': skips background/architecture diagrams in prefix, attaches only '+' line image from PR diff
    static_diff: list = []
    dynamic_diff: list = []
    build_codebase_context(
        files=pr_files,
        config={"attach_markdown_images": "diff"},
        multimodal_parts=static_diff,
        base_sha=base_sha,
        dynamic_multimodal_parts=dynamic_diff,
    )
    assert static_diff == []
    assert any("new_pr_shot.png" in p for p in dynamic_diff if isinstance(p, str))
    assert not any("banner.png" in p for p in dynamic_diff if isinstance(p, str))

    # 4. 'none': suppresses all Markdown-linked images
    static_none: list = []
    dynamic_none: list = []
    build_codebase_context(
        files=pr_files,
        config={"attach_markdown_images": "none"},
        multimodal_parts=static_none,
        base_sha=base_sha,
        dynamic_multimodal_parts=dynamic_none,
    )
    assert static_none == []
    assert dynamic_none == []

    # 5. Prefix purity: if a baseline Markdown doc references an image that does not exist at base_sha
    #    (e.g. PR creates it on disk), base_sha prefix scan MUST NOT fall back to reading working-tree disk.
    (repo / "README.md").write_text("# Readme\n![Future](assets/future.png)\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "dangling ref"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    dangling_base_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    (assets / "future.png").write_bytes(head_new_png)

    static_pure: list = []
    dynamic_pure: list = []
    build_codebase_context(
        files=[],
        config={"attach_markdown_images": "all"},
        multimodal_parts=static_pure,
        base_sha=dangling_base_sha,
        dynamic_multimodal_parts=dynamic_pure,
    )
    static_pure_labels = [p for p in static_pure if isinstance(p, str)]
    assert not any("future.png" in lbl for lbl in static_pure_labels)
    assert any("arch.png" in lbl for lbl in static_pure_labels)
    assert dynamic_pure == []
