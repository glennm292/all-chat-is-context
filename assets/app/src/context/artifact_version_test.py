"""The version stamp each work product carries, so the page reloads one that was modified."""

import json
import os
from pathlib import Path

import pytest

from context import live, topics
from context.topics import artifact_version, load_topics


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A workspace root (the app runs from it) holding one app, one skill and one picture; the stamp cache starts empty."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(topics, "_versions", {})
    monkeypatch.setattr(live, "MNGR_ROOT", tmp_path / "mngr")
    app = tmp_path / "system" / "apps" / "sudoku_game"
    (app / "src" / "sudoku" / "static").mkdir(parents=True)
    (app / "app.toml").write_text('name = "sudoku"\ndisplay_name = "Sudoku"\n')
    (app / "src" / "sudoku" / "static" / "game.js").write_text("// the game")
    other = tmp_path / "system" / "apps" / "sudoku_helper"
    other.mkdir()
    (other / "app.toml").write_text('name = "sudoku-helper"\n')
    skill = tmp_path / ".agents" / "skills" / "plan-trip"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# Plan a trip")
    pictures = tmp_path / "data" / ".apps" / "context" / "pictures"
    pictures.mkdir(parents=True)
    (pictures / "japan-map.png").write_bytes(b"png")
    return tmp_path


def _touch(path: Path, ns: int) -> None:
    """Give a file a modification time well after anything the fixture wrote."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text("x")
    os.utime(path, ns=(ns, ns))


FUTURE = 4_000_000_000_000_000_000
SUDOKU = {"kind": "app", "title": "Sudoku", "app": "sudoku"}


def _fresh_version(artifact: dict, data_dir: Path) -> str | None:
    topics._versions.clear()
    return artifact_version(artifact, data_dir)


def test_an_apps_stamp_moves_when_its_code_changes(workspace: Path) -> None:
    data_dir = workspace / "data" / ".apps" / "context"
    before = _fresh_version(SUDOKU, data_dir)
    assert before is not None
    _touch(workspace / "system/apps/sudoku_game/src/sudoku/static/game.js", FUTURE)
    assert _fresh_version(SUDOKU, data_dir) == str(FUTURE)


@pytest.mark.parametrize("path", [
    ".venv/lib/site.py", ".pytest_cache/v/cache", ".coverage", "__pycache__/runner.cpython-312.pyc",
    "src/sudoku/__pycache__/x.pyc", "node_modules/pkg/index.js", "tests/test_game.py", "test_sudoku_game.py",
    "src/sudoku/runner_test.py", "conftest.py",
])
def test_an_apps_stamp_ignores_tests_caches_and_installed_packages(workspace: Path, path: str) -> None:
    data_dir = workspace / "data" / ".apps" / "context"
    before = _fresh_version(SUDOKU, data_dir)
    _touch(workspace / "system/apps/sudoku_game" / path, FUTURE)
    assert _fresh_version(SUDOKU, data_dir) == before


def test_an_app_is_found_by_its_manifest_name_not_its_folder(workspace: Path) -> None:
    data_dir = workspace / "data" / ".apps" / "context"
    _touch(workspace / "system/apps/sudoku_helper/helper.py", FUTURE)
    assert _fresh_version(SUDOKU, data_dir) != str(FUTURE)
    assert _fresh_version({"kind": "app", "title": "Helper", "app": "sudoku-helper"}, data_dir) == str(FUTURE)
    assert _fresh_version({"kind": "app", "title": "Gone", "app": "gone"}, data_dir) is None


def test_a_skills_stamp_moves_when_its_files_change(workspace: Path) -> None:
    data_dir = workspace / "data" / ".apps" / "context"
    trip = {"kind": "skill", "title": "Plan a trip", "skill": "plan-trip"}
    assert _fresh_version(trip, data_dir) is not None
    _touch(workspace / ".agents/skills/plan-trip/scripts/book.py", FUTURE)
    assert _fresh_version(trip, data_dir) == str(FUTURE)
    assert _fresh_version({"kind": "skill", "title": "Escape", "skill": "../../etc"}, data_dir) is None


def test_a_pictures_stamp_moves_when_the_picture_is_replaced(workspace: Path) -> None:
    data_dir = workspace / "data" / ".apps" / "context"
    picture = {"kind": "image", "title": "Map", "src": "pictures/japan-map.png"}
    assert _fresh_version(picture, data_dir) is not None
    _touch(data_dir / "pictures" / "japan-map.png", FUTURE)
    assert _fresh_version(picture, data_dir) == str(FUTURE)
    _touch(workspace / "data/uploads/photo.jpg", FUTURE + 1)
    assert _fresh_version({"kind": "image", "title": "Photo", "src": "uploads/photo.jpg"}, data_dir) == str(FUTURE + 1)
    assert _fresh_version({"kind": "image", "title": "Elsewhere", "src": "secrets/key.png"}, data_dir) is None
    assert _fresh_version({"kind": "image", "title": "Missing", "src": "pictures/none.png"}, data_dir) is None


def test_a_link_with_nothing_to_watch_has_no_stamp(workspace: Path) -> None:
    assert _fresh_version({"kind": "link", "title": "Repo", "github": "https://github.com/x/y"}, workspace) is None


def test_the_stamp_is_reread_only_every_few_seconds(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data_dir = workspace / "data" / ".apps" / "context"
    clock = [100.0]
    monkeypatch.setattr(topics.time, "monotonic", lambda: clock[0])
    before = artifact_version(SUDOKU, data_dir)
    _touch(workspace / "system/apps/sudoku_game/src/sudoku/static/game.js", FUTURE)
    clock[0] += topics._VERSION_TTL - 0.5
    assert artifact_version(SUDOKU, data_dir) == before
    clock[0] += 1.0
    assert artifact_version(SUDOKU, data_dir) == str(FUTURE)


def test_each_work_product_in_a_topic_carries_its_current_stamp(workspace: Path) -> None:
    data_dir = workspace / "data" / ".apps" / "context"
    (data_dir / "conversations.json").write_text("[]")
    # A stamp stored with the topic is stale by definition: the one served is always read from disk.
    stored = [{**SUDOKU, "version": "stale"}, {"kind": "image", "title": "Map", "src": "pictures/japan-map.png"}]
    (data_dir / "topics.json").write_text(json.dumps([
        {"id": "sudoku", "name": "Sudoku", "kind": "topic", "prose": [], "artifacts": stored, "segments": []},
    ]))
    _touch(workspace / "system/apps/sudoku_game/src/sudoku/static/game.js", FUTURE)
    artifacts = load_topics(data_dir, {"sudoku": "http://sudoku.localhost/"}).topics[0].artifacts
    assert [(a.title, a.version, a.link) for a in artifacts] == [
        ("Sudoku", str(FUTURE), "http://sudoku.localhost/"),
        ("Map", str((data_dir / "pictures" / "japan-map.png").stat().st_mtime_ns), None),
    ]
