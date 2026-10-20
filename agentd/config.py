"""Settings, read from the environment (and ~/agents/.env when run by hand)."""
import os
import socket
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


_load_dotenv(ROOT / ".env")

TOKEN = os.environ.get("DISCORD_TOKEN", "")
GUILD_ID = int(os.environ["DISCORD_GUILD_ID"]) if os.environ.get("DISCORD_GUILD_ID") else None
# The bot application's owner is always allowed; list extra Discord user IDs here.
OWNER_IDS = {int(x) for x in os.environ.get("AGENTD_OWNER_IDS", "").split(",") if x.strip()}

PORT = int(os.environ.get("AGENTD_PORT", "8765"))
# Web dashboard, reachable from this machine and your own Tailscale devices. 0 turns it off.
WEB_PORT = int(os.environ.get("AGENTD_WEB_PORT", "8780"))
STATE_DIR = Path(os.path.expanduser(os.environ.get("AGENTD_STATE_DIR", "~/.local/state/agentd")))
DEFAULT_DIR = Path(os.path.expanduser(os.environ.get("AGENTD_DEFAULT_DIR", "~"))).resolve()
DEFAULT_MODEL = os.environ.get("AGENTD_DEFAULT_MODEL") or None
DEFAULT_EFFORT = os.environ.get("AGENTD_DEFAULT_EFFORT") or None
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
HOSTNAME = socket.gethostname()

# Discord's upload cap for servers without boosts.
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
