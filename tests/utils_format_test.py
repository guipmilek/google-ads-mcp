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

"""Tests for stable MCP response serialization."""

from enum import Enum
import unittest

from ads_mcp import utils


class ExampleEnum(Enum):
    ENABLED = 2


class TestFormatOutputValue(unittest.TestCase):
    def test_normalizes_nested_micros_and_reserved_type_field(self):
        value = {
            "campaign_budget": {
                "resource_name": "customers/1/campaignBudgets/2",
                "id": "2",
                "amount_micros": "20000000",
                "type_": "STANDARD",
                "delivery_method": "STANDARD",
            }
        }

        result = utils.format_output_value(value)

        self.assertEqual(result["campaign_budget"]["amount_micros"], 20000000)
        self.assertEqual(result["campaign_budget"]["id"], "2")
        self.assertEqual(result["campaign_budget"]["type"], "STANDARD")
        self.assertNotIn("type_", result["campaign_budget"])

    def test_mapping_is_not_serialized_as_a_list_of_keys(self):
        self.assertEqual(
            utils.format_output_value({"status": "ENABLED"}),
            {"status": "ENABLED"},
        )

    def test_enum_uses_name(self):
        self.assertEqual(
            utils.format_output_value(ExampleEnum.ENABLED), "ENABLED"
        )

    def test_nested_enum_uses_name(self):
        self.assertEqual(
            utils.format_output_value({"status": ExampleEnum.ENABLED}),
            {"status": "ENABLED"},
        )

    def test_set_serialization_is_deterministic(self):
        self.assertEqual(utils.format_output_value({3, 1, 2}), [1, 2, 3])


if __name__ == "__main__":
    unittest.main()
