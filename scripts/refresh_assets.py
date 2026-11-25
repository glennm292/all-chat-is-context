"""Refresh this skill's copy of the app (assets/app) from a workspace where Context is installed and changed.

For the skill's maintainer: run from that workspace's root after changing system/apps/context, before
publishing a new version. The port the workspace serves on goes back to the placeholder the installer fills in.
"""

import re
import shutil
import sys
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parent.parent
TARGET = SKILL_ROOT / "assets" / "app"
PORT_DEFAULTS = {
    "runner.py": r'PORT = int\(os\.environ\.get\("CONTEXT_PORT", "\d+"\)\)',
    "live.py": r"os\.environ\.get\('CONTEXT_PORT', '\d+'\)",
}
PLACEHOLDERS = {
    "runner.py": 'PORT = int(os.environ.get("CONTEXT_PORT", "__PORT__"))',
    "live.py": "os.environ.get('CONTEXT_PORT', '__PORT__')",
}


def main() -> None:
    source = Path.cwd() / "system" / "apps" / "context"
    if not source.is_dir():
        sys.exit(f"No Context app at {source}")
    if TARGET.exists():
        shutil.rmtree(TARGET)
    shutil.copytree(source, TARGET, ignore=shutil.ignore_patterns("__pycache__", "*.egg-info", ".pytest_cache", ".venv", "uv.lock"))
    for name, pattern in PORT_DEFAULTS.items():
        path = TARGET / "src" / "context" / name
        text, count = re.subn(pattern, PLACEHOLDERS[name], path.read_text())
        if count != 1:
            sys.exit(f"Could not put the port placeholder back in {name}")
        path.write_text(text)
    print(f"Refreshed {TARGET} from {source}")


if __name__ == "__main__":
    main()
