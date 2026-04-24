// @ts-check
/// <reference lib="dom" />
"use strict";

const vscode = acquireVsCodeApi();

// ── State ─────────────────────────────────────────────────────────────────────

let currentAssistantBubble = null;   // <div class="bubble assistant"> being streamed
let currentTextEl = null;            // <p> inside currentAssistantBubble
let pendingToolCalls = {};           // id → {card, resultEl}

// ── DOM refs ──────────────────────────────────────────────────────────────────

const messagesEl = /** @type {HTMLElement} */ (document.getElementById("messages"));
const inputEl    = /** @type {HTMLTextAreaElement} */ (document.getElementById("input"));
const sendBtn    = /** @type {HTMLButtonElement} */ (document.getElementById("sendBtn"));
const clearBtn   = /** @type {HTMLButtonElement} */ (document.getElementById("clearBtn"));

// ── Helpers ───────────────────────────────────────────────────────────────────

function scrollToBottom() {
    messagesEl.scrollTop = messagesEl.scrollHeight;
}

function escapeHtml(str) {
    return str
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");
}

/** Convert **bold** and `code` markdown to HTML (minimal). */
function renderInlineMarkdown(text) {
    return escapeHtml(text)
        .replace(/`([^`]+)`/g, "<code>$1</code>")
        .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
}

/** Append a user bubble immediately. */
function appendUserBubble(text) {
    const div = document.createElement("div");
    div.className = "bubble user";
    div.textContent = text;
    messagesEl.appendChild(div);
    scrollToBottom();
}

/** Start a new assistant bubble (returns the bubble element). */
function startAssistantBubble() {
    const div = document.createElement("div");
    div.className = "bubble assistant";
    const p = document.createElement("p");
    div.appendChild(p);
    messagesEl.appendChild(div);
    currentAssistantBubble = div;
    currentTextEl = p;
    scrollToBottom();
    return div;
}

/** Append streamed text to the current assistant bubble. */
function appendText(content) {
    if (!currentAssistantBubble) { startAssistantBubble(); }
    if (!currentTextEl) {
        currentTextEl = document.createElement("p");
        currentAssistantBubble.appendChild(currentTextEl);
    }
    // Accumulate raw text and re-render with simple markdown
    currentTextEl.dataset.raw = (currentTextEl.dataset.raw || "") + content;
    currentTextEl.innerHTML = renderInlineMarkdown(currentTextEl.dataset.raw);
    scrollToBottom();
}

/** Add a tool-call card inside the current assistant bubble. */
function addToolCall(id, name, args) {
    if (!currentAssistantBubble) { startAssistantBubble(); }

    const card = document.createElement("div");
    card.className = "tool-card";
    card.dataset.id = id;

    // Header row (click to expand)
    const header = document.createElement("div");
    header.className = "tool-header";

    const nameEl = document.createElement("span");
    nameEl.className = "tool-name";
    nameEl.textContent = name;

    const argsEl = document.createElement("span");
    argsEl.className = "tool-args";
    const argsStr = typeof args === "string" ? args : JSON.stringify(args, null, 2);
    argsEl.textContent = argsStr.length > 120 ? argsStr.slice(0, 120) + "…" : argsStr;

    const toggle = document.createElement("span");
    toggle.className = "tool-toggle";
    toggle.textContent = "▸";

    header.appendChild(toggle);
    header.appendChild(nameEl);
    header.appendChild(argsEl);

    // Expandable body
    const body = document.createElement("div");
    body.className = "tool-body";

    const resultEl = document.createElement("pre");
    resultEl.className = "tool-result";
    resultEl.textContent = "Running…";
    body.appendChild(resultEl);

    header.addEventListener("click", () => {
        const open = body.classList.toggle("open");
        toggle.textContent = open ? "▾" : "▸";
    });

    card.appendChild(header);
    card.appendChild(body);
    currentAssistantBubble.appendChild(card);

    // Null out currentTextEl so next text chunk starts a new <p>
    currentTextEl = null;

    pendingToolCalls[id] = { card, resultEl };
    scrollToBottom();
}

/** Fill in the result for a tool call card. */
function fillToolResult(id, result) {
    const entry = pendingToolCalls[id];
    if (!entry) { return; }
    entry.resultEl.textContent = result;
    delete pendingToolCalls[id];
}

function resetStreamState() {
    currentAssistantBubble = null;
    currentTextEl = null;
    pendingToolCalls = {};
}

function setInputEnabled(enabled) {
    inputEl.disabled = !enabled;
    sendBtn.disabled = !enabled;
    sendBtn.textContent = enabled ? "Send" : "…";
}

function clearMessages() {
    messagesEl.innerHTML = "";
    resetStreamState();
}

// ── Send ──────────────────────────────────────────────────────────────────────

function sendMessage() {
    const text = inputEl.value.trim();
    if (!text) { return; }

    appendUserBubble(text);
    inputEl.value = "";
    inputEl.style.height = "auto";
    setInputEnabled(false);
    resetStreamState();

    vscode.postMessage({ command: "send", text });
}

// ── Event handlers ────────────────────────────────────────────────────────────

sendBtn.addEventListener("click", sendMessage);

inputEl.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        sendMessage();
    }
});

// Auto-resize textarea
inputEl.addEventListener("input", () => {
    inputEl.style.height = "auto";
    inputEl.style.height = Math.min(inputEl.scrollHeight, 160) + "px";
});

clearBtn.addEventListener("click", () => {
    clearMessages();
    vscode.postMessage({ command: "clear" });
});

// ── Messages from extension ───────────────────────────────────────────────────

window.addEventListener("message", (/** @type {MessageEvent} */ e) => {
    const msg = e.data;

    if (msg.command === "event") {
        const ev = msg.event;

        if (ev.type === "text") {
            appendText(ev.content);
        }
        else if (ev.type === "tool_use") {
            addToolCall(ev.id, ev.name, ev.arguments);
        }
        else if (ev.type === "tool_result") {
            fillToolResult(ev.id, ev.result);
        }
        else if (ev.type === "error") {
            if (!currentAssistantBubble) { startAssistantBubble(); }
            const errEl = document.createElement("p");
            errEl.className = "error-text";
            errEl.textContent = "Error: " + ev.message;
            currentAssistantBubble.appendChild(errEl);
            currentTextEl = null;
            scrollToBottom();
        }
    }

    if (msg.command === "done") {
        setInputEnabled(true);
        resetStreamState();
        inputEl.focus();
    }

    if (msg.command === "clearUI") {
        clearMessages();
    }
});
