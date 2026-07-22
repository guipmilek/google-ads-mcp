#!/usr/bin/env python3
"""Apply the dedicated atomic AdGroupAd status endpoint change."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def replace_once(path: str, old: str, new: str) -> None:
    target = ROOT / path
    text = target.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"Expected exactly one match in {path}, found {count}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")


def append_once(path: str, marker: str, content: str) -> None:
    target = ROOT / path
    text = target.read_text(encoding="utf-8")
    if content.strip() in text:
        return
    count = text.count(marker)
    if count != 1:
        raise RuntimeError(f"Expected exactly one append marker in {path}, found {count}")
    target.write_text(text.replace(marker, content + "\n\n" + marker, 1), encoding="utf-8")


replace_once(
    "ads_mcp/mutation_policy.py",
    '_POLICY_VERSION = "mutation-safety-v4"',
    '_POLICY_VERSION = "mutation-safety-v5"',
)

replace_once(
    "ads_mcp/mutation_gateway.py",
    "import json\nimport secrets",
    "import json\nimport re\nimport secrets",
)
replace_once(
    "ads_mcp/mutation_gateway.py",
    "from typing import Any, Dict, List",
    "from typing import Any, Dict, List, Literal",
)
replace_once(
    "ads_mcp/mutation_gateway.py",
    '_POLICY_ENVELOPE_VERSION = 1\n_POLICY_ENVELOPE_KIND = "google-ads-mutation-policy-envelope"',
    '_POLICY_ENVELOPE_VERSION = 2\n_POLICY_ENVELOPE_KIND = "google-ads-mutation-policy-envelope"\n_DEFAULT_TOOL_CONTRACT_VERSION = 1\n_AD_GROUP_AD_STATUS_TOOL_CONTRACT_VERSION = 1\n_AD_GROUP_AD_STATUS_LIMIT = 10\n_AD_GROUP_AD_RESOURCE_NAME_PATTERN = re.compile(\n    r"^customers/(?P<customer_id>[0-9]+)/adGroupAds/"\n    r"(?P<ad_group_id>[0-9]+)~(?P<ad_id>[0-9]+)$"\n)',
)
replace_once(
    "ads_mcp/mutation_gateway.py",
    "    inner_confirmation: str,\n    policy_version: str,\n) -> str:",
    "    inner_confirmation: str,\n    policy_version: str,\n    endpoint: str,\n    tool_contract_version: int,\n) -> str:",
)
replace_once(
    "ads_mcp/mutation_gateway.py",
    '        "policy_version": policy_version,\n        "cid": customer_id,',
    '        "policy_version": policy_version,\n        "endpoint": endpoint,\n        "tool_contract_version": tool_contract_version,\n        "cid": customer_id,',
)
replace_once(
    "ads_mcp/mutation_gateway.py",
    "    config: MutationSafetyConfig,\n    endpoint: str,\n) -> str:",
    "    config: MutationSafetyConfig,\n    endpoint: str,\n    tool_contract_version: int = _DEFAULT_TOOL_CONTRACT_VERSION,\n) -> str:",
)
replace_once(
    "ads_mcp/mutation_gateway.py",
    '''        (\n            token_hash == operation_hash\n            and payload.get("hash") == operation_hash,\n            "CONFIRMATION_HASH_MISMATCH",\n            "Confirmation hash does not match the exact operation payload.",\n        ),''',
    '''        (\n            payload.get("endpoint") == endpoint,\n            "CONFIRMATION_ENDPOINT_MISMATCH",\n            "Confirmation endpoint does not match the executing tool.",\n        ),\n        (\n            payload.get("tool_contract_version") == tool_contract_version,\n            "CONFIRMATION_CONTRACT_VERSION_MISMATCH",\n            "Confirmation tool contract version does not match.",\n        ),\n        (\n            token_hash == operation_hash\n            and payload.get("hash") == operation_hash,\n            "CONFIRMATION_HASH_MISMATCH",\n            "Confirmation hash does not match the exact operation payload.",\n        ),''',
)
replace_once(
    "ads_mcp/mutation_gateway.py",
    "    correlation_id: str,\n) -> Dict[str, Any]:",
    "    correlation_id: str,\n    tool_contract_version: int,\n) -> Dict[str, Any]:",
)
replace_once(
    "ads_mcp/mutation_gateway.py",
    '        "endpoint": endpoint,\n        "correlation_id": correlation_id,',
    '        "endpoint": endpoint,\n        "tool_contract_version": tool_contract_version,\n        "correlation_id": correlation_id,',
)
replace_once(
    "ads_mcp/mutation_gateway.py",
    "    confirmation: str | None,\n) -> Dict[str, Any]:",
    "    confirmation: str | None,\n    tool_contract_version: int = _DEFAULT_TOOL_CONTRACT_VERSION,\n) -> Dict[str, Any]:",
)
replace_once(
    "ads_mcp/mutation_gateway.py",
    "            config=config,\n            endpoint=endpoint,\n        )",
    "            config=config,\n            endpoint=endpoint,\n            tool_contract_version=tool_contract_version,\n        )",
)
replace_once(
    "ads_mcp/mutation_gateway.py",
    "        decision=decision,\n        correlation_id=correlation_id,\n    )",
    "        decision=decision,\n        correlation_id=correlation_id,\n        tool_contract_version=tool_contract_version,\n    )",
)
replace_once(
    "ads_mcp/mutation_gateway.py",
    "            inner_confirmation=response[\"required_confirmation\"],\n            policy_version=config.policy_version,\n        )",
    "            inner_confirmation=response[\"required_confirmation\"],\n            policy_version=config.policy_version,\n            endpoint=endpoint,\n            tool_contract_version=tool_contract_version,\n        )",
)
replace_once(
    "ads_mcp/mutation_gateway.py",
    '    endpoint: str = "mutations_batch_mutate",\n) -> Dict[str, Any]:',
    '    endpoint: str = "mutations_batch_mutate",\n    tool_contract_version: int = _DEFAULT_TOOL_CONTRACT_VERSION,\n) -> Dict[str, Any]:',
)
replace_once(
    "ads_mcp/mutation_gateway.py",
    '        "confirmation_issued": False,\n        "runtime": runtime_metadata(endpoint=endpoint),',
    '        "confirmation_issued": False,\n        "tool_contract_version": tool_contract_version,\n        "runtime": runtime_metadata(endpoint=endpoint),',
)

endpoint_code = '''def _build_ad_group_ad_status_operations(\n    customer_id: str,\n    resource_names: List[str],\n    status: Literal["ENABLED", "PAUSED"],\n) -> tuple[str, List[Dict[str, Any]]]:\n    """Validates the constrained tool contract and builds atomic operations."""\n    normalized_customer_id = mutation_safety._normalize_customer_id(customer_id)\n    if not isinstance(resource_names, list):\n        raise ToolError("resource_names must be a list.")\n    if not resource_names:\n        raise ToolError("At least one AdGroupAd resource_name is required.")\n    if len(resource_names) > _AD_GROUP_AD_STATUS_LIMIT:\n        raise ToolError(\n            "This tool accepts at most "\n            f"{_AD_GROUP_AD_STATUS_LIMIT} AdGroupAd resource names."\n        )\n    if len(set(resource_names)) != len(resource_names):\n        raise ToolError("Duplicate AdGroupAd resource names are not allowed.")\n    if not isinstance(status, str) or status not in {"ENABLED", "PAUSED"}:\n        raise ToolError("status must be exactly ENABLED or PAUSED.")\n\n    operations: list[Dict[str, Any]] = []\n    for resource_name in resource_names:\n        if not isinstance(resource_name, str):\n            raise ToolError("Every resource_name must be a string.")\n        match = _AD_GROUP_AD_RESOURCE_NAME_PATTERN.fullmatch(resource_name)\n        if match is None:\n            raise ToolError(\n                "Invalid AdGroupAd resource_name. Expected "\n                "customers/{customer_id}/adGroupAds/{ad_group_id}~{ad_id}."\n            )\n        if match.group("customer_id") != normalized_customer_id:\n            raise ToolError(\n                "AdGroupAd resource_name customer does not match customer_id."\n            )\n        operations.append(\n            {\n                "action": "update",\n                "resource": "AdGroupAd",\n                "data": {\n                    "resource_name": resource_name,\n                    "status": status,\n                },\n                "update_mask": ["status"],\n            }\n        )\n    return normalized_customer_id, operations\n\n\ndef update_ad_group_ad_statuses(\n    customer_id: str,\n    resource_names: List[str],\n    status: Literal["ENABLED", "PAUSED"],\n    validate_only: bool = True,\n    confirmation: str | None = None,\n) -> Dict[str, Any]:\n    """Atomically updates only status for 1-10 AdGroupAd resources.\n\n    The tool always uses ``partial_failure=false`` and internally constructs an\n    update mask containing only ``status``. It cannot create or remove resources\n    and cannot update any other AdGroupAd field.\n    """\n    normalized_customer_id, operations = _build_ad_group_ad_status_operations(\n        customer_id, resource_names, status\n    )\n    return _invoke_mutation(\n        endpoint="mutations_update_ad_group_ad_statuses",\n        customer_id=normalized_customer_id,\n        operations=operations,\n        validate_only=validate_only,\n        partial_failure=False,\n        confirmation=confirmation,\n        tool_contract_version=_AD_GROUP_AD_STATUS_TOOL_CONTRACT_VERSION,\n    )\n'''
append_once(
    "ads_mcp/mutation_gateway.py",
    "def batch_mutate(\n",
    endpoint_code,
)

replace_once(
    "ads_mcp/tools/mutations.py",
    "    mutation_gateway.remove_resource,\n    mutation_gateway.batch_mutate,",
    "    mutation_gateway.remove_resource,\n    mutation_gateway.update_ad_group_ad_statuses,\n    mutation_gateway.batch_mutate,",
)

TEST_CONTENT = r'''# Copyright 2026 Google LLC.\n#\n# Licensed under the Apache License, Version 2.0 (the "License");\n# you may not use this file except in compliance with the License.\n# You may obtain a copy of the License at\n#\n#      http://www.apache.org/licenses/LICENSE-2.0\n#\n# Unless required by applicable law or agreed to in writing, software\n# distributed under the License is distributed on an "AS IS" BASIS,\n# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.\n# See the License for the specific language governing permissions and\n# limitations under the License.\n\n"""Tests for the constrained atomic AdGroupAd status endpoint."""\n\nimport json\nimport os\nimport unittest\nfrom unittest.mock import patch\n\nfrom fastmcp.exceptions import ToolError\n\nfrom ads_mcp import mutation_gateway\nfrom ads_mcp import mutation_safety\nfrom ads_mcp.mutation_policy import classify_operations\n\n\nclass TestAdGroupAdStatusEndpoint(unittest.TestCase):\n    CUSTOMER_ID = "8448275903"\n    RESOURCE_NAMES = [\n        "customers/8448275903/adGroupAds/127998519245~817845836549",\n        "customers/8448275903/adGroupAds/133301803367~817845836552",\n    ]\n    OPERATION_HASH = "d" * 32\n\n    def setUp(self):\n        mutation_safety._clear_confirmation_replay_cache_for_tests()\n\n    def _environment(self, **overrides):\n        values = {\n            "GOOGLE_ADS_CONFIRMATION_SECRET": "s" * 64,\n            "GOOGLE_ADS_MUTATIONS_ENABLED": "true",\n            "GOOGLE_ADS_ALLOWED_CUSTOMER_IDS": self.CUSTOMER_ID,\n        }\n        values.update(overrides)\n        return patch.dict(os.environ, values, clear=True)\n\n    def _operations(self, status="ENABLED", names=None):\n        _, operations = mutation_gateway._build_ad_group_ad_status_operations(\n            self.CUSTOMER_ID, names or self.RESOURCE_NAMES, status\n        )\n        return operations\n\n    def _context(self, status="ENABLED", names=None):\n        operations = self._operations(status=status, names=names)\n        return (\n            self.CUSTOMER_ID,\n            operations,\n            self.OPERATION_HASH,\n            classify_operations(operations, False),\n        )\n\n    def _validation_response(self, status="ENABLED", names=None):\n        operations = self._operations(status=status, names=names)\n        receipt = mutation_safety._issue_validation_receipt(\n            self.CUSTOMER_ID, operations, self.OPERATION_HASH, False\n        )\n        return {\n            "operation_hash": self.OPERATION_HASH,\n            "operations": operations,\n            "required_confirmation": receipt["confirmation"],\n            "mode": "VALIDATE_ONLY",\n        }\n\n    def _execution_response(self, status="ENABLED", names=None):\n        return {\n            "operation_hash": self.OPERATION_HASH,\n            "operations": self._operations(status=status, names=names),\n            "required_confirmation": None,\n            "mode": "EXECUTE",\n            "execution_status": "SUCCEEDED",\n        }\n\n    def _assert_reason(self, callable_, reason_code):\n        with self.assertRaises(ToolError) as raised:\n            callable_()\n        payload = json.loads(str(raised.exception))\n        self.assertEqual(payload["reason_code"], reason_code)\n        return payload\n\n    def test_accepts_one_two_and_ten_resources(self):\n        for count in (1, 2, 10):\n            names = [\n                f"customers/{self.CUSTOMER_ID}/adGroupAds/{100 + index}~{200 + index}"\n                for index in range(count)\n            ]\n            customer, operations = (\n                mutation_gateway._build_ad_group_ad_status_operations(\n                    self.CUSTOMER_ID, names, "ENABLED"\n                )\n            )\n            self.assertEqual(customer, self.CUSTOMER_ID)\n            self.assertEqual(len(operations), count)\n            self.assertTrue(\n                all(operation["update_mask"] == ["status"] for operation in operations)\n            )\n\n    def test_rejects_empty_more_than_ten_and_duplicates(self):\n        invalid_lists = (\n            [],\n            [\n                f"customers/{self.CUSTOMER_ID}/adGroupAds/{index}~{index}"\n                for index in range(11)\n            ],\n            [self.RESOURCE_NAMES[0], self.RESOURCE_NAMES[0]],\n        )\n        for names in invalid_lists:\n            with self.subTest(names=len(names)):\n                with self.assertRaises(ToolError):\n                    mutation_gateway._build_ad_group_ad_status_operations(\n                        self.CUSTOMER_ID, names, "ENABLED"\n                    )\n\n    def test_rejects_customer_type_name_and_status_mismatches(self):\n        cases = (\n            (\n                ["customers/1111111111/adGroupAds/1~2"],\n                "ENABLED",\n            ),\n            (\n                [f"customers/{self.CUSTOMER_ID}/campaigns/1"],\n                "ENABLED",\n            ),\n            (["not-a-resource-name"], "ENABLED"),\n            (self.RESOURCE_NAMES, "REMOVED"),\n            (self.RESOURCE_NAMES, "enabled"),\n        )\n        for names, status in cases:\n            with self.subTest(names=names, status=status):\n                with self.assertRaises(ToolError):\n                    mutation_gateway._build_ad_group_ad_status_operations(\n                        self.CUSTOMER_ID, names, status\n                    )\n\n    def test_enabled_requires_enable_gate_before_engine(self):\n        context = self._context()\n        with (\n            self._environment(),\n            patch.object(\n                mutation_gateway, "_canonical_context", return_value=context\n            ),\n            patch.object(\n                mutation_gateway.mutation_engine, "_run_mutations"\n            ) as run_mutations,\n        ):\n            payload = self._assert_reason(\n                lambda: mutation_gateway.update_ad_group_ad_statuses(\n                    self.CUSTOMER_ID,\n                    self.RESOURCE_NAMES,\n                    "ENABLED",\n                    validate_only=False,\n                    confirmation="unused",\n                ),\n                "ENABLE_GATE_DISABLED",\n            )\n        run_mutations.assert_not_called()\n        self.assertEqual(payload["required_gate"], "GOOGLE_ADS_ALLOW_ENABLE")\n\n    def test_preflight_is_atomic_and_does_not_execute(self):\n        context = self._context()\n        with (\n            self._environment(GOOGLE_ADS_ALLOW_ENABLE="true"),\n            patch.object(\n                mutation_gateway, "_canonical_context", return_value=context\n            ),\n            patch.object(\n                mutation_gateway.mutation_engine,\n                "_run_mutations",\n                return_value=self._validation_response(),\n            ) as run_mutations,\n        ):\n            response = mutation_gateway.update_ad_group_ad_statuses(\n                self.CUSTOMER_ID, self.RESOURCE_NAMES, "ENABLED"\n            )\n        run_mutations.assert_called_once_with(\n            self.CUSTOMER_ID,\n            context[1],\n            validate_only=True,\n            partial_failure=False,\n            confirmation=None,\n        )\n        self.assertEqual(\n            response["security_policy"]["classification"]["operation_count"], 2\n        )\n        self.assertEqual(\n            response["security_policy"]["classification"]["fields"], ["status"]\n        )\n        self.assertEqual(\n            response["security_policy"]["classification"]["resource_types"],\n            ["AdGroupAd"],\n        )\n        self.assertTrue(\n            response["required_confirmation"].startswith("ENABLE ")\n        )\n\n    def test_two_resources_execute_in_one_atomic_engine_call(self):\n        context = self._context()\n        with (\n            self._environment(GOOGLE_ADS_ALLOW_ENABLE="true"),\n            patch.object(\n                mutation_gateway, "_canonical_context", return_value=context\n            ),\n            patch.object(\n                mutation_gateway.mutation_engine,\n                "_run_mutations",\n                return_value=self._validation_response(),\n            ),\n        ):\n            validation = mutation_gateway.update_ad_group_ad_statuses(\n                self.CUSTOMER_ID, self.RESOURCE_NAMES, "ENABLED"\n            )\n\n        with (\n            self._environment(GOOGLE_ADS_ALLOW_ENABLE="true"),\n            patch.object(\n                mutation_gateway, "_canonical_context", return_value=context\n            ),\n            patch.object(\n                mutation_gateway.mutation_engine,\n                "_run_mutations",\n                return_value=self._execution_response(),\n            ) as run_mutations,\n        ):\n            result = mutation_gateway.update_ad_group_ad_statuses(\n                self.CUSTOMER_ID,\n                self.RESOURCE_NAMES,\n                "ENABLED",\n                validate_only=False,\n                confirmation=validation["required_confirmation"],\n            )\n        self.assertEqual(result["execution_status"], "SUCCEEDED")\n        self.assertEqual(run_mutations.call_count, 1)\n        self.assertEqual(len(run_mutations.call_args.args[1]), 2)\n        self.assertFalse(run_mutations.call_args.kwargs["partial_failure"])\n\n    def test_paused_does_not_require_enable_gate(self):\n        context = self._context(status="PAUSED")\n        with (\n            self._environment(),\n            patch.object(\n                mutation_gateway, "_canonical_context", return_value=context\n            ),\n            patch.object(\n                mutation_gateway.mutation_engine,\n                "_run_mutations",\n                return_value=self._validation_response(status="PAUSED"),\n            ),\n        ):\n            validation = mutation_gateway.update_ad_group_ad_statuses(\n                self.CUSTOMER_ID, self.RESOURCE_NAMES, "PAUSED"\n            )\n        self.assertTrue(validation["required_confirmation"].startswith("EXECUTE "))\n\n        with (\n            self._environment(),\n            patch.object(\n                mutation_gateway, "_canonical_context", return_value=context\n            ),\n            patch.object(\n                mutation_gateway.mutation_engine,\n                "_run_mutations",\n                return_value=self._execution_response(status="PAUSED"),\n            ) as run_mutations,\n        ):\n            mutation_gateway.update_ad_group_ad_statuses(\n                self.CUSTOMER_ID,\n                self.RESOURCE_NAMES,\n                "PAUSED",\n                validate_only=False,\n                confirmation=validation["required_confirmation"],\n            )\n        run_mutations.assert_called_once()\n\n    def test_live_execution_requires_confirmation(self):\n        context = self._context()\n        with (\n            self._environment(GOOGLE_ADS_ALLOW_ENABLE="true"),\n            patch.object(\n                mutation_gateway, "_canonical_context", return_value=context\n            ),\n            patch.object(\n                mutation_gateway.mutation_engine, "_run_mutations"\n            ) as run_mutations,\n        ):\n            self._assert_reason(\n                lambda: mutation_gateway.update_ad_group_ad_statuses(\n                    self.CUSTOMER_ID,\n                    self.RESOURCE_NAMES,\n                    "ENABLED",\n                    validate_only=False,\n                ),\n                "CONFIRMATION_REQUIRED",\n            )\n        run_mutations.assert_not_called()\n\n    def test_confirmation_is_bound_to_endpoint(self):\n        context = self._context()\n        with (\n            self._environment(GOOGLE_ADS_ALLOW_ENABLE="true"),\n            patch.object(\n                mutation_gateway, "_canonical_context", return_value=context\n            ),\n            patch.object(\n                mutation_gateway.mutation_engine,\n                "_run_mutations",\n                return_value=self._validation_response(),\n            ),\n        ):\n            batch_validation = mutation_gateway.batch_mutate(\n                self.CUSTOMER_ID, context[1], validate_only=True\n            )\n\n        with (\n            self._environment(GOOGLE_ADS_ALLOW_ENABLE="true"),\n            patch.object(\n                mutation_gateway, "_canonical_context", return_value=context\n            ),\n            patch.object(\n                mutation_gateway.mutation_engine, "_run_mutations"\n            ) as run_mutations,\n        ):\n            self._assert_reason(\n                lambda: mutation_gateway.update_ad_group_ad_statuses(\n                    self.CUSTOMER_ID,\n                    self.RESOURCE_NAMES,\n                    "ENABLED",\n                    validate_only=False,\n                    confirmation=batch_validation["required_confirmation"],\n                ),\n                "CONFIRMATION_ENDPOINT_MISMATCH",\n            )\n        run_mutations.assert_not_called()\n\n    def test_confirmation_is_bound_to_contract_version(self):\n        context = self._context()\n        with (\n            self._environment(GOOGLE_ADS_ALLOW_ENABLE="true"),\n            patch.object(\n                mutation_gateway, "_canonical_context", return_value=context\n            ),\n            patch.object(\n                mutation_gateway.mutation_engine,\n                "_run_mutations",\n                return_value=self._validation_response(),\n            ),\n        ):\n            validation = mutation_gateway.update_ad_group_ad_statuses(\n                self.CUSTOMER_ID, self.RESOURCE_NAMES, "ENABLED"\n            )\n            config = mutation_gateway.MutationSafetyConfig.from_environment()\n            self._assert_reason(\n                lambda: mutation_gateway._verify_policy_envelope(\n                    confirmation=validation["required_confirmation"],\n                    customer_id=self.CUSTOMER_ID,\n                    operation_hash=self.OPERATION_HASH,\n                    classification=context[3],\n                    config=config,\n                    endpoint="mutations_update_ad_group_ad_statuses",\n                    tool_contract_version=2,\n                ),\n                "CONFIRMATION_CONTRACT_VERSION_MISMATCH",\n            )\n\n    def test_diagnostic_does_not_issue_confirmation_or_call_api(self):\n        operations = self._operations()\n        with patch.object(\n            mutation_gateway.mutation_engine, "_run_mutations"\n        ) as run_mutations:\n            result = mutation_gateway.get_mutation_safety_status(\n                operations=operations,\n                partial_failure=False,\n                validate_only=False,\n                endpoint="mutations_update_ad_group_ad_statuses",\n                tool_contract_version=1,\n            )\n        run_mutations.assert_not_called()\n        self.assertFalse(result["api_call_performed"])\n        self.assertFalse(result["confirmation_issued"])\n\n\nif __name__ == "__main__":\n    unittest.main()\n'''
(ROOT / "tests/ad_group_ad_status_endpoint_test.py").write_text(
    TEST_CONTENT.replace("\\n", "\n"), encoding="utf-8"
)
