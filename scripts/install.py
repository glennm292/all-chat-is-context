"""Install (or update) the Context app in a Minds workspace, and report on it.

Subcommands (run from the workspace root):

- ``check``    read-only: is this a workspace Context can run in (a chat app that starts and
               takes messages, the routines command), is Context already here, and how much
               conversation the first sort will read, with its estimated cost.
- ``install``  copy the app in (keeping an existing install's port and all of its data), write its
               program, install and start it, and add the sorting routine. Nothing is sorted until
               the routine's first run, which starts at once.
- ``verify``   the app answers, its API describes itself, its own tests pass, and the routine is there.
- ``status``   where the app is, its port, whether it runs, and how many topics it holds.
"""

import argparse
import importlib
import json
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL_ROOT = HERE.parent
APP_TEMPLATE = SKILL_ROOT / "assets" / "app"
APP_NAME = "context"
PACKAGE = "context"
ROUTINE = "context-sorting"
PORT_PLACEHOLDER = "__PORT__"
FIRST_PORT = 8080
CHAT_API = "http://127.0.0.1:8010"
CANONICAL_WORKSPACE = Path("/home/user/workspace")
# Ports the workspace's own services hold without declaring them where we scan.
RESERVED_PORTS = {8000, 8010, 8030, 8040, 8081}
# Measured on a real workspace: sorting 710 messages from scratch and writing 9 topics cost $0.77.
DOLLARS_PER_MESSAGE = 0.0011
# The chat app routes Context drives; a chat app without them cannot carry a topic's conversation.
CHAT_ROUTES = ["/api/chats/create", "/api/chats/{chat_id}/message", "/api/agents"]

# Where the workspace keeps an app's own venv. Its presence marks the current layout, where each app is
# a standalone uv project with its own lock and venv; an older workspace installs apps as uv tools.
# own-process: other-tree -- the installer runs the target workspace's own link helper in that workspace, which
# may be on another release than the checkout this script runs from
VENV_LINKS = Path("system/scripts/workspace_bare_scripts/venv_links.py")

LEGACY_COMMAND = (
    'bash -c "python3 system/scripts/forward_port.py --manifest system/apps/{package}/app.toml'
    ' --url http://localhost:{port} && exec {name}"'
)
STANDALONE_COMMAND = (
    'bash -c "python3 system/scripts/workspace_bare_scripts/venv_links.py ensure-project system/apps/{package}'
    " && uv sync --locked --project system/apps/{package}"
    " && python3 system/scripts/forward_port.py --manifest system/apps/{package}/app.toml --url http://localhost:{port}"
    ' && exec system/apps/{package}/.venv/bin/{name}"'
)

PROGRAM_CONF = """[program:{name}]
command=python3 system/services/oom_priority/bin/oom_tag_service.py user {command}
directory={workspace}
autostart=true
autorestart=true
startsecs=30
startretries=5
stopasgroup=true
killasgroup=true
stdout_logfile=/var/log/supervisor/{name}-stdout.log
stderr_logfile=/var/log/supervisor/{name}-stderr.log
stdout_logfile_maxbytes=10MB
stderr_logfile_maxbytes=10MB
stdout_logfile_backups=3
stderr_logfile_backups=3
"""


class InstallError(Exception):
    """A step of the install could not be completed."""


def _say(line: str) -> None:
    sys.stderr.write(line + "\n")


def _app_dir(workspace: Path) -> Path:
    return workspace / "system" / "apps" / PACKAGE


def _conf_path(workspace: Path) -> Path:
    return workspace / "system" / "supervisord.conf.d" / f"{APP_NAME}.conf"


def _data_dir(workspace: Path) -> Path:
    return workspace / "data" / ".apps" / APP_NAME


def standalone_layout(workspace: Path) -> bool:
    """Whether apps here are standalone uv projects with their own venvs (the current layout)."""
    return (workspace / VENV_LINKS).is_file()


def check_workspace(workspace: Path) -> None:
    # own-process: other-tree -- names the target workspace's port helper, which the app's program runs there,
    # to check that workspace has it before writing a program that needs it
    for required in ("system/apps", "system/supervisord.conf.d", "system/scripts/forward_port.py", "system/routines"):
        if not (workspace / required).exists():
            raise InstallError(f"{workspace} does not look like a Minds workspace (no {required})")


def chat_routes_missing() -> list[str]:
    """The chat app routes Context needs that this workspace's chat app does not offer."""
    try:
        with urllib.request.urlopen(CHAT_API + "/api/schema", timeout=10) as response:
            paths = json.loads(response.read())["paths"]
    except (urllib.error.URLError, OSError, ValueError, KeyError):
        return ["the chat app's API document (is the chat app running on 8010?)"]
    return [route for route in CHAT_ROUTES if route not in paths]


def history_size(workspace: Path) -> tuple[int, int]:
    """Conversations and messages the first sort will read, counted the way Context reads them."""
    # import-path: other-tree -- the check runs before Context is installed anywhere, so the reader comes from the
    # app copy this skill bundles, whose path is known only once the skill is found on disk
    sys.path.insert(0, str(APP_TEMPLATE / "src"))
    transcripts = importlib.import_module("context.transcripts")

    count, messages = 0, 0
    for _, _, path in transcripts.transcript_files():
        found = len(transcripts.read_messages(path))
        if found:
            count, messages = count + 1, messages + found
    return count, messages


def existing_port(workspace: Path) -> int | None:
    try:
        match = re.search(r"--url http://localhost:(\d+)", _conf_path(workspace).read_text(encoding="utf-8"))
    except OSError:
        return None
    return int(match.group(1)) if match else None


def used_ports(workspace: Path) -> set[int]:
    ports = set(RESERVED_PORTS)
    texts = [p.read_text(encoding="utf-8") for p in (workspace / "system" / "supervisord.conf.d").glob("*.conf")]
    for extra in (workspace / "system" / "supervisord.conf", workspace / "data" / ".state" / "apps.toml"):
        if extra.exists():
            texts.append(extra.read_text(encoding="utf-8"))
    for text in texts:
        ports.update(int(p) for p in re.findall(r"(?:localhost|127\.0\.0\.1):(\d{4,5})", text))
    return ports


def _listening(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.3)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def pick_port(workspace: Path, requested: int | None) -> int:
    current = existing_port(workspace)
    if requested:
        if requested in used_ports(workspace) and requested != current:
            raise InstallError(f"port {requested} is already used in this workspace")
        return requested
    if current:
        return current
    taken = used_ports(workspace)
    return next(p for p in range(FIRST_PORT, 65535) if p not in taken and not _listening(p))


def copy_app(workspace: Path, port: int) -> Path:
    """Copy the app in, filling in its port. Replaces code, never data (data lives under data/.apps/context)."""
    target = _app_dir(workspace)
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(
        APP_TEMPLATE,
        target,
        ignore=shutil.ignore_patterns("__pycache__", "*.egg-info", ".pytest_cache", ".venv", "uv.lock"),
    )
    for name in ("runner.py", "live.py"):
        path = target / "src" / PACKAGE / name
        path.write_text(path.read_text(encoding="utf-8").replace(PORT_PLACEHOLDER, str(port)), encoding="utf-8")
    return target


def write_conf(workspace: Path, port: int) -> None:
    directory = CANONICAL_WORKSPACE if CANONICAL_WORKSPACE.resolve() == workspace.resolve() else workspace
    template = STANDALONE_COMMAND if standalone_layout(workspace) else LEGACY_COMMAND
    command = template.format(name=APP_NAME, package=PACKAGE, port=port)
    _conf_path(workspace).write_text(
        PROGRAM_CONF.format(name=APP_NAME, command=command, workspace=directory), encoding="utf-8"
    )


def _run(cmd: list[str], workspace: Path) -> str:
    proc = subprocess.run(cmd, cwd=workspace, capture_output=True, text=True)
    if proc.returncode != 0:
        raise InstallError(f"{' '.join(cmd)} failed:\n{(proc.stderr or proc.stdout).strip()[-1500:]}")
    return proc.stdout


def install_package(workspace: Path) -> None:
    if not standalone_layout(workspace):
        _run(["uv", "tool", "install", "-e", f"system/apps/{PACKAGE}", "--reinstall"], workspace)
        _run(["uv", "sync", "--all-packages"], workspace)
        return
    # The app's own lock, seeded from the root lock so it starts on the built-ins' versions and
    # resolves from the warm cache; then its own venv, linked where the workspace keeps venvs.
    app_dir = _app_dir(workspace)
    root_lock = workspace / "uv.lock"
    if root_lock.is_file() and not (app_dir / "uv.lock").exists():
        shutil.copyfile(root_lock, app_dir / "uv.lock")
    _run(["python3", str(VENV_LINKS), "ensure-project", f"system/apps/{PACKAGE}"], workspace)
    _run(["uv", "lock"], app_dir)
    _run(["uv", "sync", "--locked"], app_dir)


def sync_python(workspace: Path) -> list[str]:
    """The interpreter that has the context package: the app's own venv, or the root venv."""
    if standalone_layout(workspace):
        return [f"system/apps/{PACKAGE}/.venv/bin/python"]
    return ["uv", "run", "--no-sync", "python"]


def start(workspace: Path, port: int, timeout: float = 90.0) -> None:
    _run(["supervisorctl", "reread"], workspace)
    _run(["supervisorctl", "update"], workspace)
    subprocess.run(["supervisorctl", "restart", APP_NAME], cwd=workspace, capture_output=True, text=True)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(1)
    raise InstallError(f"the app did not answer on port {port}; see /var/log/supervisor/{APP_NAME}-stderr.log")


def add_routine(workspace: Path) -> bool:
    """Add the sorting routine unless it is there; answers whether it was added."""
    if (workspace / "system" / "routines" / f"{ROUTINE}.toml").exists():
        return False
    # own-process: other-tree -- the routine is added by the target workspace's own routines command, in that
    # workspace, which may be on another release than the checkout this script runs from
    _run([
        "uv", "run", "--no-sync", "routines", "add", ROUTINE,
        "--title", "Context: sort new conversations",
        "--what", "Files new conversations into topics in Context and rewrites the topics that changed.",
        "--app", APP_NAME, "--every", "10m", "--timeout", "30m",
        "--", "env", "CONTEXT_DATA_DIR=data/.apps/context", *sync_python(workspace), "-m", "context.sync",
    ], workspace)
    return True


def install(workspace: Path, requested_port: int | None, skip_start: bool) -> dict:
    check_workspace(workspace)
    missing = chat_routes_missing()
    if missing:
        raise InstallError("this workspace's chat app lacks what Context needs: " + ", ".join(missing))
    port = pick_port(workspace, requested_port)
    was_installed = _app_dir(workspace).exists()
    app_dir = copy_app(workspace, port)
    write_conf(workspace, port)
    _data_dir(workspace).mkdir(parents=True, exist_ok=True)
    report = {"app": APP_NAME, "dir": str(app_dir.relative_to(workspace)), "port": port, "updated": was_installed}
    if not skip_start:
        install_package(workspace)
        start(workspace, port)
        report["routine_added"] = add_routine(workspace)
    report["started"] = not skip_start
    return report


def verify(workspace: Path) -> int:
    port = existing_port(workspace)
    if port is None:
        _say("Context is not installed here.")
        return 1
    failures = []
    for path, check in (("/health", lambda body: body.get("status") == "ok"),
                        ("/api/topics", lambda body: isinstance(body.get("topics"), list)),
                        ("/api/schema", lambda body: "/api/topics" in body.get("paths", {}))):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=10) as response:
                if not check(json.loads(response.read())):
                    failures.append(f"{path} answered something unexpected")
        except (urllib.error.URLError, OSError, ValueError) as e:
            failures.append(f"{path}: {e}")
    if not (workspace / "system" / "routines" / f"{ROUTINE}.toml").exists():
        failures.append("the sorting routine is missing")
    pytest = [".venv/bin/python", "-m", "pytest", "-q"] if standalone_layout(workspace) else ["uv", "run", "--no-sync", "pytest", "-q"]
    tests = subprocess.run(pytest, cwd=_app_dir(workspace), capture_output=True, text=True)
    if tests.returncode != 0:
        failures.append("the app's tests failed:\n" + tests.stdout.strip()[-1500:])
    for failure in failures:
        _say("FAIL " + failure)
    if not failures:
        _say("ok: Context answers on port %d, describes its API, passes its tests, and has its sorting routine." % port)
    return 1 if failures else 0


def status(workspace: Path) -> dict:
    data = _data_dir(workspace)
    topics = json.loads((data / "topics.json").read_text()) if (data / "topics.json").is_file() else []
    sorted_state = json.loads((data / "sort_state.json").read_text()) if (data / "sort_state.json").is_file() else {}
    return {
        "installed": _app_dir(workspace).exists(),
        "port": existing_port(workspace),
        "program": subprocess.run(["supervisorctl", "status", APP_NAME], capture_output=True, text=True).stdout.strip(),
        "routine": (workspace / "system" / "routines" / f"{ROUTINE}.toml").exists(),
        "topics": [t["name"] for t in topics],
        "conversations_sorted": len(sorted_state),
    }


def check(workspace: Path) -> dict:
    check_workspace(workspace)
    conversations, messages = history_size(workspace)
    return {
        "workspace_ok": True,
        "chat_routes_missing": chat_routes_missing(),
        "already_installed": _app_dir(workspace).exists(),
        "port": existing_port(workspace),
        "conversations": conversations,
        "messages": messages,
        "first_sort_estimate_usd": round(messages * DOLLARS_PER_MESSAGE, 2),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("check", "install", "verify", "status"):
        p = sub.add_parser(name)
        p.add_argument("--workspace", type=Path, default=Path.cwd(), help="the workspace repo root")
        if name == "install":
            p.add_argument("--port", type=int, help="serve on this port (default: keep, else the lowest free >= 8080)")
            p.add_argument("--skip-start", action="store_true", help="copy files only; do not install, start, or schedule")
    args = parser.parse_args()
    workspace = args.workspace.resolve()
    try:
        if args.command == "check":
            print(json.dumps(check(workspace), indent=2))
        elif args.command == "install":
            print(json.dumps(install(workspace, args.port, args.skip_start), indent=2))
        elif args.command == "verify":
            sys.exit(verify(workspace))
        else:
            print(json.dumps(status(workspace), indent=2))
    except InstallError as e:
        _say(f"error: {e}")
        sys.exit(2)


if __name__ == "__main__":
    main()
