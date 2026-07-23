#!/usr/bin/env python

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

"""Common utilities used by the MCP server."""

from collections.abc import Mapping
import contextlib
from enum import Enum
import importlib.resources
import logging
import os
import re
import subprocess
from typing import Any
from unittest.mock import patch

from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.util import get_nested_attr
import google.auth
from google.protobuf.json_format import MessageToDict
from google.protobuf.message import Message as PbMessage
import proto

from ads_mcp.mcp_header_interceptor import MCPHeaderInterceptor

_GAQL_FILENAME = "gaql_resources.txt"
_PROTO_PLUS_RESERVED_FIELD_ALIASES = {"type_": "type"}
_INTEGER_STRING_PATTERN = re.compile(r"^-?\d+$")

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)

# Google Ads does not publish a separate read-only OAuth scope. Mutation access
# is constrained by the connector's tools and safety policy.
_ADS_SCOPE = "https://www.googleapis.com/auth/adwords"


@contextlib.contextmanager
def prevent_stdio_inheritance():
    """Prevents child processes from inheriting the parent's stdio handles."""
    original_popen = subprocess.Popen

    def safe_popen(*args, **kwargs):
        if kwargs.get("stdin") is None:
            kwargs["stdin"] = subprocess.DEVNULL
        return original_popen(*args, **kwargs)

    with patch("subprocess.Popen", new=safe_popen):
        yield


def _create_credentials() -> google.auth.credentials.Credentials:
    """Returns ADC credentials or the FastMCP access token when available."""
    from fastmcp.server.dependencies import get_access_token
    from google.oauth2.credentials import Credentials

    token_obj = get_access_token()
    if token_obj and token_obj.token:
        return Credentials(token=token_obj.token)

    with prevent_stdio_inheritance():
        credentials, _ = google.auth.default(scopes=[_ADS_SCOPE])
    return credentials


def _get_developer_token() -> str:
    """Returns the token materialized from MCP_CREDENTIALS."""
    dev_token = os.environ.get("GOOGLE_ADS_DEVELOPER_TOKEN")
    if dev_token is None:
        raise ValueError("MCP_CREDENTIALS.developer_token is not configured.")
    return dev_token


def _get_login_customer_id() -> str | None:
    """Returns the optional login customer materialized from MCP_CREDENTIALS."""
    return os.environ.get("GOOGLE_ADS_LOGIN_CUSTOMER_ID")


def _get_googleads_client() -> GoogleAdsClient:
    args = {
        "credentials": _create_credentials(),
        "developer_token": _get_developer_token(),
        "use_proto_plus": True,
    }

    login_customer_id = _get_login_customer_id()
    if login_customer_id:
        args["login_customer_id"] = login_customer_id

    return GoogleAdsClient(**args)


def get_googleads_service(serviceName: str) -> Any:
    return _get_googleads_client().get_service(
        serviceName, interceptors=[MCPHeaderInterceptor()]
    )


def get_googleads_type(typeName: str):
    return _get_googleads_client().get_type(typeName)


def get_googleads_client():
    return _get_googleads_client()


def _normalize_protobuf_json(value: Any, field_name: str | None = None) -> Any:
    """Normalizes nested protobuf JSON and generic connector values."""
    if isinstance(value, (proto.Message, PbMessage)):
        return _protobuf_message_to_dict(value)
    if isinstance(value, (Enum, proto.Enum)):
        return value.name
    if isinstance(value, Mapping):
        output: dict[str, Any] = {}
        for raw_key, nested in value.items():
            key = _PROTO_PLUS_RESERVED_FIELD_ALIASES.get(raw_key, raw_key)
            output[key] = _normalize_protobuf_json(nested, key)
        return output
    if isinstance(value, set):
        return [
            _normalize_protobuf_json(item, field_name)
            for item in sorted(value, key=repr)
        ]
    if isinstance(value, (list, tuple)):
        return [_normalize_protobuf_json(item, field_name) for item in value]
    if (
        isinstance(value, str)
        and field_name is not None
        and field_name.endswith("_micros")
        and _INTEGER_STRING_PATTERN.fullmatch(value)
    ):
        return int(value)
    return value


def _protobuf_message_to_dict(
    value: proto.Message | PbMessage,
) -> dict[str, Any]:
    message = value._pb if isinstance(value, proto.Message) else value
    raw = MessageToDict(
        message,
        preserving_proto_field_name=True,
        use_integers_for_enums=False,
    )
    return _normalize_protobuf_json(raw)


def format_output_value(value: Any) -> Any:
    if isinstance(value, (proto.Message, PbMessage)):
        return _protobuf_message_to_dict(value)
    if isinstance(value, (Enum, proto.Enum, Mapping, set, list, tuple)):
        return _normalize_protobuf_json(value)
    if hasattr(value, "__iter__") and not isinstance(
        value, (str, bytes, bytearray)
    ):
        return [format_output_value(item) for item in value]
    return value


def format_output_row(row: proto.Message, attributes):
    return {
        attr: format_output_value(get_nested_attr(row, attr))
        for attr in attributes
    }


def get_gaql_resources_filepath():
    package_root = importlib.resources.files("ads_mcp")
    return package_root.joinpath(_GAQL_FILENAME)
