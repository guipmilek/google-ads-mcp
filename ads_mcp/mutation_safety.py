# Copyright 2026 Google LLC.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Scope and payload validation shared by direct Google Ads CRUD tools."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from typing import Any, Dict, List

from fastmcp.exceptions import ToolError

CRUD_CONTRACT_VERSION = "direct-crud-v1"
_ACTIONS = ("create", "update", "remove")
_DEFAULT_MAX_OPERATIONS = 20
_API_MAX_MUTATE_OPERATIONS = 10_000
_OPERATION_HASH_VERSION = 4
_SENSITIVE_RESOURCES = {
    "AccountBudgetProposal",
    "AccountLink",
    "BillingSetup",
    "CustomerClientLink",
    "CustomerManagerLink",
    "CustomerUserAccess",
    "CustomerUserAccessInvitation",
}


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


def _validate_customer_scope(customer_id: str) -> str:
    normalized = _normalize_customer_id(customer_id)
    allowed = _allowed_customer_ids()
    if not allowed:
        raise ToolError("GOOGLE_ADS_ALLOWED_CUSTOMER_IDS is not configured.")
    if normalized not in allowed:
        raise ToolError(
            f"Customer {normalized} is outside GOOGLE_ADS_ALLOWED_CUSTOMER_IDS."
        )
    return normalized


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
    if not 1 <= value <= _API_MAX_MUTATE_OPERATIONS:
        raise ToolError(
            "GOOGLE_ADS_MAX_OPERATIONS_PER_REQUEST must be between 1 and "
            f"{_API_MAX_MUTATE_OPERATIONS}."
        )
    return value


def _canonical_resource_key(value: str) -> str:
    key = re.sub(r"[^a-zA-Z0-9]", "", value).lower()
    return key.removesuffix("operation")


def _apply_create_status_guard(
    data: Dict[str, Any], resource_descriptor: Any
) -> Dict[str, Any]:
    """Return create data unchanged except for an isolated defensive copy."""

    del resource_descriptor
    return copy.deepcopy(data)


def _validate_resource_name(customer_id: str, resource_name: str) -> None:
    if not resource_name.startswith(f"customers/{customer_id}/"):
        raise ToolError(
            "resource_name must belong to the same customer_id used by the "
            "request."
        )


def _validate_resource_references(
    customer_id: str, value: Any, path: str = "data"
) -> None:
    """Reject nested references to a different Google Ads customer."""

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
                "CampaignBudget total_amount_micros requires "
                "GOOGLE_ADS_MAX_TOTAL_BUDGET_MICROS."
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
            "contract_version": CRUD_CONTRACT_VERSION,
            "operation_hash_version": _OPERATION_HASH_VERSION,
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
