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

import copy
import hashlib
import json
import os
import re
from typing import Any, Dict, List

from fastmcp.exceptions import ToolError

_ACTIONS = ("create", "update", "remove")
_DEFAULT_MAX_OPERATIONS = 20
_SENSITIVE_RESOURCES = {
    "AccountBudgetProposal",
    "AccountLink",
    "BillingSetup",
    "CustomerClientLink",
    "CustomerManagerLink",
    "CustomerUserAccess",
    "CustomerUserAccessInvitation",
}


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _normalize_customer_id(customer_id: str) -> str:
    normalized = re.sub(r"\D", "", str(customer_id))
    if len(normalized) != 10:
        raise ToolError(
            "customer_id must contain exactly 10 digits, with or without hyphens."
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
    return value


def _canonical_resource_key(value: str) -> str:
    key = re.sub(r"[^a-zA-Z0-9]", "", value).lower()
    return key.removesuffix("operation")


def _apply_create_status_guard(
    data: Dict[str, Any], resource_descriptor: Any
) -> Dict[str, Any]:
    """Defaults status-bearing resources to PAUSED when the enum supports it."""
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
            "Status-bearing resources must be created as PAUSED. Review the "
            "resource, then enable it with a separate update."
        )
    prepared["status"] = "PAUSED"
    return prepared


def _validate_resource_name(customer_id: str, resource_name: str) -> None:
    if not resource_name.startswith(f"customers/{customer_id}/"):
        raise ToolError(
            "resource_name must belong to the same customer_id used by the request."
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


def _validate_budget_limit(resource_name: str, data: Dict[str, Any]) -> None:
    if resource_name != "CampaignBudget" or "amount_micros" not in data:
        return

    raw_limit = os.environ.get("GOOGLE_ADS_MAX_DAILY_BUDGET_MICROS")
    if not raw_limit:
        return
    try:
        limit = int(raw_limit)
        amount = int(data["amount_micros"])
    except (TypeError, ValueError) as exc:
        raise ToolError(
            "CampaignBudget amount_micros and "
            "GOOGLE_ADS_MAX_DAILY_BUDGET_MICROS must be integers."
        ) from exc
    if amount > limit:
        raise ToolError(
            f"Campaign budget {amount} micros exceeds the configured daily limit "
            f"of {limit} micros."
        )


def _operation_hash(
    customer_id: str,
    operations: List[Dict[str, Any]],
    partial_failure: bool,
) -> str:
    canonical = json.dumps(
        {
            "customer_id": customer_id,
            "operations": operations,
            "partial_failure": partial_failure,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


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


def _validate_live_execution(
    customer_id: str,
    operations: List[Dict[str, Any]],
    operation_hash: str,
    confirmation: str | None,
) -> str:
    if not _env_bool("GOOGLE_ADS_MUTATIONS_ENABLED", False):
        raise ToolError(
            "Live mutations are disabled. Set GOOGLE_ADS_MUTATIONS_ENABLED=true."
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
            f"Customer {customer_id} is not in GOOGLE_ADS_ALLOWED_CUSTOMER_IDS."
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
                "Remove operations are disabled. Set GOOGLE_ADS_ALLOW_REMOVE=true "
                "only when deletion is intentionally permitted."
            )

    expected = f"{_required_confirmation_verb(operations)} {operation_hash}"
    if confirmation != expected:
        raise ToolError(
            "Explicit confirmation is required for the exact validated payload. "
            f"Pass confirmation='{expected}' only after the user approves it."
        )
    return expected
