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

"""Targeted create normalization for language and location criteria."""

from __future__ import annotations

import copy
import sys
from typing import Any, Dict

from fastmcp.exceptions import ToolError

from ads_mcp import mutation_safety

_IMPLICIT_STATUS_CRITERION_TYPES = frozenset({"language", "location"})
_PATCH_MARKER = "_campaign_criterion_status_guard_installed"


def _uses_implicit_status(
    data: Dict[str, Any], resource_descriptor: Any
) -> bool:
    """Returns whether CampaignCriterion.status must be omitted on create."""
    if getattr(resource_descriptor, "name", None) != "CampaignCriterion":
        return False
    return any(
        criterion_type in data
        for criterion_type in _IMPLICIT_STATUS_CRITERION_TYPES
    )


def install_campaign_criterion_status_guard() -> None:
    """Prevents invalid status injection for language/location criteria."""
    current_guard = mutation_safety._apply_create_status_guard
    if getattr(current_guard, _PATCH_MARKER, False):
        return

    def guarded_create_status(
        data: Dict[str, Any], resource_descriptor: Any
    ) -> Dict[str, Any]:
        prepared = copy.deepcopy(data)
        if _uses_implicit_status(prepared, resource_descriptor):
            if "status" in prepared:
                raise ToolError(
                    "CampaignCriterion language and location creates must "
                    "omit status because Google Ads rejects status for these "
                    "criterion types."
                )
            return prepared
        return current_guard(prepared, resource_descriptor)

    setattr(guarded_create_status, _PATCH_MARKER, True)
    mutation_safety._apply_create_status_guard = guarded_create_status

    mutation_engine = sys.modules.get("ads_mcp.mutation_engine")
    if mutation_engine is not None:
        mutation_engine._apply_create_status_guard = guarded_create_status
