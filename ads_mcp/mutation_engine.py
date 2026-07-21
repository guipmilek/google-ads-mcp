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

"""Generic, guarded CRUD tools backed by ``GoogleAdsService.Mutate``.

Read operations remain available through the existing ``search`` tool. This
module supplies create, update, remove, schema discovery, and atomic mixed
batch operations for every CRUD resource exposed by the active API version.
"""

from __future__ import annotations

import copy
from typing import Any, Dict, List

from fastmcp.exceptions import ToolError
from google.ads.googleads.errors import GoogleAdsException
from google.protobuf.json_format import ParseDict, ParseError

import ads_mcp.utils as utils
from ads_mcp.mcp_header_interceptor import MCPHeaderInterceptor
from ads_mcp.mutation_safety import (
    _ACTIONS,
    _apply_create_status_guard,
    _max_operations,
    _normalize_customer_id,
    _normalize_update_mask,
    _operation_hash,
    _required_confirmation_verb,
    _validate_budget_limit,
    _validate_live_execution,
    _validate_resource_name,
)
from ads_mcp.mutation_schema import (
    _resolve_operation,
    _resource_descriptor,
    get_mutation_schema,
    list_mutable_resources,
)


def _prepare_operation(
    client: Any, customer_id: str, raw: Dict[str, Any]
) -> tuple[Any, Dict[str, Any]]:
    if not isinstance(raw, dict):
        raise ToolError("Each operation must be a JSON object.")

    action = str(raw.get("action", "")).lower().strip()
    if action not in _ACTIONS:
        raise ToolError("operation.action must be create, update, or remove.")

    resource = str(raw.get("resource", "")).strip()
    if not resource:
        raise ToolError("operation.resource is required.")

    wrapper_field, operation, resource_name = _resolve_operation(
        client, resource
    )
    if action not in operation._pb.DESCRIPTOR.fields_by_name:
        raise ToolError(
            f"{resource_name} does not support the '{action}' action."
        )

    prepared: Dict[str, Any] = {"action": action, "resource": resource_name}
    if action == "remove":
        target_name = str(raw.get("resource_name", "")).strip()
        if not target_name:
            raise ToolError("resource_name is required for remove operations.")
        _validate_resource_name(customer_id, target_name)
        operation._pb.remove = target_name
        prepared["resource_name"] = target_name
    else:
        data = raw.get("data")
        if not isinstance(data, dict):
            raise ToolError("data must be a JSON object for create and update.")

        descriptor = _resource_descriptor(operation, action)
        if descriptor is None:
            raise ToolError(
                f"Unable to resolve the mutable message for {resource_name}."
            )
        prepared_data = copy.deepcopy(data)
        if action == "create":
            prepared_data = _apply_create_status_guard(
                prepared_data, descriptor
            )
        else:
            target_name = str(prepared_data.get("resource_name", "")).strip()
            if not target_name:
                raise ToolError(
                    "data.resource_name is required for update operations."
                )
            _validate_resource_name(customer_id, target_name)

        if "resource_name" in prepared_data:
            _validate_resource_name(
                customer_id, str(prepared_data["resource_name"])
            )
        _validate_budget_limit(resource_name, prepared_data)

        mutable_resource = client.get_type(descriptor.name)
        try:
            ParseDict(
                prepared_data,
                mutable_resource._pb,
                ignore_unknown_fields=False,
            )
        except (ParseError, TypeError, ValueError) as exc:
            raise ToolError(
                f"Invalid {resource_name} data: {exc}. "
                "Use get_mutation_schema before retrying."
            ) from exc

        getattr(operation._pb, action).CopyFrom(mutable_resource._pb)
        prepared["data"] = prepared_data
        if action == "update":
            raw_mask = raw.get("update_mask")
            if not isinstance(raw_mask, list):
                raise ToolError(
                    "update_mask must be a list of field paths for updates."
                )
            update_mask = _normalize_update_mask(raw_mask, wrapper_field.name)
            operation._pb.update_mask.paths.extend(update_mask)
            prepared["update_mask"] = update_mask

    mutate_operation = client.get_type("MutateOperation")
    getattr(mutate_operation._pb, wrapper_field.name).CopyFrom(operation._pb)
    return mutate_operation, prepared


def _format_google_ads_exception(ex: GoogleAdsException) -> ToolError:
    messages: list[str] = []
    for error in ex.failure.errors:
        location = ""
        if error.location and error.location.field_path_elements:
            path = ".".join(
                element.field_name
                for element in error.location.field_path_elements
            )
            location = f" [{path}]"
        messages.append(f"Google Ads API Error{location}: {error.message}")
    return ToolError(f"Request ID: {ex.request_id}\n" + "\n".join(messages))


def _run_mutations(
    customer_id: str,
    operations: List[Dict[str, Any]],
    *,
    validate_only: bool,
    partial_failure: bool,
    confirmation: str | None,
) -> Dict[str, Any]:
    normalized_customer_id = _normalize_customer_id(customer_id)
    if not operations:
        raise ToolError("At least one operation is required.")
    maximum = _max_operations()
    if len(operations) > maximum:
        raise ToolError(
            f"This request contains {len(operations)} operations; the configured "
            f"maximum is {maximum}."
        )

    client = utils.get_googleads_client()
    mutate_operations: list[Any] = []
    prepared_operations: list[Dict[str, Any]] = []
    for operation in operations:
        mutate_operation, prepared = _prepare_operation(
            client, normalized_customer_id, operation
        )
        mutate_operations.append(mutate_operation)
        prepared_operations.append(prepared)

    request_hash = _operation_hash(
        normalized_customer_id,
        prepared_operations,
        partial_failure,
    )
    required_confirmation = (
        f"{_required_confirmation_verb(prepared_operations)} {request_hash}"
    )
    if not validate_only:
        _validate_live_execution(
            normalized_customer_id,
            prepared_operations,
            request_hash,
            confirmation,
        )

    service = client.get_service(
        "GoogleAdsService", interceptors=[MCPHeaderInterceptor()]
    )
    try:
        response = service.mutate(
            customer_id=normalized_customer_id,
            mutate_operations=mutate_operations,
            partial_failure=partial_failure,
            validate_only=validate_only,
            response_content_type=(
                client.enums.ResponseContentTypeEnum.MUTABLE_RESOURCE
            ),
        )
    except GoogleAdsException as ex:
        raise _format_google_ads_exception(ex) from ex

    return {
        "customer_id": normalized_customer_id,
        "validated": validate_only,
        "executed": not validate_only,
        "operation_count": len(prepared_operations),
        "operation_hash": request_hash,
        "required_confirmation": required_confirmation,
        "operations": prepared_operations,
        "response": utils.format_output_value(response),
    }


def create_resource(
    customer_id: str,
    resource: str,
    data: Dict[str, Any],
    validate_only: bool = True,
    confirmation: str | None = None,
) -> Dict[str, Any]:
    """Creates one resource after validation and explicit confirmation.

    First call with ``validate_only=true``. Status-bearing resources are forced
    to PAUSED. Repeat the exact payload with ``validate_only=false`` and the
    returned confirmation only after the user approves it.
    """
    return _run_mutations(
        customer_id,
        [{"action": "create", "resource": resource, "data": data}],
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
    """Updates one resource with an explicit field mask and confirmation."""
    return _run_mutations(
        customer_id,
        [
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
    """Removes one resource when deletion is enabled and confirmed."""
    return _run_mutations(
        customer_id,
        [
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
    """Runs an atomic mixed-resource mutation after validation.

    Each item uses ``action`` and ``resource``. Creates and updates use ``data``;
    updates also use ``update_mask``; removes use ``resource_name``. Negative
    temporary IDs may link resources created in the same request.
    """
    return _run_mutations(
        customer_id,
        operations,
        validate_only=validate_only,
        partial_failure=partial_failure,
        confirmation=confirmation,
    )
