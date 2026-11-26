"""Tests for AI Studio Flex Inference (`service_tier`), 600s timeout floor, retry/fallback, and pricing."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from google.genai import types


class _FakeAPIError(Exception):
    def __init__(self, message: str, *, code: int | None = None):
        super().__init__(message)
        self.code = code


class TestCreateGenaiClientServiceTier:
    def test_flex_raises_timeout_floor_to_600s_on_ai_studio(self, mocker):
        from gemini_review.utils import create_genai_client

        mock_factory = mocker.Mock(return_value="client")
        client, is_vertex = create_genai_client(
            model_name="gemini-4-hydrogen",
            timeout=60,
            api_key="test-key",
            use_vertexai=False,
            service_tier="flex",
            client_cls=mock_factory,
        )
        assert client == "client"
        assert is_vertex is False
        http_opts = mock_factory.call_args.kwargs["http_options"]
        assert http_opts.timeout == 600_000
        assert http_opts.api_version == "v1beta"

        # Respects explicit timeout > 600s
        create_genai_client(
            model_name="gemini-4-hydrogen",
            timeout=900,
            api_key="test-key",
            use_vertexai=False,
            service_tier="flex",
            client_cls=mock_factory,
        )
        assert mock_factory.call_args.kwargs["http_options"].timeout == 900_000

    def test_flex_warns_and_keeps_300s_floor_on_vertex_ai(self, mocker, capsys):
        from gemini_review.utils import create_genai_client

        mock_factory = mocker.Mock(return_value="v-client")
        _, is_vertex = create_genai_client(
            model_name="gemini-4-hydrogen",
            timeout=60,
            project="proj",
            location="eu",
            use_vertexai=True,
            service_tier="flex",
            client_cls=mock_factory,
        )
        assert is_vertex is True
        assert mock_factory.call_args.kwargs["http_options"].timeout == 300_000
        assert "Vertex AI" in capsys.readouterr().err


class TestFlexCapacityRetryAndFallback:
    def test_is_flex_capacity_error_classification(self):
        from gemini_review.utils import _is_flex_capacity_error

        assert _is_flex_capacity_error(_FakeAPIError("Service unavailable", code=503)) is True
        assert _is_flex_capacity_error(_FakeAPIError("Quota exhausted", code=429)) is True
        assert _is_flex_capacity_error(_FakeAPIError("Deadline exceeded", code=504)) is True
        assert _is_flex_capacity_error(RuntimeError("503 UNAVAILABLE: Capacity shed")) is True
        assert _is_flex_capacity_error(RuntimeError("429 RESOURCE_EXHAUSTED")) is True
        assert _is_flex_capacity_error(TimeoutError("Request timed out")) is True
        assert _is_flex_capacity_error(_FakeAPIError("Invalid argument", code=400)) is False
        assert _is_flex_capacity_error(ValueError("bad value")) is False

    def test_flex_retries_transient_503_and_stays_on_flex_when_retry_succeeds(self, capsys):
        from gemini_review.utils import generate_content_with_stream_fallback

        ok_resp = SimpleNamespace(text='{"summary": "OK"}')
        mock_client = MagicMock()
        mock_client.models.generate_content.side_effect = [
            _FakeAPIError("503 UNAVAILABLE", code=503),
            ok_resp,
        ]
        cfg = types.GenerateContentConfig(service_tier=types.ServiceTier.FLEX)
        state: dict = {}

        resp = generate_content_with_stream_fallback(
            mock_client,
            model="gemini-4-hydrogen",
            contents="prompt",
            config=cfg,
            service_tier_fallback=True,
            tier_state=state,
            retry_delays=(0.0, 0.0),
        )
        assert resp is ok_resp
        assert mock_client.models.generate_content.call_count == 2
        assert cfg.service_tier == types.ServiceTier.FLEX
        assert state == {"effective_service_tier": "flex", "flex_fallback": False}
        assert "retrying Flex" in capsys.readouterr().err

    def test_flex_exhausted_falls_back_to_standard_when_enabled(self, capsys):
        from gemini_review.utils import generate_content_with_stream_fallback

        std_resp = SimpleNamespace(text='{"summary": "Standard OK"}')
        seen_tiers: list[object] = []

        def _fake_gen(*, model, contents, config):
            seen_tiers.append(config.service_tier)
            if config.service_tier == types.ServiceTier.FLEX:
                raise _FakeAPIError("503 UNAVAILABLE", code=503)
            return std_resp

        mock_client = MagicMock()
        mock_client.models.generate_content.side_effect = _fake_gen
        cfg = types.GenerateContentConfig(service_tier=types.ServiceTier.FLEX)
        state: dict = {}

        resp = generate_content_with_stream_fallback(
            mock_client,
            model="gemini-4-hydrogen",
            contents="prompt",
            config=cfg,
            service_tier_fallback=True,
            tier_state=state,
            retry_delays=(0.0, 0.0),
        )
        assert resp is std_resp
        # 1 initial + 2 retries on FLEX, then 1 call on Standard (None)
        assert seen_tiers == [
            types.ServiceTier.FLEX,
            types.ServiceTier.FLEX,
            types.ServiceTier.FLEX,
            None,
        ]
        assert cfg.service_tier is None
        assert state == {"effective_service_tier": "standard", "flex_fallback": True}
        assert "falling back to Standard tier" in capsys.readouterr().err

    def test_flex_raises_when_fallback_disabled_or_non_capacity_error(self):
        from gemini_review.utils import generate_content_with_stream_fallback

        mock_client = MagicMock()
        mock_client.models.generate_content.side_effect = _FakeAPIError("429 RESOURCE_EXHAUSTED", code=429)
        cfg = types.GenerateContentConfig(service_tier=types.ServiceTier.FLEX)

        with pytest.raises(_FakeAPIError):
            generate_content_with_stream_fallback(
                mock_client,
                model="gemini-4-hydrogen",
                contents="prompt",
                config=cfg,
                service_tier_fallback=False,
                retry_delays=(0.0, 0.0),
            )
        assert mock_client.models.generate_content.call_count == 3

        # Non-capacity 400 must fail fast with 1 call (no Flex retry/fallback)
        mock_client_bad = MagicMock()
        mock_client_bad.models.generate_content.side_effect = _FakeAPIError("400 INVALID_ARGUMENT", code=400)
        cfg2 = types.GenerateContentConfig(service_tier=types.ServiceTier.FLEX)
        with pytest.raises(_FakeAPIError):
            generate_content_with_stream_fallback(
                mock_client_bad,
                model="gemini-4-hydrogen",
                contents="prompt",
                config=cfg2,
                service_tier_fallback=True,
                retry_delays=(0.0, 0.0),
            )
        assert mock_client_bad.models.generate_content.call_count == 1

    def test_resolve_tool_calls_loop_preserves_service_tier_in_tool_free_fallback(self):
        from gemini_review.utils import resolve_tool_calls_loop

        def dummy_tool() -> str:
            return "ok"

        fc_resp = SimpleNamespace(
            text="",
            candidates=[
                types.Candidate(
                    content=types.Content(
                        role="model",
                        parts=[types.Part.from_function_call(name="dummy_tool", args={})],
                    )
                )
            ],
            function_calls=None,
            prompt_feedback=SimpleNamespace(block_reason="SAFETY"),
            usage_metadata=None,
        )
        final_resp = SimpleNamespace(text='{"summary": "fallback ok"}', usage_metadata=None)
        mock_client = MagicMock()
        # Simulate tool turn error then successful tool-free fallback
        mock_client.models.generate_content.side_effect = [RuntimeError("blocked"), final_resp]

        cfg = types.GenerateContentConfig(service_tier=types.ServiceTier.FLEX)
        res = resolve_tool_calls_loop(
            client=mock_client,
            model_name="gemini-4-hydrogen",
            initial_contents="prompt",
            gen_config=cfg,
            tools=[dummy_tool],
            response=fc_resp,
            max_turns=1,
        )
        assert res is final_resp
        fallback_cfg = mock_client.models.generate_content.call_args_list[-1].kwargs["config"]
        assert fallback_cfg.service_tier == types.ServiceTier.FLEX


class TestTelemetryRendering:
    def test_post_review_renders_flex_fallback_and_regional_rows_when_applicable(self, mocker):
        from gemini_review.github import post_review
        from gemini_review.schemas import ReviewResult

        mock_post = mocker.patch("requests.post", return_value=mocker.Mock(status_code=200))
        review = ReviewResult(summary="LGTM", general_feedback=[], comments=[])

        # 1. Flex active
        post_review(
            "owner/repo",
            1,
            "sha",
            review,
            {"Authorization": "token t"},
            usage_metadata={
                "model": "gemini-4-hydrogen",
                "fresh_tokens": 100_000,
                "cached_tokens": 0,
                "candidates_tokens": 1_000,
                "thoughts_tokens": 0,
                "total_tokens": 101_000,
                "service_tier": "flex",
                "flex_fallback": False,
            },
        )
        body_flex = mock_post.call_args[1]["json"]["body"]
        assert "| **Service Tier** | Flex (50% off) |" in body_flex
        assert "| **Endpoint** |" not in body_flex

        # 2. Flex fallback + Vertex eu regional
        mock_post.reset_mock()
        post_review(
            "owner/repo",
            1,
            "sha",
            review,
            {"Authorization": "token t"},
            usage_metadata={
                "model": "gemini-4-hydrogen",
                "fresh_tokens": 100_000,
                "cached_tokens": 0,
                "candidates_tokens": 1_000,
                "thoughts_tokens": 0,
                "total_tokens": 101_000,
                "service_tier": "standard",
                "flex_fallback": True,
                "vertex_location": "eu",
            },
        )
        body_reg = mock_post.call_args[1]["json"]["body"]
        assert "| **Service Tier** | Standard (fell back from Flex) |" in body_reg
        assert "| **Endpoint** | Vertex AI (`eu`, +10% regional) |" in body_reg

        # 3. Custom rate override omits misleading 50% / +10% labels
        mock_post.reset_mock()
        post_review(
            "owner/repo",
            1,
            "sha",
            review,
            {"Authorization": "token t"},
            usage_metadata={
                "model": "gemini-4-hydrogen",
                "fresh_tokens": 100_000,
                "cached_tokens": 0,
                "candidates_tokens": 1_000,
                "thoughts_tokens": 0,
                "total_tokens": 101_000,
                "service_tier": "flex",
                "vertex_location": "eu",
            },
            config={"rate_input": 5.0, "rate_output": 10.0},
        )
        body_override = mock_post.call_args[1]["json"]["body"]
        assert "| **Service Tier** | Flex |" in body_override
        assert "50% off" not in body_override
        assert "| **Endpoint** | Vertex AI (`eu`) |" in body_override
        assert "+10% regional" not in body_override


class TestEndToEndServiceTierWiring:
    def test_pr_review_flex_tier_and_fallback_and_vertex_regional(self, mocker, monkeypatch, capsys, tmp_path):
        import json

        from google.genai import types

        import gemini_pr_review

        # Clear interfering env
        for var in (
            "GITHUB_EVENT_PATH",
            "GOOGLE_GENAI_USE_VERTEXAI",
            "GOOGLE_GENAI_USE_ENTERPRISE",
            "GOOGLE_CLOUD_PROJECT",
            "GOOGLE_CLOUD_LOCATION",
            "GEMINI_SERVICE_TIER",
            "GEMINI_SERVICE_TIER_FALLBACK",
            "GEMINI_THINKING_LEVEL",
            "GEMINI_MAX_PROMPT_TOKENS",
        ):
            monkeypatch.delenv(var, raising=False)

        monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
        monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_tok")
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        monkeypatch.setenv("GEMINI_MODEL", "gemini-4-hydrogen")
        monkeypatch.setenv("GEMINI_SKIP_INLINE_SUGGESTIONS", "false")
        monkeypatch.setenv("GEMINI_INCLUDE_COMMENT_HISTORY", "false")
        monkeypatch.setenv("GEMINI_SERVICE_TIER", "flex")

        mocker.patch(
            "gemini_review.get_local_git_files",
            return_value=[{"filename": "app.py", "status": "modified", "patch": "@@ -1 +1 @@\n+x = 1"}],
        )
        mocker.patch("gemini_review.build_codebase_context", return_value="")
        mocker.patch("gemini_review.post_commit_status")

        review_json = json.dumps({"summary": "Great PR", "general_feedback": [], "comments": []})

        def _make_resp():
            r = mocker.Mock()
            r.text = review_json
            r.candidates = [
                mocker.Mock(content=types.Content(role="model", parts=[types.Part.from_text(text=review_json)]))
            ]
            r.function_calls = None
            r.usage_metadata = mocker.Mock(
                prompt_token_count=100_000,
                cached_content_token_count=0,
                candidates_token_count=1_000,
                thoughts_token_count=0,
                total_token_count=101_000,
            )
            return r

        # 1. AI Studio Flex active: sets FLEX tier, 600s client timeout, halves cost ($0.1575 -> $0.0788)
        mock_client = mocker.MagicMock()
        mock_client.models.count_tokens.return_value = mocker.Mock(total_tokens=200)
        mock_client.models.generate_content.return_value = _make_resp()
        mock_factory = mocker.patch("google.genai.Client", return_value=mock_client)
        mock_post_req = mocker.patch("requests.post", return_value=mocker.Mock(status_code=200))

        gemini_pr_review.main()

        http_opts = mock_factory.call_args.kwargs["http_options"]
        assert http_opts.timeout == 600_000
        sent_cfg = mock_client.models.generate_content.call_args.kwargs["config"]
        assert sent_cfg.service_tier == types.ServiceTier.FLEX
        monkeypatch.setenv("GEMINI_SERVICE_TIER", "standard")
        assert mock_post_req.call_count == 0

        # 2. Flex 503 -> Standard fallback through main() non-dry-run path
        mocker.patch("gemini_review.utils.time.sleep")
        monkeypatch.setenv("GEMINI_SERVICE_TIER", "flex")
        mock_post_review = mocker.patch("gemini_review.post_review")
        event_tmp = tmp_path / "test_pr_vertex_eu_event.json"
        event_tmp.write_text(
            json.dumps({"pull_request": {"number": 5, "head": {"sha": "head1"}, "base": {"sha": "base1"}}}),
            encoding="utf-8",
        )
        monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_tmp))
        mocker.patch(
            "gemini_review.get_pr_files",
            return_value=[{"filename": "app.py", "status": "modified", "patch": "@@ -1 +1 @@\n+x = 1"}],
        )

        mock_client.models.generate_content.side_effect = [
            _FakeAPIError("503 UNAVAILABLE", code=503),
            _FakeAPIError("503 UNAVAILABLE", code=503),
            _FakeAPIError("503 UNAVAILABLE", code=503),
            _make_resp(),
        ]
        gemini_pr_review.main()
        fb_usage = mock_post_review.call_args.kwargs["usage_metadata"]
        assert fb_usage["service_tier"] == "standard"
        assert fb_usage["flex_fallback"] is True

        # 3. Vertex AI (eu) with GEMINI_SERVICE_TIER=flex forces Standard tier and applies 1.10x regional pricing
        mock_client.models.generate_content.side_effect = None
        mock_client.models.generate_content.return_value = _make_resp()
        monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "True")
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "my-proj")
        monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "eu")
        monkeypatch.setenv("GEMINI_SERVICE_TIER", "flex")

        mock_client.models.generate_content.reset_mock()
        gemini_pr_review.main()

        v_cfg = mock_client.models.generate_content.call_args.kwargs["config"]
        assert v_cfg.service_tier is None
        posted_usage = mock_post_review.call_args.kwargs["usage_metadata"]
        assert posted_usage["service_tier"] == "standard"
        assert posted_usage["vertex_location"] == "eu"
        assert "standard tier" in capsys.readouterr().err.lower()

    def test_issue_triage_wires_service_tier_on_ai_studio_and_guards_vertex(self, mocker, monkeypatch):
        import json

        from google.genai import types

        import gemini_issue_triage

        for var in (
            "GITHUB_EVENT_PATH",
            "GOOGLE_GENAI_USE_VERTEXAI",
            "GOOGLE_GENAI_USE_ENTERPRISE",
            "GOOGLE_CLOUD_PROJECT",
            "GOOGLE_CLOUD_LOCATION",
            "GEMINI_SERVICE_TIER",
            "GEMINI_SERVICE_TIER_FALLBACK",
        ):
            monkeypatch.delenv(var, raising=False)

        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        monkeypatch.setenv("GEMINI_MODEL", "gemini-4-hydrogen")
        monkeypatch.setenv("GEMINI_SERVICE_TIER", "flex")

        mock_client = mocker.MagicMock()
        triage_json = json.dumps({"selected_labels": ["bug"], "reasoning": "Looks like a bug."})
        resp = mocker.Mock()
        resp.text = triage_json
        mock_client.models.generate_content.return_value = resp
        mock_factory = mocker.patch("google.genai.Client", return_value=mock_client)

        gemini_issue_triage.main()

        assert mock_factory.call_args.kwargs["http_options"].timeout == 600_000
        sent_cfg = mock_client.models.generate_content.call_args.kwargs["config"]
        assert sent_cfg.service_tier == types.ServiceTier.FLEX

        # Vertex AI ignores Flex on triage too
        monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "True")
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj")
        mock_client.models.generate_content.reset_mock()
        gemini_issue_triage.main()
        assert mock_client.models.generate_content.call_args.kwargs["config"].service_tier is None

    def test_select_dynamic_context_files_supports_flex_service_tier(self, mocker):
        from google.genai import types

        from gemini_review.context import select_dynamic_context_files

        mock_client = MagicMock()
        resp = mocker.Mock()
        resp.text = '{"selected_files": ["a.py"], "reasoning": "needed"}'
        resp.usage_metadata = None
        mock_client.models.generate_content.return_value = resp

        selected, _, _ = select_dynamic_context_files(
            client=mock_client,
            model="gemini-4-hydrogen",
            files=[{"filename": "main.py", "patch": "+import a"}],
            candidate_files=["a.py"],
            service_tier="flex",
        )
        assert selected == ["a.py"]
        cfg = mock_client.models.generate_content.call_args.kwargs["config"]
        assert cfg.service_tier == types.ServiceTier.FLEX
