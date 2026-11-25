// Context: topics, the work they produced, and the conversations behind them, from /api/topics.
// Prose links: data-topic jumps to another topic, data-thing opens something made,
// data-moment="<agent id>:<index>" scrolls the conversation to that exact message.

let TOPICS = [];
let current = null;

const appEl = document.getElementById("app");
const $ = (id) => document.getElementById(id);

function toast(text) {
  const t = document.querySelector(".toast") || document.body.appendChild(Object.assign(document.createElement("div"), { className: "toast" }));
  t.textContent = text;
  t.classList.add("show");
  clearTimeout(t._h);
  t._h = setTimeout(() => t.classList.remove("show"), 2200);
}

const DAY = 86400000;
function relative(iso) {
  if (!iso) return "";
  const d = new Date(iso), now = new Date();
  const days = Math.floor((now.setHours(0, 0, 0, 0) - new Date(d).setHours(0, 0, 0, 0)) / DAY);
  if (days <= 0) return "Today";
  if (days === 1) return "Yesterday";
  if (days < 14) return `${days} days ago`;
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}
function dividerDate(iso) {
  const d = new Date(iso);
  const rel = relative(iso);
  const time = d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
  if (rel === "Today" || rel === "Yesterday") return `${rel}, ${time}`;
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric", year: d.getFullYear() === new Date().getFullYear() ? undefined : "numeric" });
}

// Just enough markdown to show messages the way the chat showed them.
function escapeHtml(s) {
  return s.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
}
function markdown(src) {
  const inline = (s) => escapeHtml(s)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  const out = [];
  let list = null, code = null, para = null;
  const flush = () => { if (para) { out.push(`<p>${inline(para.join(" "))}</p>`); para = null; } };
  for (const line of src.split("\n")) {
    if (line.startsWith("```")) {
      flush();
      if (code) { out.push(`<pre>${escapeHtml(code.join("\n"))}</pre>`); code = null; } else code = [];
      continue;
    }
    if (code) { code.push(line); continue; }
    const item = line.match(/^\s*(?:[-*]|\d+\.)\s+(.*)/);
    if (item) { flush(); (list ||= []).push(`<li>${inline(item[1])}</li>`); continue; }
    if (list) { out.push(`<ul>${list.join("")}</ul>`); list = null; }
    const h = line.match(/^#{1,6}\s+(.*)/);
    if (h) { flush(); out.push(`<p><strong>${inline(h[1])}</strong></p>`); }
    else if (line.trim()) (para ||= []).push(line.trim());
    else flush();
  }
  flush();
  if (list) out.push(`<ul>${list.join("")}</ul>`);
  if (code) out.push(`<pre>${escapeHtml(code.join("\n"))}</pre>`);
  return out.join("");
}

function renderTopics() {
  const nav = $("topics");
  nav.querySelectorAll(".topic-link").forEach((n) => n.remove());
  for (const t of TOPICS) {
    const b = document.createElement("button");
    b.className = "topic-link" + (t === current ? " selected" : "") + (t.kind !== "topic" ? " other" : "") + (t.kind === "internal" ? " internal" : "");
    b.dataset.topicId = t.id;
    b.innerHTML = `${escapeHtml(t.name)}<span class="when">${relative(t.last_at)}</span>`;
    b.onclick = () => select(t);
    nav.append(b);
  }
}

let shown = null;

// An app's address as this browser reaches it: the app's own label on the same workspace
// host this page is served from (the shell's rule: app labels lead, the workspace coordinate follows).
const WORKSPACE_COORDINATE_LABEL = /^(?:(?:host|agent)-[a-f0-9]+|[a-f0-9]{32})$/i;
function appAddress(link) {
  if (!link) return null;
  const label = new URL(link).hostname.split(".")[0];
  const labels = location.host.split(".");
  const at = labels.findIndex((l) => WORKSPACE_COORDINATE_LABEL.test(l));
  if (at < 0) return link;
  return `${location.protocol}//${label}.${labels.slice(at).join(".")}/`;
}

let renderedTopicId = null;
function renderContext(keep) {
  const t = current;
  const chats = new Set(t.thread.filter((i) => i.kind === "divider").map((i) => i.conversation)).size;
  const since = t.first_at ? new Date(t.first_at).toLocaleDateString(undefined, { month: "short", day: "numeric" }) : "";
  const has = t.artifacts.length > 0;
  $("context").classList.toggle("delivering", has);
  $("context").innerHTML = `<div class="context-inner" data-topic-id="${t.id}">
    <header class="topic-head">
      <h2 id="topic-name" contenteditable="true" spellcheck="false">${escapeHtml(t.name)}</h2>
      <div class="since">Since ${since} · ${chats} chat${chats === 1 ? "" : "s"}</div>
      <div class="prose ${has ? "folded" : ""}">${t.prose.map((p) => `<p>${p}</p>`).join("")}</div>
      ${has && t.prose.length > 1 ? `<button class="read-more" id="read-more">More about this topic</button>` : ""}
    </header>
    ${has ? `<div class="things" id="things"></div><div class="stage" id="stage"></div>` : ""}
  </div>`;
  if (renderedTopicId !== t.id) $("context").scrollTop = 0;
  renderedTopicId = t.id;
  if (!has) return;
  $("read-more")?.addEventListener("click", (e) => {
    const folded = document.querySelector(".prose").classList.toggle("folded");
    e.target.textContent = folded ? "More about this topic" : "Less";
  });
  show(keep || t.artifacts[0]);
}

const KIND_LABEL = { app: "App", skill: "Skill", image: "Picture" };

function renderThings() {
  const row = $("things");
  row.innerHTML = "";
  const list = document.createElement("div");
  list.className = "thing-list";
  for (const a of current.artifacts) {
    const b = document.createElement("button");
    b.className = "thing-tab" + (a === shown ? " on" : "");
    b.dataset.artifact = a.title;
    b.innerHTML = `<span class="thing-kind">${KIND_LABEL[a.kind]}</span>${escapeHtml(a.title)}`;
    b.onclick = () => show(a);
    list.append(b);
  }
  row.append(list);
  const out = appAddress(shown.link) || shown.github || (shown.src && `/media/${shown.src}`);
  if (out) {
    const a = Object.assign(document.createElement("a"), {
      className: "open-out", href: out, target: "_blank",
      textContent: shown.link ? "Open in its own window" : shown.github ? "Open on GitHub" : "Open full size",
    });
    row.append(a);
  }
}

async function show(a) {
  shown = a;
  renderThings();
  const stage = $("stage");
  stage.className = "stage " + a.kind;
  if (a.kind === "app") {
    if (!a.link) { stage.innerHTML = `<div class="stage-empty">${escapeHtml(a.title)} isn't running right now.</div>`; return; }
    stage.innerHTML = "";
    stage.append(Object.assign(document.createElement("iframe"), { src: appAddress(a.link), title: a.title }));
  } else if (a.kind === "image") {
    stage.innerHTML = `<img alt="${escapeHtml(a.title)}" src="/media/${a.src}">`;
  } else if (a.kind === "skill") {
    stage.innerHTML = `<div class="stage-empty">Loading ${escapeHtml(a.title)}…</div>`;
    const r = await fetch(`/api/skills/${encodeURIComponent(a.skill)}`);
    if (shown !== a) return;
    if (!r.ok) { stage.innerHTML = `<div class="stage-empty">${escapeHtml(a.title)} isn't in this workspace.</div>`; return; }
    const doc = await r.json();
    stage.innerHTML = `<article class="doc"><p class="doc-desc"></p>${markdown(doc.markdown)}</article>`;
    stage.querySelector(".doc-desc").textContent = doc.description;
  }
}

function renderThread() {
  const thread = $("thread");
  const fromBottom = thread.scrollHeight - thread.scrollTop - thread.clientHeight;
  const keepScroll = thread.scrollTop;
  thread.innerHTML = "";
  for (const item of current.thread) {
    const d = document.createElement("div");
    if (item.kind === "divider") {
      d.className = "sep";
      d.innerHTML = `<span class="sep-when"></span><span class="sep-about"></span>`;
      d.querySelector(".sep-when").textContent = dividerDate(item.at);
      d.querySelector(".sep-about").textContent = item.text;
      d.title = `From the chat "${item.conversation}"`;
    } else if (item.kind === "note") {
      d.className = "note";
      d.textContent = item.text.split("\n")[0].slice(0, 160);
      d.title = item.text;
      d.dataset.ref = item.ref;
    } else {
      d.className = "msg " + item.who;
      d.dataset.ref = item.ref;
      if (item.who === "you") d.textContent = item.text;
      else d.innerHTML = markdown(item.text);
      if (item.text.length > 1400 && !expanded.has(item.ref)) {
        d.classList.add("long");
        const more = Object.assign(document.createElement("button"), { className: "more", textContent: "Show all" });
        more.onclick = () => { keepOpen(item.ref); d.classList.remove("long"); more.remove(); };
        d.append(more);
      }
    }
    thread.append(d);
  }
  if (pending && pending.topic === current.id) {
    thread.append(Object.assign(document.createElement("div"), { className: "msg you sending", textContent: pending.text }));
  }
  if (current.talking || (pending && pending.topic === current.id)) {
    thread.append(Object.assign(document.createElement("div"), { className: "typing", textContent: "Working on it…" }));
  }
  // Follow new messages only when the reader was already at the end; otherwise stay exactly where they were.
  thread.scrollTop = fromBottom < 80 || pending ? thread.scrollHeight : keepScroll;
}

function select(t) {
  current = t;
  $("bar-title").textContent = t.name;
  appEl.classList.remove("drawer");
  renderTopics();
  renderContext();
  renderThread();
  $("thread").scrollTop = $("thread").scrollHeight;
}

$("context").addEventListener("click", (e) => {
  const a = e.target.closest("a");
  if (!a) return;
  if (a.dataset.topic) select(TOPICS.find((t) => t.id === a.dataset.topic));
  else if (a.dataset.thing) toast("Opens " + a.dataset.thing.split(":")[1]);
  else if (a.dataset.moment) {
    if (appEl.classList.contains("talk-hidden")) setTalkHidden(false);
    if (getComputedStyle(document.querySelector(".talk")).display === "none") setView("talk");
    const msg = document.querySelector(`[data-ref="${a.dataset.moment}"]`);
    if (!msg) return;
    keepOpen(a.dataset.moment);
    msg.classList.remove("long");
    msg.querySelector(".more")?.remove();
    msg.scrollIntoView({ behavior: "smooth", block: "center" });
    msg.classList.add("flash");
    setTimeout(() => msg.classList.remove("flash"), 1600);
  }
});

const say = $("say");
let pending = null; // what the person just sent, shown until the conversation has it
// Long messages the reader has opened stay open, through refreshes and reloads.
const expanded = new Set(JSON.parse(localStorage.getItem("context.expanded") || "[]"));
function keepOpen(ref) { expanded.add(ref); localStorage.setItem("context.expanded", JSON.stringify([...expanded].slice(-500))); }
say.addEventListener("keydown", async (e) => {
  if (e.key !== "Enter" || e.shiftKey || !say.value.trim()) return;
  e.preventDefault();
  const text = say.value.trim();
  const topic = current;
  say.value = "";
  pending = { topic: topic.id, text };
  renderThread();
  const r = await fetch(`/api/topics/${topic.id}/message`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text }),
  });
  if (!r.ok) {
    const why = (await r.json().catch(() => ({}))).detail || "The message didn't go through";
    pending = null;
    say.value = text;
    renderThread();
    toast(why);
    return;
  }
  watchFast(60);
});

// Keep the page current: quickly while a reply is coming, slowly otherwise.
let fastPolls = 0;
function watchFast(n) { fastPolls = Math.max(fastPolls, n); }
async function refresh() {
  const data = await fetch("/api/topics").then((r) => r.json()).catch(() => null);
  if (!data) return;
  const before = current;
  TOPICS = data.topics;
  const now = TOPICS.find((t) => t.id === before.id) || TOPICS[0];
  const lastOf = (t) => t.thread.length ? t.thread[t.thread.length - 1].text : "";
  const threadChanged = now.thread.length !== before.thread.length || now.talking !== before.talking || lastOf(now) !== lastOf(before);
  const proseChanged = now.prose.join() !== before.prose.join();
  const newArtifact = now.artifacts[0] && now.artifacts[0].title !== before.artifacts[0]?.title ? now.artifacts[0] : null;
  current = now;
  if (pending && now.thread.some((i) => i.who === "you" && i.text.trim() === pending.text)) pending = null;
  if (now.talking || pending) watchFast(3);
  const nameChanged = now.name !== before.name;
  renderTopics();
  if (now.artifacts.length && !document.getElementById("stage")) {
    renderContext(); // the topic just got its first piece of work
  } else {
    if (newArtifact) show(newArtifact);
    else if (now.artifacts.length !== before.artifacts.length) renderThings();
    if (proseChanged) {
      const prose = document.querySelector(".prose");
      prose.innerHTML = now.prose.map((p) => `<p>${p}</p>`).join("");
      prose.querySelectorAll("p").forEach((p) => {
        p.classList.add("fresh");
        requestAnimationFrame(() => requestAnimationFrame(() => p.classList.remove("fresh")));
      });
    }
    if (nameChanged && document.activeElement?.id !== "topic-name") $("topic-name").textContent = now.name;
  }
  if (threadChanged) renderThread();
}
(function loop() {
  const delay = fastPolls > 0 ? 2000 : 10000;
  if (fastPolls > 0) fastPolls--;
  setTimeout(async () => {
    if (!document.hidden) {
      if (current) await refresh();
      else {
        const data = await fetch("/api/topics").then((r) => r.json()).catch(() => null);
        if (data?.topics?.length) { TOPICS = data.topics; select(TOPICS[0]); }
      }
    }
    loop();
  }, delay);
})();

$("show-topics").onclick = () => appEl.classList.add("drawer");
$("scrim").onclick = () => appEl.classList.remove("drawer");
function setView(v) {
  appEl.dataset.view = v;
  $("view-topic").classList.toggle("on", v === "topic");
  $("view-talk").classList.toggle("on", v === "talk");
  if (v === "talk") { const th = $("thread"); th.scrollTop = th.scrollHeight; }
}
$("view-topic").onclick = () => setView("topic");
function setTalkHidden(hidden) {
  appEl.classList.toggle("talk-hidden", hidden);
  localStorage.setItem("context.talkHidden", hidden ? "1" : "");
}
$("hide-talk").onclick = () => setTalkHidden(true);
$("show-talk").onclick = () => setTalkHidden(false);
setTalkHidden(localStorage.getItem("context.talkHidden") === "1");
$("view-talk").onclick = () => setView("talk");

fetch("/api/topics").then((r) => r.json()).then((data) => {
  TOPICS = data.topics;
  if (TOPICS.length) select(TOPICS[0]);
  else $("context").innerHTML = `<div class="context-inner"><h2>Your topics will appear here</h2>
    <p class="since">Context is reading your conversations and sorting them into topics. This page fills in on its own.</p></div>`;
});
