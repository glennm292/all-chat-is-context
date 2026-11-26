"""Assemble each topic's thread from the stored conversations, messages exactly as exchanged."""

import json
import os
import re
import time
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

from imbue.imbue_common.frozen_model import FrozenModel
from pydantic import Field

from context.live import chat_messages, continuing_chat, read_state, split_first_message

# A pause this long inside one stretch of conversation starts a new divider.
CONVERSATION_GAP = timedelta(hours=6)

# Messages that arrive in the user's turn but that the user did not type.
NOTE_PATTERNS = re.compile(
    r"^(\[Context\] |Your permission request|Secret stored:|--- worker_agent_id|.*</background-task-report>\s*$)", re.S
)
SKIPPED = {"/compact"}


class ThreadItem(FrozenModel):
    """One line of a topic's conversation: a message, a note, or the divider before a conversation."""

    kind: str = Field(description="'message', 'note', 'divider', or 'moved' where the conversation went on to a new topic")
    who: str | None = Field(default=None, description="'you' or 'ai' for a message")
    text: str = Field(default="", description="The message exactly as exchanged, or a divider's label")
    at: str | None = Field(default=None, description="When it was sent (ISO 8601)")
    ref: str | None = Field(default=None, description="'<agent id>:<message index>', the message's place in its source")
    conversation: str | None = Field(default=None, description="The name of the chat it came from")
    topic: str | None = Field(default=None, description="For 'moved': the id of the topic the conversation went on to")


class Artifact(FrozenModel):
    """Something the topic produced, shown in the topic's middle pane."""

    kind: str = Field(description="'app', 'skill' or 'image'")
    title: str = Field(description="What it is called")
    app: str | None = Field(default=None, description="For an app: its registered name")
    link: str | None = Field(default=None, description="For an app: the address it opens at")
    skill: str | None = Field(default=None, description="For a skill: its name")
    github: str | None = Field(default=None, description="Where it is published, when it is")
    src: str | None = Field(default=None, description="For an image: its path under /media/")
    version: str | None = Field(default=None, description="Changes whenever the thing itself is modified, so a view of it can reload")


class Topic(FrozenModel):
    """A subject we keep coming back to: its living text and every message that touched it."""

    id: str = Field(description="Stable id of the topic")
    name: str = Field(description="What the topic is called")
    kind: str = Field(description="'topic', 'other' or 'internal'")
    prose: list[str] = Field(description="The topic's current text, one HTML paragraph each")
    last_at: str | None = Field(description="When the topic was last touched (ISO 8601)")
    first_at: str | None = Field(description="When the topic began (ISO 8601)")
    thread: list[ThreadItem] = Field(description="Every message that touched the topic, oldest first")
    artifacts: list[Artifact] = Field(description="The apps, skills and pictures the topic produced, newest first")
    talking: bool = Field(default=False, description="Whether the assistant is working on a reply in this topic now")


class TopicsResponse(FrozenModel):
    """Every topic, most recently touched first, with Other topics and Internal notes last."""

    topics: list[Topic] = Field(description="The topics")


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def load_topics(data_dir: Path, app_links: dict[str, str], busy_chats: set[str] | None = None) -> TopicsResponse:
    state = read_state(data_dir)
    sort_state_file = data_dir / "sort_state.json"
    sort_state = json.loads(sort_state_file.read_text()) if sort_state_file.is_file() else {}
    if not (data_dir / "topics.json").is_file() or not (data_dir / "conversations.json").is_file():
        return TopicsResponse(topics=[])
    raw_topics = json.loads((data_dir / "topics.json").read_text())
    conversations = {c["agent_id"]: c for c in json.loads((data_dir / "conversations.json").read_text())}
    topics = []
    for raw in raw_topics:
        stretches = []
        for seg in raw["segments"]:
            conv = conversations.get(seg["agent_id"])
            if conv is None:
                continue
            msgs = [
                (i, m) for i, m in enumerate(conv["messages"])
                if seg["from"] <= i and (seg["to"] is None or i < seg["to"]) and m["text"].strip() not in SKIPPED
            ]
            if msgs:
                stretches.append((seg, conv, msgs))
        chat_id = state["chats"].get(raw["id"])
        going = None if chat_id else continuing_chat(raw, raw_topics, conversations, sort_state)
        if going:
            # The newest part of a chat still on this topic, live, before the sorter has filed it.
            tail = [(i, m) for i, m in enumerate(chat_messages(going["agent_id"]))
                    if i >= going["start"] and m["text"].strip() not in SKIPPED]
            if tail:
                stretches.append(({"about": ""}, conversations[going["agent_id"]], tail))
        if chat_id:
            live = [(i, m) for i, m in enumerate(chat_messages(chat_id)) if m["text"].strip() not in SKIPPED]
            if live:
                stretches.append(({"about": "In Context"}, {"agent_id": chat_id, "name": f"Context: {raw['name']}"}, live))
        stretches.sort(key=lambda s: s[2][0][1]["at"])

        thread: list[ThreadItem] = []
        previous, previous_agent = None, None
        for seg, conv, msgs in stretches:
            if conv["agent_id"] != previous_agent:
                previous = None  # a different chat always starts with its own divider
            previous_agent = conv["agent_id"]
            for i, m in msgs:
                at = _parse(m["at"])
                if previous is None or at - previous > CONVERSATION_GAP:
                    thread.append(ThreadItem(kind="divider", text=seg["about"] if previous is None else "", at=m["at"],
                                             conversation=conv["name"]))
                previous = at
                ref = f"{conv['agent_id']}:{i}"
                first = split_first_message(m["text"]) if m["who"] == "you" else None
                if first:
                    thread.append(ThreadItem(kind="note", who="you", text="This topic's background went to the assistant with your message.\n\n" + first[0],
                                             at=m["at"], ref=None, conversation=conv["name"]))
                    thread.append(ThreadItem(kind="message", who="you", text=first[1], at=m["at"], ref=ref,
                                             conversation=conv["name"]))
                    continue
                is_note = m["who"] == "you" and NOTE_PATTERNS.match(m["text"])
                thread.append(ThreadItem(kind="note" if is_note else "message", who=m["who"], text=m["text"],
                                         at=m["at"], ref=ref, conversation=conv["name"]))
        names = {t["id"]: t["name"] for t in raw_topics}
        for move in state["moves"]:
            if move["from"] == raw["id"] and move["to"] in names:
                note = ThreadItem(kind="moved", text=f"{names[move['to']]} moved to its own topic", at=move["at"], topic=move["to"])
                where = next((n for n, item in enumerate(thread) if item.at and _parse(item.at) > _parse(move["at"])), len(thread))
                thread.insert(where, note)
        stamps = [t.at for t in thread if t.at]
        delivered = state["artifacts"].get(raw["id"], [])
        topics.append(Topic(id=raw["id"], name=raw["name"], kind=raw["kind"], prose=state["prose"].get(raw["id"], raw["prose"]),
                            talking=bool(busy_chats and ((chat_id and chat_id in busy_chats) or (going and going["chat_id"] in busy_chats))),
                            first_at=min(stamps) if stamps else None, last_at=max(stamps) if stamps else None,
                            thread=thread,
                            artifacts=[Artifact(**{k: v for k, v in a.items() if k not in ("zip", "chat", "version")},
                                                link=app_links.get(a["app"]) if a.get("app") else None,
                                                version=artifact_version(a, data_dir))
                                       for a in [*delivered, *raw.get("artifacts", [])]]))
    ordinary = sorted((t for t in topics if t.kind == "topic"), key=lambda t: t.last_at or "", reverse=True)
    pinned = [t for kind in ("other", "internal") for t in topics if t.kind == kind]
    return TopicsResponse(topics=ordinary + pinned)


# Files whose changes do not change what the user sees.
_IGNORED_DIRS = {".venv", "node_modules", "__pycache__", ".pytest_cache", ".ruff_cache", "tests", ".git"}
_VERSION_TTL = 3.0
_versions: dict[str, tuple[float, str | None]] = {}


def _newest_change(root: Path) -> str | None:
    """The latest modification time of anything under root that makes up the thing itself."""
    if root.is_file():
        return str(root.stat().st_mtime_ns)
    if not root.is_dir():
        return None
    newest = 0
    for here, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in _IGNORED_DIRS]
        for f in files:
            if f.endswith(("_test.py", ".pyc")) or f.startswith("test_"):
                continue
            try:
                newest = max(newest, os.stat(os.path.join(here, f)).st_mtime_ns)
            except OSError:
                continue
    return str(newest) if newest else None


def _app_dir(name: str) -> Path | None:
    """The folder of the app registered under this name."""
    for manifest in Path("system/apps").glob("*/app.toml"):
        if re.search(rf'^name\s*=\s*"{re.escape(name)}"\s*$', manifest.read_text(), re.M):
            return manifest.parent
    return None


def artifact_version(artifact: dict, data_dir: Path) -> str | None:
    """A stamp that moves when the app's code, the skill's files or the picture change; re-read every few seconds."""
    if artifact.get("app"):
        key = "app:" + artifact["app"]
    elif artifact.get("skill"):
        key = "skill:" + artifact["skill"]
    elif artifact.get("src"):
        key = "src:" + artifact["src"]
    else:
        return None
    cached = _versions.get(key)
    if cached and time.monotonic() - cached[0] < _VERSION_TTL:
        return cached[1]
    root: Path | None = None
    if artifact.get("app"):
        root = _app_dir(artifact["app"])
    elif artifact.get("skill"):
        if re.fullmatch(r"[a-z0-9-]+", artifact["skill"]):
            root = Path(".agents/skills") / artifact["skill"]
    else:
        base, _, rest = artifact["src"].partition("/")
        root = {"pictures": data_dir / "pictures", "uploads": Path("data/uploads")}.get(base)
        root = root / rest if root else None
    version = _newest_change(root) if root else None
    _versions[key] = (time.monotonic(), version)
    return version


class SkillDoc(FrozenModel):
    """A skill's own instructions, for reading in the middle pane."""

    name: str = Field(description="The skill's name")
    description: str = Field(description="The one-line description from its header")
    markdown: str = Field(description="The body of its SKILL.md")


def read_skill(name: str, zip_path: str | None) -> SkillDoc | None:
    """The skill installed in this workspace, or failing that the one inside an uploaded .skill file."""
    text = None
    local = Path(".agents/skills") / name / "SKILL.md"
    if re.fullmatch(r"[a-z0-9-]+", name) and local.is_file():
        text = local.read_text()
    elif zip_path and zip_path.startswith("data/uploads/") and zip_path.endswith(".skill") and Path(zip_path).is_file():
        with zipfile.ZipFile(zip_path) as z:
            member = next((n for n in z.namelist() if n.endswith("/SKILL.md") or n == "SKILL.md"), None)
            text = z.read(member).decode() if member else None
    if text is None:
        return None
    description = ""
    front = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    if front:
        found = re.search(r"^description:\s*(.*)$", front.group(1), re.M)
        description = found.group(1).strip().strip('"') if found else ""
        text = text[front.end():]
    return SkillDoc(name=name, description=description, markdown=text)
