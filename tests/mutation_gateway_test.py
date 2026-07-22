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

"""Tests for the unified mutation endpoint gateway."""

import json
import os
import unittest
from unittest.mock import patch

from fastmcp.exceptions import ToolError

from ads_mcp import mutation_gateway
from ads_mcp import mutation_safety
from ads_mcp.mutation_policy import classify_operations


class TestMutationGateway(unittest.TestCase):
    CUSTOMER_ID = "8448275903"
    OPERATION_HASH = "a" * 32
    OPERATIONS = [
        {
            "action": "update",
            "resource": "AdGroupAd",
            "data": {
                "resource_name": (
                    "customers/8448275903/adGroupAds/"
                    "127998519245~817845836549"
                ),
                "status": "ENABLED",
            },
            "update_mask": ["status"],
        },
        {
            "action": "update",
            "resource": "AdGroupAd",
            "data": {
                "resource_name": (
                    "customers/8448275903/adGroupAds/"
                    "133301803367~817845836552"
                ),
                "status": "ENABLED",
            },
            "update_mask": ["status"],
        },
    ]

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

    def _canonical_context(self):
        classification = classify_operations(self.OPERATIONS, False)
        return (
            self.CUSTOMER_ID,
            self.OPERATIONS,
            self.OPERATION_HASH,
            classification,
        )

    def _validation_response(self):
        receipt = mutation_safety._issue_validation_receipt(
            self.CUSTOMER_ID,
            self.OPERATIONS,
            self.OPERATION_HASH,
            False,
        )
        return {
            "operation_hash": self.OPERATION_HASH,
            "operations": self.OPERATIONS,
            "required_confirmation": receipt["confirmation"],
            "mode": "VALIDATE_ONLY",
        }

    def _execution_response(self):
        return {
            "operation_hash": self.OPERATION_HASH,
            "operations": self.OPERATIONS,
            "required_confirmation": None,
            "mode": "EXECUTE",
            "execution_status": "SUCCEEDED",
        }

    def test_enable_is_blocked_before_engine_when_gate_is_false(self):
        with self._environment(), patch.object(
            mutation_gateway,
            "_canonical_context",
            return_value=self._canonical_context(),
        ), patch.object(
            mutation_gateway.mutation_engine, "_run_mutations"
        ) as run_mutations:
            with self.assertRaises(ToolError) as raised:
                mutation_gateway.batch_mutate(
                    self.CUSTOMER_ID,
                    self.OPERATIONS,
                    validate_only=False,
                    confirmation="unused",
                )
        run_mutations.assert_not_called()
        payload = json.loads(str(raised.exception))
        self.assertEqual(payload["reason_code"], "ENABLE_GATE_DISABLED")
        self.assertEqual(payload["required_gate"], "GOOGLE_ADS_ALLOW_ENABLE")

    def test_two_enable_operations_remain_one_atomic_engine_call(self):
        with self._environment(GOOGLE_ADS_ALLOW_ENABLE="true"), patch.object(
            mutation_gateway,
            "_canonical_context",
            return_value=self._canonical_context(),
        ), patch.object(
            mutation_gateway.mutation_engine,
            "_run_mutations",
            return_value=self._validation_response(),
        ) as validate_call:
            validation = mutation_gateway.batch_mutate(
                self.CUSTOMER_ID,
                self.OPERATIONS,
                validate_only=True,
                partial_failure=False,
            )
        validate_call.assert_called_once_with(
            self.CUSTOMER_ID,
            self.OPERATIONS,
            validate_only=True,
            partial_failure=False,
            confirmation=None,
        )
        outer_confirmation = validation["required_confirmation"]
        self.assertTrue(outer_confirmation.startswith("ENABLE "))
        self.assertEqual(
            validation["security_policy"]["classification"]["operation_count"],
            2,
        )
        self.assertEqual(
            validation["security_policy"]["classification"]["fields"],
            ["status"],
        )

        with self._environment(GOOGLE_ADS_ALLOW_ENABLE="true"):
            inner_confirmation = mutation_gateway._verify_policy_envelope(
                confirmation=outer_confirmation,
                customer_id=self.CUSTOMER_ID,
                operation_hash=self.OPERATION_HASH,
                classification=self._canonical_context()[3],
                config=mutation_gateway.MutationSafetyConfig.from_environment(),
                endpoint="mutations_batch_mutate",
            )
        with self._environment(GOOGLE_ADS_ALLOW_ENABLE="true"), patch.object(
            mutation_gateway,
            "_canonical_context",
            return_value=self._canonical_context(),
        ), patch.object(
            mutation_gateway.mutation_engine,
            "_run_mutations",
            return_value=self._execution_response(),
        ) as execute_call:
            result = mutation_gateway.batch_mutate(
                self.CUSTOMER_ID,
                self.OPERATIONS,
                validate_only=False,
                partial_failure=False,
                confirmation=outer_confirmation,
            )
        execute_call.assert_called_once_with(
            self.CUSTOMER_ID,
            self.OPERATIONS,
            validate_only=False,
            partial_failure=False,
            confirmation=inner_confirmation,
        )
        self.assertEqual(result["execution_status"], "SUCCEEDED")
        self.assertEqual(len(execute_call.call_args.args[1]), 2)

    def test_policy_drift_blocks_before_engine(self):
        with self._environment(GOOGLE_ADS_ALLOW_ENABLE="true"), patch.object(
            mutation_gateway,
            "_canonical_context",
            return_value=self._canonical_context(),
        ), patch.object(
            mutation_gateway.mutation_engine,
            "_run_mutations",
            return_value=self._validation_response(),
        ):
            validation = mutation_gateway.batch_mutate(
                self.CUSTOMER_ID,
                self.OPERATIONS,
                validate_only=True,
            )

        changed_operations = [dict(operation) for operation in self.OPERATIONS]
        changed_operations[0] = {
            **changed_operations[0],
            "update_mask": ["status", "ad.name"],
        }
        changed_classification = classify_operations(changed_operations, False)
        changed_context = (
            self.CUSTOMER_ID,
            changed_operations,
            self.OPERATION_HASH,
            changed_classification,
        )
        with self._environment(GOOGLE_ADS_ALLOW_ENABLE="true"), patch.object(
            mutation_gateway,
            "_canonical_context",
            return_value=changed_context,
        ), patch.object(
            mutation_gateway.mutation_engine, "_run_mutations"
        ) as run_mutations:
            with self.assertRaises(ToolError) as raised:
                mutation_gateway.batch_mutate(
                    self.CUSTOMER_ID,
                    changed_operations,
                    validate_only=False,
                    confirmation=validation["required_confirmation"],
                )
        run_mutations.assert_not_called()
        payload = json.loads(str(raised.exception))
        self.assertEqual(payload["status"], "BLOCKED_BY_STATE_OR_POLICY_DRIFT")
        self.assertEqual(payload["reason_code"], "POLICY_OR_CLASSIFICATION_DRIFT")

    def test_read_only_status_exposes_sanitized_runtime_without_api_call(self):
        with self._environment(GOOGLE_ADS_ALLOW_ENABLE="true"):
            result = mutation_gateway.get_mutation_safety_status(
                operations=self.OPERATIONS,
                partial_failure=False,
                validate_only=False,
            )
        self.assertEqual(result["status"], "READ_ONLY")
        self.assertFalse(result["api_call_performed"])
        self.assertFalse(result["confirmation_issued"])
        self.assertTrue(result["safety_config"]["allow_enable"])
        self.assertTrue(result["decision"]["allowed"])
        self.assertNotIn("GOOGLE_ADS_CONFIRMATION_SECRET", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
