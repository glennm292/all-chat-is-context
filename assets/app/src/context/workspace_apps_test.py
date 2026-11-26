import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from context import workspace_apps


def test_an_app_is_linked_on_its_shared_domain_else_its_localhost_label() -> None:
    assert workspace_apps.app_link(
        {"name": "files", "label": "files-1", "share_url": "https://f.example/"}
    ) == ("https://f.example/")
    assert (
        workspace_apps.app_link(
            {"name": "files", "label": "files-1", "share_url": None}
        )
        == "http://files-1.localhost/"
    )
    assert (
        workspace_apps.app_link({"name": "files", "label": None, "share_url": None})
        == "http://files.localhost/"
    )


def test_the_apps_are_read_off_the_shells_inventory_leaving_internal_ones_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventory = {
        "apps": [
            {
                "name": "files",
                "display_name": "Files",
                "label": "files-1",
                "share_url": None,
                "internal": False,
            },
            {
                "name": "system_interface",
                "display_name": "Shell",
                "label": None,
                "share_url": None,
                "internal": True,
            },
        ]
    }

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = (
                json.dumps(inventory).encode()
                if self.path == "/api/inventory"
                else b"{}"
            )
            self.send_response(200 if self.path == "/api/inventory" else 404)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002 -- the base method names it so
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setattr(
            workspace_apps, "SHELL_URL", f"http://127.0.0.1:{server.server_port}"
        )
        assert workspace_apps.listed_apps() == [
            {
                "name": "files",
                "display_name": "Files",
                "link": "http://files-1.localhost/",
            }
        ]
    finally:
        server.shutdown()


def test_an_unreachable_shell_lists_nothing_rather_than_failing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(workspace_apps, "SHELL_URL", "http://127.0.0.1:1")
    assert workspace_apps.listed_apps() is None
