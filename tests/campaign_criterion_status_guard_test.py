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

"""Regression tests for CampaignCriterion create normalization."""

from types import SimpleNamespace
import unittest

from fastmcp.exceptions import ToolError

from ads_mcp import mutation_safety
from ads_mcp.campaign_criterion_status_guard import (
    install_campaign_criterion_status_guard,
)


class TestCampaignCriterionStatusGuard(unittest.TestCase):
    def setUp(self):
        self.original_guard = mutation_safety._apply_create_status_guard
        install_campaign_criterion_status_guard()
        self.guard = mutation_safety._apply_create_status_guard

    def tearDown(self):
        mutation_safety._apply_create_status_guard = self.original_guard

    def _descriptor(self, name="CampaignCriterion"):
        status_field = SimpleNamespace(
            enum_type=SimpleNamespace(
                values=[
                    SimpleNamespace(name="UNSPECIFIED"),
                    SimpleNamespace(name="ENABLED"),
                    SimpleNamespace(name="PAUSED"),
                    SimpleNamespace(name="REMOVED"),
                ]
            )
        )
        return SimpleNamespace(
            name=name,
            fields_by_name={"status": status_field},
        )

    def test_language_create_does_not_inject_status(self):
        data = {
            "campaign": "customers/8448275903/campaigns/24055919265",
            "language": {"language_constant": "languageConstants/1014"},
        }

        result = self.guard(data, self._descriptor())

        self.assertEqual(result, data)
        self.assertNotIn("status", result)

    def test_location_create_does_not_inject_status(self):
        data = {
            "campaign": "customers/8448275903/campaigns/24055919265",
            "location": {
                "geo_target_constant": "geoTargetConstants/1001773"
            },
        }

        result = self.guard(data, self._descriptor())

        self.assertEqual(result, data)
        self.assertNotIn("status", result)

    def test_explicit_status_is_rejected(self):
        cases = (
            (
                "language",
                {"language_constant": "languageConstants/1014"},
            ),
            (
                "location",
                {"geo_target_constant": "geoTargetConstants/1001773"},
            ),
        )
        for criterion_type, criterion_data in cases:
            with self.subTest(criterion_type=criterion_type):
                with self.assertRaisesRegex(ToolError, "must omit status"):
                    self.guard(
                        {
                            "campaign": (
                                "customers/8448275903/campaigns/24055919265"
                            ),
                            criterion_type: criterion_data,
                            "status": "PAUSED",
                        },
                        self._descriptor(),
                    )

    def test_other_resources_still_default_to_paused(self):
        result = self.guard(
            {"name": "Paused by guard"},
            self._descriptor(name="Campaign"),
        )

        self.assertEqual(result["status"], "PAUSED")

    def test_install_is_idempotent(self):
        first = mutation_safety._apply_create_status_guard

        install_campaign_criterion_status_guard()

        self.assertIs(mutation_safety._apply_create_status_guard, first)


if __name__ == "__main__":
    unittest.main()
