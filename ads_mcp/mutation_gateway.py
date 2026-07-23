# Copyright 2026 Google LLC.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Direct endpoint facade for Google Ads CRUD."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Literal

from fastmcp.exceptions import ToolError

from ads_mcp import mutation_engine, mutation_safety

_AD_GROUP_AD_STATUS_LIMIT = 10
_AD_GROUP_AD_RESOURCE_NAME_PATTERN = re.compile(
    r"^customers/(?P<customer_id>[0-9]+)/adGroupAds/"
    r"(?P<ad_group_id>[0-9]+)~(?P<ad_id>[0-9]+)$"
)


def get_mutation_crud_status() -> Dict[str, Any]:
    """Return the direct CRUD contract and non-secret Ads scope."""

    return {
        "contract_version": mutation_safety.CRUD_CONTRACT_VERSION,
        "runtime": "PYTHON_FASTMCP_HORIZON",
        "write_mode": "DIRECT",
        "dry_run_supported": True,
        "approval_workflow": False,
        "native_validate_only_before_live_execution": True,
        "allowed_customer_ids": sorted(mutation_safety._allowed_customer_ids()),
        "max_operations_per_request": mutation_safety._max_operations(),
        "atomic_by_default": True,
        "partial_failure_supported": True,
    }


def create_resource(
    customer_id: str,
    resource: str,
    data: Dict[str, Any],
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Create one Google Ads resource in a single MCP call."""

    return mutation_engine.create_resource(customer_id, resource, data, dry_run)


def update_resource(
    customer_id: str,
    resource: str,
    data: Dict[str, Any],
    update_mask: List[str],
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Update one Google Ads resource in a single MCP call."""

    return mutation_engine.update_resource(
        customer_id, resource, data, update_mask, dry_run
    )


def remove_resource(
    customer_id: str,
    resource: str,
    resource_name: str,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Remove one Google Ads resource in a single MCP call."""

    return mutation_engine.remove_resource(
        customer_id, resource, resource_name, dry_run
    )


def _build_ad_group_ad_status_operations(
    customer_id: str,
    resource_names: List[str],
    status: Literal["ENABLED", "PAUSED"],
) -> tuple[str, List[Dict[str, Any]]]:
    """Validate the constrained tool contract and build atomic operations."""

    normalized_customer_id = mutation_safety._normalize_customer_id(customer_id)
    if not isinstance(resource_names, list):
        raise ToolError("resource_names must be a list.")
    if not resource_names:
        raise ToolError("At least one AdGroupAd resource_name is required.")
    if len(resource_names) > _AD_GROUP_AD_STATUS_LIMIT:
        raise ToolError(
            "This tool accepts at most "
            f"{_AD_GROUP_AD_STATUS_LIMIT} AdGroupAd resource names."
        )
    if len(set(resource_names)) != len(resource_names):
        raise ToolError("Duplicate AdGroupAd resource names are not allowed.")
    if not isinstance(status, str) or status not in {"ENABLED", "PAUSED"}:
        raise ToolError("status must be exactly ENABLED or PAUSED.")

    operations: list[Dict[str, Any]] = []
    for resource_name in resource_names:
        if not isinstance(resource_name, str):
            raise ToolError("Every resource_name must be a string.")
        match = _AD_GROUP_AD_RESOURCE_NAME_PATTERN.fullmatch(resource_name)
        if match is None:
            raise ToolError(
                "Invalid AdGroupAd resource_name. Expected "
                "customers/{customer_id}/adGroupAds/{ad_group_id}~{ad_id}."
            )
        if match.group("customer_id") != normalized_customer_id:
            raise ToolError(
                "AdGroupAd resource_name customer does not match customer_id."
            )
        operations.append(
            {
                "action": "update",
                "resource": "AdGroupAd",
                "data": {
                    "resource_name": resource_name,
                    "status": status,
                },
                "update_mask": ["status"],
            }
        )
    return normalized_customer_id, operations


def update_ad_group_ad_statuses(
    customer_id: str,
    resource_names: List[str],
    status: Literal["ENABLED", "PAUSED"],
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Atomically update status for 1-10 AdGroupAd resources."""

    normalized_customer_id, operations = _build_ad_group_ad_status_operations(
        customer_id, resource_names, status
    )
    return mutation_engine.batch_mutate(
        normalized_customer_id,
        operations,
        dry_run=dry_run,
        partial_failure=False,
    )


def batch_mutate(
    customer_id: str,
    operations: List[Dict[str, Any]],
    dry_run: bool = False,
    partial_failure: bool = False,
) -> Dict[str, Any]:
    """Run one direct mixed-resource Google Ads batch."""

    return mutation_engine.batch_mutate(
        customer_id,
        operations,
        dry_run=dry_run,
        partial_failure=partial_failure,
    )
