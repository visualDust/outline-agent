from __future__ import annotations

from outline_agent.core.config import AppSettings
from outline_agent.utils import mermaid_validation as mermaid_module
from outline_agent.utils.mermaid_validation import (
    MermaidValidationResult,
    MermaidValidatorProbe,
    build_mermaid_validation_failure,
    clear_mermaid_validator_probe_cache,
    extract_mermaid_fences,
    validate_mermaid_markdown,
)


def test_extract_mermaid_fences_finds_multiple_blocks() -> None:
    text = """
# Title

```mermaid
graph TD
  A --> B
```

text

```mermaid
sequenceDiagram
  A->>B: hi
```
""".strip()

    fences = extract_mermaid_fences(text)

    assert len(fences) == 2
    assert fences[0].index == 1
    assert fences[0].markdown_start_line == 3
    assert "graph TD" in fences[0].source
    assert fences[1].index == 2
    assert "sequenceDiagram" in fences[1].source


def test_validate_mermaid_markdown_returns_unavailable_when_backend_missing(monkeypatch) -> None:
    clear_mermaid_validator_probe_cache()
    monkeypatch.setattr(mermaid_module, "_resolve_mermaid_cli_command", lambda: None)
    settings = AppSettings(system_prompt="prompt")

    result = validate_mermaid_markdown(
        "```mermaid\ngraph TD\n  A --> B\n```",
        settings=settings,
    )

    assert result.status == "unavailable"
    assert result.available is False
    assert result.checked_blocks == 1
    assert "not found" in (result.reason or "").lower()


def test_validate_mermaid_markdown_returns_invalid_for_parse_error(monkeypatch) -> None:
    clear_mermaid_validator_probe_cache()
    settings = AppSettings(system_prompt="prompt")

    monkeypatch.setattr(
        mermaid_module,
        "get_mermaid_validator_probe",
        lambda settings, **kwargs: MermaidValidatorProbe(
            available=True,
            backend="mermaid-cli",
            command=("mmdc",),
            version="11.12.0",
        ),
    )
    monkeypatch.setattr(
        mermaid_module,
        "_invoke_mermaid_cli",
        lambda **kwargs: mermaid_module._MermaidCliInvocation(
            returncode=1,
            stdout="",
            stderr=(
                "Error: Parse error on line 3:\n"
                "...  B --> D[Broken\n"
                "-----------------^\n"
                "Expecting 'SQE', got '1'\n"
            ),
        ),
    )

    result = validate_mermaid_markdown(
        "```mermaid\ngraph TD\n  A --> B\n  B --> D[Broken\n```",
        settings=settings,
    )

    assert result.status == "invalid"
    assert result.available is True
    assert result.checked_blocks == 1
    assert len(result.errors) == 1
    assert result.errors[0].block_index == 1
    assert result.errors[0].mermaid_line == 3
    assert "Parse error on line 3" in result.errors[0].message


def test_build_mermaid_validation_failure_skips_unavailable_in_auto_mode(monkeypatch) -> None:
    settings = AppSettings(system_prompt="prompt", mermaid_validation_mode="auto")
    monkeypatch.setattr(
        mermaid_module,
        "validate_mermaid_markdown",
        lambda *args, **kwargs: MermaidValidationResult(
            status="unavailable",
            available=False,
            checked_blocks=1,
            errors=(),
            backend="mermaid-cli",
            reason="backend missing",
        ),
    )

    failure = build_mermaid_validation_failure(
        tool_name="create_document",
        document_text="```mermaid\ngraph TD\n  A --> B\n```",
        settings=settings,
    )

    assert failure is None


def test_build_mermaid_validation_failure_blocks_unavailable_in_required_mode(monkeypatch) -> None:
    settings = AppSettings(system_prompt="prompt", mermaid_validation_mode="required")
    monkeypatch.setattr(
        mermaid_module,
        "validate_mermaid_markdown",
        lambda *args, **kwargs: MermaidValidationResult(
            status="unavailable",
            available=False,
            checked_blocks=1,
            errors=(),
            backend="mermaid-cli",
            reason="backend missing",
        ),
    )

    failure = build_mermaid_validation_failure(
        tool_name="create_document",
        document_text="```mermaid\ngraph TD\n  A --> B\n```",
        settings=settings,
    )

    assert failure is not None
    summary, data = failure
    assert "MERMAID_VALIDATION_UNAVAILABLE" in summary
    assert data["mermaid_validation"]["status"] == "unavailable"


def test_build_mermaid_validation_failure_returns_none_when_bypass_enabled(monkeypatch) -> None:
    settings = AppSettings(system_prompt="prompt", mermaid_validation_exhausted_action="allow_write")
    monkeypatch.setattr(
        mermaid_module,
        "validate_mermaid_markdown",
        lambda *args, **kwargs: MermaidValidationResult(
            status="invalid",
            available=True,
            checked_blocks=2,
            errors=(),
            backend="mermaid-cli",
            reason="invalid",
        ),
    )

    failure = build_mermaid_validation_failure(
        tool_name="create_document",
        document_text="```mermaid\ngraph TD\n  A --> B\n```",
        settings=settings,
        bypass_validation=True,
    )

    assert failure is None
