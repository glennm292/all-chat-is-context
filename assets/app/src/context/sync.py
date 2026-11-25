"""Keep Context current: re-read every conversation, sort what is new into topics, rewrite the topics that changed.

Run as `uv run python -m context.sync` from the repo root (the Context routine does this on a schedule).

  sort_state.json  agent id -> how many of its messages have been sorted
  topics.json      the topics and the stretches of conversation that make them up (this file is owned here)
  live.json        each topic's current text (written here and by the assistant in a topic)

A conversation nobody but the agent spoke in goes to Internal notes without asking Claude. Everything else is
sorted by one completion per run, then each changed topic gets its text rewritten by one completion each.
"""

import argparse
import html
import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from loguru import logger

from context import live, transcripts
from context.claude_p import claude_p_completion
from context.topics import NOTE_PATTERNS, SKIPPED

DATA = transcripts.OUT.parent
SORT_MODEL = "claude-sonnet-5-5"
WRITE_MODEL = "claude-sonnet-5-5"
MESSAGE_CHARS = 700   # how much of each message the sorter reads
BATCH_MESSAGES = 160  # how many new messages one sorting call takes at most
QUIET_FOR = timedelta(minutes=10)  # a conversation is sorted once it has been quiet this long


def _read(name: str, default):
    path = DATA / name
    return json.loads(path.read_text()) if path.is_file() else default


def _save_topics(topics: list[dict]) -> None:
    """Write the topics, keeping any a topic's conversation gave itself while this run was sorting (live.spin_off)."""
    known = {t["id"] for t in topics}
    added = [t for t in _read("topics.json", []) if t["id"] not in known]
    _write("topics.json", added + topics)
    topics[:0] = added


def _write(name: str, value) -> None:
    tmp = (DATA / name).with_suffix(".tmp")
    tmp.write_text(json.dumps(value, indent=1))
    tmp.replace(DATA / name)


class SortingAnswerError(ValueError):
    """Claude's answer to a sorting or writing request was not the JSON asked for."""


def log(*parts) -> None:
    logger.info(" ".join(str(p) for p in parts))


def starting_topics() -> list[dict]:
    """What a workspace starts with before anything is sorted: the two topics every workspace has."""
    return [
        {"id": "other", "kind": "other", "name": "Other topics", "segments": [], "prose": [], "artifacts": []},
        {"id": "internal", "kind": "internal", "name": "Internal notes", "segments": [], "prose": [], "artifacts": []},
    ]


def _typed_by_person(message: dict) -> bool:
    return message["who"] == "you" and not NOTE_PATTERNS.match(message["text"]) and message["text"].strip() not in SKIPPED


def pending_work(conversations: list[dict], sort_state: dict, topic_chats: set[str]) -> list[dict]:
    """Each conversation's messages not yet sorted, as {conversation, from, messages}."""
    work = []
    for conv in conversations:
        if conv["agent_id"] in topic_chats or conv.get("context_topic"):
            continue  # a topic's own conversation is already in its topic
        done = sort_state.get(conv["agent_id"], 0)
        last = datetime.fromisoformat(conv["messages"][-1]["at"].replace("Z", "+00:00")) if conv["messages"] else None
        if last and datetime.now(timezone.utc) - last < QUIET_FOR:
            continue  # still going: sort it once it has settled
        if len(conv["messages"]) > done:
            work.append({"conversation": conv, "from": done, "messages": conv["messages"][done:]})
    return work


def _last_topic_of(agent_id: str, topics: list[dict]) -> str | None:
    best = None
    for t in topics:
        for seg in t["segments"]:
            if seg["agent_id"] == agent_id and (best is None or seg["from"] >= best[1]):
                best = (t["id"], seg["from"])
    return best[0] if best else None


SORT_SYSTEM = """You sort a person's conversations with their AI assistant into topics: the subjects they keep coming back to.
You are given the existing topics and some new stretches of conversation. Split each stretch where its subject changes, and put each piece in the topic it belongs to.
- Prefer an existing topic when the piece is about the same subject, even loosely.
- Use "other" for a short one-off exchange that fits no topic.
- Use "internal" for messages where only the assistant or automated systems speak (background work, failed starts), and for workspace updates and other upkeep the assistant runs even when the person approved a step of it.
- Start a new topic only for a substantial subject not covered by any topic, or for a subject that has now come up again after sitting in "other".
- "about" is 2 to 6 plain words saying what that piece of conversation was about, in the person's terms.
Answer with JSON only, no prose: {"pieces": [{"stretch": <stretch number>, "from": <first message number>, "to": <last message number>, "topic": "<topic id>" | "new", "new_name": "<name, only for new>", "about": "<words>"}], "promote": [{"from_other": "<about text of the Other topics piece>", "to_topic": "<topic id or the new_name of a new topic>"}]}
Cover every message of every stretch exactly once, in order."""


def sort_batch(work: list[dict], topics: list[dict]) -> dict:
    lines = ["EXISTING TOPICS:"]
    for t in topics:
        lead = re.sub(r"<[^>]+>", "", html.unescape(live_prose(t)[0])) if live_prose(t) else ""
        lines.append(f'- id "{t["id"]}": {t["name"]}. {lead[:300]}')
    other = next((t for t in topics if t["id"] == "other"), None)
    if other:
        lines.append("Pieces already in Other topics: " + "; ".join(s.get("about", "") for s in other["segments"]))
    lines.append("\nNEW STRETCHES:")
    for n, item in enumerate(work, 1):
        conv = item["conversation"]
        previous = _last_topic_of(conv["agent_id"], topics)
        lines.append(f'\n## Stretch {n}: from the chat "{conv["name"]}"' + (f' (earlier parts of this chat went to "{previous}")' if previous else ""))
        for k, m in enumerate(item["messages"]):
            who = "PERSON" if _typed_by_person(m) else ("ASSISTANT" if m["who"] == "ai" else "SYSTEM")
            text = " ".join(m["text"].split())[:MESSAGE_CHARS]
            lines.append(f"[{k}] {m['at'][:16]} {who}: {text}")
    result = claude_p_completion("\n".join(lines), system=SORT_SYSTEM, model=SORT_MODEL)
    log(f"sorted {len(work)} stretch(es), ${result.cost_usd:.3f}")
    found = re.search(r"\{.*\}", result.text, re.S)
    if not found:
        raise SortingAnswerError(f"The sorter did not answer with JSON: {result.text[:300]}")
    return json.loads(found.group(0))


def live_prose(topic: dict) -> list[str]:
    return live.read_state(DATA)["prose"].get(topic["id"]) or topic.get("prose", [])


def apply_sorting(work: list[dict], answer: dict, topics: list[dict], sort_state: dict, moves: list[dict] | None = None) -> set[str]:
    """Add the sorted pieces to their topics; returns the ids of the topics that gained something.

    A conversation that went on from one topic to a newly made one is added to ``moves`` as {from, to, at}."""
    by_id = {t["id"]: t for t in topics}
    new_by_name: dict[str, str] = {}
    changed: set[str] = set()
    for piece in answer.get("pieces", []):
        item = work[int(piece["stretch"]) - 1]
        topic_id = piece["topic"]
        previous = _last_topic_of(item["conversation"]["agent_id"], topics)
        if topic_id == "new":
            name = (piece.get("new_name") or piece.get("about") or "New topic").strip()
            topic_id = new_by_name.get(name)
            if topic_id is None:
                topic_id = live.slug(name, set(by_id))
                by_id[topic_id] = {"id": topic_id, "name": name, "kind": "topic", "segments": [], "prose": [], "artifacts": []}
                topics.insert(0, by_id[topic_id])
                new_by_name[name] = topic_id
                log(f"new topic: {name}")
                if previous and previous != topic_id and moves is not None:  # it was going on elsewhere: say where it went
                    moves.append({"from": previous, "to": topic_id, "at": item["messages"][int(piece["from"])]["at"]})
        if topic_id not in by_id:
            topic_id = "other"
        start = item["from"] + int(piece["from"])
        end = item["from"] + int(piece["to"]) + 1
        by_id[topic_id]["segments"].append({"agent_id": item["conversation"]["agent_id"], "from": start, "to": end,
                                             "about": piece.get("about", "")})
        changed.add(topic_id)
    for move in answer.get("promote", []):
        target = new_by_name.get(move.get("to_topic"), move.get("to_topic"))
        other = by_id.get("other")
        if not other or target not in by_id or target == "other":
            continue
        keep = [s for s in other["segments"] if s.get("about") != move.get("from_other")]
        moved = [s for s in other["segments"] if s.get("about") == move.get("from_other")]
        if moved:
            other["segments"] = keep
            by_id[target]["segments"].extend(moved)
            changed |= {"other", target}
            log(f"moved '{move.get('from_other')}' out of Other topics into {target}")
    for item in work:
        sort_state[item["conversation"]["agent_id"]] = item["from"] + len(item["messages"])
    return changed


def internal_only(item: dict) -> bool:
    """Nobody but the agent ever spoke in the whole conversation (not merely in its newest stretch)."""
    return not any(_typed_by_person(m) for m in item["conversation"]["messages"])


WRITE_SYSTEM = """You write the living text of one topic in a person's workspace: a few short paragraphs saying where the subject stands now, for the person themselves.
Plain, concrete, in the person's own terms; no headings, lists, greetings or sign-offs; never mention "the topic" or "the text".
Keep what is still true from the current text, fold in what the new conversation changed, and drop what is no longer true.
You may link a phrase to the exact message it comes from with <a data-moment="REF">phrase</a>, using a REF given beside a message, and to another topic with <a data-topic="ID">phrase</a>. Use links sparingly, where they help the person find the moment. No other markup except <em>.
Also list which of the workspace's apps and skills (named in the prompt) this topic made or is about, if any; leave out ones merely mentioned in passing.
Answer with JSON only: {"paragraphs": ["...", "..."], "name": "<the topic's name, changed only if the subject has clearly shifted>", "works": [{"kind": "app" | "skill", "name": "<exact name from the list>"}]}"""


def rewrite(topic: dict, conversations: dict, topics: list[dict]) -> dict | None:
    recent = []
    for seg in topic["segments"][-12:]:
        conv = conversations.get(seg["agent_id"])
        if not conv:
            continue
        for i in range(seg["from"], seg["to"] if seg["to"] is not None else len(conv["messages"])):
            if i < len(conv["messages"]):
                m = conv["messages"][i]
                if m["text"].strip() in SKIPPED:
                    continue
                who = "PERSON" if _typed_by_person(m) else ("ASSISTANT" if m["who"] == "ai" else "SYSTEM")
                recent.append(f"(REF {seg['agent_id']}:{i}) {m['at'][:10]} {who}: {' '.join(m['text'].split())[:900]}")
    if not recent:
        return None
    others = ", ".join(f'{t["id"]} ({t["name"]})' for t in topics if t["id"] != topic["id"] and t.get("kind", "topic") == "topic")
    prompt = (f'Topic: {topic["name"]}\nCurrent text:\n' + "\n\n".join(live_prose(topic) or ["(none yet)"])
              + f"\n\nOther topics you may link to: {others}\n\nConversation in this topic, oldest first (most recent stretches):\n"
              + "\n".join(recent[-120:]))
    apps, skills = workspace_works()
    if topic.get("kind", "topic") == "topic":
        prompt += "\n\nThe workspace's apps (name: what users see): " + ", ".join(f"{n}: {d}" for n, d in apps.items())
        prompt += "\nThe workspace's skills: " + ", ".join(skills)
    if topic.get("kind") == "internal":
        prompt += "\n\nThis is the Internal notes topic: say briefly what the assistant did on its own and anything that needs the person's attention."
    if topic.get("kind") == "other":
        prompt += "\n\nThis is Other topics: one short paragraph per small, unrelated exchange."
    result = claude_p_completion(prompt, system=WRITE_SYSTEM, model=WRITE_MODEL)
    log(f"rewrote {topic['id']}, ${result.cost_usd:.3f}")
    found = re.search(r"\{.*\}", result.text, re.S)
    return json.loads(found.group(0)) if found else None


_works_cache: tuple[dict[str, str], list[str]] | None = None


def workspace_works() -> tuple[dict[str, str], list[str]]:
    """The apps (registered name -> display name) and skills this workspace has, for naming what a topic produced."""
    global _works_cache
    if _works_cache is None:
        listed = subprocess.run(["uv", "run", "--no-sync", "workspace-layout", "list"], capture_output=True, text=True)
        apps = {a["name"]: a["display_name"] for a in json.loads(listed.stdout)["apps"]} if listed.returncode == 0 else {}
        skills = sorted(p.parent.name for p in Path(".agents/skills").glob("*/SKILL.md"))
        _works_cache = (apps, skills)
    return _works_cache


def record_works(topic_id: str, works: list[dict]) -> None:
    """Add the apps and skills a topic produced to its main pane; never removes what is already there."""
    apps, skills = workspace_works()
    found = []
    for w in works or []:
        name = w.get("name", "")
        if w.get("kind") == "app" and name in apps and name != "context":
            found.append({"kind": "app", "title": apps[name], "app": name})
        elif w.get("kind") == "skill" and name in skills:
            found.append({"kind": "skill", "title": name, "skill": name})
    def change(state):
        have = state["artifacts"].setdefault(topic_id, [])
        for item in found:
            if not any(h.get("app") == item.get("app") and h.get("skill") == item.get("skill") for h in have):
                have.append(item)
    if found:
        live._update(DATA, change)


LINK = re.compile(r'&lt;a data-(moment|topic)=&quot;([A-Za-z0-9:_-]+)&quot;&gt;(.*?)&lt;/a&gt;')


def clean_paragraph(text: str) -> str:
    """Escape everything, then let back only the two kinds of link and <em>."""
    safe = html.escape(text)
    safe = LINK.sub(lambda m: f'<a data-{m.group(1)}="{m.group(2)}">{m.group(3)}</a>', safe)
    return re.sub(r"&lt;(/?em)&gt;", r"<\1>", safe)


def run(skip_refresh: bool = False, dry_run: bool = False) -> None:
    if not skip_refresh:
        transcripts.main()
    conversations = _read("conversations.json", [])
    topics = _read("topics.json", None) or starting_topics()
    sort_state = _read("sort_state.json", {})
    state = live.read_state(DATA)
    work = pending_work(conversations, sort_state, set(state["chats"].values()))
    log(f"{sum(len(w['messages']) for w in work)} new message(s) in {len(work)} conversation(s)")
    if not work:
        _summary("Nothing new to sort.")
        return
    by_id = {t["id"]: t for t in topics}
    changed: set[str] = set()
    moves: list[dict] = []

    for item in [w for w in work if internal_only(w)]:
        agent = item["conversation"]["agent_id"]
        by_id["internal"]["segments"].append({"agent_id": agent, "from": item["from"], "to": item["from"] + len(item["messages"]),
                                              "about": item["conversation"]["name"]})
        sort_state[agent] = len(item["conversation"]["messages"])
        changed.add("internal")
    to_sort = [w for w in work if not internal_only(w)]
    while to_sort:
        batch, size = [], 0
        while to_sort and (not batch or size + len(to_sort[0]["messages"]) <= BATCH_MESSAGES):
            item = to_sort.pop(0)
            if len(item["messages"]) > BATCH_MESSAGES:  # a long backlog in one chat: sort it in slices
                rest = {**item, "from": item["from"] + BATCH_MESSAGES, "messages": item["messages"][BATCH_MESSAGES:]}
                item = {**item, "messages": item["messages"][:BATCH_MESSAGES]}
                to_sort.insert(0, rest)
            batch.append(item)
            size += len(item["messages"])
        answer = sort_batch(batch, topics)
        if dry_run:
            logger.info("Sorting answer (nothing changed):\n{}", json.dumps(answer, indent=1))
            return
        changed |= apply_sorting(batch, answer, topics, sort_state, moves)

    _save_topics(topics)
    _write("sort_state.json", sort_state)
    for move in moves:
        live.record_move(DATA, move["from"], move["to"], move["at"])

    conv_by_id = {c["agent_id"]: c for c in conversations}
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = dict(zip(changed, pool.map(lambda tid: rewrite(next(t for t in topics if t["id"] == tid), conv_by_id, topics), changed)))
    for tid, answer in results.items():
        if not answer or not answer.get("paragraphs"):
            continue
        live._update(DATA, lambda s, tid=tid, answer=answer: s["prose"].__setitem__(tid, [clean_paragraph(p) for p in answer["paragraphs"]]))
        topic = next(t for t in topics if t["id"] == tid)
        if topic.get("kind", "topic") == "topic":
            record_works(tid, answer.get("works", []))
        if answer.get("name") and topic.get("kind", "topic") == "topic" and answer["name"].strip() != topic["name"]:
            log(f"renamed {topic['name']!r} -> {answer['name'].strip()!r}")
            topic["name"] = answer["name"].strip()
    _save_topics(topics)
    log("done:", ", ".join(sorted(changed)))
    names = [next(t["name"] for t in topics if t["id"] == tid) for tid in sorted(changed)]
    _summary("Updated " + ", ".join(names) + ".")


def _summary(text: str) -> None:
    """The one line the Routines app shows for this run."""
    run_dir = os.environ.get("ROUTINES_RUN_DIR")
    if run_dir:
        Path(run_dir, "result.json").write_text(json.dumps({"summary": text}))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--skip-refresh", action="store_true", help="sort what conversations.json already holds")
    parser.add_argument("--dry-run", action="store_true", help="print the first sorting answer and change nothing")
    parser.add_argument("--init", action="store_true", help="mark everything in conversations.json as sorted already")
    args = parser.parse_args()
    if args.init:
        if (DATA / "sort_state.json").exists():
            sys.exit("Already started; refusing to reset what has been sorted.")
        conversations = _read("conversations.json", [])
        lengths = {c["agent_id"]: len(c["messages"]) for c in conversations}
        topics = _read("topics.json", [])
        # A stretch that ran "to the end" ends where the conversation stands now; later messages get sorted.
        for t in topics:
            for seg in t["segments"]:
                if seg["to"] is None:
                    seg["to"] = lengths.get(seg["agent_id"], seg["from"])
        _write("topics.json", topics)
        _write("sort_state.json", lengths)
        return
    run(skip_refresh=args.skip_refresh, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
