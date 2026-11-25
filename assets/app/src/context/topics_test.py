import json
from pathlib import Path

import pytest

from context import live, sync
from context.topics import load_topics


def _message(who: str, text: str, at: str) -> dict:
    return {"who": who, "text": text, "at": at, "event_id": f"e-{at}"}


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(live, "MNGR_ROOT", tmp_path / "mngr")
    conversations = [
        {"agent_id": "agent-a", "name": "Chat A", "started_by": "user", "chat_id": None, "messages": [
            _message("you", "make a duel game", "2026-11-01T10:00:00Z"),
            _message("ai", "Here it is.", "2026-11-01T10:01:00Z"),
            _message("you", "/compact", "2026-11-01T10:02:00Z"),
            _message("you", "Your permission request for GitHub was granted", "2026-11-01T10:03:00Z"),
            _message("you", "add more figures", "2026-11-03T09:00:00Z"),
            _message("ai", "Added 40.", "2026-11-03T09:05:00Z"),
        ]},
    ]
    topics = [
        {"id": "game", "name": "The game", "kind": "topic", "prose": ["A <em>game</em>."], "artifacts": [],
         "segments": [{"agent_id": "agent-a", "from": 0, "to": 6, "about": "Making the game"}]},
        {"id": "internal", "name": "Internal notes", "kind": "internal", "prose": [], "artifacts": [], "segments": []},
        {"id": "other", "name": "Other topics", "kind": "other", "prose": [], "artifacts": [], "segments": []},
    ]
    (tmp_path / "conversations.json").write_text(json.dumps(conversations))
    (tmp_path / "topics.json").write_text(json.dumps(topics))
    return tmp_path


def test_a_topic_shows_its_messages_exactly_as_exchanged(data_dir: Path) -> None:
    game = load_topics(data_dir, {}).topics[0]
    messages = [(i.who, i.text) for i in game.thread if i.kind == "message"]
    assert messages == [("you", "make a duel game"), ("ai", "Here it is."), ("you", "add more figures"), ("ai", "Added 40.")]


def test_a_line_the_person_did_not_type_is_a_note_and_a_compact_is_left_out(data_dir: Path) -> None:
    game = load_topics(data_dir, {}).topics[0]
    assert [i.text for i in game.thread if i.kind == "note"] == ["Your permission request for GitHub was granted"]
    assert all(i.text != "/compact" for i in game.thread)


def test_a_long_pause_starts_a_new_divider_and_the_first_carries_its_label(data_dir: Path) -> None:
    dividers = [i for i in load_topics(data_dir, {}).topics[0].thread if i.kind == "divider"]
    assert [(d.text, d.at) for d in dividers] == [("Making the game", "2026-11-01T10:00:00Z"), ("", "2026-11-03T09:00:00Z")]


def test_other_topics_and_internal_notes_come_last_in_that_order(data_dir: Path) -> None:
    assert [t.id for t in load_topics(data_dir, {}).topics] == ["game", "other", "internal"]


def test_a_workspace_with_nothing_sorted_yet_has_no_topics(tmp_path: Path) -> None:
    assert load_topics(tmp_path, {}).topics == []


def test_the_assistants_rewrite_replaces_the_topic_text(data_dir: Path) -> None:
    live.set_prose(data_dir, "game", ["Now with <strong>80</strong> figures <script>x</script>"])
    assert load_topics(data_dir, {}).topics[0].prose == ["Now with <strong>80</strong> figures &lt;script&gt;x&lt;/script&gt;"]


def test_a_chat_still_on_the_topic_shows_its_unsorted_messages_live(data_dir: Path) -> None:
    transcript = data_dir / "mngr" / "agents" / "agent-a" / "events" / "claude" / "common_transcript"
    transcript.mkdir(parents=True)
    steps = [
        {"type": "step", "source": "user", "message": "make a duel game", "timestamp": "2026-11-01T10:00:00Z"},
        *[{"type": "step", "source": "agent", "message": f"reply {n}", "timestamp": "2026-11-01T10:01:00Z"} for n in range(5)],
        {"type": "step", "source": "user", "message": "one more thing", "timestamp": "2026-11-03T09:06:00Z"},
    ]
    (transcript / "events.jsonl").write_text("\n".join(json.dumps(s) for s in steps))
    game = load_topics(data_dir, {}).topics[0]
    assert game.thread[-1].text == "one more thing"
    assert game.thread[-1].ref == "agent-a:6"


def test_the_first_message_of_a_topic_conversation_splits_into_background_and_what_was_typed() -> None:
    first = live.BRIEFING_MARK + " background here" + live.PERSON_SAYS + "add Mewtwo"
    assert live.split_first_message(first) == (live.BRIEFING_MARK + " background here", "add Mewtwo")
    assert live.split_first_message("add Mewtwo") is None


def test_rewritten_text_keeps_only_message_and_topic_links_and_emphasis() -> None:
    cleaned = sync.clean_paragraph('See <a data-moment="agent-a:4">this</a>, <a href="https://x">that</a>, <em>now</em> <img src=x onerror=1>')
    assert cleaned == ('See <a data-moment="agent-a:4">this</a>, &lt;a href=&quot;https://x&quot;&gt;that&lt;/a&gt;, '
                       "<em>now</em> &lt;img src=x onerror=1&gt;")
