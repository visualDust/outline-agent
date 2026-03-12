from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from outline_agent import bootstrap
from outline_agent.clients.outline_client import OutlineClientError, OutlineUser
from outline_agent.core.config import AppSettings
from outline_agent.models.model_profiles import ModelProfileError, ResolvedModelProfile
from outline_agent.state.store import ProcessedEventStore


def _settings(tmp_path: Path, **overrides: object) -> AppSettings:
    base = dict(
        outline_api_base_url="https://outline.example",
        outline_api_key="ol_api_test",
        workspace_root=tmp_path / "agents",
        dedupe_store_path=tmp_path / "processed.json",
        webhook_log_dir=tmp_path / "webhooks",
        log_file_path=tmp_path / "logs" / "app.log",
        model_ref="primary",
        memory_model_ref="memory",
        action_router_model_ref="router",
        document_update_model_ref="document",
        tool_model_ref="tool",
        thread_session_model_ref="thread",
    )
    base.update(overrides)
    return AppSettings(**base)


def test_resolve_primary_model_status_returns_profile_summary(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path)

    class FakeResolver:
        def __init__(self, config_path: Path) -> None:
            self.config_path = config_path

        def resolve(self, model_ref: str | None = None) -> ResolvedModelProfile:
            assert model_ref == "primary"
            return ResolvedModelProfile(
                alias="primary",
                provider="openai",
                base_url="https://api.example",
                api_key="secret",
                model="gpt-test",
            )

    monkeypatch.setattr(bootstrap, "ModelProfileResolver", FakeResolver)

    status = bootstrap.resolve_primary_model_status(settings)

    assert status == {
        "ok": True,
        "provider": "openai",
        "model": "gpt-test",
        "alias": "primary",
        "base_url": "https://api.example",
    }


def test_resolve_primary_model_status_returns_error_payload(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path)

    class FakeResolver:
        def __init__(self, config_path: Path) -> None:
            self.config_path = config_path

        def resolve(self, model_ref: str | None = None) -> ResolvedModelProfile:
            raise ModelProfileError("bad model config")

    monkeypatch.setattr(bootstrap, "ModelProfileResolver", FakeResolver)

    status = bootstrap.resolve_primary_model_status(settings)

    assert status == {"ok": False, "error": "bad model config"}


def test_build_request_resources_creates_outline_client_and_store(tmp_path: Path) -> None:
    settings = _settings(tmp_path)

    outline_client, store = bootstrap.build_request_resources(settings)

    assert outline_client.base_url == "https://outline.example/api"
    assert outline_client.api_key == "ol_api_test"
    assert isinstance(store, ProcessedEventStore)
    assert store.path == tmp_path / "processed.json"


def test_build_request_resources_requires_outline_base_url(tmp_path: Path) -> None:
    settings = _settings(tmp_path, outline_api_base_url=None)

    with pytest.raises(ValueError, match="OUTLINE_API_BASE_URL is not configured"):
        bootstrap.build_request_resources(settings)


def test_validate_outline_runtime_identity_caches_user(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path)

    class FakeOutlineClient:
        async def current_user(self) -> OutlineUser:
            return OutlineUser(id="user-123", name="Agent Smith", email="agent@example.com")

    monkeypatch.setattr(bootstrap, "build_outline_client", lambda current_settings: FakeOutlineClient())

    current_user = asyncio.run(bootstrap.validate_outline_runtime_identity(settings))

    assert current_user.id == "user-123"
    assert settings.runtime_outline_user_id == "user-123"
    assert settings.runtime_outline_user_name == "Agent Smith"


def test_validate_outline_runtime_identity_propagates_outline_errors(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path)

    class FakeOutlineClient:
        async def current_user(self) -> OutlineUser:
            raise OutlineClientError("Outline API error 401: invalid api key")

    monkeypatch.setattr(bootstrap, "build_outline_client", lambda current_settings: FakeOutlineClient())

    with pytest.raises(OutlineClientError, match="invalid api key"):
        asyncio.run(bootstrap.validate_outline_runtime_identity(settings))


def test_build_comment_processor_uses_explicit_model_refs(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    outline_client, store = bootstrap.build_request_resources(settings)
    resolve_calls: list[str | None] = []
    built_profiles: list[tuple[object, AppSettings]] = []
    captured_processor_kwargs: dict[str, object] = {}

    class FakeResolver:
        def __init__(self, config_path: Path) -> None:
            self.config_path = config_path

        def resolve(self, model_ref: str | None = None) -> str:
            resolve_calls.append(model_ref)
            return f"profile:{model_ref}"

    def fake_build_model_client(profile: object, current_settings: AppSettings) -> str:
        built_profiles.append((profile, current_settings))
        return f"client:{profile}"

    class FakeCommentProcessor:
        def __init__(self, **kwargs: object) -> None:
            captured_processor_kwargs.update(kwargs)

    monkeypatch.setattr(bootstrap, "ModelProfileResolver", FakeResolver)
    monkeypatch.setattr(bootstrap, "_build_model_client", fake_build_model_client)
    monkeypatch.setattr(bootstrap, "CommentProcessor", FakeCommentProcessor)

    processor = bootstrap.build_comment_processor(
        settings=settings,
        outline_client=outline_client,
        store=store,
    )

    assert isinstance(processor, FakeCommentProcessor)
    assert resolve_calls == ["primary", "memory", "router", "document", "tool", "thread"]
    assert [profile for profile, _ in built_profiles] == [
        "profile:primary",
        "profile:memory",
        "profile:thread",
        "profile:document",
        "profile:tool",
        "profile:router",
    ]
    assert captured_processor_kwargs["settings"] is settings
    assert captured_processor_kwargs["outline_client"] is outline_client
    assert captured_processor_kwargs["store"] is store
    assert captured_processor_kwargs["model_client"] == "client:profile:primary"
    assert captured_processor_kwargs["memory_model_client"] == "client:profile:memory"
    assert captured_processor_kwargs["thread_session_model_client"] == "client:profile:thread"
    assert captured_processor_kwargs["document_update_model_client"] == "client:profile:document"
    assert captured_processor_kwargs["tool_model_client"] == "client:profile:tool"
    assert captured_processor_kwargs["action_router_model_client"] == "client:profile:router"


def test_build_comment_processor_uses_fallback_refs_when_features_disabled(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(
        tmp_path,
        memory_update_enabled=False,
        document_update_enabled=False,
        tool_use_enabled=False,
        thread_session_update_enabled=False,
    )
    outline_client, store = bootstrap.build_request_resources(settings)
    resolve_calls: list[str | None] = []
    built_profiles: list[object] = []

    class FakeResolver:
        def __init__(self, config_path: Path) -> None:
            self.config_path = config_path

        def resolve(self, model_ref: str | None = None) -> str:
            resolve_calls.append(model_ref)
            return f"profile:{model_ref}"

    def fake_build_model_client(profile: object, current_settings: AppSettings) -> str:
        built_profiles.append(profile)
        return f"client:{profile}"

    class FakeCommentProcessor:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs

    monkeypatch.setattr(bootstrap, "ModelProfileResolver", FakeResolver)
    monkeypatch.setattr(bootstrap, "_build_model_client", fake_build_model_client)
    monkeypatch.setattr(bootstrap, "CommentProcessor", FakeCommentProcessor)

    bootstrap.build_comment_processor(
        settings=settings,
        outline_client=outline_client,
        store=store,
    )

    assert resolve_calls == ["primary", "router"]
    assert built_profiles == [
        "profile:primary",
        "profile:primary",
        "profile:primary",
        "profile:primary",
        "profile:primary",
        "profile:router",
    ]
