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

"""Configuration management for the Google Ads MCP server."""

import os
import importlib.resources
from typing import Any, Dict
import yaml
import logging

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_FILE = "tools_config.yaml"
CONFIG_PATH_ENV_VAR = "GOOGLE_ADS_MCP_TOOLS_CONFIG"
ALL_CATEGORIES = ["customers", "search", "metadata", "mutations"]


class ToolsConfig:
    """Manages tool registration configuration parsed from YAML."""

    def __init__(self, config_dict: Dict[str, Any] | None = None):
        self._config = config_dict or {}

    @classmethod
    def _resolve_config_path(cls, filepath: str | None) -> str | None:
        """Resolves the explicit, local, or bundled configuration path."""
        explicit = filepath or os.environ.get(CONFIG_PATH_ENV_VAR)
        if explicit:
            if not os.path.exists(explicit):
                raise FileNotFoundError(
                    f"Tools configuration file '{explicit}' not found."
                )
            return explicit

        if os.path.exists(DEFAULT_CONFIG_FILE):
            return DEFAULT_CONFIG_FILE

        bundled = importlib.resources.files("ads_mcp").joinpath(
            DEFAULT_CONFIG_FILE
        )
        if bundled.is_file():
            logger.info(
                "No local '%s' found; using the bundled default configuration.",
                DEFAULT_CONFIG_FILE,
            )
            return str(bundled)
        return None

    @classmethod
    def load(cls, filepath: str | None = None) -> "ToolsConfig":
        """Loads a YAML configuration or enables all known namespaces."""
        resolved = cls._resolve_config_path(filepath)
        if resolved is None:
            logger.warning(
                "No tools configuration file found; enabling all default tool "
                "namespaces (%s).",
                ", ".join(ALL_CATEGORIES),
            )
            return cls()

        try:
            with open(resolved, "r") as file:
                data = yaml.safe_load(file)
                if not isinstance(data, dict):
                    raise ValueError(
                        "Configuration root must be a YAML mapping/dictionary"
                    )
                return cls(data)
        except Exception as exc:
            raise ValueError(
                f"Failed to parse configuration file '{resolved}': {exc}"
            ) from exc

    def is_namespace_enabled(self, category: str) -> bool:
        """Determines if a tool category or namespace is enabled."""
        namespaces = self._config.get("namespaces", {})
        if not namespaces:
            return category in ALL_CATEGORIES

        category_config = namespaces.get(category)
        if category_config is None:
            return False
        if isinstance(category_config, bool):
            return category_config
        if isinstance(category_config, str):
            return True
        if isinstance(category_config, dict):
            return category_config.get("enabled", True)
        return False

    def get_namespace_prefix(self, category: str) -> str | None:
        """Returns the configured namespace prefix."""
        namespaces = self._config.get("namespaces", {})
        if not namespaces:
            return category

        category_config = namespaces.get(category)
        if isinstance(category_config, str):
            return category_config
        if isinstance(category_config, dict):
            if "prefix" in category_config:
                return category_config["prefix"]
            return category
        if category_config is True:
            return category
        return None

    def is_tool_enabled(self, category: str, tool_name: str) -> bool:
        """Determines if one tool inside a category is enabled."""
        if not self.is_namespace_enabled(category):
            return False

        namespaces = self._config.get("namespaces", {})
        if not namespaces:
            return True

        category_config = namespaces.get(category)
        if not isinstance(category_config, dict):
            return True

        enabled_tools = category_config.get("enabled_tools")
        if enabled_tools is None:
            return True

        if isinstance(enabled_tools, list):
            for item in enabled_tools:
                if isinstance(item, dict) and tool_name in item:
                    return bool(item[tool_name])
                if isinstance(item, str) and item == tool_name:
                    return True
            return False
        return True
