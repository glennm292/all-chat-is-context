---
name: all-chat-is-context
description: "Install Context in a Minds workspace: an app that turns every conversation with the AI into living topics instead of separate chat threads. Each topic shows where its subject stands (text that rewrites itself), the work it produced (apps running live, skills, pictures) in the middle, and the real conversation beside it, which continues in place. New conversations are sorted into topics automatically every 10 minutes; one-offs go to Other topics, agent-only work and updates to Internal notes. Use when the user wants Context, topics instead of chats, 'all chat is context', or gives this skill's repo. Self-contained: bundles the app, its sorting routine, its tests and an installer."
metadata:
  self_contained: true
---

# All chat is context

Installs **Context**: the workspace's conversations, organized by what they are about rather
than by when a chat was opened.

- **Topics, not threads.** Every meaningful exchange between the person and the AI lands in a
  topic, which can gather pieces of many chats. A chat that changes subject partway is split
  between topics. **Other topics** holds the small one-offs, and **Internal notes** holds what
  the agent did on its own (updates, background work).
- **Each topic has three parts:**
  - a few paragraphs saying where the subject stands, which rewrite themselves as things move
  - the work it produced, in the middle: apps running live, skills, pictures
  - the actual conversation, exactly as exchanged, with a divider wherever a separate
    conversation began
- **Talking continues in place.** Typing in a topic continues the chat still on that subject, or
  starts the topic's own chat, handed the topic's background. What the AI makes appears in the
  middle pane.
- **The person owns the work.** The conversation can be folded away and the work stays.
  `references/design.md` gives the principles and how each is kept.

## What is in this skill

- `scripts/install.py` has four subcommands:
  - `check` (read-only): whether the workspace can run Context, and how much history the first
    sort will read and roughly what it costs
  - `install`: copy the app, start it, add the sorting routine
  - `verify`: the app answers, describes its API, passes its tests, and has its routine
  - `status`: where it is, its port, its topics
- `assets/app/` holds the app: a Flask service, its page, and the sorting pipeline
  (`python -m context.sync`), with its tests.
- `references/design.md` covers the principles, the data files, how sorting and talking in place
  work, and how to test without speaking for the person.
- `scripts/refresh_assets.py` is for the maintainer: it refreshes `assets/app` from a workspace
  where Context was changed.

## The flow

Run each step from the workspace root, and show the user what it found before moving on.

### 1. See what is there `[script]`

```
python3 .agents/skills/all-chat-is-context/scripts/install.py check
```

It reports:
- whether the chat app offers what Context drives (starting a chat, sending to it, telling
  whether it is busy)
- whether Context is already installed
- how many conversations and messages the first sort will read, with an estimated cost (about
  a dollar per thousand messages)

If chat routes are missing, stop and tell the user the chat app here is too old or too
different; do not install.

### 2. Tell the user the cost, and install `[script]`

Tell the user, in plain terms, what the first sort will read and roughly what it will cost. After
that, sorting costs cents per run when something changed, and nothing when nothing did. Then:

```
python3 .agents/skills/all-chat-is-context/scripts/install.py install
```

It does four things:
1. Copies the app to `system/apps/context/`, keeping an existing install's port and leaving its
   data under `data/.apps/context/` untouched.
2. Writes the app's program.
3. Installs and starts it.
4. Adds the `context-sorting` routine.

The routine's first run starts at once and sorts the whole history. That takes a few minutes,
and the page fills in on its own while it runs.

### 3. Check it `[script]`

```
python3 .agents/skills/all-chat-is-context/scripts/install.py verify
```

### 4. Show it `[prose]`

Open the app beside the chat with `uv run --no-sync workspace-layout open context --beside`.
Tell the user, in plain terms:
- **Topics appear as the first sort finishes.** For a large history it can take a few minutes.
- **The middle pane is their work.** It shows the apps, skills and pictures each topic produced.
- **Typing in a topic carries on that conversation.** It never starts a separate chat somewhere
  else.

## Updating

Run `install` again with a newer copy of this skill. It replaces the app's code and keeps its
port, its data and its routine.

## Things to know

- **Transcripts.** Context reads every agent transcript on the host, including destroyed agents'
  preserved ones.
- **Chat list.** Chats Context starts for a topic also appear in the Chat app's list, named
  "Context: <topic>".
- **Excluding conversations.** To keep a conversation out for good, add its agent id to
  `data/.apps/context/excluded.json`, for example a test that spoke in the person's name.
- **Testing.** Never test talking in place in a real topic. `references/design.md` gives the
  scratch-copy recipe.
