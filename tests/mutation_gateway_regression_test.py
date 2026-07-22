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

"""Regression tests for endpoint and confirmation gate consistency."""

import json
import os
import unittest
from unittest.mock import patch

from fastmcp.exceptions import ToolError

from ads_mcp import mutation_gateway
from ads_mcp import mutation_safety
from ads_mcp.mutation_policy import MutationSafetyConfig, classify_operations


class TestMutationGatewayRegressions(unittest.TestCase):
    CUSTOMER_ID = "8448275903"
    OPERATION_HASH = "b" * 32
    ENABLE_OPERATIONS = [
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
    REMOVE_OPERATION = {
        "action": "remove",
        "resource": "CampaignCriterion",
        "resource_name": (
            "customers/8448275903/campaignCriteria/15232862584~1000"
        ),
    }

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

    def _enable_context(self, partial_failure=False):
        return (
            self.CUSTOMER_ID,
            self.ENABLE_OPERATIONS,
            self.OPERATION_HASH,
            classify_operations(self.ENABLE_OPERATIONS, partial_failure),
        )

    def _validation_response(self, partial_failure=False):
        receipt = mutation_safety._issue_validation_receipt(
            self.CUSTOMER_ID,
            self.ENABLE_OPERATIONS,
            self.OPERATION_HASH,
            partial_failure,
        )
        return {
            "operation_hash": self.OPERATION_HASH,
            "operations": self.ENABLE_OPERATIONS,
            "required_confirmation": receipt["confirmation"],
            "mode": "VALIDATE_ONLY",
        }

    def _outer_confirmation(self):
        with (
            self._environment(GOOGLE_ADS_ALLOW_ENABLE="true"),
            patch.object(
                mutation_gateway,
                "_canonical_context",
                return_value=self._enable_context(),
            ),
            patch.object(
                mutation_gateway.mutation_engine,
                "_run_mutations",
                return_value=self._validation_response(),
            ),
        ):
            response = mutation_gateway.batch_mutate(
                self.CUSTOMER_ID,
                self.ENABLE_OPERATIONS,
                validate_only=True,
            )
        return response["required_confirmation"]

    def _assert_reason(self, callable_, expected):
        with self.assertRaises(ToolError) as raised:
            callable_()
        payload = json.loads(str(raised.exception))
        self.assertEqual(payload["reason_code"], expected)
        return payload

    def test_batch_and_specific_remove_require_the_same_gate(self):
        operations = [self.REMOVE_OPERATION]
        context = (
            self.CUSTOMER_ID,
            operations,
            self.OPERATION_HASH,
            classify_operations(operations, False),
        )
        with (
            self._environment(),
            patch.object(
                mutation_gateway, "_canonical_context", return_value=context
            ),
            patch.object(
                mutation_gateway.mutation_engine, "_run_mutations"
            ) as run_mutations,
        ):
            batch = self._assert_reason(
                lambda: mutation_gateway.batch_mutate(
                    self.CUSTOMER_ID,
                    operations,
                    validate_only=False,
                    confirmation="unused",
                ),
                "REMOVE_GATE_DISABLED",
            )
            specific = self._assert_reason(
                lambda: mutation_gateway.remove_resource(
                    self.CUSTOMER_ID,
                    "CampaignCriterion",
                    self.REMOVE_OPERATION["resource_name"],
                    validate_only=False,
                    confirmation="unused",
                ),
                "REMOVE_GATE_DISABLED",
            )
        run_mutations.assert_not_called()
        self.assertEqual(batch["required_gate"], specific["required_gate"])
        self.assertEqual(batch["safety_config"], specific["safety_config"])

    def test_partial_failure_and_sensitive_gates_are_simulated_fail_closed(
        self,
    ):
        partial = mutation_gateway.get_mutation_safety_status(
            operations=[
                {
                    "action": "update",
                    "resource": "Campaign",
                    "data": {"status": "PAUSED"},
                    "update_mask": ["status"],
                }
            ],
            partial_failure=True,
            validate_only=False,
        )
        sensitive = mutation_gateway.get_mutation_safety_status(
            operations=[
                {
                    "action": "update",
                    "resource": "BillingSetup",
                    "data": {"status": "PAUSED"},
                    "update_mask": ["status"],
                }
            ],
            validate_only=False,
        )
        self.assertEqual(
            partial["decision"]["reason_code"],
            "PARTIAL_FAILURE_GATE_DISABLED",
        )
        self.assertEqual(
            sensitive["decision"]["reason_code"],
            "SENSITIVE_MUTATION_GATE_DISABLED",
        )

    def test_invalid_gate_value_never_enables_execution(self):
        with (
            self._environment(GOOGLE_ADS_ALLOW_ENABLE="sometimes"),
            patch.object(
                mutation_gateway,
                "_canonical_context",
                return_value=self._enable_context(),
            ),
            patch.object(
                mutation_gateway.mutation_engine, "_run_mutations"
            ) as run_mutations,
        ):
            payload = self._assert_reason(
                lambda: mutation_gateway.batch_mutate(
                    self.CUSTOMER_ID,
                    self.ENABLE_OPERATIONS,
                    validate_only=False,
                    confirmation="unused",
                ),
                "UNKNOWN_BOOLEAN_ENV_VALUE",
            )
        run_mutations.assert_not_called()
        self.assertFalse(payload["safety_config"]["allow_enable"])

    def test_confirmation_binding_reason_codes(self):
        confirmation = self._outer_confirmation()
        classification = self._enable_context()[3]
        with self._environment(GOOGLE_ADS_ALLOW_ENABLE="true"):
            config = MutationSafetyConfig.from_environment()
            cases = (
                (
                    {
                        "customer_id": "1111111111",
                        "operation_hash": self.OPERATION_HASH,
                        "classification": classification,
                    },
                    "CONFIRMATION_CUSTOMER_MISMATCH",
                ),
                (
                    {
                        "customer_id": self.CUSTOMER_ID,
                        "operation_hash": "c" * 32,
                        "classification": classification,
                    },
                    "CONFIRMATION_HASH_MISMATCH",
                ),
                (
                    {
                        "customer_id": self.CUSTOMER_ID,
                        "operation_hash": self.OPERATION_HASH,
                        "classification": classify_operations(
                            [self.REMOVE_OPERATION], False
                        ),
                    },
                    "CONFIRMATION_VERB_MISMATCH",
                ),
                (
                    {
                        "customer_id": self.CUSTOMER_ID,
                        "operation_hash": self.OPERATION_HASH,
                        "classification": classify_operations(
                            self.ENABLE_OPERATIONS, True
                        ),
                    },
                    "CONFIRMATION_PARTIAL_FAILURE_MISMATCH",
                ),
            )
            for arguments, reason_code in cases:
                with self.subTest(reason_code=reason_code):
                    self._assert_reason(
                        lambda arguments=arguments: (
                            mutation_gateway._verify_policy_envelope(
                                confirmation=confirmation,
                                config=config,
                                endpoint="mutations_batch_mutate",
                                **arguments,
                            )
                        ),
                        reason_code,
                    )

    def test_expired_confirmation_is_rejected_before_engine(self):
        confirmation = self._outer_confirmation()
        _, token = confirmation.split(" ", 1)
        _, payload_part, _ = token.split(".", 2)
        payload = json.loads(
            mutation_safety._b64url_decode(payload_part).decode("utf-8")
        )
        with (
            self._environment(GOOGLE_ADS_ALLOW_ENABLE="true"),
            patch.object(
                mutation_gateway,
                "_canonical_context",
                return_value=self._enable_context(),
            ),
            patch.object(
                mutation_gateway.time, "time", return_value=payload["exp"]
            ),
            patch.object(
                mutation_gateway.mutation_engine, "_run_mutations"
            ) as run_mutations,
        ):
            self._assert_reason(
                lambda: mutation_gateway.batch_mutate(
                    self.CUSTOMER_ID,
                    self.ENABLE_OPERATIONS,
                    validate_only=False,
                    confirmation=confirmation,
                ),
                "CONFIRMATION_EXPIRED",
            )
        run_mutations.assert_not_called()

    def test_replay_reason_is_sanitized(self):
        confirmation = self._outer_confirmation()
        with (
            self._environment(GOOGLE_ADS_ALLOW_ENABLE="true"),
            patch.object(
                mutation_gateway,
                "_canonical_context",
                return_value=self._enable_context(),
            ),
            patch.object(
                mutation_gateway.mutation_engine,
                "_run_mutations",
                side_effect=ToolError("Confirmation token already used."),
            ),
        ):
            self._assert_reason(
                lambda: mutation_gateway.batch_mutate(
                    self.CUSTOMER_ID,
                    self.ENABLE_OPERATIONS,
                    validate_only=False,
                    confirmation=confirmation,
                ),
                "CONFIRMATION_REPLAYED",
            )

    def test_all_public_mutation_tools_route_to_the_common_gateway(self):
        with patch.object(
            mutation_gateway, "_invoke_mutation", return_value={"ok": True}
        ) as invoke:
            mutation_gateway.create_resource(
                self.CUSTOMER_ID, "Campaign", {"name": "x"}
            )
            mutation_gateway.update_resource(
                self.CUSTOMER_ID,
                "Campaign",
                {"resource_name": "customers/8448275903/campaigns/1"},
                ["name"],
            )
            mutation_gateway.remove_resource(
                self.CUSTOMER_ID,
                "Campaign",
                "customers/8448275903/campaigns/1",
            )
            mutation_gateway.batch_mutate(
                self.CUSTOMER_ID, self.ENABLE_OPERATIONS
            )
        self.assertEqual(
            [call.kwargs["endpoint"] for call in invoke.call_args_list],
            [
                "mutations_create_resource",
                "mutations_update_resource",
                "mutations_remove_resource",
                "mutations_batch_mutate",
            ],
        )


if __name__ == "__main__":
    unittest.main()
