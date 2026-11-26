import json
from pathlib import Path

import pytest

from context import live, permissions
from context.topics import load_topics


def _call(call_id: str, at: str, command: str) -> dict:
    return {"type": "step", "source": "agent", "timestamp": at, "message": "",
            "tool_calls": [{"tool_call_id": call_id, "function_name": "Bash", "arguments": {"command": command}}]}


def _result(call_id: str, at: str, content: str) -> dict:
    return {"type": "observation", "timestamp": at, "results": [{"source_call_id": call_id, "content": content}]}


def _said(who: str, text: str, at: str) -> dict:
    return {"type": "step", "source": who, "timestamp": at, "message": text, "event_id": f"e-{at}"}


REQUEST = ("latchkey curl -XPOST http://latchkey-self.invalid/permission-requests -H 'Content-Type: application/json' "
           "-d '{\"type\": \"predefined\"}'")


def _echo(request_id: str, **payload) -> str:
    return "  % Total ...\n" + json.dumps({"request_id": request_id, "request_type": "predefined", "rationale": "To publish.",
                                          "payload": payload or {"scope": "github-git", "permissions": ["github-git-write"]}})


def _write(path: Path, records: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records))
    return path


def test_a_filed_request_is_read_with_its_tagged_answer(tmp_path: Path) -> None:
    path = _write(tmp_path / "t.jsonl", [
        _call("c1", "2026-11-26T10:00:00Z", REQUEST),
        _result("c1", "2026-11-26T10:00:01Z", _echo("r-1")),
        _said("user", "Your permission request was granted. (resolution: granted, request_id: r-1)", "2026-11-26T10:01:00Z"),
    ])
    [request] = permissions.read_requests(path)
    assert request == {"request_id": "r-1", "at": "2026-11-26T10:00:00Z", "resolution": "granted",
                       "title": "Use GitHub", "what": "git write", "rationale": "To publish."}


def test_an_untagged_notice_answers_the_oldest_waiting_request_and_a_failed_filing_is_skipped(tmp_path: Path) -> None:
    path = _write(tmp_path / "t.jsonl", [
        _call("c1", "2026-11-26T10:00:00Z", REQUEST),
        _result("c1", "2026-11-26T10:00:01Z", "curl: (7) Failed to connect"),
        _call("c2", "2026-11-26T10:02:00Z", REQUEST),
        _result("c2", "2026-11-26T10:02:01Z", _echo("r-2")),
        _call("c3", "2026-11-26T10:03:00Z", REQUEST),
        _result("c3", "2026-11-26T10:03:01Z", _echo("r-3", path="/Users/me/Downloads", access="WRITE")),
        _said("user", "Your permission request for GitHub was denied", "2026-11-26T10:04:00Z"),
    ])
    found = permissions.read_requests(path)
    assert [(r["request_id"], r["resolution"]) for r in found] == [("r-2", "denied"), ("r-3", None)]
    assert (found[1]["title"], found[1]["what"]) == ("Access to a folder on your computer", "/Users/me/Downloads (write)")


def test_a_command_that_only_reads_the_request_host_is_not_a_request(tmp_path: Path) -> None:
    path = _write(tmp_path / "t.jsonl", [
        _call("c1", "2026-11-26T10:00:00Z", "latchkey curl http://latchkey-self.invalid/permission-requests"),
        _result("c1", "2026-11-26T10:00:01Z", _echo("r-1")),
    ])
    assert permissions.read_requests(path) == []


@pytest.fixture
def two_topics(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """One chat that spent a morning in topic A, then moved to topic B, filing a request in each."""
    monkeypatch.setattr(live, "MNGR_ROOT", tmp_path / "mngr")
    _write(tmp_path / "mngr" / "agents" / "agent-a" / "events" / "claude" / "common_transcript" / "events.jsonl", [
        _said("user", "publish it", "2020-11-26T09:00:00Z"),
        _call("c1", "2020-11-26T09:01:00Z", REQUEST),
        _result("c1", "2020-11-26T09:01:01Z", _echo("r-a")),
        _said("agent", "Approval waiting.", "2020-11-26T09:02:00Z"),
        _said("user", "Your request was granted (resolution: granted, request_id: r-a)", "2020-11-26T09:03:00Z"),
        _said("user", "now the other thing", "2020-11-26T12:00:00Z"),
        _said("agent", "Asking for access.", "2020-11-26T12:00:30Z"),
        _call("c2", "2020-11-26T12:01:00Z", REQUEST),
        _result("c2", "2020-11-26T12:01:01Z", _echo("r-b")),
    ])
    messages = [
        {"who": "you", "text": "publish it", "at": "2020-11-26T09:00:00Z", "event_id": "1"},
        {"who": "ai", "text": "Approval waiting.", "at": "2020-11-26T09:02:00Z", "event_id": "2"},
        {"who": "you", "text": "Your request was granted (resolution: granted, request_id: r-a)", "at": "2020-11-26T09:03:00Z",
         "event_id": "2b"},
        {"who": "you", "text": "now the other thing", "at": "2020-11-26T12:00:00Z", "event_id": "3"},
        {"who": "ai", "text": "Asking for access.", "at": "2020-11-26T12:00:30Z", "event_id": "4"},
    ]
    (tmp_path / "conversations.json").write_text(json.dumps([
        {"agent_id": "agent-a", "name": "Chat A", "started_by": "user", "chat_id": None, "messages": messages}]))
    (tmp_path / "topics.json").write_text(json.dumps([
        {"id": "a", "name": "A", "kind": "topic", "prose": [], "artifacts": [],
         "segments": [{"agent_id": "agent-a", "from": 0, "to": 3, "about": "Publishing"}]},
        {"id": "b", "name": "B", "kind": "topic", "prose": [], "artifacts": [],
         "segments": [{"agent_id": "agent-a", "from": 3, "to": 5, "about": "Other thing"}]},
    ]))
    (tmp_path / "sort_state.json").write_text(json.dumps({"agent-a": 5}))  # every message sorted
    return tmp_path


def test_each_request_shows_once_in_the_topic_its_chat_was_in(two_topics: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("context.topics.PERMISSION_STALE", __import__("datetime").timedelta(days=36500))
    topics = {t.id: t for t in load_topics(two_topics, {}).topics}
    in_a = [(i.permission.request_id, i.permission.resolution) for i in topics["a"].thread if i.permission]
    in_b = [(i.permission.request_id, i.permission.resolution) for i in topics["b"].thread if i.permission]
    assert in_a == [("r-a", "granted")]
    assert in_b == [("r-b", None)]  # still waiting: shows just after the chat's last message
    kinds = [i.kind for i in topics["a"].thread]
    assert kinds.index("permission") < [n for n, i in enumerate(topics["a"].thread) if i.text == "Approval waiting."][0]


def test_a_request_never_answered_stops_waiting_after_a_day(two_topics: Path) -> None:
    topics = {t.id: t for t in load_topics(two_topics, {}).topics}
    assert [i.permission.resolution for i in topics["b"].thread if i.permission] == ["expired"]
