"""Talking in place: each topic's own ongoing conversation, and what it adds to the topic.

State kept under the data dir in live.json:
  chats:     topic id -> the chat (its first agent's id) that carries the topic's conversation
  artifacts: topic id -> things the assistant delivered into the topic, newest first
  prose:     topic id -> the topic's current text, when the assistant has rewritten it
  moves:     conversations that went on to a new topic, as {from, to, at}, shown in the old topic's thread
"""

import html
import json
import os
import re
import threading
from datetime import datetime, timezone
import urllib.error
import urllib.request
from pathlib import Path

from context.transcripts import MNGR_ROOT, read_messages

CHAT_API = os.environ.get("CONTEXT_CHAT_API", "http://127.0.0.1:8010")
CONTEXT_API = os.environ.get("CONTEXT_SELF_API") or f"http://127.0.0.1:{os.environ.get('CONTEXT_PORT', '__PORT__')}"
BRIEFING_MARK = "[Context]"
# Ends the background in a topic conversation's first message; what follows is what the person typed.
PERSON_SAYS = "\n\n--- The person says:\n"

_lock = threading.Lock()


def _path(data_dir: Path) -> Path:
    return data_dir / "live.json"


def read_state(data_dir: Path) -> dict:
    path = _path(data_dir)
    state = json.loads(path.read_text()) if path.is_file() else {}
    for key in ("chats", "artifacts", "prose"):
        state.setdefault(key, {})
    state.setdefault("moves", [])
    return state


def record_move(data_dir: Path, from_topic: str, to_topic: str, at: str) -> None:
    """Note in the old topic's thread that a conversation went on to a new topic, at the moment it did."""
    _update(data_dir, lambda s: s["moves"].append({"from": from_topic, "to": to_topic, "at": at}))


def _update(data_dir: Path, change) -> dict:
    with _lock:
        state = read_state(data_dir)
        change(state)
        tmp = _path(data_dir).with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=1))
        tmp.replace(_path(data_dir))
        return state


def _call_chat(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    request = urllib.request.Request(
        CHAT_API + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {"detail": str(e)}


def briefing(topic: dict) -> str:
    """The first message of a topic's conversation: what the topic is, and how to put work in front of the user."""
    text = "\n\n".join(re.sub(r"<[^>]+>", "", html.unescape(p)) for p in topic["prose"])
    tid = topic["id"]
    return f"""{BRIEFING_MARK} This conversation belongs to the topic "{topic['name']}" in the Context app. The background comes first; the person's own message is at the end, after "--- The person says:". Answer that.

Where the topic stands:
{text}

The person sees this conversation beside the topic's main pane, which shows the things the topic produced. The work belongs to them: when you make or change something they should see (an app, a skill, a picture), put it in the main pane:
  curl -s -X POST {CONTEXT_API}/api/topics/{tid}/deliveries -H 'Content-Type: application/json' -d '{{"kind": "app", "title": "<name>", "app": "<registered app name>"}}'
  (kind "skill" takes "skill": "<skill name>"; kind "image" takes "path": "<absolute path of a .png/.jpg/.svg under /home/user/workspace/data>")
When where the topic stands has changed, rewrite its text (a few short plain paragraphs, the person's words, no headings or lists):
  curl -s -X PUT {CONTEXT_API}/api/topics/{tid}/text -H 'Content-Type: application/json' -d '{{"paragraphs": ["...", "..."]}}'
Earlier conversations on this topic are in the Context app's data at data/.apps/context/conversations.json if you need them.{_spin_off_note(topic)}"""


def _spin_off_note(topic: dict) -> str:
    """Other topics and Internal notes hold small things; a conversation there that grows into its own subject moves out."""
    if topic.get("kind", "topic") == "topic":
        return ""
    return f"""

"{topic['name']}" only holds small one-off exchanges. The moment this conversation produces something lasting (the first working version of an app, a skill, a document) or clearly grows into a subject of its own, give it its own topic, before telling the person you are done:
  curl -s -X POST {CONTEXT_API}/api/topics/{topic['id']}/spin-off -H 'Content-Type: application/json' -d '{{"name": "<short name for the subject>", "paragraphs": ["<where it stands>"]}}'
It answers with the new topic's id. This conversation and what it delivered move there, and from then on use that id instead of "{topic['id']}" for deliveries and the text. Then rewrite "{topic['name']}"'s text without what moved out."""


def split_first_message(text: str) -> tuple[str, str] | None:
    """A topic conversation's first message as (background, what the person typed), or None for any other message."""
    if not text.startswith(BRIEFING_MARK) or PERSON_SAYS not in text:
        return None
    background, _, said = text.partition(PERSON_SAYS)
    return background, said


def send(data_dir: Path, topic: dict, text: str, chat_name: str | None = None) -> tuple[bool, str]:
    """Send the person's message into the topic's conversation; the first one starts it, carrying the background."""
    chat_id = read_state(data_dir)["chats"].get(topic["id"])
    if chat_id is None:
        status, answer = _call_chat("POST", "/api/chats/create", {
            "name": (chat_name or f"Context: {topic['name']}")[:60],
            "message": briefing(topic) + PERSON_SAYS + text,
            "labels": {"context_topic": topic["id"]},
        })
        if status not in (200, 201):
            return False, answer.get("detail", f"The assistant could not be started ({status})")
        chat_id = answer["chat_id"]
        _update(data_dir, lambda s: s["chats"].__setitem__(topic["id"], chat_id))
        return True, chat_id
    status, answer = _call_chat("POST", f"/api/chats/{chat_id}/message", {"message": text})
    if status not in (200, 202):
        return False, answer.get("detail", f"The message could not be sent ({status})")
    return True, chat_id


def is_busy(chat_id: str) -> bool:
    status, answer = _call_chat("GET", "/api/agents")
    if status != 200:
        return False
    return any(a.get("chat_id") == chat_id and a.get("is_busy") for a in answer.get("agents", []))


def chat_messages(chat_id: str) -> list[dict]:
    """The topic conversation's messages so far, read from its transcript as they are written."""
    homes = [MNGR_ROOT / "agents" / chat_id, *(MNGR_ROOT / "preserved").glob(f"*--{chat_id}")]  # running, or removed
    for home in homes:
        for path in (home / "events").glob("*/common_transcript/events.jsonl"):
            if path.parent.parent.name != "logs":  # the converter's own log sits beside the conversation
                return read_messages(path)
    return []


def add_artifact(data_dir: Path, topic_id: str, artifact: dict) -> None:
    def change(state):
        items = [a for a in state["artifacts"].get(topic_id, []) if a.get("title") != artifact["title"]]
        chat_id = state["chats"].get(topic_id)
        # Remember which conversation delivered it, so it goes along if that conversation becomes its own topic.
        state["artifacts"][topic_id] = [{**artifact, "chat": chat_id} if chat_id else artifact, *items]
    _update(data_dir, change)


def slug(name: str, taken: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "topic"
    found, n = base, 2
    while found in taken:
        found, n = f"{base}-{n}", n + 1
    return found


class SpinOffError(ValueError):
    """The topic has no conversation of its own to move out."""


def spin_off(data_dir: Path, topic_id: str, name: str, paragraphs: list[str]) -> str:
    """Give the conversation going on in Other topics (or Internal notes) a topic of its own; returns the new topic's id.

    The conversation, what it delivered and the given text move to the new topic, which the sorter then treats like
    any other (topics.json is otherwise written only by the sorter, which keeps topics added while it ran)."""
    topics_path = data_dir / "topics.json"
    with _lock:
        state = read_state(data_dir)
        chat_id = state["chats"].get(topic_id)
        if chat_id is None:
            raise SpinOffError(f"{topic_id} has no conversation going on to give its own topic")
        topics = json.loads(topics_path.read_text())
        new_id = slug(name, {t["id"] for t in topics})
        topics.insert(0, {"id": new_id, "name": name, "kind": "topic", "segments": [], "prose": [], "artifacts": []})
        tmp = topics_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(topics, indent=1))
        tmp.replace(topics_path)
    def change(s):
        del s["chats"][topic_id]
        s["chats"][new_id] = chat_id
        mine = [a for a in s["artifacts"].get(topic_id, []) if a.get("chat") == chat_id]
        s["artifacts"][topic_id] = [a for a in s["artifacts"].get(topic_id, []) if a.get("chat") != chat_id]
        s["artifacts"][new_id] = mine
    _update(data_dir, change)
    set_prose(data_dir, new_id, paragraphs)
    # All of the conversation moves, so the note sits where it began.
    messages = chat_messages(chat_id)
    record_move(data_dir, topic_id, new_id, messages[0]["at"] if messages else datetime.now(timezone.utc).isoformat())
    return new_id


ALLOWED_TAGS = re.compile(r"</?(?:em|strong)>")


def set_prose(data_dir: Path, topic_id: str, paragraphs: list[str]) -> None:
    # Plain text in, safe HTML out: escape everything, then allow back only <em> and <strong>.
    clean = [ALLOWED_TAGS.sub(lambda m: m.group(0), html.escape(p, quote=False)) for p in paragraphs]
    clean = [re.sub(r"&lt;(/?(?:em|strong))&gt;", r"<\1>", p) for p in clean]
    _update(data_dir, lambda s: s["prose"].__setitem__(topic_id, clean))


def continuing_chat(topic: dict, topics: list[dict], conversations: dict[str, dict], sort_state: dict) -> dict | None:
    """The person's own chat that is still going on this topic, if there is one: where its newest part was sorted
    is this topic and its agent is still there. Typing in the topic continues that chat rather than starting another.

    Answers {"chat_id", "agent_id", "start"}: the chat to send to, the agent whose transcript to read, and the first
    message not yet sorted (shown live in the topic until the sorter files it)."""
    if topic.get("kind", "topic") != "topic":
        return None
    best = None
    for agent_id in {seg["agent_id"] for seg in topic["segments"]}:
        conv = conversations.get(agent_id)
        if not conv or conv.get("started_by") != "user" or not (MNGR_ROOT / "agents" / agent_id).is_dir():
            continue
        latest = max(((t["id"], seg) for t in topics for seg in t["segments"] if seg["agent_id"] == agent_id),
                     key=lambda pair: pair[1]["from"])
        if latest[0] != topic["id"]:
            continue  # the chat has moved on to another subject
        last_at = conv["messages"][-1]["at"] if conv["messages"] else ""
        if best is None or last_at > best[0]:
            end = max(seg["to"] or 0 for seg in topic["segments"] if seg["agent_id"] == agent_id)
            best = (last_at, {"chat_id": conv.get("chat_id") or agent_id, "agent_id": agent_id,
                              "start": max(end, sort_state.get(agent_id, 0))})
    return best[1] if best else None


def busy_chat_ids() -> set[str]:
    status, answer = _call_chat("GET", "/api/agents")
    if status != 200:
        return set()
    return {a.get("chat_id") for a in answer.get("agents", []) if a.get("is_busy")}


def send_to_chat(chat_id: str, text: str) -> tuple[bool, str]:
    """Continue an existing chat: no background needed, it already has its own."""
    status, answer = _call_chat("POST", f"/api/chats/{chat_id}/message", {"message": text})
    if status not in (200, 202):
        return False, answer.get("detail", f"The message could not be sent ({status})")
    return True, chat_id
