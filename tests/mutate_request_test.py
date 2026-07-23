# Copyright 2026 Google LLC.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Regression tests for the direct GoogleAdsService.Mutate contract."""

import inspect
import os
import unittest
from unittest.mock import MagicMock, patch

from fastmcp.exceptions import ToolError

from ads_mcp import mutation_engine, mutation_gateway, mutation_safety
from ads_mcp.tools.mutations import mutations_mcp


class TestMutateRequest(unittest.TestCase):
    def _environment(self):
        return {
            "GOOGLE_ADS_ALLOWED_CUSTOMER_IDS": "8448275903",
            "GOOGLE_ADS_MAX_OPERATIONS_PER_REQUEST": "20",
            "GOOGLE_ADS_MUTATIONS_ENABLED": "false",
            "GOOGLE_ADS_ALLOW_REMOVE": "false",
            "GOOGLE_ADS_CONFIRMATION_SECRET": "unused",
        }

    def _prepared_operation(self):
        return (
            MagicMock(),
            {
                "action": "create",
                "resource": "CampaignBudget",
                "data": {"amount_micros": 20000000},
            },
        )

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
        self.assertEqual(request.customer_id, "8448275903")
        request.mutate_operations.extend.assert_called_once_with(operations)
        self.assertTrue(request.partial_failure)
        self.assertTrue(request.validate_only)
        self.assertEqual(request.response_content_type, 2)

    @patch(
        "ads_mcp.mutation_engine.utils.format_output_value",
        return_value={},
    )
    @patch("ads_mcp.mutation_engine._prepare_operation")
    @patch("ads_mcp.mutation_engine.utils.get_googleads_client")
    def test_dry_run_uses_one_native_validate_only_call(
        self, get_client, prepare, format_output
    ):
        client = MagicMock()
        request = MagicMock()
        service = MagicMock()
        client.get_type.return_value = request
        client.get_service.return_value = service
        client.enums.ResponseContentTypeEnum.MUTABLE_RESOURCE = 2
        service.mutate.return_value = MagicMock()
        get_client.return_value = client
        prepare.return_value = self._prepared_operation()

        with patch.dict(os.environ, self._environment(), clear=True):
            result = mutation_engine.create_resource(
                "8448275903",
                "CampaignBudget",
                {"amount_micros": 20000000},
                dry_run=True,
            )

        service.mutate.assert_called_once_with(request=request)
        self.assertTrue(request.validate_only)
        self.assertEqual("DRY_RUN", result["mode"])
        self.assertEqual("NOT_EXECUTED", result["execution_status"])
        self.assertFalse(result["verification"]["google_ads_mutation_sent"])
        format_output.assert_called_once()

    @patch(
        "ads_mcp.mutation_engine.utils.format_output_value",
        side_effect=[
            {},
            {
                "results": [
                    {"resource_name": "customers/8448275903/campaignBudgets/1"}
                ]
            },
        ],
    )
    @patch("ads_mcp.mutation_engine._prepare_operation")
    @patch("ads_mcp.mutation_engine.utils.get_googleads_client")
    def test_live_call_validates_then_executes_without_confirmation(
        self, get_client, prepare, _format_output
    ):
        client = MagicMock()
        validation_request = MagicMock()
        execution_request = MagicMock()
        client.get_type.side_effect = [validation_request, execution_request]
        service = MagicMock()
        service.mutate.side_effect = [MagicMock(), MagicMock()]
        client.get_service.return_value = service
        client.enums.ResponseContentTypeEnum.MUTABLE_RESOURCE = 2
        get_client.return_value = client
        prepare.return_value = self._prepared_operation()

        with patch.dict(os.environ, self._environment(), clear=True):
            result = mutation_engine.create_resource(
                "8448275903",
                "CampaignBudget",
                {"amount_micros": 20000000},
            )

        self.assertEqual(2, service.mutate.call_count)
        self.assertTrue(validation_request.validate_only)
        self.assertFalse(execution_request.validate_only)
        self.assertEqual("EXECUTE", result["mode"])
        self.assertEqual("SUCCEEDED", result["execution_status"])
        self.assertNotIn("required_confirmation", result)
        self.assertTrue(
            result["verification"]["native_validate_only_completed"]
        )

    def test_public_write_signatures_are_direct(self):
        for function in (
            mutation_gateway.create_resource,
            mutation_gateway.update_resource,
            mutation_gateway.remove_resource,
            mutation_gateway.update_ad_group_ad_statuses,
            mutation_gateway.batch_mutate,
        ):
            parameters = inspect.signature(function).parameters
            self.assertIn("dry_run", parameters)
            self.assertNotIn("validate_only", parameters)
            self.assertNotIn("confirmation", parameters)

    def test_horizon_annotations_are_truthful(self):
        components = {
            component.name: component
            for key, component in mutations_mcp.local_provider._components.items()
            if key.startswith("tool:")
        }
        remove = components["remove_resource"].annotations
        self.assertFalse(remove.readOnlyHint)
        self.assertTrue(remove.destructiveHint)
        self.assertFalse(remove.idempotentHint)
        self.assertTrue(remove.openWorldHint)
        self.assertTrue(
            components["get_mutation_crud_status"].annotations.readOnlyHint
        )

    def test_legacy_gate_environment_is_ignored_but_scope_is_enforced(self):
        with patch.dict(os.environ, self._environment(), clear=True):
            status = mutation_gateway.get_mutation_crud_status()
            self.assertEqual("DIRECT", status["write_mode"])
            self.assertFalse(status["approval_workflow"])
            self.assertEqual(["8448275903"], status["allowed_customer_ids"])
            self.assertEqual(
                "8448275903",
                mutation_safety._validate_customer_scope("844-827-5903"),
            )

        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(
                ToolError, "ALLOWED_CUSTOMER_IDS is not configured"
            ):
                mutation_safety._validate_customer_scope("8448275903")

    def test_ad_group_status_builder_rejects_cross_customer_target(self):
        with self.assertRaisesRegex(ToolError, "does not match"):
            mutation_gateway._build_ad_group_ad_status_operations(
                "8448275903",
                ["customers/1111111111/adGroupAds/2~3"],
                "PAUSED",
            )

    @patch("ads_mcp.mutation_engine._prepare_operation")
    @patch("ads_mcp.mutation_engine.utils.get_googleads_client")
    def test_partial_failure_rejects_temporary_ids(self, get_client, prepare):
        client = MagicMock()
        get_client.return_value = client
        prepare.return_value = (
            MagicMock(),
            {
                "action": "create",
                "resource": "Campaign",
                "data": {"resource_name": "customers/8448275903/campaigns/-1"},
            },
        )
        with (
            patch.dict(os.environ, self._environment(), clear=True),
            self.assertRaisesRegex(ToolError, "temporary negative"),
        ):
            mutation_engine.batch_mutate(
                "8448275903",
                [{"action": "create", "resource": "Campaign", "data": {}}],
                dry_run=True,
                partial_failure=True,
            )
        client.get_service.assert_not_called()


if __name__ == "__main__":
    unittest.main()
