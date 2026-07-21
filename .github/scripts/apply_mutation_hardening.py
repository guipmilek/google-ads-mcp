#!/usr/bin/env python3
"""Apply the one-shot mutation hardening payload."""

from __future__ import annotations

import base64
import io
from pathlib import Path, PurePosixPath
import tarfile

_EXPECTED = {
    "MUTATIONS.md",
    "ads_mcp/mutation_engine.py",
    "ads_mcp/mutation_safety.py",
    "ads_mcp/mutation_schema.py",
    "ads_mcp/utils.py",
    "tests/mutation_engine_test.py",
    "tests/mutate_request_test.py",
    "tests/mutation_schema_test.py",
    "tests/utils_format_test.py",
}
_PARTS = [
    Path(f".github/scripts/mutation-hardening.part{index}")
    for index in range(1, 5)
]


def _safe_relative_path(name: str) -> Path:
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts:
        raise RuntimeError(f"Unsafe archive member: {name}")
    return Path(*path.parts)


def main() -> None:
    encoded = "".join(path.read_text().strip() for path in _PARTS)
    archive = base64.b64decode(encoded, validate=True)

    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as bundle:
        members = bundle.getmembers()
        names = {member.name for member in members if member.isfile()}
        if names != _EXPECTED:
            raise RuntimeError(
                "Unexpected payload members: " + ", ".join(sorted(names))
            )

        for member in members:
            if not member.isfile():
                continue
            target = _safe_relative_path(member.name)
            source = bundle.extractfile(member)
            if source is None:
                raise RuntimeError(f"Unable to read archive member: {member.name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read())


if __name__ == "__main__":
    main()
