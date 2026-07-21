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

"""Regression tests for GoogleAdsService.Mutate request construction."""

import unittest
from unittest.mock import MagicMock, patch

from ads_mcp import mutation_engine


class TestMutateRequest(unittest.TestCase):
    def test_build_mutate_request_sets_all_request_fields(self):
        client = MagicMock()
        request = MagicMock()
        client.get_type.return_value = request
        client.enums.ResponseContentTypeEnum.MUTABLE_RESOURCE = 2
        operations = [MagicMock(), MagicMock()]

        result = mutation_engine._build_mutate_request(
            client,
            "8448275903",
            operations,
            partial_failure=True,
            validate_only=True,
        )

        self.assertIs(result, request)
        client.get_type.assert_called_once_with("MutateGoogleAdsRequest")
        self.assertEqual(request.customer_id, "8448275903")
        request.mutate_operations.extend.assert_called_once_with(operations)
        self.assertTrue(request.partial_failure)
        self.assertTrue(request.validate_only)
        self.assertEqual(request.response_content_type, 2)

    @patch("ads_mcp.mutation_engine.utils.format_output_value", return_value={})
    @patch("ads_mcp.mutation_engine._prepare_operation")
    @patch("ads_mcp.mutation_engine.utils.get_googleads_client")
    def test_run_mutations_passes_only_request_to_service(
        self,
        mock_get_client,
        mock_prepare_operation,
        mock_format_output,
    ):
        client = MagicMock()
        request = MagicMock()
        service = MagicMock()
        operation = MagicMock()
        response = MagicMock()

        client.get_type.return_value = request
        client.get_service.return_value = service
        client.enums.ResponseContentTypeEnum.MUTABLE_RESOURCE = 2
        service.mutate.return_value = response
        mock_get_client.return_value = client
        mock_prepare_operation.return_value = (
            operation,
            {
                "action": "create",
                "resource": "CampaignBudget",
                "data": {"amount_micros": 20000000},
            },
        )

        result = mutation_engine._run_mutations(
            "8448275903",
            [
                {
                    "action": "create",
                    "resource": "CampaignBudget",
                    "data": {"amount_micros": 20000000},
                }
            ],
            validate_only=True,
            partial_failure=False,
            confirmation=None,
        )

        service.mutate.assert_called_once_with(request=request)
        request.mutate_operations.extend.assert_called_once_with([operation])
        self.assertTrue(result["validated"])
        self.assertFalse(result["executed"])
        mock_format_output.assert_called_once_with(response)


if __name__ == "__main__":
    unittest.main()
