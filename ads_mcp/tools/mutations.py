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

"""Registers direct Google Ads CRUD tools with FastMCP."""

from fastmcp import FastMCP
from fastmcp.tools import Tool
from mcp.types import ToolAnnotations

from ads_mcp import mutation_engine
from ads_mcp import mutation_gateway

mutations_mcp = FastMCP("mutations")

_READ_TOOLS = (
    mutation_engine.list_mutable_resources,
    mutation_engine.get_mutation_schema,
    mutation_gateway.get_mutation_crud_status,
)
_MUTATION_TOOLS = (
    (mutation_gateway.create_resource, False, False),
    (mutation_gateway.update_resource, True, True),
    (mutation_gateway.remove_resource, True, False),
    (mutation_gateway.update_ad_group_ad_statuses, True, True),
    (mutation_gateway.batch_mutate, True, False),
)

for function in _READ_TOOLS:
    mutations_mcp.add_tool(
        Tool.from_function(
            function,
            annotations=ToolAnnotations(
                title=function.__name__.replace("_", " ").title(),
                readOnlyHint=True,
                destructiveHint=False,
                idempotentHint=True,
                openWorldHint=True,
            ),
        )
    )

for function, destructive, idempotent in _MUTATION_TOOLS:
    mutations_mcp.add_tool(
        Tool.from_function(
            function,
            annotations=ToolAnnotations(
                title=function.__name__.replace("_", " ").title(),
                readOnlyHint=False,
                destructiveHint=destructive,
                idempotentHint=idempotent,
                openWorldHint=True,
            ),
        )
    )
