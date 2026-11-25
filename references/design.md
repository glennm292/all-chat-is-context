# How Context is designed, and why

Context replaces the idea of separate chat threads with **topics**: the subjects a person and
their AI keep coming back to. Nearly every meaningful interaction in the workspace ends up in
some topic. Each principle below came from the person who asked for it, and each has an
implementation consequence worth keeping.

## Principles

- **The person owns the work; the AI does not gate it.** This reverses the usual chat UI, where
  the conversation owns its artifacts and surfaces them when it chooses. In Context the topic's
  apps, skills and pictures sit in the middle pane whether the conversation is open or folded away.
- **The middle pane is the delivery surface.** Apps run live inside it, skills show their
  instructions, and pictures show full size. The topic's text sits above it, with only its
  first paragraph showing until the person asks for more.
- **Fluid, not widgety.** A topic is a few paragraphs of plain text that rewrite themselves as
  things move. There are no status badges, to-do lists or sorted action items.
- **Conversations continue in place.** Typing in a topic continues the chat that is still on that
  subject, if one exists; otherwise it starts the topic's own chat. It never opens "a new chat"
  somewhere else.
- **The record is the record.** The conversation pane shows messages exactly as they were
  exchanged. Lines the person didn't type (permission grants, task reports) show as small notes,
  and `/compact` is left out. The person's own messages are part of the record the instant they
  are sent: never greyed out, never pending. Any lag in processing is shown only by
  "Working on it…".
- **Never undo what the reader did.** Refreshes keep scroll positions. A long message the reader
  opened stays open. Text and delivery updates touch only what changed, so a running app is never
  reloaded.
- **Two topics every workspace has.** "Other topics" holds small one-off exchanges. A subject that
  comes up there again moves into a topic of its own. "Internal notes" holds conversations nobody
  but the agent spoke in, plus workspace updates and other upkeep.

## How it works

Data lives under `data/.apps/context/`:

| File | What it holds | Who writes it |
|---|---|---|
| `conversations.json` | every conversation on the host, messages as exchanged, each with its source transcript | `context.transcripts` (each sort run) |
| `topics.json` | the topics and the stretches of conversation (`agent_id`, `from`, `to`) that make them up | `context.sync` |
| `sort_state.json` | per conversation, how many messages have been sorted | `context.sync` |
| `live.json` | each topic's current text, its own chat (if Context started one), and the work delivered into it | `context.sync`, the API |
| `excluded.json` | (optional) conversations that must never appear, e.g. tests that spoke in the person's name | by hand |
| `artifacts/` | pictures delivered into topics | the API |

**Sorting** (`python -m context.sync`, the `context-sorting` routine, every 10 minutes):

1. Re-reads every transcript.
2. Takes each conversation's unsorted messages, once the conversation has been quiet for 10
   minutes. A conversation nobody but the agent spoke in goes to Internal notes without a model
   call.
3. Sends the rest to one completion per batch, which splits stretches where the subject changes
   and files each piece into a topic, a new topic, Other topics or Internal notes. It can also move
   a recurring subject out of Other topics.
4. Rewrites every topic that changed, with one completion per topic. The rewrite returns new
   paragraphs, which may link to exact messages (`data-moment`) and other topics (`data-topic`).
   It may also rename the topic and list the workspace apps and skills the topic produced, which
   join its middle pane.

Cost, measured on a real workspace: about $0.001 per message for the first full sort, then
cents per run when something changed, and nothing when nothing did.

**Talking in place** (`POST /api/topics/<id>/message`) goes to one of two places:

- **A chat still on the topic.** This is a person-started chat whose newest sorted stretch is in
  this topic and whose agent still exists. The message goes straight to that chat through the
  chat app's API, and its unsorted tail shows in the topic live.
- **No such chat.** Context starts the topic's own chat. Its first message carries the topic's
  background together with what the person typed. Sending the background alone made the
  assistant invent a question to answer, so the two always go together.

That background tells the assistant about two routes:

- `POST /api/topics/<id>/artifacts` puts an app, skill or picture in the middle pane.
- `PUT /api/topics/<id>/text` rewrites the topic's text.

The text is escaped except for `<em>` and `<strong>`.

**App addresses.** The registry's link for an app is its port-less inside-only address. The page
builds the address the browser can reach by putting the app's label on the workspace coordinate
that Context itself is served from.

## Testing without speaking for the person

Never test talking in place by sending into a real topic: the test message then sits in the
person's record as if they had typed it. Test instead on a scratch copy:

1. Copy the data directory and run a second instance with `CONTEXT_PORT`, `CONTEXT_DATA_DIR` and
   `CONTEXT_SELF_API` pointing at it.
2. Name the chat it starts with a `Context-test` prefix, which the reader skips.
3. Destroy that chat afterwards.
