# Copyright 2026 Google LLC.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Central mutation gate parsing, classification, authorization, and audit."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
import os
import socket
import time
from typing import Any, Dict, Iterable, List

_POLICY_VERSION = "mutation-safety-v4"
_PROCESS_STARTED_AT = time.time()
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"", "0", "false", "no", "off"})
_GATE_NAMES = (
    "GOOGLE_ADS_ALLOW_ENABLE",
    "GOOGLE_ADS_ALLOW_REMOVE",
    "GOOGLE_ADS_ALLOW_PARTIAL_FAILURE",
    "GOOGLE_ADS_ALLOW_SENSITIVE_MUTATIONS",
)
_SENSITIVE_RESOURCES = frozenset(
    {
        "AccountBudgetProposal",
        "AccountLink",
        "BillingSetup",
        "CustomerClientLink",
        "CustomerManagerLink",
        "CustomerUserAccess",
        "CustomerUserAccessInvitation",
    }
)
_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class BooleanEnvironmentValue:
    """Sanitized result of parsing one boolean environment variable."""

    name: str
    present: bool
    normalized_text: str
    value: bool
    valid: bool
    source: str = "environment"

    def public_dict(self) -> Dict[str, Any]:
        normalized = self.normalized_text if self.valid else "<invalid>"
        return {
            "name": self.name,
            "present": self.present,
            "normalized_text": normalized,
            "value": self.value,
            "valid": self.valid,
            "source": self.source,
        }


def parse_boolean_environment(
    name: str, default: bool = False
) -> BooleanEnvironmentValue:
    """Parses a gate strictly and defaults unknown values to disabled."""
    raw = os.environ.get(name)
    if raw is None:
        return BooleanEnvironmentValue(
            name=name,
            present=False,
            normalized_text="<absent>",
            value=default,
            valid=True,
        )

    normalized = raw.strip().lower()
    if normalized in _TRUE_VALUES:
        value = True
        valid = True
    elif normalized in _FALSE_VALUES:
        value = False
        valid = True
    else:
        value = False
        valid = False

    return BooleanEnvironmentValue(
        name=name,
        present=True,
        normalized_text=normalized,
        value=value,
        valid=valid,
    )


@dataclass(frozen=True)
class MutationSafetyConfig:
    """Immutable per-request snapshot of the four live mutation gates."""

    allow_enable: bool
    allow_remove: bool
    allow_partial_failure: bool
    allow_sensitive_mutations: bool
    values: tuple[BooleanEnvironmentValue, ...]
    policy_version: str = _POLICY_VERSION
    config_source: str = "environment"
    loaded: str = "per_request"

    @classmethod
    def from_environment(cls) -> "MutationSafetyConfig":
        values = tuple(parse_boolean_environment(name) for name in _GATE_NAMES)
        by_name = {item.name: item for item in values}
        return cls(
            allow_enable=by_name["GOOGLE_ADS_ALLOW_ENABLE"].value,
            allow_remove=by_name["GOOGLE_ADS_ALLOW_REMOVE"].value,
            allow_partial_failure=by_name[
                "GOOGLE_ADS_ALLOW_PARTIAL_FAILURE"
            ].value,
            allow_sensitive_mutations=by_name[
                "GOOGLE_ADS_ALLOW_SENSITIVE_MUTATIONS"
            ].value,
            values=values,
        )

    @property
    def invalid_names(self) -> tuple[str, ...]:
        return tuple(item.name for item in self.values if not item.valid)

    def public_dict(self) -> Dict[str, Any]:
        return {
            "policy_version": self.policy_version,
            "config_source": self.config_source,
            "loaded": self.loaded,
            "allow_enable": self.allow_enable,
            "allow_remove": self.allow_remove,
            "allow_partial_failure": self.allow_partial_failure,
            "allow_sensitive_mutations": self.allow_sensitive_mutations,
            "invalid_names": list(self.invalid_names),
            "values": [item.public_dict() for item in self.values],
        }


@dataclass(frozen=True)
class MutationClassification:
    """Canonical risk classification used by every mutation endpoint."""

    operation_count: int
    resource_types: tuple[str, ...]
    fields: tuple[str, ...]
    contains_enable: bool
    contains_remove: bool
    contains_sensitive_mutations: bool
    partial_failure: bool
    confirmation_verb: str

    def public_dict(self) -> Dict[str, Any]:
        return {
            "operation_count": self.operation_count,
            "resource_types": list(self.resource_types),
            "fields": list(self.fields),
            "contains_enable": self.contains_enable,
            "contains_remove": self.contains_remove,
            "contains_sensitive_mutations": self.contains_sensitive_mutations,
            "partial_failure": self.partial_failure,
            "confirmation_verb": self.confirmation_verb,
        }


def _walk_for_enabled_status(value: Any) -> bool:
    if isinstance(value, dict):
        for key, nested in value.items():
            if key == "status" and str(nested).upper() == "ENABLED":
                return True
            if _walk_for_enabled_status(nested):
                return True
    elif isinstance(value, list):
        return any(_walk_for_enabled_status(item) for item in value)
    return False


def _operation_fields(operation: Dict[str, Any]) -> Iterable[str]:
    action = str(operation.get("action", "")).lower()
    if action == "update":
        for path in operation.get("update_mask", []) or []:
            yield str(path)
        return
    if action == "create":
        data = operation.get("data")
        if isinstance(data, dict):
            for key in data:
                yield str(key)
        return
    if action == "remove":
        yield "resource_name"


def classify_operations(
    operations: List[Dict[str, Any]], partial_failure: bool
) -> MutationClassification:
    """Classifies normalized or raw operations deterministically."""
    resources = tuple(
        sorted({str(operation.get("resource", "")) for operation in operations})
    )
    fields = tuple(
        sorted(
            {
                field
                for operation in operations
                for field in _operation_fields(operation)
            }
        )
    )
    contains_remove = any(
        str(operation.get("action", "")).lower() == "remove"
        for operation in operations
    )
    contains_enable = any(
        _walk_for_enabled_status(operation.get("data"))
        for operation in operations
    )
    contains_sensitive = bool(set(resources) & _SENSITIVE_RESOURCES)

    if contains_sensitive:
        confirmation_verb = "SENSITIVE"
    elif contains_remove and contains_enable:
        confirmation_verb = "REMOVE_AND_ENABLE"
    elif contains_remove:
        confirmation_verb = "REMOVE"
    elif contains_enable:
        confirmation_verb = "ENABLE"
    else:
        confirmation_verb = "EXECUTE"

    return MutationClassification(
        operation_count=len(operations),
        resource_types=resources,
        fields=fields,
        contains_enable=contains_enable,
        contains_remove=contains_remove,
        contains_sensitive_mutations=contains_sensitive,
        partial_failure=partial_failure,
        confirmation_verb=confirmation_verb,
    )


@dataclass(frozen=True)
class AuthorizationDecision:
    """Structured result of applying live mutation gates."""

    allowed: bool
    status: str
    reason_code: str
    required_gate: str | None
    observed_gate: bool | None
    execution_mode: str
    policy_version: str

    def public_dict(self) -> Dict[str, Any]:
        return {
            "allowed": self.allowed,
            "status": self.status,
            "reason_code": self.reason_code,
            "required_gate": self.required_gate,
            "observed_gate": self.observed_gate,
            "execution_mode": self.execution_mode,
            "policy_version": self.policy_version,
        }


def authorize_mutation_execution(
    classification: MutationClassification,
    safety_config: MutationSafetyConfig,
    validate_only: bool,
) -> AuthorizationDecision:
    """Applies one fail-closed gate policy to every mutation endpoint."""
    execution_mode = "VALIDATE_ONLY" if validate_only else "EXECUTE"
    if validate_only:
        return AuthorizationDecision(
            allowed=True,
            status="ALLOWED",
            reason_code="VALIDATION_ONLY",
            required_gate=None,
            observed_gate=None,
            execution_mode=execution_mode,
            policy_version=safety_config.policy_version,
        )

    if safety_config.invalid_names:
        return AuthorizationDecision(
            allowed=False,
            status="BLOCKED_BY_SECURITY_GATE",
            reason_code="UNKNOWN_BOOLEAN_ENV_VALUE",
            required_gate=safety_config.invalid_names[0],
            observed_gate=False,
            execution_mode=execution_mode,
            policy_version=safety_config.policy_version,
        )

    checks = (
        (
            classification.contains_sensitive_mutations,
            safety_config.allow_sensitive_mutations,
            "GOOGLE_ADS_ALLOW_SENSITIVE_MUTATIONS",
            "SENSITIVE_MUTATION_GATE_DISABLED",
        ),
        (
            classification.contains_remove,
            safety_config.allow_remove,
            "GOOGLE_ADS_ALLOW_REMOVE",
            "REMOVE_GATE_DISABLED",
        ),
        (
            classification.contains_enable,
            safety_config.allow_enable,
            "GOOGLE_ADS_ALLOW_ENABLE",
            "ENABLE_GATE_DISABLED",
        ),
        (
            classification.partial_failure,
            safety_config.allow_partial_failure,
            "GOOGLE_ADS_ALLOW_PARTIAL_FAILURE",
            "PARTIAL_FAILURE_GATE_DISABLED",
        ),
    )
    for required, observed, gate, reason_code in checks:
        if required and not observed:
            return AuthorizationDecision(
                allowed=False,
                status="BLOCKED_BY_SECURITY_GATE",
                reason_code=reason_code,
                required_gate=gate,
                observed_gate=observed,
                execution_mode=execution_mode,
                policy_version=safety_config.policy_version,
            )

    return AuthorizationDecision(
        allowed=True,
        status="ALLOWED",
        reason_code="ALL_REQUIRED_GATES_ENABLED",
        required_gate=None,
        observed_gate=None,
        execution_mode=execution_mode,
        policy_version=safety_config.policy_version,
    )


def runtime_metadata(
    *, endpoint: str, executor_path: str = "GoogleAdsService.Mutate"
) -> Dict[str, Any]:
    """Returns only sanitized runtime identifiers and policy metadata."""
    commit = next(
        (
            os.environ.get(name)
            for name in (
                "GOOGLE_ADS_MCP_COMMIT_SHA",
                "GITHUB_SHA",
                "RENDER_GIT_COMMIT",
                "SOURCE_VERSION",
            )
            if os.environ.get(name)
        ),
        None,
    )
    revision = next(
        (
            os.environ.get(name)
            for name in (
                "GOOGLE_ADS_MCP_REVISION",
                "K_REVISION",
                "RENDER_SERVICE_ID",
                "DEPLOYMENT_REVISION",
            )
            if os.environ.get(name)
        ),
        None,
    )
    instance = next(
        (
            os.environ.get(name)
            for name in ("INSTANCE_ID", "K_REVISION", "HOSTNAME")
            if os.environ.get(name)
        ),
        socket.gethostname(),
    )
    return {
        "application_commit": commit,
        "deployment_revision": revision,
        "process_started_at": datetime.fromtimestamp(
            _PROCESS_STARTED_AT, timezone.utc
        ).isoformat(),
        "instance_id": instance,
        "config_source": "environment",
        "config_loaded": "per_request",
        "endpoint": endpoint,
        "executor_path": executor_path,
        "security_policy_version": _POLICY_VERSION,
    }


def log_policy_decision(
    *,
    endpoint: str,
    correlation_id: str,
    config: MutationSafetyConfig,
    classification: MutationClassification,
    decision: AuthorizationDecision,
    validate_only: bool,
) -> None:
    """Emits one structured, secret-free policy decision record."""
    payload = {
        "event": "google_ads_mutation_policy_decision",
        "endpoint": endpoint,
        "correlation_id": correlation_id,
        "policy_version": _POLICY_VERSION,
        "config": {
            "allow_enable": config.allow_enable,
            "allow_remove": config.allow_remove,
            "allow_partial_failure": config.allow_partial_failure,
            "allow_sensitive_mutations": config.allow_sensitive_mutations,
            "invalid_names": list(config.invalid_names),
        },
        "classification": classification.public_dict(),
        "decision": decision.public_dict(),
        "validate_only": validate_only,
        "runtime": runtime_metadata(endpoint=endpoint),
    }
    _LOGGER.info(json.dumps(payload, sort_keys=True, ensure_ascii=True))
