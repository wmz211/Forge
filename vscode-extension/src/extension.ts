import * as vscode from "vscode";
import * as http from "http";
import * as cp from "child_process";
import * as path from "path";
import * as fs from "fs";

// ── Types matching server.py SSE events ──────────────────────────────────────

interface TextEvent      { type: "text";        content: string }
interface ToolUseEvent   { type: "tool_use";    name: string; arguments: unknown; id: string }
interface ToolResultEvent{ type: "tool_result"; name: string; result: string; id: string }
interface DoneEvent      { type: "done";        reason: string }
interface ErrorEvent     { type: "error";       message: string }

type AgentEvent = TextEvent | ToolUseEvent | ToolResultEvent | DoneEvent | ErrorEvent;

// ── Server process manager ────────────────────────────────────────────────────

let serverProcess: cp.ChildProcess | undefined;
let statusBarItem: vscode.StatusBarItem;
let chatPanel: vscode.WebviewPanel | undefined;

function getConfig() {
    const cfg = vscode.workspace.getConfiguration("codingAgent");
    return {
        port:       cfg.get<number>("port", 8765),
        python:     cfg.get<string>("pythonPath", "python"),
        serverPath: cfg.get<string>("serverPath", ""),
        apiKey:     cfg.get<string>("apiKey", ""),
        autoStart:  cfg.get<boolean>("autoStart", true),
    };
}

function getServerDir(): string {
    const cfg = getConfig();
    if (cfg.serverPath) { return cfg.serverPath; }
    const folders = vscode.workspace.workspaceFolders;
    if (folders && folders.length > 0) { return folders[0].uri.fsPath; }
    return process.cwd();
}

function getCwd(): string {
    const folders = vscode.workspace.workspaceFolders;
    if (folders && folders.length > 0) { return folders[0].uri.fsPath; }
    return process.cwd();
}

function setStatus(text: string, tooltip?: string) {
    statusBarItem.text = text;
    statusBarItem.tooltip = tooltip ?? text;
}

async function isServerRunning(port: number): Promise<boolean> {
    return new Promise((resolve) => {
        const req = http.get(`http://127.0.0.1:${port}/health`, (res) => {
            resolve(res.statusCode === 200);
            res.resume();
        });
        req.on("error", () => resolve(false));
        req.setTimeout(1000, () => { req.destroy(); resolve(false); });
    });
}

async function startServer(outputChannel: vscode.OutputChannel): Promise<boolean> {
    const cfg = getConfig();
    const serverDir = getServerDir();
    const serverScript = path.join(serverDir, "server.py");

    if (!fs.existsSync(serverScript)) {
        vscode.window.showErrorMessage(
            `server.py not found at ${serverScript}. ` +
            `Set codingAgent.serverPath in settings.`
        );
        return false;
    }

    if (await isServerRunning(cfg.port)) {
        setStatus("$(check) CodingAgent", "Server already running");
        return true;
    }

    setStatus("$(sync~spin) CodingAgent", "Starting server…");

    const env: NodeJS.ProcessEnv = { ...process.env };
    if (cfg.apiKey) { env["CODING_AGENT_API_KEY"] = cfg.apiKey; }

    serverProcess = cp.spawn(cfg.python, [
        serverScript,
        "--port", String(cfg.port),
        "--cwd",  getCwd(),
    ], { cwd: serverDir, env });

    serverProcess.stdout?.on("data", (d: Buffer) => outputChannel.append(d.toString()));
    serverProcess.stderr?.on("data", (d: Buffer) => outputChannel.append(d.toString()));
    serverProcess.on("exit", (code) => {
        outputChannel.appendLine(`[server exited with code ${code}]`);
        serverProcess = undefined;
        setStatus("$(circle-slash) CodingAgent", "Server stopped");
    });

    // Wait up to 10 s for the server to come up
    for (let i = 0; i < 20; i++) {
        await new Promise(r => setTimeout(r, 500));
        if (await isServerRunning(cfg.port)) {
            setStatus("$(check) CodingAgent", `Listening on :${cfg.port}`);
            return true;
        }
    }

    vscode.window.showErrorMessage("CodingAgent server failed to start. Check the Output panel.");
    return false;
}

function stopServer() {
    if (serverProcess) {
        serverProcess.kill();
        serverProcess = undefined;
    }
    setStatus("$(circle-slash) CodingAgent", "Server stopped");
}

// ── SSE streaming via Node http ───────────────────────────────────────────────

function postSSE(
    port: number,
    path: string,
    body: object,
    onEvent: (ev: AgentEvent) => void,
    onEnd: () => void,
): () => void {
    const bodyStr = JSON.stringify(body);
    let cancelled = false;
    let req: http.ClientRequest;

    const options: http.RequestOptions = {
        hostname: "127.0.0.1",
        port,
        path,
        method: "POST",
        headers: {
            "Content-Type": "application/json",
            "Content-Length": Buffer.byteLength(bodyStr),
            "Accept": "text/event-stream",
        },
    };

    req = http.request(options, (res) => {
        let buf = "";
        res.setEncoding("utf8");
        res.on("data", (chunk: string) => {
            if (cancelled) { return; }
            buf += chunk;
            const parts = buf.split("\n\n");
            buf = parts.pop() ?? "";
            for (const part of parts) {
                const line = part.trim();
                if (line.startsWith("data: ")) {
                    try {
                        const ev = JSON.parse(line.slice(6)) as AgentEvent;
                        onEvent(ev);
                    } catch { /* ignore malformed */ }
                }
            }
        });
        res.on("end", () => { if (!cancelled) { onEnd(); } });
    });

    req.on("error", (e) => {
        if (!cancelled) {
            onEvent({ type: "error", message: e.message });
            onEnd();
        }
    });

    req.write(bodyStr);
    req.end();

    return () => { cancelled = true; req.destroy(); };
}

// ── Webview panel ─────────────────────────────────────────────────────────────

function getWebviewContent(
    webview: vscode.Webview,
    extensionUri: vscode.Uri,
): string {
    const scriptUri = webview.asWebviewUri(
        vscode.Uri.joinPath(extensionUri, "media", "main.js")
    );
    const styleUri = webview.asWebviewUri(
        vscode.Uri.joinPath(extensionUri, "media", "style.css")
    );
    const nonce = Math.random().toString(36).slice(2);

    return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta http-equiv="Content-Security-Policy"
    content="default-src 'none';
             style-src ${webview.cspSource} 'unsafe-inline';
             script-src 'nonce-${nonce}';">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <link rel="stylesheet" href="${styleUri}">
  <title>CodingAgent</title>
</head>
<body>
  <div id="header">
    <span id="title">CodingAgent</span>
    <button id="clearBtn" title="Clear conversation">&#x1F5D1;</button>
  </div>
  <div id="messages"></div>
  <div id="inputArea">
    <textarea id="input" placeholder="Ask anything… (Enter to send, Shift+Enter for newline)"
              rows="3"></textarea>
    <button id="sendBtn">Send</button>
  </div>
  <script nonce="${nonce}" src="${scriptUri}"></script>
</body>
</html>`;
}

function createChatPanel(context: vscode.ExtensionContext, outputChannel: vscode.OutputChannel) {
    if (chatPanel) {
        chatPanel.reveal();
        return;
    }

    chatPanel = vscode.window.createWebviewPanel(
        "codingAgent",
        "CodingAgent",
        vscode.ViewColumn.Beside,
        {
            enableScripts: true,
            localResourceRoots: [vscode.Uri.joinPath(context.extensionUri, "media")],
            retainContextWhenHidden: true,
        }
    );

    chatPanel.webview.html = getWebviewContent(chatPanel.webview, context.extensionUri);

    const cfg = getConfig();
    let cancelCurrentStream: (() => void) | undefined;

    chatPanel.webview.onDidReceiveMessage(async (msg: { command: string; text?: string }) => {
        if (msg.command === "send" && msg.text) {
            cancelCurrentStream?.();
            cancelCurrentStream = postSSE(
                cfg.port,
                "/chat",
                { message: msg.text, cwd: getCwd() },
                (ev) => chatPanel?.webview.postMessage({ command: "event", event: ev }),
                ()  => chatPanel?.webview.postMessage({ command: "done" }),
            );
        }

        if (msg.command === "clear") {
            cancelCurrentStream?.();
            // Fire-and-forget clear request
            const body = JSON.stringify({ message: "", cwd: getCwd() });
            const req = http.request({
                hostname: "127.0.0.1", port: cfg.port,
                path: "/clear", method: "POST",
                headers: { "Content-Type": "application/json",
                           "Content-Length": Buffer.byteLength(body) },
            });
            req.on("error", () => {});
            req.write(body); req.end();
        }
    }, undefined, context.subscriptions);

    chatPanel.onDidDispose(() => {
        cancelCurrentStream?.();
        chatPanel = undefined;
    }, undefined, context.subscriptions);
}

// ── Extension lifecycle ───────────────────────────────────────────────────────

export async function activate(context: vscode.ExtensionContext) {
    const outputChannel = vscode.window.createOutputChannel("CodingAgent");

    statusBarItem = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 100);
    statusBarItem.command = "codingAgent.openChat";
    setStatus("$(circle-slash) CodingAgent", "Click to open chat");
    statusBarItem.show();
    context.subscriptions.push(statusBarItem);

    context.subscriptions.push(
        vscode.commands.registerCommand("codingAgent.openChat", async () => {
            const cfg = getConfig();
            if (cfg.autoStart && !(await isServerRunning(cfg.port))) {
                const ok = await startServer(outputChannel);
                if (!ok) { return; }
            }
            createChatPanel(context, outputChannel);
        }),

        vscode.commands.registerCommand("codingAgent.startServer", async () => {
            await startServer(outputChannel);
            outputChannel.show(true);
        }),

        vscode.commands.registerCommand("codingAgent.stopServer", () => {
            stopServer();
        }),

        vscode.commands.registerCommand("codingAgent.clearChat", () => {
            chatPanel?.webview.postMessage({ command: "clearUI" });
        }),
    );
}

export function deactivate() {
    stopServer();
}
