"""The work product on show in the middle pane reloads by itself once it is modified, driven in a real browser.

The page's clock is faked, so the 10-second poll and the 4-second settle run in moments.
"""

import json
import os
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from playwright.sync_api import Browser
from playwright.sync_api import Page
from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server

from context import live, runner, topics

pytestmark = [pytest.mark.browser, pytest.mark.timeout(120, func_only=False)]

# Playwright's browser-cache lookup only finds builds it downloaded itself, so the workspace's own engine is named.
FORTRESS_CHROMIUM_PATH = Path("/opt/fortress/tilion-fortress/tilion")
FUTURE = 4_000_000_000_000_000_000


@pytest.fixture(scope="module")
def module_browser() -> Iterator[Browser]:
    """A Chromium for this module; without Fortress (CI, a laptop) Playwright's own, and one that cannot launch fails."""
    launch_args = {"executable_path": str(FORTRESS_CHROMIUM_PATH)} if FORTRESS_CHROMIUM_PATH.exists() else {}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(**launch_args)
        try:
            yield browser
        finally:
            browser.close()


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A workspace with a Sudoku topic (the app and a picture) and a Japan topic (another app)."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(topics, "_versions", {})
    # The stamp cache runs on the server's real clock, which the faked page clock outruns.
    monkeypatch.setattr(topics, "_VERSION_TTL", 0.0)
    monkeypatch.setattr(live, "MNGR_ROOT", tmp_path / "mngr")
    for folder, name in (("sudoku", "sudoku"), ("trip_planner", "trip-planner")):
        app = tmp_path / "system" / "apps" / folder
        (app / "static").mkdir(parents=True)
        (app / "app.toml").write_text(f'name = "{name}"\n')
        (app / "static" / "game.js").write_text("// the app")
    data_dir = tmp_path / "data" / ".apps" / "context"
    (data_dir / "pictures").mkdir(parents=True)
    (data_dir / "pictures" / "board.png").write_bytes(b"png")
    (data_dir / "conversations.json").write_text("[]")
    (data_dir / "topics.json").write_text(json.dumps([
        {"id": "sudoku", "name": "Sudoku", "kind": "topic", "prose": ["A Sudoku."], "segments": [], "artifacts": [
            {"kind": "app", "title": "Sudoku", "app": "sudoku"},
            {"kind": "image", "title": "Board", "src": "pictures/board.png"},
        ]},
        {"id": "japan", "name": "Japan trip", "kind": "topic", "prose": ["A trip."], "segments": [], "artifacts": [
            {"kind": "app", "title": "Trip planner", "app": "trip-planner"},
        ]},
    ]))
    monkeypatch.setattr(runner, "DATA_DIR", data_dir)
    monkeypatch.setattr(runner, "ARTIFACT_ROOTS", {"pictures": data_dir / "pictures"})
    monkeypatch.setattr(runner, "busy_chats", set)
    return tmp_path


@pytest.fixture
def page(module_browser: Browser, workspace: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Page]:
    """The Context page from a throwaway server over the workspace, with the page's clock under the test's control."""
    server = make_server("127.0.0.1", 0, runner.app, threaded=True)
    base = f"http://127.0.0.1:{server.server_port}"
    # Each app's address: a page of this same server, so the frame loads without a second server.
    monkeypatch.setattr(runner, "app_links", lambda: {"sudoku": f"{base}/static/style.css", "trip-planner": f"{base}/static/app.js"})
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    context = module_browser.new_context(viewport={"width": 1400, "height": 900})
    try:
        page = context.new_page()
        page.clock.install()
        page.goto(base + "/")
        page.wait_for_selector("#stage iframe")
        yield page
    finally:
        context.close()
        server.shutdown()
        thread.join()


def _touch(path: Path, ns: int) -> None:
    os.utime(path, ns=(ns, ns))


def _mark_current_frame(page: Page) -> None:
    """Tag the frame on show, so a reload (which builds a new one) is visible as the tag's absence."""
    page.eval_on_selector("#stage iframe", "f => f.dataset.before = '1'")


def _advance(page: Page, ms: int) -> None:
    """Move the page's clock on in small slices, letting each poll's request finish before the next timer is due."""
    for _ in range(ms // 1000):
        page.clock.run_for(1000)
        page.wait_for_timeout(30)


def test_the_app_on_show_reloads_once_its_code_changes(page: Page, workspace: Path) -> None:
    _mark_current_frame(page)
    _advance(page, 25_000)  # two polls with nothing modified: the frame stays as it was
    assert page.locator("#stage iframe[data-before]").count() == 1

    _touch(workspace / "system/apps/sudoku/static/game.js", FUTURE)
    _advance(page, 16_000)  # the next poll sees the change; the reload follows once it settles
    frame = page.locator("#stage iframe")
    assert frame.count() == 1
    assert frame.get_attribute("data-before") is None
    assert frame.get_attribute("title") == "Sudoku"
    assert page.locator(".thing-tab.on").inner_text().endswith("Sudoku")

    _mark_current_frame(page)
    _advance(page, 25_000)  # reloaded once for the change, not again on every later poll
    assert page.locator("#stage iframe[data-before]").count() == 1


def test_a_change_to_a_work_product_not_on_show_leaves_the_view_alone(page: Page, workspace: Path) -> None:
    _mark_current_frame(page)
    _touch(workspace / "system/apps/trip_planner/static/game.js", FUTURE)
    _touch(workspace / "data/.apps/context/pictures/board.png", FUTURE)
    _advance(page, 25_000)
    assert page.locator("#stage iframe[data-before]").count() == 1


def test_switching_topic_before_the_reload_keeps_the_new_topic_on_show(page: Page, workspace: Path) -> None:
    _touch(workspace / "system/apps/sudoku/static/game.js", FUTURE)
    _advance(page, 11_000)  # the poll has seen the change and the reload is waiting to settle
    page.click(".topic-link[data-topic-id='japan']")
    page.wait_for_selector("#stage iframe[title='Trip planner']")
    _mark_current_frame(page)
    _advance(page, 8_000)
    frame = page.locator("#stage iframe")
    assert frame.get_attribute("title") == "Trip planner"
    assert frame.get_attribute("data-before") == "1"


def test_a_picture_on_show_reloads_past_the_browsers_cache_once_replaced(page: Page, workspace: Path) -> None:
    page.click(".thing-tab[data-artifact='Board']")
    img = page.locator("#stage img")
    first = img.get_attribute("src")
    assert first is not None and first.startswith("/media/pictures/board.png?v=")
    _touch(workspace / "data/.apps/context/pictures/board.png", FUTURE)
    _advance(page, 16_000)
    assert img.get_attribute("src") == f"/media/pictures/board.png?v={FUTURE}"
    assert page.locator(".thing-tab.on").inner_text().endswith("Board")
