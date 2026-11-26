"""Every conversation with your AI, organized into living topics: where each subject stands, the work it produced, and the conversation to continue it..

Services run from /home/user/workspace (the repo root). Conventions:

- Persistent state (anything written and read across runs -- cursors,
  caches, snapshots, user records): read and write it under ``DATA_DIR``
  (defined below), never a hardcoded ``data/.apps/context/`` at the
  call site. ``DATA_DIR`` defaults to ``data/.apps/context/`` but
  honors the ``CONTEXT_DATA_DIR`` env var, so an editing agent can point a
  throwaway instance at a *copy* of the data instead of the live store
  (see the update-app skill). Do NOT use ``Path(__file__)``-based
  paths for state -- the bug to avoid is one process writing to
  ``/home/user/workspace/data/.apps/...`` while another reads from
  ``/home/user/workspace/system/apps/<pkg>/data/...``.
- Static assets shipped alongside this file (templates, default
  configs, bundled JSON): ``Path(__file__).parent / "assets/..."`` is
  fine and is the right pattern.
- Listen port: bind ``PORT`` (defined below), which defaults to this
  app's assigned port but honors the ``CONTEXT_PORT`` env var, so
  an editing agent can boot a throwaway instance on a *spare* port
  alongside the live one (see the update-app skill). Never hardcode
  the port at the ``run_simple`` call.

This is a synchronous Flask app served by the threaded Werkzeug server.
The app owns its own browser origin (the forwarder routes
``http://context.<workspace-host>/`` straight to this port), so it serves
at ``/`` and root-absolute URLs, cookies, and service workers all work
unmodified -- nothing rewrites anything. Use ``flask_sock`` if you need
WebSockets.
"""

import base64
import hashlib
import json
import os
import shutil
import time
from pathlib import Path

from app_schema.api_schema import (
    DEFAULT_API_SCHEMA_PATH,
    RouteModels,
    RouteTable,
    SchemalessRoute,
    register_api_schema_route,
)
from app_schema.routes import error_response, model_response, parse_request_body
from flask import Flask, Response, abort, request, send_file
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import setup_logging
from loguru import logger
from pydantic import Field

from context import live, workspace_apps
from context.topics import SkillDoc, TopicsResponse, load_topics, read_skill


class SayRequest(FrozenModel):
    """What the person typed in a topic."""

    text: str = Field(description="The message, as typed")


class DoneResponse(FrozenModel):
    """A change that was made."""

    status: str = Field(description="Always 'ok'")


class DeliverRequest(FrozenModel):
    """Something the assistant made, to show in a topic's main pane."""

    kind: str = Field(description="'app', 'skill' or 'image'")
    title: str = Field(description="What to call it")
    app: str | None = Field(default=None, description="For an app: its registered name")
    skill: str | None = Field(default=None, description="For a skill: its name under .agents/skills/")
    path: str | None = Field(default=None, description="For an image: its absolute path under /home/user/workspace/data")


class SpinOffRequest(FrozenModel):
    """A conversation in Other topics or Internal notes that has grown into a subject of its own."""

    name: str = Field(description="What to call the new topic")
    paragraphs: list[str] = Field(description="Where the subject stands, as the new topic's text")


class SpinOffResponse(FrozenModel):
    """The topic the conversation moved to."""

    topic_id: str = Field(description="The new topic's id, for deliveries and text from now on")


class TextRequest(FrozenModel):
    """A topic's rewritten text."""

    paragraphs: list[str] = Field(description="Short plain paragraphs; <em> and <strong> are kept, anything else shows as typed")
from werkzeug.serving import run_simple

# Persistent state for this app lives under DATA_DIR. It defaults to
# ``data/.apps/context/`` but is overridable via the ``CONTEXT_DATA_DIR`` env var
# so a throwaway instance can run against a *copy* of the data while editing --
# see the update-app skill. Always read/write state through DATA_DIR;
# never hardcode ``data/.apps/context/`` at a call site, or the override is
# bypassed. A writing call site should ``DATA_DIR.mkdir(parents=True,
# exist_ok=True)`` before writing.
DATA_DIR = Path(os.environ.get("CONTEXT_DATA_DIR", "data/.apps/context"))

# Listen port. Defaults to this app's assigned port but is overridable via
# the ``CONTEXT_PORT`` env var so an editing agent can boot a throwaway
# instance on a spare port next to the live one (see the update-app skill).
# Never hardcode the port at the ``run_simple`` call, or the override is bypassed.
PORT = int(os.environ.get("CONTEXT_PORT", "__PORT__"))

# The browser-side modules the workspace shell builds and every app serves from
# its own origin: the app contract (how a page talks to the shell framing it) and
# the element context menu (the right-click menu whose last rows hand the
# clicked element to a chat). A module import is a fetch without cookies, which
# the forwarder refuses across origins, so they are served here rather than from
# the shell. Relative to the repo root the service runs from, like DATA_DIR.
SHELL_STATIC_MODULES_DIR = Path("system/apps/system_interface/imbue/system_interface/static/_static")
SHELL_STATIC_MODULE_NAMES = ('app_contract.js', 'context_menu.js')

# The script every page serves (keep it on every page): it connects the page to
# the shell, reports where the page is on the handshake so the shell can reopen
# this app's window at the same place, and installs the element context menu.
# A page visited outside the shell runs it harmlessly: nothing arrives, and the
# menu's Explain and Modify rows grey out.
SHELL_PAGE_SCRIPT_BODY = """
  import { connectToShell } from "/_static/app_contract.js";
  import { installElementContextMenu } from "/_static/context_menu.js";
  let handshake = null;
  const connection = connectToShell({
    onHandshake: (received) => {
      handshake = received;
      connection.location(location.pathname + location.search, document.title);
    },
  });
  installElementContextMenu({ connection, handshake: () => handshake });
"""
SHELL_PAGE_SCRIPT = f'<script type="module">{SHELL_PAGE_SCRIPT_BODY}</script>'


def inline_script_source(body: str) -> str:
    """The Content-Security-Policy source admitting one inline <script> whose body is exactly ``body``."""
    return "'sha256-" + base64.b64encode(hashlib.sha256(body.encode()).digest()).decode() + "'"


# Every HTML page is served under this policy (the app is reachable from the
# internet once the workspace is shared): scripts only from this origin plus the
# inline blocks listed here by hash, fetches, sockets and images only from this
# origin, inline styles, frames of any http(s) page, no plugins. Anything else a
# page pulls in (a CDN script, Google Fonts, an image hosted elsewhere, a
# third-party API called from the browser) is blocked until its origin is added
# to the matching directive; a new inline <script> needs its
# ``inline_script_source`` added.
# The browser posts every block to ``/_csp-report`` below, which logs it on
# stderr (the supervisor log), so a blocked resource is never silent; the
# ``'report-sample'`` makes the browser include where a blocked inline script
# is and how it starts, since the report otherwise names it only as "inline".
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    f"script-src 'self' 'report-sample' {inline_script_source(SHELL_PAGE_SCRIPT_BODY)}; "
    "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; frame-src http: https:; "
    "object-src 'none'; base-uri 'self'; form-action 'self'; "
    "report-uri /_csp-report"
)
BROWSER_EXTENSION_SCHEMES = ("chrome-extension:", "moz-extension:", "safari-web-extension:")


class HealthResponse(FrozenModel):
    """What the health probe answers."""

    status: str = Field(description="Always 'ok'")


# What every route exchanges, which the app's API document publishes and every parse and answer
# is checked against (build-app skill, "The app describes its own API"): a route that reads or
# answers JSON names its pydantic models here and uses ``parse_request_body`` and
# ``model_response``; a page, a file or a stream is declared schemaless with the reason.
ROUTE_TABLE: RouteTable = {
    ("GET", "/"): SchemalessRoute(reason="the app's page"),
    ("GET", "/_static/{basename}"): SchemalessRoute(reason="a browser-side module the shell builds"),
    ("GET", "/static/{basename}"): SchemalessRoute(reason="a script or stylesheet of the page"),
    ("GET", "/api/topics"): RouteModels(response_model=TopicsResponse),
    ("GET", "/api/skills/{name}"): RouteModels(response_model=SkillDoc),
    ("POST", "/api/topics/{topic_id}/message"): RouteModels(request_model=SayRequest, response_model=DoneResponse),
    ("POST", "/api/topics/{topic_id}/deliveries"): RouteModels(request_model=DeliverRequest, response_model=DoneResponse, success_status=201),
    ("PUT", "/api/topics/{topic_id}/text"): RouteModels(request_model=TextRequest, response_model=DoneResponse),
    ("POST", "/api/topics/{topic_id}/spin-off"): RouteModels(request_model=SpinOffRequest, response_model=SpinOffResponse, success_status=201),
    ("GET", "/media/{path}"): SchemalessRoute(reason="a picture a topic produced or was sent"),
    ("GET", "/health"): RouteModels(response_model=HealthResponse),
    ("POST", "/_csp-report"): SchemalessRoute(reason="the browser's report of what the page's policy blocked"),
}

app = Flask("context", static_folder=None)
# The app describes its own API: an OpenAPI document of every route below, served at the
# manifest's api_schema_path.
register_api_schema_route(
    app, DEFAULT_API_SCHEMA_PATH, title='Context', description='Every conversation with your AI, organized into living topics: where each subject stands, the work it produced, and the conversation to continue it.', route_table=ROUTE_TABLE
)


@app.after_request
def add_content_security_policy(response: Response) -> Response:
    if response.mimetype == "text/html":
        response.headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
    return response


STATIC_DIR = Path(__file__).parent / "static"
STATIC_TYPES = {"app.js": "text/javascript", "style.css": "text/css"}


@app.route("/")
def index() -> Response:
    """The app's page."""
    page = (STATIC_DIR / "index.html").read_text().replace("</body>", SHELL_PAGE_SCRIPT + "</body>")
    return Response(page, mimetype="text/html")


@app.route("/api/topics")
def topics() -> Response:
    """Every topic with its current text and the messages that touched it, exactly as exchanged."""
    return model_response(load_topics(Path(DATA_DIR), app_links(), busy_chats()))


def busy_chats() -> set[str]:
    return live.busy_chat_ids()


def seeded_topic(topic_id: str) -> dict | None:
    if not (Path(DATA_DIR) / "topics.json").is_file():
        return None
    return next((t for t in json.loads((Path(DATA_DIR) / "topics.json").read_text()) if t["id"] == topic_id), None)


@app.route("/api/topics/<topic_id>/message", methods=["POST"])
def say(topic_id: str) -> Response:
    """Send what the person typed into the topic's own conversation with the assistant, starting it if needed."""
    body = parse_request_body(SayRequest)
    topic = seeded_topic(topic_id)
    if topic is None:
        return error_response(f"There is no topic {topic_id}", 404)
    if not body.text.strip():
        return error_response("The message is empty", 400)
    state = live.read_state(Path(DATA_DIR))
    going = None
    if topic_id not in state["chats"]:
        data = Path(DATA_DIR)
        conversations = {c["agent_id"]: c for c in json.loads((data / "conversations.json").read_text())}
        sort_state = json.loads((data / "sort_state.json").read_text()) if (data / "sort_state.json").is_file() else {}
        going = live.continuing_chat(topic, json.loads((data / "topics.json").read_text()), conversations, sort_state)
    if going:
        ok, detail = live.send_to_chat(going["chat_id"], body.text)
    else:
        current = state["prose"].get(topic_id)
        ok, detail = live.send(Path(DATA_DIR), {**topic, "prose": current or topic["prose"]}, body.text)
    if not ok:
        return error_response(detail, 502)
    return model_response(DoneResponse(status="ok"))


@app.route("/api/topics/<topic_id>/deliveries", methods=["POST"])
def deliver(topic_id: str) -> Response:
    """Put something the assistant made in front of the person, in the topic's main pane."""
    body = parse_request_body(DeliverRequest)
    if seeded_topic(topic_id) is None:
        return error_response(f"There is no topic {topic_id}", 404)
    artifact: dict = {"kind": body.kind, "title": body.title}
    if body.kind == "app" and body.app:
        artifact["app"] = body.app
    elif body.kind == "skill" and body.skill and (Path(".agents/skills") / body.skill / "SKILL.md").is_file():
        artifact["skill"] = body.skill
    elif body.kind == "image" and body.path:
        source = Path(body.path).resolve()
        if not source.is_file() or Path("data").resolve() not in source.parents or source.suffix.lower() not in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"):
            return error_response("An image must be a picture file under the workspace's data folder", 400)
        (Path(DATA_DIR) / "pictures").mkdir(parents=True, exist_ok=True)
        target = Path(DATA_DIR) / "pictures" / f"{topic_id}-{source.name}"
        shutil.copyfile(source, target)
        artifact["src"] = f"pictures/{target.name}"
    else:
        return error_response("An app needs 'app', a skill an installed 'skill', an image a 'path'", 400)
    live.add_artifact(Path(DATA_DIR), topic_id, artifact)
    return model_response(DoneResponse(status="ok"), 201)


@app.route("/api/topics/<topic_id>/text", methods=["PUT"])
def rewrite(topic_id: str) -> Response:
    """Replace the topic's text with where it stands now."""
    body = parse_request_body(TextRequest)
    if seeded_topic(topic_id) is None:
        return error_response(f"There is no topic {topic_id}", 404)
    paragraphs = [p.strip() for p in body.paragraphs if p.strip()]
    if not paragraphs:
        return error_response("The text is empty", 400)
    live.set_prose(Path(DATA_DIR), topic_id, paragraphs)
    return model_response(DoneResponse(status="ok"))


@app.route("/api/topics/<topic_id>/spin-off", methods=["POST"])
def spin_off(topic_id: str) -> Response:
    """Give a conversation going on in Other topics or Internal notes a topic of its own, with what it delivered."""
    body = parse_request_body(SpinOffRequest)
    topic = seeded_topic(topic_id)
    if topic is None:
        return error_response(f"There is no topic {topic_id}", 404)
    if topic.get("kind", "topic") == "topic":
        return error_response("Only a conversation in Other topics or Internal notes moves to a topic of its own", 400)
    paragraphs = [p.strip() for p in body.paragraphs if p.strip()]
    if not body.name.strip() or not paragraphs:
        return error_response("The new topic needs a name and some text", 400)
    try:
        new_id = live.spin_off(Path(DATA_DIR), topic_id, body.name.strip(), paragraphs)
    except live.SpinOffError as e:
        return error_response(str(e), 409)
    return model_response(SpinOffResponse(topic_id=new_id), 201)


_app_links: tuple[float, dict[str, str]] = (0.0, {})


def app_links() -> dict[str, str]:
    """Each app's address, as the workspace opens it; re-read at most once a minute."""
    global _app_links
    if time.time() - _app_links[0] > 60:
        listed = workspace_apps.listed_apps()
        if listed is not None:
            _app_links = (time.time(), {a["name"]: a["link"] for a in listed})
    return _app_links[1]


@app.route("/api/skills/<name>")
def skill(name: str) -> Response:
    """A skill's own instructions, so a topic can show what it produced."""
    raw_topics = json.loads((Path(DATA_DIR) / "topics.json").read_text()) if (Path(DATA_DIR) / "topics.json").is_file() else []
    delivered = live.read_state(Path(DATA_DIR))["artifacts"]
    for raw in raw_topics:
        for artifact in [*raw.get("artifacts", []), *delivered.get(raw["id"], [])]:
            if artifact.get("skill") == name:
                doc = read_skill(name, artifact.get("zip"))
                if doc is not None:
                    return model_response(doc)
    return error_response(f"No skill called {name} belongs to a topic", 404)


ARTIFACT_ROOTS = {"pictures": Path(DATA_DIR) / "pictures", "uploads": Path("data/uploads")}


@app.route("/media/<path:path>")
def artifact_file(path: str) -> Response:
    """A picture a topic produced or was sent."""
    root_name, _, rest = path.partition("/")
    root = ARTIFACT_ROOTS.get(root_name)
    if root is None:
        abort(404)
    target = (root / rest).resolve()
    if not target.is_file() or root.resolve() not in target.parents:
        abort(404)
    return send_file(target)


@app.route("/static/<basename>")
def static_asset(basename: str) -> Response:
    """A script or stylesheet of the page."""
    if basename not in STATIC_TYPES:
        abort(404)
    return Response((STATIC_DIR / basename).read_text(), mimetype=STATIC_TYPES[basename])


@app.route("/_csp-report", methods=["POST"])
def content_security_policy_report() -> Response:
    """Where the browser reports what the page's security policy blocked."""
    body = request.get_json(force=True, silent=True)
    report = body.get("csp-report") if isinstance(body, dict) else None
    if not isinstance(report, dict):
        return Response(status=204)
    blocked, page, source, directive, line, sample = (
        str(report.get(key, ""))[:500]
        for key in ("blocked-uri", "document-uri", "source-file", "effective-directive", "line-number", "script-sample")
    )
    # A block caused by a browser extension is the policy doing its job and is not logged.
    if blocked.startswith(BROWSER_EXTENSION_SCHEMES) or source.startswith(BROWSER_EXTENSION_SCHEMES):
        return Response(status=204)
    # An inline script or style is reported as "inline": say where it is and how it starts instead.
    where = f" at {source}:{line}" if source and line else ""
    starting = f" starting {sample!r}" if sample else ""
    remedy = (
        "add its inline_script_source to CONTENT_SECURITY_POLICY if the page needs it"
        if blocked == "inline"
        else "add its origin to CONTENT_SECURITY_POLICY if the page needs it"
    )
    logger.error(
        "Content-Security-Policy blocked {!r} on {!r} ({}){}{}; {}", blocked, page, directive, where, starting, remedy
    )
    return Response(status=204)


@app.route("/_static/<basename>")
def shell_module(basename: str) -> Response:
    """One of the two browser-side modules the workspace shell builds and every app serves from its own origin."""
    # The two shell-built modules and nothing else: a name that is not one of
    # them is a 404, so this route can never read outside that directory.
    if basename not in SHELL_STATIC_MODULE_NAMES:
        abort(404)
    module_path = SHELL_STATIC_MODULES_DIR / basename
    if not module_path.is_file():
        abort(404)
    # Flask resolves a relative path against the app's own directory, not the cwd.
    return send_file(module_path.absolute(), mimetype="text/javascript")


@app.route("/health")
def health() -> Response:
    """Whether the app is up."""
    return model_response(HealthResponse(status="ok"))


def main() -> None:
    setup_logging()
    run_simple(
        "127.0.0.1", PORT, app, threaded=True, use_reloader=False, use_debugger=False
    )


if __name__ == "__main__":
    main()
