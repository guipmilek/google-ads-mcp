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

import json
import unittest
from unittest.mock import MagicMock, patch

from fastmcp.exceptions import ToolError

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

    def _prepared_operation(self):
        return (
            MagicMock(),
            {
                "action": "create",
                "resource": "CampaignBudget",
                "data": {"amount_micros": 20000000},
            },
        )

    @patch("ads_mcp.mutation_engine._validate_confirmation_configuration")
    @patch(
        "ads_mcp.mutation_engine._issue_validation_receipt",
        return_value={
            "confirmation": "EXECUTE hash.payload.signature",
            "expires_at": 1000,
            "cross_instance_valid": True,
            "replay_protection": "BEST_EFFORT_PROCESS_LOCAL",
            "globally_single_use": False,
        },
    )
    @patch(
        "ads_mcp.mutation_engine.utils.format_output_value",
        return_value={},
    )
    @patch("ads_mcp.mutation_engine._prepare_operation")
    @patch("ads_mcp.mutation_engine.utils.get_googleads_client")
    def test_validation_passes_request_object_and_issues_confirmation(
        self,
        mock_get_client,
        mock_prepare_operation,
        mock_format_output,
        mock_issue_receipt,
        mock_validate_config,
    ):
        client = MagicMock()
        request = MagicMock()
        service = MagicMock()
        response = MagicMock()

        client.get_type.return_value = request
        client.get_service.return_value = service
        client.enums.ResponseContentTypeEnum.MUTABLE_RESOURCE = 2
        service.mutate.return_value = response
        mock_get_client.return_value = client
        mock_prepare_operation.return_value = self._prepared_operation()

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
        self.assertTrue(result["validated"])
        self.assertFalse(result["executed"])
        self.assertEqual(result["validation_status"], "PASSED")
        self.assertIsNotNone(result["required_confirmation"])
        mock_format_output.assert_called_once_with(response)
        mock_issue_receipt.assert_called_once()
        mock_validate_config.assert_called_once()

    @patch("ads_mcp.mutation_engine._validate_confirmation_configuration")
    @patch("ads_mcp.mutation_engine._issue_validation_receipt")
    @patch(
        "ads_mcp.mutation_engine.utils.format_output_value",
        return_value={
            "partial_failure_error": {
                "code": 3,
                "message": "One operation is invalid.",
            }
        },
    )
    @patch("ads_mcp.mutation_engine._prepare_operation")
    @patch("ads_mcp.mutation_engine.utils.get_googleads_client")
    def test_partial_failure_validation_does_not_issue_confirmation(
        self,
        mock_get_client,
        mock_prepare_operation,
        mock_format_output,
        mock_issue_receipt,
        mock_validate_config,
    ):
        client = MagicMock()
        client.get_type.return_value = MagicMock()
        client.get_service.return_value.mutate.return_value = MagicMock()
        client.enums.ResponseContentTypeEnum.MUTABLE_RESOURCE = 2
        mock_get_client.return_value = client
        mock_prepare_operation.return_value = self._prepared_operation()

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
            partial_failure=True,
            confirmation=None,
        )

        self.assertFalse(result["validated"])
        self.assertEqual(result["validation_status"], "FAILED_PARTIAL")
        self.assertIsNone(result["required_confirmation"])
        mock_issue_receipt.assert_not_called()
        mock_format_output.assert_called_once()
        mock_validate_config.assert_called_once()

    @patch(
        "ads_mcp.mutation_engine._validate_live_execution",
        return_value={
            "registered_before_api_call": True,
            "confirmation": "EXECUTE hash.payload.signature",
        },
    )
    @patch(
        "ads_mcp.mutation_engine.utils.format_output_value",
        return_value={
            "partial_failure_error": {
                "code": 3,
                "message": "One operation failed.",
            }
        },
    )
    @patch("ads_mcp.mutation_engine._prepare_operation")
    @patch("ads_mcp.mutation_engine.utils.get_googleads_client")
    def test_execute_reports_partial_failure_without_claiming_success(
        self,
        mock_get_client,
        mock_prepare_operation,
        mock_format_output,
        mock_validate_execution,
    ):
        client = MagicMock()
        client.get_type.return_value = MagicMock()
        client.get_service.return_value.mutate.return_value = MagicMock()
        client.enums.ResponseContentTypeEnum.MUTABLE_RESOURCE = 2
        mock_get_client.return_value = client
        mock_prepare_operation.return_value = self._prepared_operation()

        result = mutation_engine._run_mutations(
            "8448275903",
            [
                {
                    "action": "create",
                    "resource": "CampaignBudget",
                    "data": {"amount_micros": 20000000},
                }
            ],
            validate_only=False,
            partial_failure=True,
            confirmation="EXECUTE hash.payload.signature",
        )

        self.assertIsNone(result["executed"])
        self.assertTrue(result["execution_attempted"])
        self.assertEqual(result["execution_status"], "PARTIAL_FAILURE")
        self.assertIsNotNone(result["partial_failure_error"])
        mock_validate_execution.assert_called_once()
        mock_format_output.assert_called_once()

    @patch("ads_mcp.mutation_engine._prepare_operation")
    @patch("ads_mcp.mutation_engine.utils.get_googleads_client")
    def test_partial_failure_rejects_temporary_resource_ids(
        self,
        mock_get_client,
        mock_prepare_operation,
    ):
        client = MagicMock()
        mock_get_client.return_value = client
        mock_prepare_operation.return_value = (
            MagicMock(),
            {
                "action": "create",
                "resource": "Campaign",
                "data": {
                    "resource_name": (
                        "customers/8448275903/campaigns/-1"
                    )
                },
            },
        )

        with self.assertRaisesRegex(ToolError, "temporary negative"):
            mutation_engine._run_mutations(
                "8448275903",
                [
                    {
                        "action": "create",
                        "resource": "Campaign",
                        "data": {
                            "resource_name": (
                                "customers/8448275903/campaigns/-1"
                            )
                        },
                    }
                ],
                validate_only=True,
                partial_failure=True,
                confirmation=None,
            )
        client.get_service.assert_not_called()

    @patch("ads_mcp.mutation_engine._prepare_operation")
    @patch("ads_mcp.mutation_engine.utils.get_googleads_client")
    def test_unexpected_live_error_reports_unknown_execution_state(
        self,
        mock_get_client,
        mock_prepare_operation,
    ):
        client = MagicMock()
        client.get_type.return_value = MagicMock()
        client.get_service.return_value.mutate.side_effect = RuntimeError(
            "transport disconnected"
        )
        client.enums.ResponseContentTypeEnum.MUTABLE_RESOURCE = 2
        mock_get_client.return_value = client
        mock_prepare_operation.return_value = self._prepared_operation()

        with patch(
            "ads_mcp.mutation_engine._validate_live_execution",
            return_value={"registered_before_api_call": True},
        ), self.assertRaises(ToolError) as context:
            mutation_engine._run_mutations(
                "8448275903",
                [
                    {
                        "action": "create",
                        "resource": "CampaignBudget",
                        "data": {"amount_micros": 20000000},
                    }
                ],
                validate_only=False,
                partial_failure=False,
                confirmation="EXECUTE hash.payload.signature",
            )

        payload = json.loads(str(context.exception))
        self.assertEqual(payload["execution_state"], "UNKNOWN")
        self.assertTrue(payload["execution_may_have_completed"])
        self.assertFalse(payload["automatic_retry_safe"])


if __name__ == "__main__":
    unittest.main()
