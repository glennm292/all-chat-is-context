"""Read every conversation on this host into one file of the messages as they were exchanged.

Each conversation keeps where it came from (the agent, its transcript file, and each
message's event id), so anything built on top can point back at the original.
"""

import json
import os
import re
import subprocess
from pathlib import Path

from loguru import logger

MNGR_ROOT = Path(os.environ.get("MNGR_HOST_DIR", "/home/user/.mngr"))
OUT = Path(os.environ.get("CONTEXT_DATA_DIR", "data/.apps/context")) / "conversations.json"

# Text the harness puts in the user's turn that the user did not type.
INJECTED = re.compile(r"^\s*(<[a-z-]+[ >]|Base directory for this skill|Caveat: |\[Request interrupted)")


def agent_labels() -> dict[str, dict]:
    out = subprocess.run(["uv", "run", "--no-sync", "mngr", "list", "--format", "json"], capture_output=True, text=True, check=True)
    data = json.loads(out.stdout)
    return {a["id"]: a for a in data.get("agents", data if isinstance(data, list) else [])}


def transcript_files() -> list[tuple[str, str | None, Path]]:
    found = []
    for base, preserved in ((MNGR_ROOT / "agents", False), (MNGR_ROOT / "preserved", True)):
        for path in base.glob("*/events/*/common_transcript/events.jsonl"):
            if "/events/logs/" in str(path):
                continue
            dirname = path.parts[len(base.parts)]
            name, _, agent_id = dirname.rpartition("--") if preserved else (None, "", dirname)
            found.append((agent_id, name, path))
    return found


def read_messages(path: Path) -> list[dict]:
    messages = []
    for line in path.read_text().splitlines():
        record = json.loads(line)
        if record.get("type") != "step" or record.get("source") not in ("user", "agent"):
            continue
        text = record.get("message")
        if not isinstance(text, str) or not text.strip():
            continue
        if record["source"] == "user" and INJECTED.match(text):
            continue
        messages.append({
            "who": "you" if record["source"] == "user" else "ai",
            "text": text,
            "at": record.get("timestamp"),
            "event_id": record.get("event_id"),
        })
    return messages


def main() -> None:
    labels = agent_labels()
    # Conversations that must never appear in Context (tests that spoke in the person's name).
    excluded_file = OUT.parent / "excluded.json"
    excluded = set(json.loads(excluded_file.read_text())) if excluded_file.is_file() else set()
    conversations = []
    for agent_id, preserved_name, path in transcript_files():
        info = labels.get(agent_id, {})
        if agent_id in excluded or (info.get("name") or preserved_name or "").startswith("Context-test"):
            continue
        agent_labels_ = info.get("labels", {})
        messages = read_messages(path)
        if not messages:
            continue
        conversations.append({
            "agent_id": agent_id,
            "name": agent_labels_.get("display_name") or info.get("name") or preserved_name or agent_id,
            "started_by": "agent" if agent_labels_.get("agent_created") == "true" or preserved_name else "user",
            "lead_agent": agent_labels_.get("lead_agent"),
            "chat_id": agent_labels_.get("chat_id"),
            "context_topic": agent_labels_.get("context_topic"),
            "source": str(path),
            "first_at": messages[0]["at"],
            "last_at": messages[-1]["at"],
            "messages": messages,
        })
    conversations.sort(key=lambda c: c["first_at"] or "")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT.with_suffix(".tmp")
    tmp.write_text(json.dumps(conversations, indent=1))
    tmp.replace(OUT)
    logger.info("{} conversations, {} messages -> {}", len(conversations), sum(len(c["messages"]) for c in conversations), OUT)


if __name__ == "__main__":
    main()
