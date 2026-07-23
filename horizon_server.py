import base64
import json
import os
from pathlib import Path

_ADC_PATH = Path("/tmp/google-ads-adc.json")


def configure_deployment_credentials() -> Path | None:
    """Load the shared MCP_CREDENTIALS envelope for Horizon.

    Workload identity or an already configured GOOGLE_APPLICATION_CREDENTIALS
    path remains supported when google_credentials is absent.
    """
    encoded = os.getenv("MCP_CREDENTIALS", "").strip()
    if not encoded:
        return None

    try:
        envelope = json.loads(
            base64.b64decode(encoded, validate=True).decode("utf-8")
        )
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            "MCP_CREDENTIALS must be a base64-encoded JSON object."
        ) from exc

    if not isinstance(envelope, dict):
        raise RuntimeError("MCP_CREDENTIALS must decode to a JSON object.")

    developer_token = envelope.get("developer_token")
    if not isinstance(developer_token, str) or not developer_token.strip():
        raise RuntimeError(
            "MCP_CREDENTIALS.developer_token must be a non-empty string."
        )
    os.environ["GOOGLE_ADS_DEVELOPER_TOKEN"] = developer_token.strip()

    login_customer_id = envelope.get("login_customer_id")
    if login_customer_id is not None:
        normalized = str(login_customer_id).replace("-", "").strip()
        if not normalized.isdigit():
            raise RuntimeError(
                "MCP_CREDENTIALS.login_customer_id must be numeric."
            )
        os.environ["GOOGLE_ADS_LOGIN_CUSTOMER_ID"] = normalized

    credentials = envelope.get("google_credentials")
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
