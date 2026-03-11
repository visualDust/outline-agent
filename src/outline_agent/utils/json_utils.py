from __future__ import annotations

import json


class JsonExtractionError(ValueError):
    """Raised when no JSON object can be extracted from model output."""


def extract_json_object(text: str) -> dict[str, object]:
    stripped = _strip_code_fences(text.strip())
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        parsed = _scan_for_json_object(stripped)

    if not isinstance(parsed, dict):
        raise JsonExtractionError("Expected a JSON object")
    return parsed


def _strip_code_fences(text: str) -> str:
    if text.startswith("```") and text.endswith("```"):
        lines = text.splitlines()
        if len(lines) >= 2:
            return "\n".join(lines[1:-1]).strip()
    return text


def _scan_for_json_object(text: str) -> dict[str, object] | object:
    for start in range(len(text)):
        if text[start] != "{":
            continue
        depth = 0
        in_string = False
        escape = False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escape:
                    escape = False
                elif char == "\\":
                    escape = True
                elif char == '"':
                    in_string = False
                continue

            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start : index + 1]
                    try:
                        return json.loads(candidate)
                    except json.JSONDecodeError:
                        break
    raise JsonExtractionError("No JSON object found in text")
