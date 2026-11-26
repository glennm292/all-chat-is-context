"""Permission requests a conversation filed, read from its transcript, and how each was answered.

An agent asks the person for a permission by POSTing to the reserved latchkey host; the gateway
echoes the filed request (with its id) back on the command's output. When the person answers in
Imbue Studio, a notice arrives in the conversation as a user message tagged
"(resolution: granted, request_id: <id>)". These are the same rules the chat app reads them by.
"""

import json
import re
from pathlib import Path

PERMISSION_REQUEST_HOST = "latchkey-self.invalid/permission-requests"
_POST = re.compile(r"-X\s*POST|--request\s*POST", re.IGNORECASE)
_RESOLUTION_TAG = re.compile(r"\(resolution:\s*(granted|denied|error),\s*request_id:\s*([^)\s]+)\)")
# Notices from before the tag carried only prose.
_LEGACY = (
    (re.compile(r"^Your\b.*\brequest\b.*\bwas granted\b"), "granted"),
    (re.compile(r"^Your\b.*\brequest\b.*\bwas denied\b"), "denied"),
    (re.compile(r"^Your\b.*\brequest\b.*\bcould not be completed\b"), "error"),
)
_LEGACY_ID = re.compile(r"\(request_id:\s*([^)\s]+)\)")

# Service names as people know them, for a scope like "github-git".
_SERVICE_NAMES = {
    "github": "GitHub", "gitlab": "GitLab", "google": "Google", "gmail": "Gmail", "slack": "Slack",
    "notion": "Notion", "linear": "Linear", "dropbox": "Dropbox", "discord": "Discord", "telegram": "Telegram",
    "openai": "OpenAI", "anthropic": "Anthropic", "jira": "Jira", "figma": "Figma", "aws": "AWS",
}

_cache: dict[Path, tuple[tuple[int, int], list[dict]]] = {}


def _echoed_request(content: str) -> dict | None:
    """The filed request the gateway echoed back, found anywhere in the command's output."""
    decoder = json.JSONDecoder()
    for start in (m.start() for m in re.finditer(r"\{", content)):
        try:
            found, _ = decoder.raw_decode(content, start)
        except ValueError:
            continue
        if isinstance(found, dict) and isinstance(found.get("request_id"), str):
            return found
    return None


def _service(scope: str | None) -> str | None:
    if not scope:
        return None
    first = scope.split("-")[0]
    return _SERVICE_NAMES.get(first, first.capitalize())


def _describe(request: dict) -> dict:
    raw_payload = request.get("payload")
    payload: dict = raw_payload if isinstance(raw_payload, dict) else {}
    scope = payload.get("scope") if isinstance(payload.get("scope"), str) else None
    permissions = [p for p in payload.get("permissions", []) if isinstance(p, str)]
    path = payload.get("path") if isinstance(payload.get("path"), str) else None
    if request.get("request_type") == "file-sharing" or path:
        access = str(payload.get("access") or "").lower()
        title = "Access to a folder on your computer"
        what = f"{path} ({access})" if access else (path or "")
    else:
        service = _service(scope) or payload.get("domain") or "a service"
        title = f"Use {service}"
        prefix = f"{scope.split('-')[0]}-" if scope else ""
        what = ", ".join(p[len(prefix):].replace("-", " ") if p.startswith(prefix) else p.replace("-", " ") for p in permissions)
    return {"title": title, "what": what, "rationale": request.get("rationale") or ""}


def read_requests(path: Path) -> list[dict]:
    """Every permission request filed in this transcript, oldest first, with its answer once given."""
    try:
        stat = path.stat()
    except OSError:
        return []
    key = (stat.st_mtime_ns, stat.st_size)
    cached = _cache.get(path)
    if cached and cached[0] == key:
        return cached[1]
    calls: dict[str, str] = {}  # tool call id -> when it was made
    requests: list[dict] = []
    for line in path.read_text().splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if record.get("type") == "step":
            for call in record.get("tool_calls") or []:
                arguments = call.get("arguments") if isinstance(call.get("arguments"), dict) else {}
                command = arguments.get("command")
                if isinstance(command, str) and PERMISSION_REQUEST_HOST in command and _POST.search(command):
                    calls[call.get("tool_call_id")] = record.get("timestamp")
            text = record.get("message")
            if record.get("source") == "user" and isinstance(text, str):
                _apply_resolution(text, requests)
        elif record.get("type") == "observation":
            for result in record.get("results") or []:
                at = calls.pop(result.get("source_call_id"), None)
                content = result.get("content")
                if at is None or not isinstance(content, str):
                    continue
                filed = _echoed_request(content)
                if filed:
                    requests.append({"request_id": filed["request_id"], "at": at, "resolution": None, **_describe(filed)})
    _cache[path] = (key, requests)
    return requests


def _apply_resolution(text: str, requests: list[dict]) -> None:
    tag = _RESOLUTION_TAG.search(text)
    if tag:
        verdict, request_id = tag.group(1), tag.group(2)
    else:
        verdict = next((v for pattern, v in _LEGACY if pattern.search(text)), None)
        if verdict is None:
            return
        legacy_id = _LEGACY_ID.search(text)
        request_id = legacy_id.group(1) if legacy_id else None
    target = next((r for r in requests if r["request_id"] == request_id), None) if request_id else None
    if target is None and request_id is None:
        # An untagged notice answers the oldest request still waiting.
        target = next((r for r in requests if r["resolution"] is None), None)
    if target is not None:
        target["resolution"] = verdict
