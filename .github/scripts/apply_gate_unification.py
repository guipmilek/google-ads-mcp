#!/usr/bin/env python3
"""Apply the one-shot mutation gate unification payload."""

from __future__ import annotations

import base64
import io
from pathlib import Path, PurePosixPath
import tarfile

_EXPECTED = {
    "ads_mcp/mutation_policy.py",
    "ads_mcp/mutation_gateway.py",
    "ads_mcp/tools/mutations.py",
    "tests/mutation_policy_test.py",
    "tests/mutation_gateway_test.py",
}
_PART = Path(".github/scripts/gate-unification.part1")
_IMPORT_OLD = "from fastmcp.exceptions import ToolError\n"
_IMPORT_NEW = """from fastmcp.exceptions import ToolError

from ads_mcp.mutation_policy import (
    MutationSafetyConfig,
    authorize_mutation_execution,
    classify_operations,
)
"""
_POLICY_OLD = '''    resources = {operation["resource"] for operation in operations}
    sensitive = sorted(resources & _SENSITIVE_RESOURCES)
    if sensitive and not _env_bool(
        "GOOGLE_ADS_ALLOW_SENSITIVE_MUTATIONS", False
    ):
        raise ToolError(
            "Sensitive account-access or billing mutations are disabled: "
            + ", ".join(sensitive)
        )

    if any(operation["action"] == "remove" for operation in operations):
        if not _env_bool("GOOGLE_ADS_ALLOW_REMOVE", False):
            raise ToolError(
                "Remove operations are disabled. Set "
                "GOOGLE_ADS_ALLOW_REMOVE=true only when deletion is "
                "intentionally permitted."
            )

    if _contains_enabled_status(operations) and not _env_bool(
        "GOOGLE_ADS_ALLOW_ENABLE", False
    ):
        raise ToolError(
            "Enabling resources is disabled. Set GOOGLE_ADS_ALLOW_ENABLE=true "
            "only when activation and spending are intentionally permitted."
        )

    if partial_failure and _contains_temporary_resource_id(operations):
        raise ToolError(
            "partial_failure=true cannot be used with temporary negative "
            "resource IDs or dependent operations."
        )

    if partial_failure and not _env_bool(
        "GOOGLE_ADS_ALLOW_PARTIAL_FAILURE", False
    ):
        raise ToolError(
            "Live partial-failure execution is disabled. Keep "
            "partial_failure=false for atomic execution, or explicitly set "
            "GOOGLE_ADS_ALLOW_PARTIAL_FAILURE=true."
        )
'''
_POLICY_NEW = '''    if partial_failure and _contains_temporary_resource_id(operations):
        raise ToolError(
            "partial_failure=true cannot be used with temporary negative "
            "resource IDs or dependent operations."
        )

    config = MutationSafetyConfig.from_environment()
    classification = classify_operations(operations, partial_failure)
    decision = authorize_mutation_execution(
        classification, config, validate_only=False
    )
    if not decision.allowed:
        raise ToolError(
            json.dumps(
                {
                    "status": decision.status,
                    "reason_code": decision.reason_code,
                    "required_gate": decision.required_gate,
                    "observed_gate": decision.observed_gate,
                    "policy_version": decision.policy_version,
                    "message": {
                        "ENABLE_GATE_DISABLED": (
                            "Enabling resources is disabled. Set "
                            "GOOGLE_ADS_ALLOW_ENABLE=true only when activation "
                            "is intentionally permitted."
                        ),
                        "REMOVE_GATE_DISABLED": (
                            "Remove operations are disabled. Set "
                            "GOOGLE_ADS_ALLOW_REMOVE=true only when deletion "
                            "is intentionally permitted."
                        ),
                        "PARTIAL_FAILURE_GATE_DISABLED": (
                            "Live partial-failure execution is disabled."
                        ),
                        "SENSITIVE_MUTATION_GATE_DISABLED": (
                            "Sensitive account-access or billing mutations are "
                            "disabled."
                        ),
                        "UNKNOWN_BOOLEAN_ENV_VALUE": (
                            "A mutation gate contains an unknown boolean value."
                        ),
                    }.get(decision.reason_code, "Mutation blocked."),
                    "classification": classification.public_dict(),
                    "safety_config": config.public_dict(),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
'''
_DOC_APPEND = '''

## Centralized gate diagnostics

All create, update, remove, and batch endpoints pass through one policy gateway.
The four capability gates use a strict, fail-closed parser. Accepted true values
are `true`, `1`, `yes`, and `on`; accepted false values are `false`, `0`, `no`,
`off`, empty, and absent. Unknown values remain disabled and produce the
sanitized reason code `UNKNOWN_BOOLEAN_ENV_VALUE`.

Use the read-only tool below before generating a new preflight after a deploy:

```text
mutations_get_mutation_safety_status
```

It reports the interpreted booleans, policy version, sanitized runtime metadata,
and an optional local payload simulation without calling Google Ads or issuing a
confirmation. Live responses include specific reason codes such as
`ENABLE_GATE_DISABLED`, `REMOVE_GATE_DISABLED`,
`PARTIAL_FAILURE_GATE_DISABLED`, and `SENSITIVE_MUTATION_GATE_DISABLED`.

Validation receipts are wrapped in a signed policy envelope that preserves the
existing confirmation format and operation hash v3 while binding execution to
the policy version and recalculated operation classification. A mismatch blocks
with `BLOCKED_BY_STATE_OR_POLICY_DRIFT` before the Google Ads API is called.
'''


def _replace_once(path: Path, old: str, new: str) -> None:
    content = path.read_text()
    count = content.count(old)
    if count != 1:
        raise RuntimeError(
            f"Expected exactly one match in {path}, found {count}."
        )
    path.write_text(content.replace(old, new, 1))


def _safe_relative_path(name: str) -> Path:
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts:
        raise RuntimeError(f"Unsafe archive member: {name}")
    return Path(*path.parts)


def main() -> None:
    archive = base64.b64decode(_PART.read_text().strip(), validate=True)
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

    safety = Path("ads_mcp/mutation_safety.py")
    _replace_once(safety, _IMPORT_OLD, _IMPORT_NEW)
    _replace_once(safety, _POLICY_OLD, _POLICY_NEW)

    documentation = Path("MUTATIONS.md")
    if "## Centralized gate diagnostics" not in documentation.read_text():
        with documentation.open("a") as handle:
            handle.write(_DOC_APPEND)


if __name__ == "__main__":
    main()
