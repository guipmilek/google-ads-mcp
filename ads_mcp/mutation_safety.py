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

"""Safety policies shared by the Google Ads mutation tools."""

from __future__ import annotations

import base64
import binascii
import copy
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from typing import Any, Dict, List

from fastmcp.exceptions import ToolError

_ACTIONS = ("create", "update", "remove")
_DEFAULT_MAX_OPERATIONS = 20
_API_MAX_MUTATE_OPERATIONS = 10_000
_DEFAULT_CONFIRMATION_TTL_SECONDS = 15 * 60
_MAX_CONFIRMATION_TTL_SECONDS = 60 * 60
_MIN_CONFIRMATION_SECRET_BYTES = 32
_OPERATION_HASH_VERSION = 3
_CONFIRMATION_TOKEN_VERSION = 1
_SENSITIVE_RESOURCES = {
    "AccountBudgetProposal",
    "AccountLink",
    "BillingSetup",
    "CustomerClientLink",
    "CustomerManagerLink",
    "CustomerUserAccess",
    "CustomerUserAccessInvitation",
}

# A signed confirmation is valid across replicas. This local cache only adds
# best-effort replay protection inside one Python process. Exactly-once delivery
# requires a durable shared store, which this connector does not assume.
_USED_CONFIRMATIONS: dict[str, int] = {}
_USED_CONFIRMATIONS_LOCK = threading.Lock()


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _normalize_customer_id(customer_id: str) -> str:
    normalized = re.sub(r"\D", "", str(customer_id))
    if len(normalized) != 10:
        raise ToolError(
            "customer_id must contain exactly 10 digits, with or without "
            "hyphens."
        )
    return normalized


def _allowed_customer_ids() -> set[str]:
    raw = os.environ.get("GOOGLE_ADS_ALLOWED_CUSTOMER_IDS", "")
    return {
        _normalize_customer_id(value.strip())
        for value in raw.split(",")
        if value.strip()
    }


def _max_operations() -> int:
    raw = os.environ.get(
        "GOOGLE_ADS_MAX_OPERATIONS_PER_REQUEST",
        str(_DEFAULT_MAX_OPERATIONS),
    )
    try:
        value = int(raw)
    except ValueError as exc:
        raise ToolError(
            "GOOGLE_ADS_MAX_OPERATIONS_PER_REQUEST must be an integer."
        ) from exc
    if value < 1:
        raise ToolError(
            "GOOGLE_ADS_MAX_OPERATIONS_PER_REQUEST must be greater than zero."
        )
    if value > _API_MAX_MUTATE_OPERATIONS:
        raise ToolError(
            "GOOGLE_ADS_MAX_OPERATIONS_PER_REQUEST cannot exceed "
            f"{_API_MAX_MUTATE_OPERATIONS}."
        )
    return value


def _confirmation_ttl_seconds() -> int:
    raw = os.environ.get(
        "GOOGLE_ADS_CONFIRMATION_TTL_SECONDS",
        str(_DEFAULT_CONFIRMATION_TTL_SECONDS),
    )
    try:
        value = int(raw)
    except ValueError as exc:
        raise ToolError(
            "GOOGLE_ADS_CONFIRMATION_TTL_SECONDS must be an integer."
        ) from exc
    if value < 1:
        raise ToolError(
            "GOOGLE_ADS_CONFIRMATION_TTL_SECONDS must be greater than zero."
        )
    if value > _MAX_CONFIRMATION_TTL_SECONDS:
        raise ToolError(
            "GOOGLE_ADS_CONFIRMATION_TTL_SECONDS cannot exceed "
            f"{_MAX_CONFIRMATION_TTL_SECONDS}."
        )
    return value


def _confirmation_secret() -> bytes:
    raw = os.environ.get("GOOGLE_ADS_CONFIRMATION_SECRET", "").strip()
    secret = raw.encode("utf-8")
    if len(secret) < _MIN_CONFIRMATION_SECRET_BYTES:
        raise ToolError(
            "GOOGLE_ADS_CONFIRMATION_SECRET must be configured with at least "
            f"{_MIN_CONFIRMATION_SECRET_BYTES} bytes before executable "
            "confirmations can be issued."
        )
    return secret


def _validate_confirmation_configuration() -> None:
    """Fails before validation when confirmation signing is misconfigured."""
    _confirmation_secret()
    _confirmation_ttl_seconds()


def _canonical_resource_key(value: str) -> str:
    key = re.sub(r"[^a-zA-Z0-9]", "", value).lower()
    return key.removesuffix("operation")


def _apply_create_status_guard(
    data: Dict[str, Any], resource_descriptor: Any
) -> Dict[str, Any]:
    """Defaults a create to PAUSED only when that resource supports PAUSED."""
    prepared = copy.deepcopy(data)
    status_field = resource_descriptor.fields_by_name.get("status")
    if status_field is None or status_field.enum_type is None:
        return prepared

    enum_names = {value.name for value in status_field.enum_type.values}
    if "PAUSED" not in enum_names:
        return prepared

    requested_status = str(prepared.get("status", "PAUSED")).upper()
    if requested_status != "PAUSED":
        raise ToolError(
            "Resources that support PAUSED must be created as PAUSED. Review "
            "the resource, then enable it with a separate update."
        )
    prepared["status"] = "PAUSED"
    return prepared


def _validate_resource_name(customer_id: str, resource_name: str) -> None:
    if not resource_name.startswith(f"customers/{customer_id}/"):
        raise ToolError(
            "resource_name must belong to the same customer_id used by the "
            "request."
        )


def _validate_resource_references(
    customer_id: str, value: Any, path: str = "data"
) -> None:
    """Rejects nested references to a different Google Ads customer."""
    if isinstance(value, dict):
        for key, nested in value.items():
            _validate_resource_references(customer_id, nested, f"{path}.{key}")
        return
    if isinstance(value, list):
        for index, nested in enumerate(value):
            _validate_resource_references(
                customer_id, nested, f"{path}[{index}]"
            )
        return
    if isinstance(value, str) and value.startswith("customers/"):
        if not value.startswith(f"customers/{customer_id}/"):
            raise ToolError(
                f"Resource reference at '{path}' belongs to another "
                "customer_id."
            )


def _contains_temporary_resource_id(value: Any) -> bool:
    if isinstance(value, dict):
        return any(
            _contains_temporary_resource_id(item) for item in value.values()
        )
    if isinstance(value, list):
        return any(_contains_temporary_resource_id(item) for item in value)
    return (
        isinstance(value, str) and re.search(r"/-\d+(?:$|/)", value) is not None
    )


def _normalize_update_mask(
    update_mask: List[str], resource_field_name: str
) -> List[str]:
    normalized: list[str] = []
    prefix = resource_field_name.removesuffix("_operation")
    for path in update_mask:
        value = str(path).strip()
        if value.startswith(f"{prefix}."):
            value = value[len(prefix) + 1 :]
        value = ".".join(
            "type" if segment == "type_" else segment
            for segment in value.split(".")
        )
        if not value:
            continue
        if value == "resource_name":
            raise ToolError(
                "resource_name must not be included in update_mask."
            )
        normalized.append(value)
    if not normalized:
        raise ToolError(
            "update_mask must contain at least one mutable field path."
        )
    return sorted(set(normalized))


def _positive_micros(value: Any, field_name: str) -> int:
    try:
        amount = int(value)
    except (TypeError, ValueError) as exc:
        raise ToolError(
            f"CampaignBudget {field_name} must be an integer."
        ) from exc
    if amount <= 0:
        raise ToolError(
            f"CampaignBudget {field_name} must be greater than zero."
        )
    return amount


def _configured_positive_limit(name: str) -> int | None:
    raw = os.environ.get(name)
    if not raw:
        return None
    try:
        limit = int(raw)
    except ValueError as exc:
        raise ToolError(f"{name} must be an integer.") from exc
    if limit <= 0:
        raise ToolError(f"{name} must be greater than zero.")
    return limit


def _validate_budget_limit(resource_name: str, data: Dict[str, Any]) -> None:
    if resource_name != "CampaignBudget":
        return

    if "amount_micros" in data:
        amount = _positive_micros(data["amount_micros"], "amount_micros")
        limit = _configured_positive_limit("GOOGLE_ADS_MAX_DAILY_BUDGET_MICROS")
        if limit is not None and amount > limit:
            raise ToolError(
                f"Campaign budget {amount} micros exceeds the configured "
                f"daily limit of {limit} micros."
            )

    if "total_amount_micros" in data:
        total = _positive_micros(
            data["total_amount_micros"], "total_amount_micros"
        )
        total_limit = _configured_positive_limit(
            "GOOGLE_ADS_MAX_TOTAL_BUDGET_MICROS"
        )
        if total_limit is None:
            raise ToolError(
                "CampaignBudget total_amount_micros is blocked until "
                "GOOGLE_ADS_MAX_TOTAL_BUDGET_MICROS is configured."
            )
        if total > total_limit:
            raise ToolError(
                f"Campaign total budget {total} micros exceeds the "
                f"configured limit of {total_limit} micros."
            )


def _operation_hash(
    customer_id: str,
    operations: List[Dict[str, Any]],
    partial_failure: bool,
) -> str:
    canonical = json.dumps(
        {
            "confirmation_version": _OPERATION_HASH_VERSION,
            "customer_id": customer_id,
            "operations": operations,
            "partial_failure": partial_failure,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def _contains_enabled_status(value: Any) -> bool:
    if isinstance(value, dict):
        for key, nested in value.items():
            if key == "status" and str(nested).upper() == "ENABLED":
                return True
            if _contains_enabled_status(nested):
                return True
    elif isinstance(value, list):
        return any(_contains_enabled_status(item) for item in value)
    return False


def _required_confirmation_verb(operations: List[Dict[str, Any]]) -> str:
    resources = {operation["resource"] for operation in operations}
    if resources & _SENSITIVE_RESOURCES:
        return "SENSITIVE"

    has_remove = any(
        operation["action"] == "remove" for operation in operations
    )
    has_enable = any(
        _contains_enabled_status(operation.get("data"))
        for operation in operations
    )
    if has_remove and has_enable:
        return "REMOVE_AND_ENABLE"
    if has_remove:
        return "REMOVE"
    if has_enable:
        return "ENABLE"
    return "EXECUTE"


def _validate_live_execution_policy(
    customer_id: str,
    operations: List[Dict[str, Any]],
    partial_failure: bool,
) -> None:
    if not _env_bool("GOOGLE_ADS_MUTATIONS_ENABLED", False):
        raise ToolError(
            "Live mutations are disabled. Set "
            "GOOGLE_ADS_MUTATIONS_ENABLED=true."
        )

    allowed = _allowed_customer_ids()
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

    resources = {operation["resource"] for operation in operations}
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


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    try:
        decoded = base64.b64decode(
            value + padding, altchars=b"-_", validate=True
        )
    except (binascii.Error, ValueError, TypeError) as exc:
        raise ToolError("Confirmation token is malformed.") from exc
    if _b64url_encode(decoded) != value:
        raise ToolError("Confirmation token encoding is not canonical.")
    return decoded


def _confirmation_payload_bytes(payload: Dict[str, Any]) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def _prune_used_confirmations(now: int) -> None:
    expired = [
        token_id
        for token_id, expires_at in _USED_CONFIRMATIONS.items()
        if expires_at <= now
    ]
    for token_id in expired:
        _USED_CONFIRMATIONS.pop(token_id, None)


def _issue_validation_receipt(
    customer_id: str,
    operations: List[Dict[str, Any]],
    operation_hash: str,
    partial_failure: bool,
) -> Dict[str, Any]:
    """Issues an expiring HMAC-signed confirmation valid across replicas."""
    now = int(time.time())
    expires_at = now + _confirmation_ttl_seconds()
    confirmation_verb = _required_confirmation_verb(operations)
    payload = {
        "v": _CONFIRMATION_TOKEN_VERSION,
        "cid": customer_id,
        "hash": operation_hash,
        "verb": confirmation_verb,
        "partial_failure": partial_failure,
        "iat": now,
        "exp": expires_at,
        "nonce": secrets.token_urlsafe(12),
    }
    payload_bytes = _confirmation_payload_bytes(payload)
    signature = hmac.new(
        _confirmation_secret(), payload_bytes, hashlib.sha256
    ).digest()
    confirmation = (
        f"{confirmation_verb} {operation_hash}."
        f"{_b64url_encode(payload_bytes)}.{_b64url_encode(signature)}"
    )
    return {
        "confirmation": confirmation,
        "confirmation_verb": confirmation_verb,
        "expires_at": expires_at,
        "cross_instance_valid": True,
        "replay_protection": "BEST_EFFORT_PROCESS_LOCAL",
        "globally_single_use": False,
    }


def _decode_confirmation(confirmation: str) -> tuple[str, str, Dict[str, Any]]:
    try:
        prefix, token = confirmation.split(" ", 1)
        operation_hash, payload_part, signature_part = token.split(".", 2)
    except ValueError as exc:
        raise ToolError("Confirmation token is malformed.") from exc

    payload_bytes = _b64url_decode(payload_part)
    provided_signature = _b64url_decode(signature_part)
    expected_signature = hmac.new(
        _confirmation_secret(), payload_bytes, hashlib.sha256
    ).digest()
    if not hmac.compare_digest(provided_signature, expected_signature):
        raise ToolError("Confirmation signature is invalid.")

    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ToolError("Confirmation payload is malformed.") from exc
    if not isinstance(payload, dict):
        raise ToolError("Confirmation payload is malformed.")
    return prefix, operation_hash, payload


def _consume_validation_receipt(
    customer_id: str,
    operations: List[Dict[str, Any]],
    operation_hash: str,
    partial_failure: bool,
    confirmation: str | None,
) -> Dict[str, Any]:
    if not confirmation:
        raise ToolError(
            "A confirmation issued by a successful validate_only call is "
            "required."
        )

    prefix, token_hash, payload = _decode_confirmation(confirmation)
    expected_verb = _required_confirmation_verb(operations)
    now = int(time.time())
    mismatches: list[str] = []
    if payload.get("v") != _CONFIRMATION_TOKEN_VERSION:
        mismatches.append("token_version")
    if prefix != expected_verb or payload.get("verb") != expected_verb:
        mismatches.append("confirmation_verb")
    if token_hash != operation_hash or payload.get("hash") != operation_hash:
        mismatches.append("operation_hash")
    if payload.get("cid") != customer_id:
        mismatches.append("customer_id")
    if payload.get("partial_failure") != partial_failure:
        mismatches.append("partial_failure")
    if mismatches:
        raise ToolError(
            "Confirmation does not match the exact validated payload: "
            + ", ".join(mismatches)
            + ". Re-run validate_only=true."
        )

    issued_at = payload.get("iat")
    expires_at = payload.get("exp")
    if not isinstance(issued_at, int) or not isinstance(expires_at, int):
        raise ToolError("Confirmation timestamps are invalid.")
    if not isinstance(payload.get("nonce"), str) or not payload["nonce"]:
        raise ToolError("Confirmation nonce is invalid.")
    if issued_at > now + 60:
        raise ToolError("Confirmation was issued in the future.")
    if expires_at <= issued_at:
        raise ToolError("Confirmation validity interval is invalid.")
    if expires_at - issued_at > _MAX_CONFIRMATION_TTL_SECONDS:
        raise ToolError("Confirmation validity interval is too long.")
    if expires_at <= now:
        raise ToolError(
            "Confirmation expired. Re-run validate_only=true before executing."
        )

    token_id = hashlib.sha256(confirmation.encode("utf-8")).hexdigest()
    with _USED_CONFIRMATIONS_LOCK:
        _prune_used_confirmations(now)
        if token_id in _USED_CONFIRMATIONS:
            raise ToolError(
                "Confirmation was already used by this server process. "
                "Re-run validate_only=true before executing again."
            )
        # Register before the live call. If the API commits but the response is
        # lost, the same process will not retry the write with this token.
        _USED_CONFIRMATIONS[token_id] = expires_at

    return {
        "confirmation": confirmation,
        "confirmation_verb": expected_verb,
        "expires_at": expires_at,
        "cross_instance_valid": True,
        "replay_protection": "BEST_EFFORT_PROCESS_LOCAL",
        "globally_single_use": False,
        "registered_before_api_call": True,
    }


def _validate_live_execution(
    customer_id: str,
    operations: List[Dict[str, Any]],
    operation_hash: str,
    confirmation: str | None,
    partial_failure: bool = False,
) -> Dict[str, Any]:
    _validate_live_execution_policy(customer_id, operations, partial_failure)
    return _consume_validation_receipt(
        customer_id,
        operations,
        operation_hash,
        partial_failure,
        confirmation,
    )


def _clear_confirmation_replay_cache_for_tests() -> None:
    with _USED_CONFIRMATIONS_LOCK:
        _USED_CONFIRMATIONS.clear()
