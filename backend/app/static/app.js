let token = null;
let currentUser = null;
let runId = null;
let runActive = false; // true while the current job is still working (messages become notes)
let activeAssistantMsg = null;
let eventSource = null;
let sessionRunIds = []; // runs of the current chat session (sent as context for follow-ups)
let chat = []; // [{ role: "user" | "assistant" | "system", text }]

const userEl = document.getElementById("user");
const whoEl = document.getElementById("who");
const msgEl = document.getElementById("messages");
const eventsEl = document.getElementById("events");
const proposalArea = document.getElementById("proposalArea");
const proposalCards = new Map(); // proposal id -> { root, status, buttons, runId }

function isPlainObject(value) {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function addEvent(type, payload) {
  const row = document.createElement("div");
  row.className = "event-row";

  const badge = document.createElement("div");
  badge.className = "event-badge";
  badge.textContent = type;

  const text = document.createElement("div");
  text.textContent = JSON.stringify(payload ?? {}, null, 2);

  row.appendChild(badge);
  row.appendChild(text);
  eventsEl.appendChild(row);
  eventsEl.scrollTop = eventsEl.scrollHeight;
}

// Minimal, safe formatting: **bold**, `code`, "- " bullets and paragraphs (built with textContent, no innerHTML).
function appendInline(parent, text) {
  const parts = text.split(/(\*\*[^*]+\*\*|`[^`]+`)/g);
  for (const part of parts) {
    if (!part) continue;
    if (part.startsWith("**") && part.endsWith("**") && part.length > 4) {
      const el = document.createElement("strong");
      el.textContent = part.slice(2, -2);
      parent.appendChild(el);
    } else if (part.startsWith("`") && part.endsWith("`") && part.length > 2) {
      const el = document.createElement("code");
      el.textContent = part.slice(1, -1);
      parent.appendChild(el);
    } else {
      parent.appendChild(document.createTextNode(part));
    }
  }
}

function renderRich(container, text) {
  let list = null;
  for (const raw of text.split("\n")) {
    const line = raw.trim();
    const bullet = line.match(/^[-*\u2022]\s+(.*)$/);
    if (bullet) {
      if (!list) {
        list = document.createElement("ul");
        container.appendChild(list);
      }
      const li = document.createElement("li");
      appendInline(li, bullet[1]);
      list.appendChild(li);
      continue;
    }
    list = null;
    if (!line) continue;
    const p = document.createElement("p");
    appendInline(p, line);
    container.appendChild(p);
  }
}

function renderChat() {
  msgEl.innerHTML = "";
  if (chat.length === 0) {
    const hint = document.createElement("div");
    hint.className = "chat-empty";
    hint.textContent = token
      ? "Ask a question or request leave. The conversation stays here until you log out or switch user."
      : "Log in first, then type a message.";
    msgEl.appendChild(hint);
    return;
  }
  for (const m of chat) {
    const bubble = document.createElement("div");
    bubble.className = `bubble ${m.role}`;
    if (m.role === "assistant" && m.text) {
      renderRich(bubble, m.text);
    } else if (m.role === "note") {
      bubble.className = "bubble user note";
      bubble.textContent = `${m.text}\n(added to the running request)`;
    } else {
      bubble.textContent = m.text || (m.role === "assistant" ? "Thinking..." : "");
    }
    msgEl.appendChild(bubble);
  }
  msgEl.scrollTop = msgEl.scrollHeight;
}

function resetSessionUi() {
  runId = null;
  setRunActive(false);
  activeAssistantMsg = null;
  sessionRunIds = [];
  chat = [];
  if (eventSource) {
    eventSource.close();
    eventSource = null;
  }
  proposalCards.clear();
  proposalArea.innerHTML = "";
  eventsEl.innerHTML = "";
  // Do not leave the previous user's data on screen.
  ["policyResults", "leaveList"].forEach((id) => { document.getElementById(id).innerHTML = ""; });
  document.getElementById("leaveBalance").textContent = "";
  document.getElementById("policyQuery").value = "";
  renderChat();
}

async function api(path, method = "GET", body = null) {
  const res = await fetch(path, {
    method,
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: body ? JSON.stringify(body) : null,
  });
  if (!res.ok) {
    const t = await res.text();
    let detail = t;
    try { detail = JSON.parse(t).detail || t; } catch (_) { /* plain text */ }
    const err = new Error(detail || `HTTP ${res.status}`);
    err.status = res.status;
    throw err;
  }
  return res.json();
}

function setRunActive(active) {
  runActive = active;
  document.getElementById("runHint").textContent = active
    ? "Working... messages you send now are added to this request."
    : "";
}

document.getElementById("loginBtn").addEventListener("click", async () => {
  const username = userEl.value;
  try {
    const out = await api("/login", "POST", { username });
    if (!out.token) {
      throw new Error(out.error || "login failed");
    }
    if (currentUser !== username) {
      resetSessionUi(); // different user -> new conversation
    }
    token = out.token;
    currentUser = username;
    whoEl.textContent = `Connected: ${username}`;
    whoEl.className = "status-pill";
    renderChat();
  } catch (e) {
    alert(e.message);
  }
});

// EventSource cannot send an Authorization header, so read the SSE stream with fetch.
function openStream(url) {
  const listeners = {};
  const controller = new AbortController();
  const source = {
    addEventListener(name, fn) { listeners[name] = fn; },
    close() { controller.abort(); },
  };

  (async () => {
    try {
      const res = await fetch(url, {
        headers: { Authorization: `Bearer ${token}`, Accept: "text/event-stream" },
        signal: controller.signal,
      });
      if (!res.ok) {
        throw new Error(`stream HTTP ${res.status}: ${await res.text()}`);
      }
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buf = "";
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true }).replace(/\r\n/g, "\n");
        let idx;
        while ((idx = buf.indexOf("\n\n")) !== -1) {
          const block = buf.slice(0, idx);
          buf = buf.slice(idx + 2);
          let name = "message";
          const data = [];
          for (const line of block.split("\n")) {
            if (line.startsWith("event:")) name = line.slice(6).trim();
            else if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
          }
          const fn = listeners[name];
          if (fn) await fn({ type: name, data: data.join("\n") });
          if (name === "run.done" || name === "run.error") {
            controller.abort();
            return;
          }
        }
      }
    } catch (e) {
      if (e.name !== "AbortError") {
        addEvent("stream.error", { error: e.message });
      }
    }
  })();

  return source;
}

function describeProposal(proposal) {
  const payload = proposal.payload || {};
  const days = payload.days;
  const dates = Array.isArray(payload.dates) ? payload.dates : [];
  const when = dates.length ? dates.join(", ") : "dates not specified";
  let text = `Leave request: ${days} day(s)\n${when}`;
  const old = proposal.replaces;
  if (old && old.status === "confirmed") {
    const oldDays = (old.payload || {}).days;
    const net = proposal.net_days;
    text += `\nChanges your confirmed ${oldDays}-day booking. On Confirm your balance changes by ${net} day(s), not ${days}.`;
  }
  return text;
}

function setCardStatus(id, status) {
  const card = proposalCards.get(id);
  if (!card) return;
  card.status.textContent = `Status: ${status}`;
  if (status !== "pending") {
    card.buttons.forEach((b) => { b.disabled = true; });
  }
}

function addProposalCard(proposal, forRunId) {
  const payloadValue = isPlainObject(proposal) ? proposal : { id: String(proposal), payload: {} };

  // A newer proposal in the same run supersedes the older pending one.
  for (const [id, card] of proposalCards) {
    if (card.runId === forRunId && id !== payloadValue.id && card.status.textContent === "Status: pending") {
      setCardStatus(id, "superseded");
    }
  }

  const root = document.createElement("div");
  root.className = "proposal-card";

  const head = document.createElement("div");
  head.className = "proposal-head";
  const tag = document.createElement("span");
  tag.className = "proposal-type";
  tag.textContent = "proposal";
  const title = document.createElement("strong");
  title.textContent = `#${String(payloadValue.id || "unknown").slice(0, 8)}`;
  head.appendChild(tag);
  head.appendChild(title);

  const body = document.createElement("div");
  body.className = "proposal-body";
  body.textContent = describeProposal(payloadValue);

  const status = document.createElement("div");
  status.className = "proposal-status";
  status.textContent = "Status: pending";

  const actions = document.createElement("div");
  actions.className = "proposal-actions";

  const confirm = document.createElement("button");
  confirm.className = "primary-btn";
  confirm.textContent = "Confirm";
  const reject = document.createElement("button");
  reject.className = "ghost-btn";
  reject.textContent = "Reject";

  confirm.onclick = async () => {
    confirm.disabled = true;
    reject.disabled = true;
    try {
      await api(`/proposals/${payloadValue.id}/confirm`, "POST");
      addEvent("proposal.confirmed", { id: payloadValue.id });
      setCardStatus(payloadValue.id, "confirmed");
      const old = payloadValue.replaces;
      if (old && old.status === "confirmed") {
        setCardStatus(old.id, "amended");
        chat.push({ role: "system", text: "Change confirmed. Your earlier booking was replaced; only the difference was charged." });
      } else {
        chat.push({ role: "system", text: "Leave request confirmed and saved." });
      }
      renderChat();
    } catch (e) {
      let detail = e.message;
      addEvent("proposal.confirm_failed", { id: payloadValue.id, error: detail });
      confirm.disabled = false;
      reject.disabled = false;
      chat.push({ role: "system", text: `Could not confirm: ${detail}` });
      renderChat();
    }
  };

  reject.onclick = async () => {
    confirm.disabled = true;
    reject.disabled = true;
    try {
      await api(`/proposals/${payloadValue.id}/reject`, "POST");
      addEvent("proposal.rejected", { id: payloadValue.id });
      setCardStatus(payloadValue.id, "rejected");
      chat.push({ role: "system", text: "Leave request rejected. Nothing was saved." });
      renderChat();
    } catch (e) {
      addEvent("proposal.reject_failed", { id: payloadValue.id, error: e.message });
      confirm.disabled = false;
      reject.disabled = false;
    }
  };

  actions.appendChild(confirm);
  actions.appendChild(reject);
  root.appendChild(head);
  root.appendChild(body);
  root.appendChild(status);
  root.appendChild(actions);
  proposalArea.appendChild(root);
  proposalCards.set(payloadValue.id, { root, status, buttons: [confirm, reject], runId: forRunId });
}

function connectEvents(forRunId, assistantMsg) {
  if (eventSource) {
    eventSource.close();
  }
  const source = openStream(`/runs/${forRunId}/events`);
  eventSource = source;

  [
    "run.started", "plan", "task.started", "tool.call", "tool.result", "note.received",
    "plan.updated", "answer.start", "answer.delta", "proposal", "llm.retry", "llm.failed",
    "run.done", "run.error",
  ].forEach((name) => {
    source.addEventListener(name, async (ev) => {
      const payload = JSON.parse(ev.data || "{}");
      addEvent(name, payload);

      if (name === "answer.start") {
        // A note restarted the answer: replace the partial text instead of appending to it.
        assistantMsg.text = "";
        renderChat();
      }
      if (name === "answer.delta") {
        assistantMsg.text += payload.delta || "";
        renderChat();
      }
      if (name === "proposal") {
        addProposalCard(payload, forRunId);
      }
      if (name === "run.error" || name === "run.done") {
        if (forRunId === runId) setRunActive(false);
      }
      if (name === "run.error") {
        assistantMsg.text = assistantMsg.text || `Something went wrong: ${payload.error || "unknown error"}`;
        renderChat();
      }
      if (name === "run.done" && !assistantMsg.text) {
        assistantMsg.text = "(no answer)";
        renderChat();
      }
    });
  });
}

async function startRun(message) {
  chat.push({ role: "user", text: message });
  const assistantMsg = { role: "assistant", text: "" };
  chat.push(assistantMsg);
  renderChat();
  try {
    const out = await api("/runs", "POST", { message, context_run_ids: sessionRunIds });
    runId = out.run_id;
    activeAssistantMsg = assistantMsg;
    sessionRunIds.push(runId);
    setRunActive(true);
    addEvent("run.created", { run_id: runId });
    connectEvents(runId, assistantMsg);
  } catch (e) {
    assistantMsg.text = `Could not start the run: ${e.message}`;
    renderChat();
    addEvent("run.create_failed", { error: e.message });
  }
}

// While a job is working, a new message is an extra instruction for the same job (a "note").
// If the job finished in the meantime the server answers 409 and we start a follow-up run instead.
async function sendNote(text) {
  try {
    await api(`/runs/${runId}/notes`, "POST", { text });
    // Keep the assistant bubble last so the updated answer streams below the note.
    chat.splice(chat.indexOf(activeAssistantMsg), 0, { role: "note", text });
    renderChat();
    addEvent("note.sent", { run_id: runId, text });
    return true;
  } catch (e) {
    if (e.status === 409) {
      setRunActive(false);
      return false;
    }
    addEvent("note.failed", { error: e.message });
    chat.push({ role: "system", text: `Could not add your note: ${e.message}` });
    renderChat();
    return true;
  }
}

async function onSend() {
  if (!token) {
    chat.push({ role: "system", text: "Please log in first (left sidebar), then send your message." });
    renderChat();
    return;
  }
  const messageEl = document.getElementById("message");
  const message = messageEl.value.trim();
  if (!message) return;
  messageEl.value = "";

  if (runActive && runId && (await sendNote(message))) return;
  await startRun(message);
}

document.getElementById("sendBtn").addEventListener("click", onSend);
document.getElementById("message").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    onSend();
  }
});

// ---- Sidebar views -------------------------------------------------------
const viewIds = ["chatView", "policiesView", "leaveView"];

function showView(id) {
  viewIds.forEach((v) => { document.getElementById(v).hidden = v !== id; });
  document.querySelectorAll(".nav-item").forEach((b) => {
    b.classList.toggle("active", b.dataset.view === id);
  });
  if (id === "leaveView") loadLeave();
}

document.querySelectorAll(".nav-item").forEach((b) => {
  b.addEventListener("click", () => showView(b.dataset.view));
});

function emptyNote(container, text) {
  container.innerHTML = "";
  const el = document.createElement("div");
  el.className = "view-note";
  el.textContent = text;
  container.appendChild(el);
}

async function loadLeave() {
  const balanceEl = document.getElementById("leaveBalance");
  const listEl = document.getElementById("leaveList");
  if (!token) {
    balanceEl.textContent = "";
    emptyNote(listEl, "Log in first to see your leave requests.");
    return;
  }
  try {
    const out = await api("/me/leave");
    balanceEl.textContent = `${currentUser}: current leave balance is ${out.balance} day(s).`;
    listEl.innerHTML = "";
    if (!out.proposals.length) {
      emptyNote(listEl, "No leave requests yet. Ask the assistant to book leave from Overview.");
      return;
    }
    for (const p of out.proposals) {
      const row = document.createElement("div");
      row.className = "view-row";
      const left = document.createElement("div");
      const dates = p.payload && p.payload.dates && p.payload.dates.length ? p.payload.dates.join(", ") : "dates not specified";
      left.textContent = `${p.payload ? p.payload.days : "?"} day(s) - ${dates}`;
      const badge = document.createElement("span");
      badge.className = `badge ${p.status}`;
      badge.textContent = p.status;
      row.appendChild(left);
      row.appendChild(badge);
      listEl.appendChild(row);
    }
  } catch (e) {
    emptyNote(listEl, `Could not load leave requests: ${e.message}`);
  }
}

async function searchPolicies() {
  const resultsEl = document.getElementById("policyResults");
  if (!token) {
    emptyNote(resultsEl, "Log in first to search policies.");
    return;
  }
  const q = document.getElementById("policyQuery").value.trim();
  if (!q) {
    emptyNote(resultsEl, "Type something to search.");
    return;
  }
  emptyNote(resultsEl, "Searching...");
  try {
    const out = await api(`/policies/search?q=${encodeURIComponent(q)}`);
    resultsEl.innerHTML = "";
    if (!out.items.length) {
      emptyNote(resultsEl, "No accessible policy documents matched.");
      return;
    }
    for (const item of out.items) {
      const row = document.createElement("div");
      row.className = "view-row";
      const box = document.createElement("div");
      const title = document.createElement("strong");
      title.textContent = item.title;
      const text = document.createElement("div");
      text.className = "view-note";
      text.textContent = item.text;
      box.appendChild(title);
      box.appendChild(text);
      row.appendChild(box);
      resultsEl.appendChild(row);
    }
  } catch (e) {
    emptyNote(resultsEl, `Search failed: ${e.message}`);
  }
}

document.getElementById("leaveRefresh").addEventListener("click", loadLeave);
document.getElementById("policyBtn").addEventListener("click", searchPolicies);
document.getElementById("policyQuery").addEventListener("keydown", (e) => {
  if (e.key === "Enter") searchPolicies();
});

renderChat();
