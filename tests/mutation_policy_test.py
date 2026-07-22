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

"""Tests for the centralized mutation safety policy."""

import os
import unittest
from unittest.mock import patch

from ads_mcp.mutation_policy import (
    MutationSafetyConfig,
    authorize_mutation_execution,
    classify_operations,
    parse_boolean_environment,
)


class TestMutationPolicy(unittest.TestCase):
    def test_boolean_parser_accepts_documented_true_values(self):
        for raw in ("true", "TRUE", " true ", "1", "yes", "on"):
            with (
                self.subTest(raw=raw),
                patch.dict(os.environ, {"GATE": raw}, clear=True),
            ):
                parsed = parse_boolean_environment("GATE")
                self.assertTrue(parsed.value)
                self.assertTrue(parsed.valid)

    def test_boolean_parser_accepts_documented_false_values(self):
        for raw in ("false", "0", "no", "off", ""):
            with (
                self.subTest(raw=raw),
                patch.dict(os.environ, {"GATE": raw}, clear=True),
            ):
                parsed = parse_boolean_environment("GATE")
                self.assertFalse(parsed.value)
                self.assertTrue(parsed.valid)

    def test_boolean_parser_defaults_absent_and_invalid_to_false(self):
        with patch.dict(os.environ, {}, clear=True):
            absent = parse_boolean_environment("GATE")
        self.assertFalse(absent.present)
        self.assertFalse(absent.value)
        self.assertTrue(absent.valid)

        with patch.dict(os.environ, {"GATE": "maybe"}, clear=True):
            invalid = parse_boolean_environment("GATE")
        self.assertTrue(invalid.present)
        self.assertFalse(invalid.value)
        self.assertFalse(invalid.valid)
        self.assertEqual(invalid.public_dict()["normalized_text"], "<invalid>")

    def test_enable_gate_is_fail_closed(self):
        classification = classify_operations(
            [
                {
                    "action": "update",
                    "resource": "AdGroupAd",
                    "data": {"status": "ENABLED"},
                    "update_mask": ["status"],
                }
            ],
            partial_failure=False,
        )
        with patch.dict(os.environ, {}, clear=True):
            config = MutationSafetyConfig.from_environment()
            decision = authorize_mutation_execution(
                classification, config, validate_only=False
            )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason_code, "ENABLE_GATE_DISABLED")
        self.assertEqual(decision.required_gate, "GOOGLE_ADS_ALLOW_ENABLE")

        with patch.dict(
            os.environ, {"GOOGLE_ADS_ALLOW_ENABLE": "true"}, clear=True
        ):
            config = MutationSafetyConfig.from_environment()
            decision = authorize_mutation_execution(
                classification, config, validate_only=False
            )
        self.assertTrue(decision.allowed)

    def test_remove_partial_failure_and_sensitive_gates(self):
        cases = (
            (
                [
                    {
                        "action": "remove",
                        "resource": "Campaign",
                        "resource_name": "customers/8448275903/campaigns/1",
                    }
                ],
                False,
                "REMOVE_GATE_DISABLED",
            ),
            (
                [
                    {
                        "action": "update",
                        "resource": "Campaign",
                        "data": {"status": "PAUSED"},
                        "update_mask": ["status"],
                    }
                ],
                True,
                "PARTIAL_FAILURE_GATE_DISABLED",
            ),
            (
                [
                    {
                        "action": "update",
                        "resource": "BillingSetup",
                        "data": {"status": "PAUSED"},
                        "update_mask": ["status"],
                    }
                ],
                False,
                "SENSITIVE_MUTATION_GATE_DISABLED",
            ),
        )
        for operations, partial_failure, reason_code in cases:
            with (
                self.subTest(reason_code=reason_code),
                patch.dict(os.environ, {}, clear=True),
            ):
                config = MutationSafetyConfig.from_environment()
                classification = classify_operations(
                    operations, partial_failure=partial_failure
                )
                decision = authorize_mutation_execution(
                    classification, config, validate_only=False
                )
                self.assertFalse(decision.allowed)
                self.assertEqual(decision.reason_code, reason_code)

    def test_invalid_boolean_blocks_live_execution(self):
        operations = [
            {
                "action": "update",
                "resource": "Campaign",
                "data": {"status": "PAUSED"},
                "update_mask": ["status"],
            }
        ]
        with patch.dict(
            os.environ, {"GOOGLE_ADS_ALLOW_ENABLE": "invalid"}, clear=True
        ):
            config = MutationSafetyConfig.from_environment()
            decision = authorize_mutation_execution(
                classify_operations(operations, False),
                config,
                validate_only=False,
            )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason_code, "UNKNOWN_BOOLEAN_ENV_VALUE")

    def test_validate_only_is_allowed_without_opening_gates(self):
        operations = [
            {
                "action": "update",
                "resource": "AdGroupAd",
                "data": {"status": "ENABLED"},
                "update_mask": ["status"],
            }
        ]
        with patch.dict(os.environ, {}, clear=True):
            decision = authorize_mutation_execution(
                classify_operations(operations, False),
                MutationSafetyConfig.from_environment(),
                validate_only=True,
            )
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.reason_code, "VALIDATION_ONLY")
        self.assertEqual(
            decision.public_dict(),
            {
                "allowed": True,
                "status": "ALLOWED",
                "reason_code": "VALIDATION_ONLY",
                "required_gate": None,
                "observed_gate": None,
                "execution_mode": "VALIDATE_ONLY",
                "policy_version": "mutation-safety-v4",
            },
        )


if __name__ == "__main__":
    unittest.main()
