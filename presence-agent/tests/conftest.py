from pathlib import Path
import sys


AGENT_ROOT = Path(__file__).resolve().parents[1]
SERVER_ROOT = AGENT_ROOT.parent / "server" / "main" / "xiaozhi-server"

for path in (AGENT_ROOT, SERVER_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
