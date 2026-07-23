import base64
import json
import os
from pathlib import Path

_ADC_PATH = Path("/tmp/google-ads-adc.json")


def configure_deployment_credentials() -> Path | None:
    """Load Google Ads credentials for Horizon.

    ``MCP_CREDENTIALS`` accepts either the shared credential envelope or a raw
    Google credential object. Existing deployments that still use
    ``GOOGLE_APPLICATION_CREDENTIALS_JSON_BASE64`` remain functional while
    they are migrated.
    """
    encoded = os.getenv("MCP_CREDENTIALS", "").strip()
    source_name = "MCP_CREDENTIALS"
    if not encoded:
        encoded = os.getenv(
            "GOOGLE_APPLICATION_CREDENTIALS_JSON_BASE64", ""
        ).strip()
        source_name = "GOOGLE_APPLICATION_CREDENTIALS_JSON_BASE64"
    if not encoded:
        return None

    try:
        payload = json.loads(
            base64.b64decode(encoded, validate=True).decode("utf-8")
        )
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"{source_name} must be a base64-encoded JSON object."
        ) from exc

    if not isinstance(payload, dict):
        raise RuntimeError(f"{source_name} must decode to a JSON object.")

    developer_token = payload.get("developer_token")
    if not isinstance(developer_token, str) or not developer_token.strip():
        developer_token = os.getenv("GOOGLE_ADS_DEVELOPER_TOKEN", "")
    if not isinstance(developer_token, str) or not developer_token.strip():
        raise RuntimeError(
            "MCP_CREDENTIALS.developer_token must be a non-empty string."
        )
    os.environ["GOOGLE_ADS_DEVELOPER_TOKEN"] = developer_token.strip()

    login_customer_id = payload.get(
        "login_customer_id",
        os.getenv("GOOGLE_ADS_LOGIN_CUSTOMER_ID"),
    )
    if login_customer_id is not None:
        normalized = str(login_customer_id).replace("-", "").strip()
        if not normalized.isdigit():
            raise RuntimeError(
                "MCP_CREDENTIALS.login_customer_id must be numeric."
            )
        os.environ["GOOGLE_ADS_LOGIN_CUSTOMER_ID"] = normalized

    credentials = payload.get("google_credentials")
    if credentials is None and isinstance(payload.get("type"), str):
        credentials = payload
    if credentials is None:
        return None
    if not isinstance(credentials, dict):
        raise RuntimeError(
            "MCP_CREDENTIALS.google_credentials must be a JSON object."
        )

    raw = json.dumps(credentials, separators=(",", ":")).encode("utf-8")
    credentials_path = _ADC_PATH
    credentials_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = credentials_path.with_suffix(
        credentials_path.suffix + ".tmp"
    )
    temporary_path.write_bytes(raw)
    temporary_path.chmod(0o600)
    temporary_path.replace(credentials_path)
    credentials_path.chmod(0o600)
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(credentials_path)
    return credentials_path


configure_deployment_credentials()

from ads_mcp.coordinator import mcp
