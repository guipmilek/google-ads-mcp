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

"""Runtime discovery for mutable Google Ads resources and protobuf fields."""

from __future__ import annotations

from typing import Any, Dict, List

from fastmcp.exceptions import ToolError
from google.protobuf.descriptor import FieldDescriptor

import ads_mcp.utils as utils
from ads_mcp.mutation_safety import (
    _ACTIONS,
    _SENSITIVE_RESOURCES,
    _canonical_resource_key,
)


def _resolve_operation(client: Any, resource: str) -> tuple[Any, Any, str]:
    """Returns the MutateOperation field, operation message, and resource type."""
    requested = _canonical_resource_key(resource)
    mutate_operation = client.get_type("MutateOperation")

    matches: list[tuple[Any, str]] = []
    for field in mutate_operation._pb.DESCRIPTOR.fields:
        if field.message_type is None:
            continue
        if not any(
            action in field.message_type.fields_by_name for action in _ACTIONS
        ):
            continue
        operation_name = field.message_type.name
        resource_name = operation_name.removesuffix("Operation")
        aliases = {
            _canonical_resource_key(field.name.removesuffix("_operation")),
            _canonical_resource_key(operation_name),
            _canonical_resource_key(resource_name),
        }
        if requested in aliases:
            matches.append((field, resource_name))

    if not matches:
        raise ToolError(
            f"Resource '{resource}' is not exposed by GoogleAdsService.Mutate. "
            "Use list_mutable_resources to discover supported resources."
        )
    if len(matches) > 1:
        names = ", ".join(sorted(match[1] for match in matches))
        raise ToolError(
            f"Resource '{resource}' is ambiguous. Use one of: {names}."
        )

    field, resource_name = matches[0]
    return field, client.get_type(field.message_type.name), resource_name


def _resource_descriptor(operation: Any, action: str) -> Any | None:
    field = operation._pb.DESCRIPTOR.fields_by_name.get(action)
    if field is None or field.message_type is None:
        return None
    return field.message_type


_SCALAR_FIELD_TYPES = {
    FieldDescriptor.TYPE_DOUBLE: "double",
    FieldDescriptor.TYPE_FLOAT: "float",
    FieldDescriptor.TYPE_INT64: "int64",
    FieldDescriptor.TYPE_UINT64: "uint64",
    FieldDescriptor.TYPE_INT32: "int32",
    FieldDescriptor.TYPE_FIXED64: "fixed64",
    FieldDescriptor.TYPE_FIXED32: "fixed32",
    FieldDescriptor.TYPE_BOOL: "bool",
    FieldDescriptor.TYPE_STRING: "string",
    FieldDescriptor.TYPE_GROUP: "group",
    FieldDescriptor.TYPE_BYTES: "bytes",
    FieldDescriptor.TYPE_UINT32: "uint32",
    FieldDescriptor.TYPE_SFIXED32: "sfixed32",
    FieldDescriptor.TYPE_SFIXED64: "sfixed64",
    FieldDescriptor.TYPE_SINT32: "sint32",
    FieldDescriptor.TYPE_SINT64: "sint64",
}


def _field_type(field: FieldDescriptor) -> str:
    if field.message_type is not None:
        if field.message_type.GetOptions().map_entry:
            return "map"
        return field.message_type.name
    if field.enum_type is not None:
        return field.enum_type.name
    return _SCALAR_FIELD_TYPES.get(field.type, f"protobuf_type_{field.type}")


def _describe_message(
    descriptor: Any,
    depth: int,
    seen: set[str] | None = None,
) -> List[Dict[str, Any]]:
    seen = set(seen or set())
    if descriptor.full_name in seen:
        return []
    seen.add(descriptor.full_name)

    output: list[Dict[str, Any]] = []
    for field in descriptor.fields:
        item: Dict[str, Any] = {
            "name": field.name,
            "type": _field_type(field),
            "repeated": field.is_repeated,
        }
        if field.containing_oneof is not None:
            item["oneof"] = field.containing_oneof.name
        if field.enum_type is not None:
            item["enum_values"] = [
                value.name for value in field.enum_type.values
            ]
        if (
            depth > 0
            and field.message_type is not None
            and not field.message_type.GetOptions().map_entry
        ):
            nested = _describe_message(field.message_type, depth - 1, seen)
            if nested:
                item["fields"] = nested
        output.append(item)
    return output


def list_mutable_resources() -> List[Dict[str, Any]]:
    """Lists resources and CRUD actions supported by GoogleAdsService.Mutate."""
    client = utils.get_googleads_client()
    mutate_operation = client.get_type("MutateOperation")
    output: list[Dict[str, Any]] = []
    for field in mutate_operation._pb.DESCRIPTOR.fields:
        if field.message_type is None:
            continue
        actions = [
            action
            for action in _ACTIONS
            if action in field.message_type.fields_by_name
        ]
        if not actions:
            continue
        resource_name = field.message_type.name.removesuffix("Operation")
        output.append(
            {
                "resource": resource_name,
                "operation_field": field.name,
                "actions": actions,
                "sensitive": resource_name in _SENSITIVE_RESOURCES,
            }
        )
    return sorted(output, key=lambda item: item["resource"])


def get_mutation_schema(
    resource: str,
    max_depth: int = 1,
) -> Dict[str, Any]:
    """Returns writable protobuf fields for a mutable Google Ads resource."""
    if max_depth < 0 or max_depth > 3:
        raise ToolError("max_depth must be between 0 and 3.")

    client = utils.get_googleads_client()
    wrapper_field, operation, resource_name = _resolve_operation(client, resource)
    operation_descriptor = operation._pb.DESCRIPTOR
    actions = [
        action
        for action in _ACTIONS
        if action in operation_descriptor.fields_by_name
    ]
    create_descriptor = _resource_descriptor(operation, "create")
    update_descriptor = _resource_descriptor(operation, "update")
    resource_descriptor = create_descriptor or update_descriptor

    return {
        "resource": resource_name,
        "operation_field": wrapper_field.name,
        "operation_type": operation_descriptor.name,
        "actions": actions,
        "sensitive": resource_name in _SENSITIVE_RESOURCES,
        "fields": (
            _describe_message(resource_descriptor, max_depth)
            if resource_descriptor is not None
            else []
        ),
        "update_mask_note": (
            "Update masks use resource-relative paths such as 'status' or "
            "'network_settings.target_search_network'."
        ),
    }
