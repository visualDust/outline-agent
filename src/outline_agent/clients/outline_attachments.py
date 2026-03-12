from __future__ import annotations

from pathlib import Path
from typing import Any


def build_multipart_body(
    *,
    boundary: str,
    form_fields: dict[str, Any],
    file_path: Path,
    content_type: str,
) -> bytes:
    boundary_bytes = boundary.encode("utf-8")
    body = bytearray()

    def add_field(name: str, value: Any) -> None:
        body.extend(b"--" + boundary_bytes + b"\r\n")
        body.extend(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"))
        if isinstance(value, bytes):
            body.extend(value)
        else:
            body.extend(str(value).encode("utf-8"))
        body.extend(b"\r\n")

    for name, value in form_fields.items():
        if value is None:
            continue
        add_field(name, value)

    body.extend(b"--" + boundary_bytes + b"\r\n")
    body.extend(
        (
            f'Content-Disposition: form-data; name="file"; filename="{file_path.name}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode("utf-8")
    )
    body.extend(file_path.read_bytes())
    body.extend(b"\r\n")
    body.extend(b"--" + boundary_bytes + b"--\r\n")
    return bytes(body)
