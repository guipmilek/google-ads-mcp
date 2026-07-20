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

"""Tests for guarded Google Ads mutation helpers."""

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastmcp.exceptions import ToolError

from ads_mcp import mutation_engine


class TestMutationEngine(unittest.TestCase):
    def test_normalize_customer_id(self):
        self.assertEqual(
            mutation_engine._normalize_customer_id("844-827-5903"),
            "8448275903",
        )

    def test_normalize_customer_id_rejects_invalid_length(self):
        with self.assertRaises(ToolError):
            mutation_engine._normalize_customer_id("123")

    def test_operation_hash_is_deterministic(self):
        operations = [
            {
                "action": "update",
                "resource": "Campaign",
                "data": {"status": "PAUSED"},
                "update_mask": ["status"],
            }
        ]
        first = mutation_engine._operation_hash(
            "8448275903", operations, False
        )
        second = mutation_engine._operation_hash(
            "8448275903", operations, False
        )
        self.assertEqual(first, second)
        self.assertEqual(len(first), 16)

    def test_create_status_defaults_to_paused(self):
        status_field = SimpleNamespace(
            enum_type=SimpleNamespace(
                values=[
                    SimpleNamespace(name="UNSPECIFIED"),
                    SimpleNamespace(name="ENABLED"),
                    SimpleNamespace(name="PAUSED"),
                ]
            )
        )
        descriptor = SimpleNamespace(fields_by_name={"status": status_field})
        result = mutation_engine._apply_create_status_guard(
            {"name": "Campaign"}, descriptor
        )
        self.assertEqual(result["status"], "PAUSED")

    def test_create_status_rejects_enabled(self):
        status_field = SimpleNamespace(
            enum_type=SimpleNamespace(
                values=[
                    SimpleNamespace(name="ENABLED"),
                    SimpleNamespace(name="PAUSED"),
                ]
            )
        )
        descriptor = SimpleNamespace(fields_by_name={"status": status_field})
        with self.assertRaises(ToolError):
            mutation_engine._apply_create_status_guard(
                {"status": "ENABLED"}, descriptor
            )

    def test_update_mask_accepts_qualified_paths(self):
        result = mutation_engine._normalize_update_mask(
            ["campaign.status", "name", "name"],
            "campaign_operation",
        )
        self.assertEqual(result, ["name", "status"])

    def test_budget_limit(self):
        with patch.dict(
            os.environ,
            {"GOOGLE_ADS_MAX_DAILY_BUDGET_MICROS": "100000000"},
            clear=True,
        ):
            mutation_engine._validate_budget_limit(
                "CampaignBudget", {"amount_micros": 100000000}
            )
            with self.assertRaises(ToolError):
                mutation_engine._validate_budget_limit(
                    "CampaignBudget", {"amount_micros": 100000001}
                )

    def test_live_execution_requires_allowlist_and_exact_confirmation(self):
        operations = [
            {
                "action": "update",
                "resource": "Campaign",
                "data": {"status": "PAUSED"},
                "update_mask": ["status"],
            }
        ]
        request_hash = mutation_engine._operation_hash(
            "8448275903", operations, False
        )
        with patch.dict(
            os.environ,
            {
                "GOOGLE_ADS_MUTATIONS_ENABLED": "true",
                "GOOGLE_ADS_ALLOWED_CUSTOMER_IDS": "8448275903",
            },
            clear=True,
        ):
            expected = f"EXECUTE {request_hash}"
            self.assertEqual(
                mutation_engine._validate_live_execution(
                    "8448275903",
                    operations,
                    request_hash,
                    expected,
                ),
                expected,
            )
            with self.assertRaises(ToolError):
                mutation_engine._validate_live_execution(
                    "8448275903",
                    operations,
                    request_hash,
                    "EXECUTE wrong",
                )

    def test_enable_and_remove_have_stronger_confirmations(self):
        enable = [
            {
                "action": "update",
                "resource": "Campaign",
                "data": {"status": "ENABLED"},
            }
        ]
        remove = [
            {
                "action": "remove",
                "resource": "Campaign",
                "resource_name": "customers/8448275903/campaigns/1",
            }
        ]
        self.assertEqual(
            mutation_engine._required_confirmation_verb(enable), "ENABLE"
        )
        self.assertEqual(
            mutation_engine._required_confirmation_verb(remove), "REMOVE"
        )

    def test_remove_is_disabled_by_default(self):
        operations = [
            {
                "action": "remove",
                "resource": "Campaign",
                "resource_name": "customers/8448275903/campaigns/1",
            }
        ]
        request_hash = mutation_engine._operation_hash(
            "8448275903", operations, False
        )
        with patch.dict(
            os.environ,
            {
                "GOOGLE_ADS_MUTATIONS_ENABLED": "true",
                "GOOGLE_ADS_ALLOWED_CUSTOMER_IDS": "8448275903",
            },
            clear=True,
        ):
            with self.assertRaises(ToolError):
                mutation_engine._validate_live_execution(
                    "8448275903",
                    operations,
                    request_hash,
                    f"REMOVE {request_hash}",
                )


if __name__ == "__main__":
    unittest.main()
