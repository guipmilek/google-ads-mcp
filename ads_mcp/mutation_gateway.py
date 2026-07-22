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

"""Unified endpoint gateway for guarded Google Ads mutations."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from typing import Any, Dict, List

from fastmcp.exceptions import ToolError

from ads_mcp import mutation_engine
from ads_mcp import mutation_safety
import ads_mcp.utils as utils
from ads_mcp.mutation_policy import (
    MutationClassification,
    MutationSafetyConfig,
    authorize_mutation_execution,
    classify_operations,
    log_policy_decision,
    runtime_metadata,
)

_POLICY_ENVELOPE_VERSION = 1
_POLICY_ENVELOPE_KIND = "google-ads-mutation-policy-envelope"


def _centralized_live_execution_policy(
    customer_id: str,
    operations: List[Dict[str, Any]],
    partial_failure: bool,
) -> None:
    """Defense-in-depth policy used by the underlying mutation engine."""
    if not mutation_safety._env_bool(
        "GOOGLE_ADS_MUTATIONS_ENABLED", False
    ):
        raise ToolError(
            "Live mutations are disabled. Set "
            "GOOGLE_ADS_MUTATIONS_ENABLED=true."
        )

    allowed = mutation_safety._allowed_customer_ids()
    if not allowed:
        raise ToolError(
            "No live mutation accounts are configured. Set "
            "GOOGLE_ADS_ALLOWED_CUSTOMER_IDS to an explicit comma-separated "
            "allowlist."
        )
    if customer_id not in allowed:
        raise ToolError(
            f"Customer {customer_id} is not in "
            "GOOGLE_ADS_ALLOWED_CUSTOMER_IDS."
        )

    if partial_failure and mutation_safety._contains_temporary_resource_id(
        operations
    ):
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
        raise _structured_error(
            status=decision.status,
            reason_code=decision.reason_code,
            message=(
                "The mutation was blocked by the centralized defense-in-depth "
                "policy."
            ),
            endpoint="mutation_engine_live_execution",
            classification=classification,
            config=config,
            required_gate=decision.required_gate,
            observed_gate=decision.observed_gate,
        )


def _install_unified_engine_policy() -> None:
    """Makes direct and batch engine paths use the same gate policy."""
    mutation_safety._validate_live_execution_policy = (
        _centralized_live_execution_policy
    )


_install_unified_engine_policy()


def _structured_error(
    *,
    status: str,
    reason_code: str,
    message: str,
    endpoint: str,
    classification: MutationClassification | None = None,
    config: MutationSafetyConfig | None = None,
    required_gate: str | None = None,
    observed_gate: bool | None = None,
) -> ToolError:
    payload: Dict[str, Any] = {
        "status": status,
        "reason_code": reason_code,
        "message": message,
        "endpoint": endpoint,
        "runtime": runtime_metadata(endpoint=endpoint),
    }
    if classification is not None:
        payload["classification"] = classification.public_dict()
    if config is not None:
        payload["safety_config"] = config.public_dict()
    if required_gate is not None:
        payload["required_gate"] = required_gate
        payload["observed_gate"] = observed_gate
    return ToolError(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _canonical_context(
    customer_id: str,
    operations: List[Dict[str, Any]],
    partial_failure: bool,
) -> tuple[str, List[Dict[str, Any]], str, MutationClassification]:
    normalized_customer_id = mutation_safety._normalize_customer_id(customer_id)
    if not operations:
        raise ToolError("At least one operation is required.")
    maximum = mutation_safety._max_operations()
    if len(operations) > maximum:
        raise ToolError(
            f"This request contains {len(operations)} operations; the "
            f"configured maximum is {maximum}. Split the plan into "
            "independently validated stages."
        )

    client = utils.get_googleads_client()
    prepared_operations: list[Dict[str, Any]] = []
    for operation in operations:
        _, prepared = mutation_engine._prepare_operation(
            client, normalized_customer_id, operation
        )
        prepared_operations.append(prepared)

    operation_hash = mutation_safety._operation_hash(
        normalized_customer_id,
        prepared_operations,
        partial_failure,
    )
    classification = classify_operations(prepared_operations, partial_failure)
    return (
        normalized_customer_id,
        prepared_operations,
        operation_hash,
        classification,
    )


def _classification_digest(classification: MutationClassification) -> str:
    canonical = json.dumps(
        classification.public_dict(),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _issue_policy_envelope(
    *,
    customer_id: str,
    operation_hash: str,
    classification: MutationClassification,
    inner_confirmation: str,
    policy_version: str,
) -> str:
    prefix, inner_hash, inner_payload = mutation_safety._decode_confirmation(
        inner_confirmation
    )
    if inner_hash != operation_hash:
        raise RuntimeError("Inner validation receipt hash mismatch.")
    expires_at = inner_payload.get("exp")
    issued_at = inner_payload.get("iat")
    if not isinstance(expires_at, int) or not isinstance(issued_at, int):
        raise RuntimeError("Inner validation receipt timestamps are invalid.")

    payload = {
        "v": _POLICY_ENVELOPE_VERSION,
        "kind": _POLICY_ENVELOPE_KIND,
        "policy_version": policy_version,
        "cid": customer_id,
        "hash": operation_hash,
        "verb": prefix,
        "partial_failure": classification.partial_failure,
        "classification": classification.public_dict(),
        "classification_digest": _classification_digest(classification),
        "inner_confirmation": inner_confirmation,
        "iat": issued_at,
        "exp": expires_at,
        "nonce": secrets.token_urlsafe(12),
    }
    payload_bytes = mutation_safety._confirmation_payload_bytes(payload)
    signature = hmac.new(
        mutation_safety._confirmation_secret(), payload_bytes, hashlib.sha256
    ).digest()
    return (
        f"{prefix} {operation_hash}."
        f"{mutation_safety._b64url_encode(payload_bytes)}."
        f"{mutation_safety._b64url_encode(signature)}"
    )


def _verify_policy_envelope(
    *,
    confirmation: str | None,
    customer_id: str,
    operation_hash: str,
    classification: MutationClassification,
    config: MutationSafetyConfig,
    endpoint: str,
) -> str:
    if not confirmation:
        raise _structured_error(
            status="BLOCKED_BY_SECURITY_GATE",
            reason_code="CONFIRMATION_REQUIRED",
            message="A confirmation issued by validate_only=true is required.",
            endpoint=endpoint,
            classification=classification,
            config=config,
        )

    try:
        prefix, token = confirmation.split(" ", 1)
        token_hash, payload_part, signature_part = token.split(".", 2)
        payload_bytes = mutation_safety._b64url_decode(payload_part)
        provided_signature = mutation_safety._b64url_decode(signature_part)
    except (ValueError, ToolError) as exc:
        raise _structured_error(
            status="BLOCKED_BY_SECURITY_GATE",
            reason_code="CONFIRMATION_MALFORMED",
            message="Confirmation token is malformed.",
            endpoint=endpoint,
            classification=classification,
            config=config,
        ) from exc

    expected_signature = hmac.new(
        mutation_safety._confirmation_secret(), payload_bytes, hashlib.sha256
    ).digest()
    if not hmac.compare_digest(provided_signature, expected_signature):
        raise _structured_error(
            status="BLOCKED_BY_SECURITY_GATE",
            reason_code="CONFIRMATION_SIGNATURE_INVALID",
            message="Confirmation signature is invalid.",
            endpoint=endpoint,
            classification=classification,
            config=config,
        )

    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _structured_error(
            status="BLOCKED_BY_SECURITY_GATE",
            reason_code="CONFIRMATION_MALFORMED",
            message="Confirmation payload is malformed.",
            endpoint=endpoint,
            classification=classification,
            config=config,
        ) from exc
    if not isinstance(payload, dict):
        raise _structured_error(
            status="BLOCKED_BY_SECURITY_GATE",
            reason_code="CONFIRMATION_MALFORMED",
            message="Confirmation payload is malformed.",
            endpoint=endpoint,
            classification=classification,
            config=config,
        )

    expected_verb = classification.confirmation_verb
    checks = (
        (
            payload.get("v") == _POLICY_ENVELOPE_VERSION
            and payload.get("kind") == _POLICY_ENVELOPE_KIND,
            "CONFIRMATION_VERSION_MISMATCH",
            "Confirmation envelope version is not supported.",
        ),
        (
            token_hash == operation_hash and payload.get("hash") == operation_hash,
            "CONFIRMATION_HASH_MISMATCH",
            "Confirmation hash does not match the exact operation payload.",
        ),
        (
            payload.get("cid") == customer_id,
            "CONFIRMATION_CUSTOMER_MISMATCH",
            "Confirmation customer does not match the request.",
        ),
        (
            prefix == expected_verb and payload.get("verb") == expected_verb,
            "CONFIRMATION_VERB_MISMATCH",
            "Confirmation verb does not match the operation risk.",
        ),
        (
            payload.get("partial_failure") == classification.partial_failure,
            "CONFIRMATION_PARTIAL_FAILURE_MISMATCH",
            "Confirmation partial_failure value does not match the request.",
        ),
    )
    for passed, reason_code, message in checks:
        if not passed:
            raise _structured_error(
                status="BLOCKED_BY_SECURITY_GATE",
                reason_code=reason_code,
                message=message,
                endpoint=endpoint,
                classification=classification,
                config=config,
            )

    expires_at = payload.get("exp")
    issued_at = payload.get("iat")
    now = int(time.time())
    if not isinstance(expires_at, int) or not isinstance(issued_at, int):
        raise _structured_error(
            status="BLOCKED_BY_SECURITY_GATE",
            reason_code="CONFIRMATION_TIMESTAMP_INVALID",
            message="Confirmation timestamps are invalid.",
            endpoint=endpoint,
            classification=classification,
            config=config,
        )
    if expires_at <= now:
        raise _structured_error(
            status="BLOCKED_BY_SECURITY_GATE",
            reason_code="CONFIRMATION_EXPIRED",
            message="Confirmation expired. Re-run validate_only=true.",
            endpoint=endpoint,
            classification=classification,
            config=config,
        )

    expected_classification = classification.public_dict()
    if (
        payload.get("policy_version") != config.policy_version
        or payload.get("classification") != expected_classification
        or payload.get("classification_digest")
        != _classification_digest(classification)
    ):
        raise _structured_error(
            status="BLOCKED_BY_STATE_OR_POLICY_DRIFT",
            reason_code="POLICY_OR_CLASSIFICATION_DRIFT",
            message=(
                "The current policy or recalculated operation classification "
                "does not match the validated card."
            ),
            endpoint=endpoint,
            classification=classification,
            config=config,
        )

    inner_confirmation = payload.get("inner_confirmation")
    if not isinstance(inner_confirmation, str) or not inner_confirmation:
        raise _structured_error(
            status="BLOCKED_BY_SECURITY_GATE",
            reason_code="CONFIRMATION_MALFORMED",
            message="Confirmation does not contain the inner validation receipt.",
            endpoint=endpoint,
            classification=classification,
            config=config,
        )
    return inner_confirmation


def _required_gates(classification: MutationClassification) -> List[str]:
    gates: list[str] = []
    if classification.contains_sensitive_mutations:
        gates.append("GOOGLE_ADS_ALLOW_SENSITIVE_MUTATIONS")
    if classification.contains_remove:
        gates.append("GOOGLE_ADS_ALLOW_REMOVE")
    if classification.contains_enable:
        gates.append("GOOGLE_ADS_ALLOW_ENABLE")
    if classification.partial_failure:
        gates.append("GOOGLE_ADS_ALLOW_PARTIAL_FAILURE")
    return gates


def _enrich_response(
    response: Dict[str, Any],
    *,
    endpoint: str,
    config: MutationSafetyConfig,
    classification: MutationClassification,
    decision: Any,
    correlation_id: str,
) -> Dict[str, Any]:
    enriched = dict(response)
    enriched["security_policy"] = {
        "policy_version": config.policy_version,
        "endpoint": endpoint,
        "correlation_id": correlation_id,
        "config": config.public_dict(),
        "classification": classification.public_dict(),
        "required_gates": _required_gates(classification),
        "decision": decision.public_dict(),
        "runtime": runtime_metadata(endpoint=endpoint),
    }
    return enriched


def _invoke_mutation(
    *,
    endpoint: str,
    customer_id: str,
    operations: List[Dict[str, Any]],
    validate_only: bool,
    partial_failure: bool,
    confirmation: str | None,
) -> Dict[str, Any]:
    (
        normalized_customer_id,
        prepared_operations,
        operation_hash,
        classification,
    ) = _canonical_context(customer_id, operations, partial_failure)
    config = MutationSafetyConfig.from_environment()
    decision = authorize_mutation_execution(
        classification, config, validate_only=validate_only
    )
    correlation_id = secrets.token_hex(8)
    log_policy_decision(
        endpoint=endpoint,
        correlation_id=correlation_id,
        config=config,
        classification=classification,
        decision=decision,
        validate_only=validate_only,
    )

    if not decision.allowed:
        raise _structured_error(
            status=decision.status,
            reason_code=decision.reason_code,
            message="The mutation was blocked by the centralized gate policy.",
            endpoint=endpoint,
            classification=classification,
            config=config,
            required_gate=decision.required_gate,
            observed_gate=decision.observed_gate,
        )

    inner_confirmation = confirmation
    if not validate_only:
        inner_confirmation = _verify_policy_envelope(
            confirmation=confirmation,
            customer_id=normalized_customer_id,
            operation_hash=operation_hash,
            classification=classification,
            config=config,
            endpoint=endpoint,
        )

    try:
        response = mutation_engine._run_mutations(
            normalized_customer_id,
            operations,
            validate_only=validate_only,
            partial_failure=partial_failure,
            confirmation=inner_confirmation,
        )
    except ToolError as exc:
        text = str(exc)
        if "already used" in text:
            raise _structured_error(
                status="BLOCKED_BY_SECURITY_GATE",
                reason_code="CONFIRMATION_REPLAYED",
                message="Confirmation was already used by this server process.",
                endpoint=endpoint,
                classification=classification,
                config=config,
            ) from exc
        raise

    if response.get("operation_hash") != operation_hash:
        raise _structured_error(
            status="BLOCKED_BY_STATE_OR_POLICY_DRIFT",
            reason_code="OPERATION_HASH_DRIFT",
            message="Mutation engine operation hash changed after canonicalization.",
            endpoint=endpoint,
            classification=classification,
            config=config,
        )
    if response.get("operations") != prepared_operations:
        raise _structured_error(
            status="BLOCKED_BY_STATE_OR_POLICY_DRIFT",
            reason_code="OPERATION_NORMALIZATION_DRIFT",
            message="Mutation engine normalized operations changed unexpectedly.",
            endpoint=endpoint,
            classification=classification,
            config=config,
        )

    enriched = _enrich_response(
        response,
        endpoint=endpoint,
        config=config,
        classification=classification,
        decision=decision,
        correlation_id=correlation_id,
    )
    if validate_only and response.get("required_confirmation"):
        enriched["required_confirmation"] = _issue_policy_envelope(
            customer_id=normalized_customer_id,
            operation_hash=operation_hash,
            classification=classification,
            inner_confirmation=response["required_confirmation"],
            policy_version=config.policy_version,
        )
    return enriched


def get_mutation_safety_status(
    customer_id: str | None = None,
    operations: List[Dict[str, Any]] | None = None,
    partial_failure: bool = False,
    validate_only: bool = False,
    endpoint: str = "mutations_batch_mutate",
) -> Dict[str, Any]:
    """Returns sanitized gate/runtime state and optionally simulates a payload.

    This tool never calls Google Ads and never issues an executable confirmation.
    When ``customer_id`` and ``operations`` are supplied, it canonicalizes the
    operations locally and reports the exact gate decision that the endpoint
    would make.
    """
    config = MutationSafetyConfig.from_environment()
    result: Dict[str, Any] = {
        "status": "READ_ONLY",
        "simulation_only": True,
        "api_call_performed": False,
        "confirmation_issued": False,
        "runtime": runtime_metadata(endpoint=endpoint),
        "safety_config": config.public_dict(),
    }
    if operations is None:
        return result

    if customer_id:
        (
            normalized_customer_id,
            prepared_operations,
            operation_hash,
            classification,
        ) = _canonical_context(customer_id, operations, partial_failure)
        result.update(
            {
                "customer_id": normalized_customer_id,
                "operation_hash": operation_hash,
                "operations": prepared_operations,
            }
        )
    else:
        classification = classify_operations(operations, partial_failure)

    decision = authorize_mutation_execution(
        classification, config, validate_only=validate_only
    )
    result.update(
        {
            "classification": classification.public_dict(),
            "required_gates": _required_gates(classification),
            "decision": decision.public_dict(),
        }
    )
    return result


def create_resource(
    customer_id: str,
    resource: str,
    data: Dict[str, Any],
    validate_only: bool = True,
    confirmation: str | None = None,
) -> Dict[str, Any]:
    """Creates one resource through the centralized mutation policy."""
    return _invoke_mutation(
        endpoint="mutations_create_resource",
        customer_id=customer_id,
        operations=[{"action": "create", "resource": resource, "data": data}],
        validate_only=validate_only,
        partial_failure=False,
        confirmation=confirmation,
    )


def update_resource(
    customer_id: str,
    resource: str,
    data: Dict[str, Any],
    update_mask: List[str],
    validate_only: bool = True,
    confirmation: str | None = None,
) -> Dict[str, Any]:
    """Updates one resource through the centralized mutation policy."""
    return _invoke_mutation(
        endpoint="mutations_update_resource",
        customer_id=customer_id,
        operations=[
            {
                "action": "update",
                "resource": resource,
                "data": data,
                "update_mask": update_mask,
            }
        ],
        validate_only=validate_only,
        partial_failure=False,
        confirmation=confirmation,
    )


def remove_resource(
    customer_id: str,
    resource: str,
    resource_name: str,
    validate_only: bool = True,
    confirmation: str | None = None,
) -> Dict[str, Any]:
    """Removes one resource through the centralized mutation policy."""
    return _invoke_mutation(
        endpoint="mutations_remove_resource",
        customer_id=customer_id,
        operations=[
            {
                "action": "remove",
                "resource": resource,
                "resource_name": resource_name,
            }
        ],
        validate_only=validate_only,
        partial_failure=False,
        confirmation=confirmation,
    )


def batch_mutate(
    customer_id: str,
    operations: List[Dict[str, Any]],
    validate_only: bool = True,
    partial_failure: bool = False,
    confirmation: str | None = None,
) -> Dict[str, Any]:
    """Runs one atomic mixed-resource batch through the centralized policy."""
    return _invoke_mutation(
        endpoint="mutations_batch_mutate",
        customer_id=customer_id,
        operations=operations,
        validate_only=validate_only,
        partial_failure=partial_failure,
        confirmation=confirmation,
    )
