import base64
import os
from pathlib import Path

encoded_credentials = os.environ.get(
    "GOOGLE_APPLICATION_CREDENTIALS_JSON_BASE64"
)

if not encoded_credentials:
    raise RuntimeError(
        "GOOGLE_APPLICATION_CREDENTIALS_JSON_BASE64 não configurada."
    )

credentials_path = Path("/tmp/google-ads-adc.json")
credentials_path.write_bytes(base64.b64decode(encoded_credentials))

os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(credentials_path)

from ads_mcp.coordinator import mcp