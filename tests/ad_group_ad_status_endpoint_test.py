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

"""Tests for the constrained atomic AdGroupAd status endpoint."""

import json
import os
import unittest
from unittest.mock import patch

from fastmcp.exceptions import ToolError

from ads_mcp import mutation_gateway
from ads_mcp import mutation_safety
from ads_mcp.mutation_policy import classify_operations


class TestAdGroupAdStatusEndpoint(unittest.TestCase):
    CUSTOMER_ID = "8448275903"
    RESOURCE_NAMES = [
        "customers/8448275903/adGroupAds/127998519245~817845836549",
        "customers/8448275903/adGroupAds/133301803367~817845836552",
    ]
    OPERATION_HASH = "d" * 32

    def setUp(self):
        mutation_safety._clear_confirmation_replay_cache_for_tests()

    def _environment(self, **overrides):
        values = {
            "GOOGLE_ADS_CONFIRMATION_SECRET": "s" * 64,
            "GOOGLE_ADS_MUTATIONS_ENABLED": "true",
            "GOOGLE_ADS_ALLOWED_CUSTOMER_IDS": self.CUSTOMER_ID,
        }
        values.update(overrides)
        return patch.dict(os.environ, values, clear=True)

    def _operations(self, status="ENABLED", names=None):
        _, operations = mutation_gateway._build_ad_group_ad_status_operations(
            self.CUSTOMER_ID, names or self.RESOURCE_NAMES, status
        )
        return operations

    def _context(self, status="ENABLED", names=None):
        operations = self._operations(status=status, names=names)
        return (
            self.CUSTOMER_ID,
            operations,
            self.OPERATION_HASH,
            classify_operations(operations, False),
        )

    def _validation_response(self, status="ENABLED", names=None):
        operations = self._operations(status=status, names=names)
        receipt = mutation_safety._issue_validation_receipt(
            self.CUSTOMER_ID, operations, self.OPERATION_HASH, False
        )
        return {
            "operation_hash": self.OPERATION_HASH,
            "operations": operations,
            "required_confirmation": receipt["confirmation"],
            "mode": "VALIDATE_ONLY",
        }

    def _execution_response(self, status="ENABLED", names=None):
        return {
            "operation_hash": self.OPERATION_HASH,
            "operations": self._operations(status=status, names=names),
            "required_confirmation": None,
            "mode": "EXECUTE",
            "execution_status": "SUCCEEDED",
        }

    def _assert_reason(self, callable_, reason_code):
        with self.assertRaises(ToolError) as raised:
            callable_()
        payload = json.loads(str(raised.exception))
        self.assertEqual(payload["reason_code"], reason_code)
        return payload

    def test_accepts_one_two_and_ten_resources(self):
        for count in (1, 2, 10):
            names = [
                f"customers/{self.CUSTOMER_ID}/adGroupAds/{100 + index}~{200 + index}"
                for index in range(count)
            ]
            customer, operations = (
                mutation_gateway._build_ad_group_ad_status_operations(
                    self.CUSTOMER_ID, names, "ENABLED"
                )
            )
            self.assertEqual(customer, self.CUSTOMER_ID)
            self.assertEqual(len(operations), count)
            self.assertTrue(
                all(
                    operation["update_mask"] == ["status"]
                    for operation in operations
                )
            )

    def test_rejects_empty_more_than_ten_and_duplicates(self):
        invalid_lists = (
            [],
            [
                f"customers/{self.CUSTOMER_ID}/adGroupAds/{index}~{index}"
                for index in range(11)
            ],
            [self.RESOURCE_NAMES[0], self.RESOURCE_NAMES[0]],
        )
        for names in invalid_lists:
            with self.subTest(names=len(names)):
                with self.assertRaises(ToolError):
                    mutation_gateway._build_ad_group_ad_status_operations(
                        self.CUSTOMER_ID, names, "ENABLED"
                    )

    def test_rejects_customer_type_name_and_status_mismatches(self):
        cases = (
            (
                ["customers/1111111111/adGroupAds/1~2"],
                "ENABLED",
            ),
            (
                [f"customers/{self.CUSTOMER_ID}/campaigns/1"],
                "ENABLED",
            ),
            (["not-a-resource-name"], "ENABLED"),
            (self.RESOURCE_NAMES, "REMOVED"),
            (self.RESOURCE_NAMES, "enabled"),
        )
        for names, status in cases:
            with self.subTest(names=names, status=status):
                with self.assertRaises(ToolError):
                    mutation_gateway._build_ad_group_ad_status_operations(
                        self.CUSTOMER_ID, names, status
                    )

    def test_enabled_requires_enable_gate_before_engine(self):
        context = self._context()
        with (
            self._environment(),
            patch.object(
                mutation_gateway, "_canonical_context", return_value=context
            ),
            patch.object(
                mutation_gateway.mutation_engine, "_run_mutations"
            ) as run_mutations,
        ):
            payload = self._assert_reason(
                lambda: mutation_gateway.update_ad_group_ad_statuses(
                    self.CUSTOMER_ID,
                    self.RESOURCE_NAMES,
                    "ENABLED",
                    validate_only=False,
                    confirmation="unused",
                ),
                "ENABLE_GATE_DISABLED",
            )
        run_mutations.assert_not_called()
        self.assertEqual(payload["required_gate"], "GOOGLE_ADS_ALLOW_ENABLE")

    def test_preflight_is_atomic_and_does_not_execute(self):
        context = self._context()
        with (
            self._environment(GOOGLE_ADS_ALLOW_ENABLE="true"),
            patch.object(
                mutation_gateway, "_canonical_context", return_value=context
            ),
            patch.object(
                mutation_gateway.mutation_engine,
                "_run_mutations",
                return_value=self._validation_response(),
            ) as run_mutations,
        ):
            response = mutation_gateway.update_ad_group_ad_statuses(
                self.CUSTOMER_ID, self.RESOURCE_NAMES, "ENABLED"
            )
        run_mutations.assert_called_once_with(
            self.CUSTOMER_ID,
            context[1],
            validate_only=True,
            partial_failure=False,
            confirmation=None,
        )
        self.assertEqual(
            response["security_policy"]["classification"]["operation_count"], 2
        )
        self.assertEqual(
            response["security_policy"]["classification"]["fields"], ["status"]
        )
        self.assertEqual(
            response["security_policy"]["classification"]["resource_types"],
            ["AdGroupAd"],
        )
        self.assertTrue(response["required_confirmation"].startswith("ENABLE "))

    def test_two_resources_execute_in_one_atomic_engine_call(self):
        context = self._context()
        with (
            self._environment(GOOGLE_ADS_ALLOW_ENABLE="true"),
            patch.object(
                mutation_gateway, "_canonical_context", return_value=context
            ),
            patch.object(
                mutation_gateway.mutation_engine,
                "_run_mutations",
                return_value=self._validation_response(),
            ),
        ):
            validation = mutation_gateway.update_ad_group_ad_statuses(
                self.CUSTOMER_ID, self.RESOURCE_NAMES, "ENABLED"
            )

        with (
            self._environment(GOOGLE_ADS_ALLOW_ENABLE="true"),
            patch.object(
                mutation_gateway, "_canonical_context", return_value=context
            ),
            patch.object(
                mutation_gateway.mutation_engine,
                "_run_mutations",
                return_value=self._execution_response(),
            ) as run_mutations,
        ):
            result = mutation_gateway.update_ad_group_ad_statuses(
                self.CUSTOMER_ID,
                self.RESOURCE_NAMES,
                "ENABLED",
                validate_only=False,
                confirmation=validation["required_confirmation"],
            )
        self.assertEqual(result["execution_status"], "SUCCEEDED")
        self.assertEqual(run_mutations.call_count, 1)
        self.assertEqual(len(run_mutations.call_args.args[1]), 2)
        self.assertFalse(run_mutations.call_args.kwargs["partial_failure"])

    def test_paused_does_not_require_enable_gate(self):
        context = self._context(status="PAUSED")
        with (
            self._environment(),
            patch.object(
                mutation_gateway, "_canonical_context", return_value=context
            ),
            patch.object(
                mutation_gateway.mutation_engine,
                "_run_mutations",
                return_value=self._validation_response(status="PAUSED"),
            ),
        ):
            validation = mutation_gateway.update_ad_group_ad_statuses(
                self.CUSTOMER_ID, self.RESOURCE_NAMES, "PAUSED"
            )
        self.assertTrue(
            validation["required_confirmation"].startswith("EXECUTE ")
        )

        with (
            self._environment(),
            patch.object(
                mutation_gateway, "_canonical_context", return_value=context
            ),
            patch.object(
                mutation_gateway.mutation_engine,
                "_run_mutations",
                return_value=self._execution_response(status="PAUSED"),
            ) as run_mutations,
        ):
            mutation_gateway.update_ad_group_ad_statuses(
                self.CUSTOMER_ID,
                self.RESOURCE_NAMES,
                "PAUSED",
                validate_only=False,
                confirmation=validation["required_confirmation"],
            )
        run_mutations.assert_called_once()

    def test_live_execution_requires_confirmation(self):
        context = self._context()
        with (
            self._environment(GOOGLE_ADS_ALLOW_ENABLE="true"),
            patch.object(
                mutation_gateway, "_canonical_context", return_value=context
            ),
            patch.object(
                mutation_gateway.mutation_engine, "_run_mutations"
            ) as run_mutations,
        ):
            self._assert_reason(
                lambda: mutation_gateway.update_ad_group_ad_statuses(
                    self.CUSTOMER_ID,
                    self.RESOURCE_NAMES,
                    "ENABLED",
                    validate_only=False,
                ),
                "CONFIRMATION_REQUIRED",
            )
        run_mutations.assert_not_called()

    def test_confirmation_is_bound_to_endpoint(self):
        context = self._context()
        with (
            self._environment(GOOGLE_ADS_ALLOW_ENABLE="true"),
            patch.object(
                mutation_gateway, "_canonical_context", return_value=context
            ),
            patch.object(
                mutation_gateway.mutation_engine,
                "_run_mutations",
                return_value=self._validation_response(),
            ),
        ):
            batch_validation = mutation_gateway.batch_mutate(
                self.CUSTOMER_ID, context[1], validate_only=True
            )

        with (
            self._environment(GOOGLE_ADS_ALLOW_ENABLE="true"),
            patch.object(
                mutation_gateway, "_canonical_context", return_value=context
            ),
            patch.object(
                mutation_gateway.mutation_engine, "_run_mutations"
            ) as run_mutations,
        ):
            self._assert_reason(
                lambda: mutation_gateway.update_ad_group_ad_statuses(
                    self.CUSTOMER_ID,
                    self.RESOURCE_NAMES,
                    "ENABLED",
                    validate_only=False,
                    confirmation=batch_validation["required_confirmation"],
                ),
                "CONFIRMATION_ENDPOINT_MISMATCH",
            )
        run_mutations.assert_not_called()

    def test_confirmation_is_bound_to_contract_version(self):
        context = self._context()
        with (
            self._environment(GOOGLE_ADS_ALLOW_ENABLE="true"),
            patch.object(
                mutation_gateway, "_canonical_context", return_value=context
            ),
            patch.object(
                mutation_gateway.mutation_engine,
                "_run_mutations",
                return_value=self._validation_response(),
            ),
        ):
            validation = mutation_gateway.update_ad_group_ad_statuses(
                self.CUSTOMER_ID, self.RESOURCE_NAMES, "ENABLED"
            )
            config = mutation_gateway.MutationSafetyConfig.from_environment()
            self._assert_reason(
                lambda: mutation_gateway._verify_policy_envelope(
                    confirmation=validation["required_confirmation"],
                    customer_id=self.CUSTOMER_ID,
                    operation_hash=self.OPERATION_HASH,
                    classification=context[3],
                    config=config,
                    endpoint="mutations_update_ad_group_ad_statuses",
                    tool_contract_version=2,
                ),
                "CONFIRMATION_CONTRACT_VERSION_MISMATCH",
            )

    def test_diagnostic_does_not_issue_confirmation_or_call_api(self):
        operations = self._operations()
        with patch.object(
            mutation_gateway.mutation_engine, "_run_mutations"
        ) as run_mutations:
            result = mutation_gateway.get_mutation_safety_status(
                operations=operations,
                partial_failure=False,
                validate_only=False,
                endpoint="mutations_update_ad_group_ad_statuses",
                tool_contract_version=1,
            )
        run_mutations.assert_not_called()
        self.assertFalse(result["api_call_performed"])
        self.assertFalse(result["confirmation_issued"])


if __name__ == "__main__":
    unittest.main()
