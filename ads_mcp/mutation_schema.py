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

try:
    from google.api import field_behavior_pb2
except ImportError:  # pragma: no cover - google-ads installs this dependency.
    field_behavior_pb2 = None

import ads_mcp.utils as utils
from ads_mcp.mutation_safety import (
    _ACTIONS,
    _SENSITIVE_RESOURCES,
    _canonical_resource_key,
)


def _resolve_operation(client: Any, resource: str) -> tuple[Any, Any, str]:
    """Returns the operation field, operation message, and resource type."""
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


def _field_behaviors(field: FieldDescriptor) -> List[str]:
    if field_behavior_pb2 is None:
        return []
    try:
        values = field.GetOptions().Extensions[
            field_behavior_pb2.field_behavior
        ]
        return [
            field_behavior_pb2.FieldBehavior.Name(value)
            for value in values
        ]
    except (KeyError, TypeError, ValueError):
        return []


def _field_permissions(field: FieldDescriptor) -> Dict[str, Any]:
    behaviors = _field_behaviors(field)
    output_only = "OUTPUT_ONLY" in behaviors
    immutable = "IMMUTABLE" in behaviors
    is_resource_name = field.name == "resource_name"
    return {
        "field_behaviors": behaviors,
        "required": "REQUIRED" in behaviors,
        "output_only": output_only,
        "immutable": immutable,
        "writable_on_create": not output_only,
        "writable_on_update": (
            not output_only and not immutable and not is_resource_name
        ),
        "identifier_for_update": is_resource_name,
    }


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
            **_field_permissions(field),
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


def _input_field_name(value: str) -> str:
    return "type" if value == "type_" else value


def _normalize_mutation_input(value: Any, path: str = "data") -> Any:
    """Normalizes proto-plus aliases and rejects ambiguous duplicate keys."""
    if isinstance(value, dict):
        output: dict[str, Any] = {}
        for raw_key, nested in value.items():
            field_name = _input_field_name(str(raw_key))
            if field_name in output:
                raise ToolError(
                    f"Duplicate mutation field alias at '{path}.{field_name}'."
                )
            output[field_name] = _normalize_mutation_input(
                nested, f"{path}.{field_name}"
            )
        return output
    if isinstance(value, list):
        return [
            _normalize_mutation_input(item, f"{path}[{index}]")
            for index, item in enumerate(value)
        ]
    return value


def _validate_mutation_data(
    descriptor: Any,
    data: Dict[str, Any],
    action: str,
    path: str = "",
) -> None:
    """Rejects known output-only or immutable fields before the API call."""
    for raw_name, value in data.items():
        field_name = _input_field_name(str(raw_name))
        field = descriptor.fields_by_name.get(field_name)
        if field is None:
            # ParseDict returns the authoritative unknown-field error.
            continue

        field_path = f"{path}.{field_name}" if path else field_name
        permissions = _field_permissions(field)
        if permissions["output_only"]:
            raise ToolError(
                f"Field '{field_path}' is output-only and cannot be mutated."
            )
        if (
            action == "update"
            and permissions["immutable"]
            and field_path != "resource_name"
        ):
            raise ToolError(
                f"Field '{field_path}' is immutable and cannot be updated."
            )

        if field.message_type is None or value is None:
            continue
        if field.message_type.GetOptions().map_entry:
            continue
        if isinstance(value, dict):
            _validate_mutation_data(
                field.message_type, value, action, field_path
            )
        elif field.is_repeated and isinstance(value, list):
            for index, item in enumerate(value):
                if isinstance(item, dict):
                    _validate_mutation_data(
                        field.message_type,
                        item,
                        action,
                        f"{field_path}[{index}]",
                    )


def _validate_update_mask_paths(
    descriptor: Any, update_mask: List[str]
) -> None:
    """Rejects unknown, output-only, or immutable update-mask paths."""
    for path in update_mask:
        current_descriptor = descriptor
        segments = path.split(".")
        for index, segment in enumerate(segments):
            field = current_descriptor.fields_by_name.get(segment)
            if field is None:
                raise ToolError(
                    f"Unknown update_mask field path '{path}'."
                )
            permissions = _field_permissions(field)
            if permissions["output_only"]:
                raise ToolError(
                    f"update_mask field '{path}' is output-only."
                )
            if permissions["immutable"]:
                raise ToolError(
                    f"update_mask field '{path}' is immutable."
                )

            is_last = index == len(segments) - 1
            if is_last:
                continue
            if field.message_type is None:
                raise ToolError(
                    f"update_mask field '{path}' traverses a scalar field."
                )
            if field.message_type.GetOptions().map_entry:
                raise ToolError(
                    f"update_mask field '{path}' traverses a map field."
                )
            current_descriptor = field.message_type


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
    """Returns protobuf fields and mutability metadata for a resource."""
    if max_depth < 0 or max_depth > 3:
        raise ToolError("max_depth must be between 0 and 3.")

    client = utils.get_googleads_client()
    wrapper_field, operation, resource_name = _resolve_operation(
        client, resource
    )
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
        "field_behavior_note": (
            "Do not send output-only fields. Immutable fields may be supplied "
            "during create but not changed during update. resource_name is the "
            "update identifier and must not appear in update_mask."
        ),
        "update_mask_note": (
            "Update masks use resource-relative paths such as 'status' or "
            "'network_settings.target_search_network'."
        ),
    }
