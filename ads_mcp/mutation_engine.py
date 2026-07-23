# Copyright 2026 Google LLC.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Generic direct CRUD tools backed by ``GoogleAdsService.Mutate``.

Read operations remain available through the existing ``search`` tool. This
module supplies create, update, remove, schema discovery, and mixed batch
operations for every CRUD resource exposed by the active API version.
"""

from __future__ import annotations

from collections import Counter
import copy
import json
from typing import Any, Dict, List

from fastmcp.exceptions import ToolError
from google.ads.googleads.errors import GoogleAdsException
from google.protobuf.json_format import ParseDict, ParseError

import ads_mcp.utils as utils
from ads_mcp.mcp_header_interceptor import MCPHeaderInterceptor
from ads_mcp.mutation_safety import (
    _ACTIONS,
    _OPERATION_HASH_VERSION,
    _apply_create_status_guard,
    _contains_enabled_status,
    _contains_temporary_resource_id,
    _max_operations,
    _normalize_update_mask,
    _operation_hash,
    _validate_budget_limit,
    _validate_customer_scope,
    _validate_resource_name,
    _validate_resource_references,
    CRUD_CONTRACT_VERSION,
)
from ads_mcp.mutation_schema import (
    _resolve_operation,
    _normalize_mutation_input,
    _resource_descriptor,
    _validate_mutation_data,
    _validate_update_mask_paths,
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
        prepared_data = _normalize_mutation_input(copy.deepcopy(data))
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
        _validate_resource_references(customer_id, prepared_data)
        _validate_budget_limit(resource_name, prepared_data)

        _validate_mutation_data(descriptor, prepared_data, action)

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
            _validate_update_mask_paths(descriptor, update_mask)
            operation._pb.update_mask.paths.extend(update_mask)
            prepared["update_mask"] = update_mask

    mutate_operation = client.get_type("MutateOperation")
    getattr(mutate_operation._pb, wrapper_field.name).CopyFrom(operation._pb)
    return mutate_operation, prepared


def _error_code_name(error_code: Any) -> str:
    formatted = utils.format_output_value(error_code)
    if isinstance(formatted, dict):
        for category, value in formatted.items():
            if value not in (None, "", "UNSPECIFIED", 0):
                return f"{category}.{value}"
    return str(formatted)


def _format_field_path_element(element: Any) -> str:
    name = str(getattr(element, "field_name", ""))
    protobuf_value = getattr(element, "_pb", element)
    has_index = False
    try:
        has_index = protobuf_value.HasField("index")
    except (AttributeError, ValueError):
        index = getattr(element, "index", None)
        has_index = index not in (None, 0)
    if has_index:
        return f"{name}[{getattr(element, 'index', 0)}]"
    return name


def _format_google_ads_exception(
    ex: GoogleAdsException,
    operation_hash: str,
    validate_only: bool,
) -> ToolError:
    errors: list[dict[str, Any]] = []
    for error in ex.failure.errors:
        field_path: list[str] = []
        if error.location and error.location.field_path_elements:
            field_path = [
                _format_field_path_element(element)
                for element in error.location.field_path_elements
            ]
        errors.append(
            {
                "code": _error_code_name(error.error_code),
                "message": error.message,
                "field_path": field_path,
            }
        )
    payload = {
        "error_type": "GOOGLE_ADS_API_ERROR",
        "request_id": ex.request_id,
        "operation_hash": operation_hash,
        "mode": "VALIDATE_ONLY" if validate_only else "EXECUTE",
        "execution_state": "NOT_EXECUTED" if validate_only else "FAILED",
        "execution_may_have_completed": False,
        "revalidation_required": True,
        "errors": errors,
    }
    return ToolError(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _format_internal_mutation_error(
    exc: Exception,
    operation_hash: str,
    validate_only: bool,
) -> ToolError:
    message = str(exc)
    if len(message) > 1000:
        message = message[:1000] + "…"
    payload = {
        "error_type": "CONNECTOR_INTERNAL_ERROR",
        "exception_class": type(exc).__name__,
        "operation_hash": operation_hash,
        "mode": "VALIDATE_ONLY" if validate_only else "EXECUTE",
        "message": message,
        "execution_state": "NOT_EXECUTED" if validate_only else "UNKNOWN",
        "execution_may_have_completed": not validate_only,
        "automatic_retry_safe": validate_only,
        "revalidation_required": True,
    }
    return ToolError(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _build_mutate_request(
    client: Any,
    customer_id: str,
    mutate_operations: List[Any],
    *,
    partial_failure: bool,
    validate_only: bool,
) -> Any:
    """Builds the request object required by the generated API client."""
    request = client.get_type("MutateGoogleAdsRequest")
    request.customer_id = customer_id
    request.mutate_operations.extend(mutate_operations)
    request.partial_failure = partial_failure
    request.validate_only = validate_only
    request.response_content_type = (
        client.enums.ResponseContentTypeEnum.MUTABLE_RESOURCE
    )
    return request


def _build_operation_scope(
    prepared_operations: List[Dict[str, Any]],
) -> Dict[str, Any]:
    action_counts = Counter(
        operation["action"] for operation in prepared_operations
    )
    resource_counts = Counter(
        operation["resource"] for operation in prepared_operations
    )
    requested_resource_names = sorted(
        {
            operation["resource_name"]
            for operation in prepared_operations
            if operation.get("resource_name")
        }
        | {
            str(operation["data"]["resource_name"])
            for operation in prepared_operations
            if isinstance(operation.get("data"), dict)
            and operation["data"].get("resource_name")
        }
    )
    return {
        "actions": dict(sorted(action_counts.items())),
        "resources": dict(sorted(resource_counts.items())),
        "requested_resource_names": requested_resource_names,
        "contains_remove": action_counts.get("remove", 0) > 0,
        "contains_enable": _contains_enabled_status(prepared_operations),
    }


def _collect_resource_names(value: Any) -> List[str]:
    names: set[str] = set()

    def walk(current: Any) -> None:
        if isinstance(current, dict):
            for key, nested in current.items():
                if key == "resource_name" and isinstance(nested, str):
                    names.add(nested)
                else:
                    walk(nested)
        elif isinstance(current, list):
            for nested in current:
                walk(nested)

    walk(value)
    return sorted(names)


def _partial_failure_error(response_payload: Any) -> Dict[str, Any] | None:
    if not isinstance(response_payload, dict):
        return None
    error = response_payload.get("partial_failure_error")
    if not isinstance(error, dict) or not error:
        return None
    if not any(
        error.get(field) not in (None, "", 0, [], {})
        for field in ("code", "message", "details")
    ):
        return None
    return error


def _run_mutations(
    customer_id: str,
    operations: List[Dict[str, Any]],
    *,
    dry_run: bool,
    partial_failure: bool,
) -> Dict[str, Any]:
    """Validate natively and execute in one direct connector call."""

    normalized_customer_id = _validate_customer_scope(customer_id)
    if not operations:
        raise ToolError("At least one operation is required.")
    maximum = _max_operations()
    if len(operations) > maximum:
        raise ToolError(
            f"This request contains {len(operations)} operations; the "
            f"configured maximum is {maximum}."
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
    if partial_failure and _contains_temporary_resource_id(prepared_operations):
        raise ToolError(
            "partial_failure=true cannot be used with temporary negative "
            "resource IDs or dependent operations."
        )

    service = client.get_service(
        "GoogleAdsService", interceptors=[MCPHeaderInterceptor()]
    )

    validation_request = _build_mutate_request(
        client,
        normalized_customer_id,
        mutate_operations,
        partial_failure=partial_failure,
        validate_only=True,
    )
    try:
        validation_response = service.mutate(request=validation_request)
    except GoogleAdsException as exc:
        raise _format_google_ads_exception(exc, request_hash, True) from exc
    except Exception as exc:
        raise _format_internal_mutation_error(exc, request_hash, True) from exc

    validation_payload = utils.format_output_value(validation_response)
    validation_partial_error = _partial_failure_error(validation_payload)
    base = {
        "contract_version": CRUD_CONTRACT_VERSION,
        "customer_id": normalized_customer_id,
        "operation_count": len(prepared_operations),
        "operation_hash": request_hash,
        "operation_hash_version": _OPERATION_HASH_VERSION,
        "operations": prepared_operations,
        "operation_scope": _build_operation_scope(prepared_operations),
        "native_validation": {
            "method": "GoogleAdsService.Mutate",
            "validate_only": True,
            "completed": True,
            "partial_failure_error": validation_partial_error,
        },
    }

    if dry_run:
        return {
            **base,
            "mode": "DRY_RUN",
            "validated": validation_partial_error is None,
            "validated_in_current_call": True,
            "validation_status": (
                "PASSED"
                if validation_partial_error is None
                else "PASSED_WITH_PARTIAL_FAILURES"
            ),
            "execution_attempted": False,
            "executed": False,
            "execution_status": "NOT_EXECUTED",
            "api_call": {
                "method": "GoogleAdsService.Mutate",
                "validate_only": True,
                "partial_failure": partial_failure,
                "completed": True,
            },
            "response": validation_payload,
            "verification": {
                "native_validate_only_completed": True,
                "google_ads_mutation_sent": False,
                "post_mutation_read_performed": False,
            },
        }

    execution_request = _build_mutate_request(
        client,
        normalized_customer_id,
        mutate_operations,
        partial_failure=partial_failure,
        validate_only=False,
    )
    try:
        response = service.mutate(request=execution_request)
    except GoogleAdsException as exc:
        raise _format_google_ads_exception(exc, request_hash, False) from exc
    except Exception as exc:
        raise _format_internal_mutation_error(exc, request_hash, False) from exc

    response_payload = utils.format_output_value(response)
    partial_error = _partial_failure_error(response_payload)
    response_resource_names = _collect_resource_names(response_payload)
    return {
        **base,
        "mode": "EXECUTE",
        "validated": True,
        "validated_in_current_call": True,
        "validation_status": (
            "PASSED"
            if validation_partial_error is None
            else "PASSED_WITH_PARTIAL_FAILURES"
        ),
        "execution_attempted": True,
        "executed": None if partial_error else True,
        "execution_status": "PARTIAL_FAILURE" if partial_error else "SUCCEEDED",
        "api_call": {
            "method": "GoogleAdsService.Mutate",
            "validate_only": False,
            "partial_failure": partial_failure,
            "completed": True,
        },
        "partial_failure_error": partial_error,
        "response_resource_names": response_resource_names,
        "verification": {
            "native_validate_only_completed": True,
            "mutate_response_received": True,
            "post_mutation_read_performed": False,
            "claims_limited_to_mutate_response": True,
            "partial_success_not_inferred_from_response_count": True,
        },
        "response": response_payload,
    }


def create_resource(
    customer_id: str,
    resource: str,
    data: Dict[str, Any],
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Create one Google Ads resource directly."""

    return _run_mutations(
        customer_id,
        [{"action": "create", "resource": resource, "data": data}],
        dry_run=dry_run,
        partial_failure=False,
    )


def update_resource(
    customer_id: str,
    resource: str,
    data: Dict[str, Any],
    update_mask: List[str],
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Update one Google Ads resource directly."""

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
        dry_run=dry_run,
        partial_failure=False,
    )


def remove_resource(
    customer_id: str,
    resource: str,
    resource_name: str,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Remove one Google Ads resource directly."""

    return _run_mutations(
        customer_id,
        [
            {
                "action": "remove",
                "resource": resource,
                "resource_name": resource_name,
            }
        ],
        dry_run=dry_run,
        partial_failure=False,
    )


def batch_mutate(
    customer_id: str,
    operations: List[Dict[str, Any]],
    dry_run: bool = False,
    partial_failure: bool = False,
) -> Dict[str, Any]:
    """Run a direct mixed-resource Google Ads mutation."""

    return _run_mutations(
        customer_id,
        operations,
        dry_run=dry_run,
        partial_failure=partial_failure,
    )
