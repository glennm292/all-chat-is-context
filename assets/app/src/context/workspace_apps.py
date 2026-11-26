"""The apps this workspace has, read off the shell's inventory.

What ``workspace-layout list`` prints for its apps, read the same way (the shell's ``/api/inventory``) without
starting that command as a process: every app a user can open, with its display name and the link to write for it.
"""

import json
import os
import urllib.error
import urllib.request

from loguru import logger

SHELL_URL = os.environ.get("WORKSPACE_SHELL_URL", "http://127.0.0.1:8000").rstrip("/")
INVENTORY_ROUTE = "/api/inventory"
TIMEOUT_SECONDS = 10


def app_link(app: dict) -> str:
    """The link to write for an app, as ``workspace-layout list`` gives it: its address on the domain the workspace
    was last shared under, else its port-less ``<label>.localhost`` address."""
    if app.get("share_url"):
        return app["share_url"]
    return f"http://{app.get('label') or app['name']}.localhost/"


def listed_apps() -> list[dict] | None:
    """Every app a user can open (internal ones left out): ``{"name", "display_name", "link"}``; None when the
    shell cannot be read."""
    try:
        with urllib.request.urlopen(
            SHELL_URL + INVENTORY_ROUTE, timeout=TIMEOUT_SECONDS
        ) as response:
            inventory = json.loads(response.read())
        return [
            {
                "name": app["name"],
                "display_name": app.get("display_name") or app["name"],
                "link": app_link(app),
            }
            for app in inventory["apps"]
            if not app.get("internal")
        ]
    except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError) as error:
        logger.warning("Could not read the workspace's apps from the shell: {}", error)
        return None
