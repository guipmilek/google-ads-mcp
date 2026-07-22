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

"""Tests for mutation field behavior enforcement."""

import unittest
from unittest.mock import patch

from fastmcp.exceptions import ToolError

from ads_mcp import mutation_schema


class FakeField:
    def __init__(self, name, behaviors=(), message_type=None, repeated=False):
        self.name = name
        self.behaviors = behaviors
        self.message_type = message_type
        self.is_repeated = repeated


class FakeDescriptor:
    def __init__(self, fields):
        self.fields_by_name = {field.name: field for field in fields}


class TestMutationSchema(unittest.TestCase):
    def _permissions(self, field):
        behaviors = list(field.behaviors)
        output_only = "OUTPUT_ONLY" in behaviors
        immutable = "IMMUTABLE" in behaviors
        return {
            "field_behaviors": behaviors,
            "required": "REQUIRED" in behaviors,
            "output_only": output_only,
            "immutable": immutable,
            "writable_on_create": not output_only,
            "writable_on_update": (
                not output_only
                and not immutable
                and field.name != "resource_name"
            ),
            "identifier_for_update": field.name == "resource_name",
        }

    def test_type_alias_is_normalized_recursively(self):
        result = mutation_schema._normalize_mutation_input(
            {"type_": "STANDARD", "nested": [{"type_": "SEARCH"}]}
        )
        self.assertEqual(
            result,
            {"type": "STANDARD", "nested": [{"type": "SEARCH"}]},
        )

    def test_duplicate_type_alias_is_rejected(self):
        with self.assertRaisesRegex(ToolError, "Duplicate mutation field"):
            mutation_schema._normalize_mutation_input(
                {"type": "STANDARD", "type_": "SEARCH"}
            )

    def test_output_only_field_is_rejected(self):
        descriptor = FakeDescriptor(
            [FakeField("id", behaviors=("OUTPUT_ONLY",))]
        )
        with (
            patch(
                "ads_mcp.mutation_schema._field_permissions",
                side_effect=self._permissions,
            ),
            self.assertRaisesRegex(ToolError, "output-only"),
        ):
            mutation_schema._validate_mutation_data(
                descriptor, {"id": "123"}, "create"
            )

    def test_immutable_field_is_rejected_on_update(self):
        descriptor = FakeDescriptor(
            [
                FakeField("resource_name", behaviors=("IMMUTABLE",)),
                FakeField("campaign", behaviors=("IMMUTABLE",)),
            ]
        )
        with (
            patch(
                "ads_mcp.mutation_schema._field_permissions",
                side_effect=self._permissions,
            ),
            self.assertRaisesRegex(ToolError, "immutable"),
        ):
            mutation_schema._validate_mutation_data(
                descriptor,
                {"resource_name": "customers/1/items/2", "campaign": "x"},
                "update",
            )

    def test_resource_name_is_allowed_as_update_identifier(self):
        descriptor = FakeDescriptor(
            [
                FakeField("resource_name", behaviors=("IMMUTABLE",)),
                FakeField("name"),
            ]
        )
        with patch(
            "ads_mcp.mutation_schema._field_permissions",
            side_effect=self._permissions,
        ):
            mutation_schema._validate_mutation_data(
                descriptor,
                {"resource_name": "customers/1/items/2", "name": "New"},
                "update",
            )

    def test_update_mask_rejects_output_only_field(self):
        descriptor = FakeDescriptor(
            [FakeField("id", behaviors=("OUTPUT_ONLY",))]
        )
        with (
            patch(
                "ads_mcp.mutation_schema._field_permissions",
                side_effect=self._permissions,
            ),
            self.assertRaisesRegex(ToolError, "output-only"),
        ):
            mutation_schema._validate_update_mask_paths(descriptor, ["id"])

    def test_update_mask_rejects_immutable_field(self):
        descriptor = FakeDescriptor(
            [FakeField("campaign", behaviors=("IMMUTABLE",))]
        )
        with (
            patch(
                "ads_mcp.mutation_schema._field_permissions",
                side_effect=self._permissions,
            ),
            self.assertRaisesRegex(ToolError, "immutable"),
        ):
            mutation_schema._validate_update_mask_paths(
                descriptor, ["campaign"]
            )

    def test_update_mask_rejects_unknown_field(self):
        descriptor = FakeDescriptor([FakeField("name")])
        with self.assertRaisesRegex(ToolError, "Unknown"):
            mutation_schema._validate_update_mask_paths(descriptor, ["missing"])


if __name__ == "__main__":
    unittest.main()
