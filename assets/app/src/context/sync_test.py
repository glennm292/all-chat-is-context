import json
from pathlib import Path

import pytest
from datetime import datetime, timedelta, timezone

from context import sync


def _conv(agent_id: str, *messages: tuple[str, str], minutes_ago: int = 60) -> dict:
    at = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat().replace("+00:00", "Z")
    return {"agent_id": agent_id, "name": agent_id, "messages": [{"who": w, "text": t, "at": at} for w, t in messages]}


def _topics() -> list[dict]:
    return [
        {"id": "game", "name": "The game", "kind": "topic", "prose": [], "artifacts": [], "segments": []},
        *sync.starting_topics(),
    ]


def test_only_what_is_new_and_settled_is_offered_for_sorting() -> None:
    settled = _conv("a", ("you", "hi"), ("ai", "hello"), ("you", "more"))
    still_going = _conv("b", ("you", "hi"), minutes_ago=2)
    topic_chat = _conv("c", ("you", "hi"))
    work = sync.pending_work([settled, still_going, topic_chat], {"a": 2}, {"c"})
    assert [(w["conversation"]["agent_id"], w["from"], len(w["messages"])) for w in work] == [("a", 2, 1)]


def test_a_conversation_is_internal_only_when_the_person_never_spoke_in_it() -> None:
    conv = _conv("a", ("you", "build it"), ("ai", "on it"), ("ai", "done"))
    newest_stretch = {"conversation": conv, "from": 1, "messages": conv["messages"][1:]}
    assert not sync.internal_only(newest_stretch)
    update = _conv("u", ("you", "--- worker_agent_id: x"), ("ai", "Login expired"))
    assert sync.internal_only({"conversation": update, "from": 0, "messages": update["messages"]})


def test_sorted_pieces_join_their_topics_and_a_new_subject_becomes_a_topic() -> None:
    topics, state = _topics(), {}
    conv = _conv("a", ("you", "add Mewtwo"), ("ai", "added"), ("you", "banana bread?"), ("ai", "recipe"),
                 ("you", "plan Japan"), ("ai", "plan"))
    work = [{"conversation": conv, "from": 10, "messages": conv["messages"]}]
    answer = {"pieces": [
        {"stretch": 1, "from": 0, "to": 1, "topic": "game", "about": "Adding Mewtwo"},
        {"stretch": 1, "from": 2, "to": 3, "topic": "other", "about": "Banana bread"},
        {"stretch": 1, "from": 4, "to": 5, "topic": "new", "new_name": "Japan trip", "about": "Itinerary"},
    ]}
    changed = sync.apply_sorting(work, answer, topics, state)
    by_id = {t["id"]: t for t in topics}
    assert by_id["game"]["segments"] == [{"agent_id": "a", "from": 10, "to": 12, "about": "Adding Mewtwo"}]
    assert by_id["other"]["segments"][0]["from"] == 12
    assert by_id["japan-trip"]["segments"] == [{"agent_id": "a", "from": 14, "to": 16, "about": "Itinerary"}]
    assert changed == {"game", "other", "japan-trip"}
    assert state == {"a": 16}


def test_a_subject_that_comes_back_moves_out_of_other_topics() -> None:
    topics = _topics()
    other = next(t for t in topics if t["id"] == "other")
    other["segments"].append({"agent_id": "old", "from": 0, "to": 2, "about": "Phone access"})
    conv = _conv("b", ("you", "phone link broke"), ("ai", "fixed"))
    answer = {"pieces": [{"stretch": 1, "from": 0, "to": 1, "topic": "new", "new_name": "Phone access", "about": "Broken link"}],
              "promote": [{"from_other": "Phone access", "to_topic": "Phone access"}]}
    sync.apply_sorting([{"conversation": conv, "from": 0, "messages": conv["messages"]}], answer, topics, {})
    phone = next(t for t in topics if t["name"] == "Phone access")
    assert other["segments"] == []
    assert [s["agent_id"] for s in phone["segments"]] == ["b", "old"]


def test_a_topic_the_sorter_names_but_does_not_exist_falls_back_to_other_topics() -> None:
    topics = _topics()
    conv = _conv("a", ("you", "x"), ("ai", "y"))
    sync.apply_sorting([{"conversation": conv, "from": 0, "messages": conv["messages"]}],
                       {"pieces": [{"stretch": 1, "from": 0, "to": 1, "topic": "nonexistent", "about": "x"}]}, topics, {})
    assert next(t for t in topics if t["id"] == "other")["segments"][0]["agent_id"] == "a"


def test_a_topic_added_while_sorting_ran_is_kept(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sync, "DATA", tmp_path)
    (tmp_path / "topics.json").write_text(json.dumps([{"id": "sudoku", "name": "Sudoku", "kind": "topic", "segments": []}]))
    topics = sync.starting_topics()
    sync._save_topics(topics)
    assert [t["id"] for t in json.loads((tmp_path / "topics.json").read_text())] == ["sudoku", "other", "internal"]
    assert topics[0]["id"] == "sudoku"


def test_a_conversation_that_goes_on_to_a_new_topic_is_noted_where_it_was() -> None:
    topics = _topics()
    topics[0]["segments"].append({"agent_id": "a", "from": 0, "to": 2, "about": "Game"})
    conv = _conv("a", ("you", "game"), ("ai", "ok"), ("you", "plan Japan"), ("ai", "plan"))
    moves: list[dict] = []
    answer = {"pieces": [{"stretch": 1, "from": 0, "to": 1, "topic": "new", "new_name": "Japan trip", "about": "Itinerary"}]}
    sync.apply_sorting([{"conversation": conv, "from": 2, "messages": conv["messages"][2:]}], answer, topics, {}, moves)
    assert moves == [{"from": "game", "to": "japan-trip", "at": conv["messages"][2]["at"]}]


def test_a_brand_new_conversation_that_starts_a_topic_has_nowhere_to_be_noted() -> None:
    moves: list[dict] = []
    conv = _conv("n", ("you", "plan Japan"), ("ai", "plan"))
    answer = {"pieces": [{"stretch": 1, "from": 0, "to": 1, "topic": "new", "new_name": "Japan trip", "about": "Itinerary"}]}
    sync.apply_sorting([{"conversation": conv, "from": 0, "messages": conv["messages"]}], answer, _topics(), {}, moves)
    assert moves == []
