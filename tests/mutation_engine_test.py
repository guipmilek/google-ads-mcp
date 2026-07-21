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
    def setUp(self):
        mutation_engine._clear_confirmation_replay_cache_for_tests()

    def _confirmation_env(self, **overrides):
        values = {
            "GOOGLE_ADS_CONFIRMATION_SECRET": "s" * 64,
            "GOOGLE_ADS_MUTATIONS_ENABLED": "true",
            "GOOGLE_ADS_ALLOWED_CUSTOMER_IDS": "8448275903",
        }
        values.update(overrides)
        return patch.dict(os.environ, values, clear=True)

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
        self.assertEqual(len(first), 32)

    def test_create_status_defaults_to_paused_when_supported(self):
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

    def test_create_status_is_untouched_without_paused(self):
        status_field = SimpleNamespace(
            enum_type=SimpleNamespace(
                values=[
                    SimpleNamespace(name="UNSPECIFIED"),
                    SimpleNamespace(name="ENABLED"),
                    SimpleNamespace(name="REMOVED"),
                ]
            )
        )
        descriptor = SimpleNamespace(fields_by_name={"status": status_field})
        result = mutation_engine._apply_create_status_guard(
            {"name": "Budget"}, descriptor
        )
        self.assertNotIn("status", result)

    def test_create_status_rejects_enabled_when_paused_is_supported(self):
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

    def test_update_mask_accepts_qualified_paths_and_type_alias(self):
        result = mutation_engine._normalize_update_mask(
            ["campaign.status", "type_", "name", "name"],
            "campaign_operation",
        )
        self.assertEqual(result, ["name", "status", "type"])

    def test_budget_limit_and_floor(self):
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
            with self.assertRaises(ToolError):
                mutation_engine._validate_budget_limit(
                    "CampaignBudget", {"amount_micros": 0}
                )

    def test_total_budget_requires_an_explicit_cap(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ToolError, "MAX_TOTAL"):
                mutation_engine._validate_budget_limit(
                    "CampaignBudget",
                    {"total_amount_micros": 500000000},
                )

        with patch.dict(
            os.environ,
            {"GOOGLE_ADS_MAX_TOTAL_BUDGET_MICROS": "500000000"},
            clear=True,
        ):
            mutation_engine._validate_budget_limit(
                "CampaignBudget",
                {"total_amount_micros": 500000000},
            )

    def test_nested_cross_customer_resource_reference_is_rejected(self):
        with self.assertRaisesRegex(ToolError, "another customer_id"):
            mutation_engine._validate_resource_references(
                "8448275903",
                {
                    "campaign_budget": (
                        "customers/1111111111/campaignBudgets/2"
                    )
                },
            )

    def test_same_customer_nested_resource_reference_is_allowed(self):
        mutation_engine._validate_resource_references(
            "8448275903",
            {
                "campaign_budget": (
                    "customers/8448275903/campaignBudgets/-1"
                )
            },
        )

    def test_confirmation_secret_and_ttl_are_validated(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ToolError, "CONFIRMATION_SECRET"):
                mutation_engine._validate_confirmation_configuration()

        with patch.dict(
            os.environ,
            {
                "GOOGLE_ADS_CONFIRMATION_SECRET": "s" * 64,
                "GOOGLE_ADS_CONFIRMATION_TTL_SECONDS": "3601",
            },
            clear=True,
        ):
            with self.assertRaisesRegex(ToolError, "cannot exceed"):
                mutation_engine._validate_confirmation_configuration()

    def test_signed_confirmation_survives_process_local_cache_reset(self):
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
        with self._confirmation_env(), patch(
            "ads_mcp.mutation_safety.secrets.token_urlsafe",
            return_value="receipt-token",
        ):
            receipt = mutation_engine._issue_validation_receipt(
                "8448275903", operations, request_hash, False
            )
            mutation_engine._clear_confirmation_replay_cache_for_tests()
            verified = mutation_engine._validate_live_execution(
                "8448275903",
                operations,
                request_hash,
                receipt["confirmation"],
                partial_failure=False,
            )
        self.assertTrue(verified["cross_instance_valid"])
        self.assertTrue(verified["registered_before_api_call"])

    def test_signed_confirmation_is_payload_bound_and_replay_checked(self):
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
        with self._confirmation_env():
            receipt = mutation_engine._issue_validation_receipt(
                "8448275903", operations, request_hash, False
            )
            mutation_engine._validate_live_execution(
                "8448275903",
                operations,
                request_hash,
                receipt["confirmation"],
                partial_failure=False,
            )
            with self.assertRaisesRegex(ToolError, "already used"):
                mutation_engine._validate_live_execution(
                    "8448275903",
                    operations,
                    request_hash,
                    receipt["confirmation"],
                    partial_failure=False,
                )

    def test_confirmation_tampering_is_rejected(self):
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
        with self._confirmation_env():
            receipt = mutation_engine._issue_validation_receipt(
                "8448275903", operations, request_hash, False
            )
            confirmation = receipt["confirmation"]
            tampered = confirmation[:-1] + (
                "A" if confirmation[-1] != "A" else "B"
            )
            with self.assertRaises(ToolError):
                mutation_engine._validate_live_execution(
                    "8448275903",
                    operations,
                    request_hash,
                    tampered,
                    partial_failure=False,
                )

    def test_live_partial_failure_is_disabled_by_default(self):
        operations = [
            {
                "action": "update",
                "resource": "Campaign",
                "data": {"status": "PAUSED"},
                "update_mask": ["status"],
            }
        ]
        request_hash = mutation_engine._operation_hash(
            "8448275903", operations, True
        )
        with self._confirmation_env():
            receipt = mutation_engine._issue_validation_receipt(
                "8448275903", operations, request_hash, True
            )
            with self.assertRaisesRegex(ToolError, "partial-failure"):
                mutation_engine._validate_live_execution(
                    "8448275903",
                    operations,
                    request_hash,
                    receipt["confirmation"],
                    partial_failure=True,
                )

    def test_enable_is_disabled_by_default(self):
        operations = [
            {
                "action": "update",
                "resource": "Campaign",
                "data": {"status": "ENABLED"},
                "update_mask": ["status"],
            }
        ]
        request_hash = mutation_engine._operation_hash(
            "8448275903", operations, False
        )
        with self._confirmation_env():
            receipt = mutation_engine._issue_validation_receipt(
                "8448275903", operations, request_hash, False
            )
            with self.assertRaisesRegex(ToolError, "ALLOW_ENABLE"):
                mutation_engine._validate_live_execution(
                    "8448275903",
                    operations,
                    request_hash,
                    receipt["confirmation"],
                    partial_failure=False,
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
        with self._confirmation_env():
            receipt = mutation_engine._issue_validation_receipt(
                "8448275903", operations, request_hash, False
            )
            with self.assertRaises(ToolError):
                mutation_engine._validate_live_execution(
                    "8448275903",
                    operations,
                    request_hash,
                    receipt["confirmation"],
                    partial_failure=False,
                )


if __name__ == "__main__":
    unittest.main()
