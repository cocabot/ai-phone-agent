import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Deterministic settings for every test; never touch the real .env / network.
_tmp = tempfile.mkdtemp(prefix="vonage-agent-test-")
os.environ.update(
    {
        "DATA_DIR": _tmp,
        "TUNNEL": "none",
        "PUBLIC_URL": "https://unit-test.trycloudflare.com",
        "VONAGE_AUTO_SYNC": "0",
        "VONAGE_APPLICATION_ID": "app-123",
        "VONAGE_API_KEY": "key",
        "VONAGE_API_SECRET": "secret",
        "VONAGE_FROM_NUMBER": "+12015550123",
        "VONAGE_PRIVATE_KEY_PATH": str(Path(_tmp) / "missing.key"),
        "GEMINI_API_KEY": "",
        "WEBHOOK_TOKEN": "hooktoken",
        "AGENT_API_TOKEN": "",
        "CALL_ALLOWLIST": "",
        "ALLOWED_COUNTRY_CODES": "81",
        "BRIDGE": "",
    }
)
