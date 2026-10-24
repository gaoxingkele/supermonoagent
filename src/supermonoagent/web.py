PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover, interactive-widget=resizes-content">
<title>SuperMonoAgent</title>
<link rel="stylesheet" href="/xterm.css">
<link rel="stylesheet" href="/tab-themes.css">
<style>
  :root { color-scheme: dark; }
  body { margin: 0; height: 100vh; height: 100dvh; display: flex; flex-direction: column; overflow: hidden; overscroll-behavior: none; font: 14px/1.4 ui-monospace, Consolas, monospace; background: #12140f; color: #ececec; }
  #tools { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; padding: 8px; background: #1c2118; }
  #tools label { display: flex; align-items: center; gap: 6px; color: #b7c4a4; }
  #tools select { min-width: 128px; }
  #tools input { font: inherit; width: 148px; background: #12140f; color: inherit; border: 1px solid #3d4636; border-radius: 6px; padding: 6px 8px; }
  #tools #status { margin-left: auto; color: #b7c4a4; }
  #gate { position: fixed; inset: 0; z-index: 90; display: flex; align-items: center; justify-content: center; background: #12140f; }
  #gate form { width: min(360px, 92vw); display: flex; flex-direction: column; gap: 8px; }
  #gate input { font: inherit; padding: 8px; background: #12140f; color: inherit; border: 1px solid #3d4636; border-radius: 6px; }
  #gate.hidden { display: none; }
  .appname { margin: 6px 8px 2px 28px; color: #8e8e8e; font-size: 12px; }
  .xterm-viewport { touch-action: pan-y; overflow-x: hidden; }
  #desk { flex: 1; min-height: 0; display: flex; flex-direction: row; }
  #app { flex: 1; min-width: 0; min-height: 0; display: flex; flex-direction: column; }
  #side { width: 260px; flex: none; display: flex; flex-direction: column; background: #171717; color: #ececec; border-right: 1px solid #2a2a2a; padding: 10px 8px 8px; font-family: ui-sans-serif, system-ui, "Segoe UI", sans-serif; }
  #groups { flex: 1; min-height: 0; overflow: auto; }
  .nav-label, .row { display: flex; align-items: center; gap: 4px; border-radius: 8px; }
  .nav-label { height: 32px; padding: 0 8px; color: #8e8e8e; font-size: 13px; }
  .nav-label span, .row .name { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; text-align: left; }
  .row { height: 36px; padding: 0 4px 0 8px; }
  .row.child { height: 32px; padding-left: 52px; color: #d6d6d6; }
  .row:hover, .row.on { background: #2f2f2f; }
  #side button.mark { width: 16px; height: 16px; flex: none; border: 1px solid #8d8d8d; border-radius: 2px; background: #171717; color: #f2f2f2; font: 14px/14px Consolas, ui-monospace, monospace; padding: 0; }
  #side-toggle { align-self: flex-start; margin: 0 0 8px 2px; }
  #side.collapsed { width: 36px; padding-left: 8px; padding-right: 4px; }
  #side.collapsed #groups { display: none; }
  #side.collapsed #side-toggle { margin-bottom: 0; }
  .row.app { height: 28px; padding-left: 26px; color: #bdbdbd; }
  #side button, .menu button { background: transparent; border: 0; border-radius: 8px; color: inherit; padding: 0; }
  .more { width: 28px; height: 28px; flex: none; opacity: 0; font-size: 16px; line-height: 1; }
  .row:hover .more, .nav-label:hover .more, .row.on .more { opacity: 1; }
  .more:hover, .menu button:hover { background: #424242; }
  .menu { position: fixed; z-index: 80; min-width: 188px; padding: 6px; background: #2a2a2a; border: 1px solid #3f3f3f; border-radius: 12px; box-shadow: 0 12px 40px rgba(0,0,0,.45); }
  .menu button { display: flex; align-items: center; justify-content: space-between; gap: 16px; width: 100%; text-align: left; padding: 8px 10px; }
  .menu .keys { color: #9a9a9a; font-size: 12px; white-space: nowrap; }
  .menu button.danger:hover { background: #6e2222; }
  dialog { background: #1c2118; color: inherit; border: 1px solid #3d4636; width: min(640px, 96vw); font-family: ui-sans-serif, system-ui, "Segoe UI", sans-serif; }
  dialog::backdrop { background: rgba(0,0,0,.45); }
  .hint { color: #b7c4a4; }
  .preset { display: grid; grid-template-columns: 1fr 1.4fr 1fr auto; gap: 6px; margin-bottom: 6px; }
  .preset input, .preset button, dialog .side-actions button { font: inherit; background: #12140f; color: inherit; border: 1px solid #3d4636; border-radius: 6px; padding: 6px 8px; }
  dialog .side-actions { display: flex; gap: 6px; margin-top: 8px; }
  body.tools-hidden #tools { display: none; }
  #tools-toggle { position: fixed; z-index: 75; top: 8px; left: 8px; min-height: 36px; padding: 6px 12px; touch-action: manipulation; background: #2a3124; border: 1px solid #6d7c58; box-shadow: 0 2px 8px rgba(0,0,0,.35); }
  @media (max-width: 720px) {
    #app { padding: 0 env(safe-area-inset-right) env(safe-area-inset-bottom) env(safe-area-inset-left); }
    #tools { position: fixed; z-index: 60; top: env(safe-area-inset-top); left: 0; right: 0; max-height: 70dvh; overflow: auto; -webkit-overflow-scrolling: touch; box-shadow: 0 8px 24px rgba(0,0,0,.45); }
    #tools input, #tools select, #tools button { font-size: 16px; touch-action: manipulation; }
    #side { position: fixed; z-index: 65; top: 0; bottom: 0; left: 0; width: min(300px, 88vw); max-height: none; padding-top: calc(10px + env(safe-area-inset-top)); padding-bottom: env(safe-area-inset-bottom); box-shadow: 8px 0 28px rgba(0,0,0,.5); }
    #side.collapsed { top: calc(8px + env(safe-area-inset-top)); left: calc(8px + env(safe-area-inset-left)); bottom: auto; width: auto; height: auto; padding: 0; border: 0; background: transparent; box-shadow: none; z-index: 70; }
    #groups { -webkit-overflow-scrolling: touch; }
    #side #side-toggle { width: auto; min-width: 44px; height: 44px; margin: 0 0 8px; padding: 0 12px; border: 1px solid #5a5a5a; border-radius: 10px; background: #2a2a2a; color: #f2f2f2; font: 15px/44px ui-sans-serif, system-ui, "Segoe UI", sans-serif; touch-action: manipulation; }
    #side.collapsed #side-toggle { margin: 0; }
    .more { opacity: 1; }
    .card { min-width: 0; }
  }
  @media (hover: none) {
    .more { opacity: 1; }
  }
  button, select { font: inherit; background: #2a3124; color: inherit; border: 1px solid #3d4636; border-radius: 6px; padding: 6px 8px; }
  button { cursor: pointer; }
  #status { color: #b7c4a4; }
  #empty { margin: 24px; color: #b7c4a4; pointer-events: none; }
  #grid { position: relative; flex: 1; min-height: 0; overflow: hidden; background: #1a1e16; }
  #dock { display: flex; gap: 6px; flex-wrap: wrap; padding: 4px 8px; background: #1c2118; }
  #dock:empty { display: none; }
  .card { position: absolute; display: flex; flex-direction: column; min-width: 280px; border: 1px solid #5a5344; background: #000; box-shadow: 0 10px 28px rgba(0,0,0,.45); overflow: hidden; }
  .card.active { border-color: #d7c07a; }
  .card:not(.active) .bar { background: #2a261f; color: #b7b0a2; }
  .card.max { border-width: 0; }
  .bar { display: flex; gap: 0; align-items: stretch; padding: 0 0 0 10px; min-height: 32px; background: #343022; cursor: move; user-select: none; }
  .bar span { flex: 1; display: flex; align-items: center; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .bar button.win { width: 46px; height: 32px; padding: 0; border: 0; border-radius: 0; background: transparent; color: inherit; display: inline-flex; align-items: center; justify-content: center; flex: none; }
  .bar button.win svg { width: 12px; height: 12px; display: block; }
  .bar button.win:hover { background: rgba(255,255,255,.12); }
  .bar button.win.close:hover { background: #e81123; color: #fff; }
  .bar button.win.link { width: 28px; }
  .bar button.win.link i { width: 8px; height: 8px; border-radius: 50%; display: block; background: #e81123; }
  .bar button.win.link.on i { background: #16c60c; }
  .bar .rate { flex: none; align-self: center; margin-right: 4px; font: 12px/1 Consolas, ui-monospace, monospace; color: #d5d0c4; white-space: nowrap; pointer-events: none; }
  .screen { flex: 1; min-width: 0; min-height: 0; width: 100%; height: 0; overflow: hidden; position: relative; }
  .screen > .xterm { position: absolute; left: 0; top: 0; right: 0; bottom: 0; }
  .grip { position: absolute; z-index: 2; }
  .grip.n, .grip.s { left: 8px; right: 8px; height: 6px; cursor: ns-resize; }
  .grip.e, .grip.w { top: 8px; bottom: 8px; width: 6px; cursor: ew-resize; }
  .grip.n { top: 0; } .grip.s { bottom: 0; } .grip.e { right: 0; } .grip.w { left: 0; }
  .grip.nw, .grip.ne, .grip.sw, .grip.se { width: 12px; height: 12px; }
  .grip.nw { top: 0; left: 0; cursor: nwse-resize; } .grip.se { right: 0; bottom: 0; cursor: nwse-resize; }
  .grip.ne { top: 0; right: 0; cursor: nesw-resize; } .grip.sw { left: 0; bottom: 0; cursor: nesw-resize; }
  .card.max .grip { display: none; }
</style>
</head>
<body>
<div id="gate">
  <div>
    <form id="gate-local">
      <h2>邮箱账户</h2>
      <input id="auth-name" type="email" placeholder="邮箱" autocomplete="username">
      <input id="auth-pass" type="password" placeholder="密码" autocomplete="current-password">
      <button id="do-login" type="button">登录</button>
      <button id="do-register" type="button">注册</button>
    </form>
    <form>
      <h2>Token</h2>
      <input id="auth-token" placeholder="输入 Token 进入">
      <button id="do-token" type="button">进入</button>
    </form>
    <p id="gate-msg" class="hint">没有 Token 不能看窗口和频道。用邮箱注册并登录后得到自己的 Token。本机页面会填上默认 Token。</p>
  </div>
</div>
<header id="tools">
  <label>命令 <select id="shell"></select></label>
  <button id="new-window" type="button">新窗口</button>
  <button id="arrange" type="button" title="排列窗口">窗口</button>
  <button id="close-window" type="button">关窗口</button>
  <button id="close-cli" type="button">关 CLI</button>
  <button id="open-settings" type="button">设定</button>
  <label>中继 <select id="relay"></select></label>
  <button id="edit-relays" type="button">登记</button>
  <input id="win-title" type="text" placeholder="窗口标题" maxlength="40">
  <button id="rename-title" type="button">改标题</button>
  <button id="auto-title" type="button">自动标题</button>
  <button id="copy-token" type="button">复制 Token</button>
  <span id="status">未连接</span>
</header>
<button id="tools-toggle" type="button" title="收起顶部菜单" aria-expanded="true">收起菜单</button>
<div id="desk">
<aside id="side">
  <button id="side-toggle" class="mark" type="button" title="收起">-</button>
  <div id="groups"></div>
</aside>
<div id="app">
<p id="empty">用顶部的命令打开会话。左侧是项目、电脑和窗口。</p>
<div id="grid"></div>
<div id="dock"></div>
</div>
</div>
<dialog id="settings">
  <form id="preset-form">
    <h2>项目</h2>
    <p class="hint">左边按项目、电脑、窗口排列。电脑名称用这台主机的名字。项目可以自己改，一台电脑只属于一个项目。</p>
    <p class="hint">电脑 <span id="machine-name"></span></p>
    <input id="project-name" placeholder="项目" maxlength="40">
    <div class="side-actions">
      <button id="save-identity" type="button">保存项目</button>
    </div>
    <h2>命令行与快捷键</h2>
    <p class="hint">电脑是生产者，手机和网页是订阅端。分组的频道名就是推送频道，走现有 WebSocket 中继。不另开 MQTT，少一个常驻进程，按键也少一跳。快捷键要带 Alt。</p>
    <div id="preset-rows"></div>
    <div class="side-actions">
      <button id="add-preset" type="button">加一条</button>
      <button id="save-presets" type="button">保存</button>
      <button id="close-settings" type="button">关闭</button>
    </div>
  </form>
</dialog>
<dialog id="relay-box">
  <form id="relay-form">
    <h2>中继</h2>
    <p class="hint">可以登记多台。保存后办公室电脑会同时向外连接。手机用上面的下拉切换要看的那一台。</p>
    <div id="relay-rows"></div>
    <div class="side-actions">
      <button id="add-relay" type="button">加一台</button>
      <button id="save-relays" type="button">保存</button>
      <button id="close-relays" type="button">关闭</button>
    </div>
  </form>
</dialog>
<script src="/xterm.js"></script>
<script src="/xterm-fit.js"></script>
<script src="/xterm-unicode11.js"></script>
<script src="/xterm-webgl.js"></script>
<script src="/tab-themes.js"></script>
<script>
const params = new URLSearchParams(location.search);
let relayCode = params.get("code") || "";
function tokenKey(code) {
  const value = code === undefined ? relayCode : code;
  return value ? "sma-token:" + value : "sma-token";
}
const fragmentToken = new URLSearchParams(location.hash.slice(1)).get("token");
if (fragmentToken || params.has("token")) {
  const clean = new URL(location.href); clean.searchParams.delete("token"); clean.hash = "";
  history.replaceState(null, "", clean.pathname + clean.search);
}
let token = fragmentToken || params.get("token") || sessionStorage.getItem(tokenKey()) || "";
if (token) {
  sessionStorage.setItem(tokenKey(), token);

}
const grid = document.getElementById("grid");
const dock = document.getElementById("dock");
const empty = document.getElementById("empty");
const status = document.getElementById("status");
const panes = new Map();
let control;
let catalog = [];
let groups = [];
let presets = [
  {id: "powershell", label: "PowerShell", command: "powershell", shortcut: "Ctrl+Alt+1"},
  {id: "pwsh", label: "pwsh", command: "pwsh", shortcut: "Ctrl+Alt+2"},
  {id: "cmd", label: "cmd", command: "cmd", shortcut: "Ctrl+Alt+3"},
  {id: "claude", label: "claude", command: "claude", shortcut: "Ctrl+Alt+4"},
  {id: "codex", label: "codex", command: "codex", shortcut: "Ctrl+Alt+5"}
];
let viewGroup = "";
let viewChannel = "";
let machineName = "";
let projectName = "默认";
let listedProjects = [];
let phoneToolsOpen = false;
let activeId = "";
let conn = "未连接";
let resizeTimer = 0;
let zTop = 1;
window.__lastTerm = null;

function pack(op, bytes) {
  const body = new Uint8Array(1 + bytes.length);
  body[0] = op;
  body.set(bytes, 1);
  const frame = new Uint8Array(4 + body.length);
  new DataView(frame.buffer).setUint32(0, body.length, true);
  frame.set(body, 4);
  return frame;
}
function packJson(op, obj) {
  return pack(op, new TextEncoder().encode(JSON.stringify(obj)));
}
function unpack(buf) {
  const view = new DataView(buf);
  const bytes = new Uint8Array(buf);
  const length = view.getUint32(0, true);
  return [bytes[4], bytes.slice(5, 4 + length)];
}
function loadRelayBook() {
  try { return JSON.parse(localStorage.getItem("sma-relays") || "[]"); }
  catch (err) { return []; }
}
let relayBook = loadRelayBook();
let relayPick = localStorage.getItem("sma-relay-pick") || "";
if (params.get("direct") === "1") { relayCode = ""; relayPick = ""; }
function fitCells(boxW, boxH, cellW, cellH, scrollBar) {
  if (!(cellW > 0) || !(cellH > 0)) return null;
  const cols = Math.floor((boxW - (scrollBar || 0)) / cellW);
  const rows = Math.floor(boxH / cellH);
  if (cols < 2 || rows < 2) return null;
  return {cols: cols, rows: rows};
}
function shouldRefit(reason, prevBox, nextBox) {
  if (reason === "scroll" || reason === "chrome") return false;
  if (!nextBox || !(nextBox.w > 8) || !(nextBox.h > 8)) return false;
  if (prevBox && prevBox.w === nextBox.w && prevBox.h === nextBox.h) return false;
  return true;
}
function viewportReason(prev, next) {
  if (!prev || !next || next.w !== prev.w) return "box";
  return "chrome";
}
function toolbarHidden(width, open) {
  return width <= 720 && !open;
}
window.__fitCells = fitCells;
window.__shouldRefit = shouldRefit;
window.__viewportReason = viewportReason;
window.__toolbarHidden = toolbarHidden;
function relayBase(url) {
  let text = (url || "").trim();
  if (!text) return "";
  if (text.startsWith("http://")) text = "ws://" + text.slice(7);
  else if (text.startsWith("https://")) text = "wss://" + text.slice(8);
  else if (!/^wss?:\/\//i.test(text)) text = "ws://" + text;
  return text.replace(/\/$/, "");
}
function pickedRelay() {
  return relayBook.find((item) => item.id === relayPick) || null;
}
function currentCode() {
  const item = pickedRelay();
  return item && item.code ? item.code : relayCode;
}
function socketUrl() {
  const item = pickedRelay();
  const base = item ? relayBase(item.url) : "";
  if (base) return base + "/ws";
  const proto = location.protocol === "https:" ? "wss" : "ws";
  return proto + "://" + location.host + "/ws";
}
function rememberThisRelay() {
  if (!relayCode) return;
  const url = (location.protocol === "https:" ? "wss://" : "ws://") + location.host;
  if (relayBook.some((item) => relayBase(item.url) === url && item.code === relayCode)) return;
  relayBook.push({id: Date.now().toString(36), name: location.host, url, code: relayCode});
  localStorage.setItem("sma-relays", JSON.stringify(relayBook));
}
function showStatus(error) {
  const pane = panes.get(activeId);
  const where = pane ? pane.label : "";
  const text = error || [conn, where].filter(Boolean).join(" · ");
  status.textContent = text;
  document.title = "SuperMonoAgent · " + (error || conn || "未连接");
}
function layoutPane(pane) {
  const box = pane.place;
  const fill = isPhone() && !pane.minimized && shown(pane);
  pane.card.classList.toggle("max", (pane.maximized || fill) && !pane.minimized);
  pane.card.style.display = pane.minimized || !shown(pane) ? "none" : "flex";
  pane.card.style.zIndex = String(box.z);
  if ((pane.maximized || fill) && !pane.minimized) {
    pane.card.style.left = "0";
    pane.card.style.top = "0";
    pane.card.style.width = "100%";
    pane.card.style.height = "100%";
    if (fill) pane.card.style.minWidth = "0";
  } else {
    if (pane.card.style.minWidth === "0px") pane.card.style.minWidth = "";
    pane.card.style.left = box.x + "px";
    pane.card.style.top = box.y + "px";
    pane.card.style.width = box.w + "px";
    pane.card.style.height = box.h + "px";
  }
}
function renderDock() {
  dock.replaceChildren();
  for (const [id, pane] of panes) {
    if (!pane.minimized || !shown(pane)) continue;
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = pane.label;
    button.onclick = () => {
      pane.minimized = false;
      layoutPane(pane);
      activate(id);
    };
    dock.append(button);
  }
}
function markActive() {
  for (const [id, pane] of panes) {
    const on = id === activeId;
    pane.card.classList.toggle("active", on);
    if (pane.term) pane.term.options.cursorBlink = on;
    if (!on && pane.term) pane.term.blur();
  }
  renderDock();
  showStatus();
  const box = document.getElementById("win-title");
  const win = catalog.find((item) => item.id === (panes.get(activeId) || {}).windowId);
  if (box && document.activeElement !== box) box.value = win ? win.title : "";
}
function focusActiveTerm() {
  const pane = panes.get(activeId);
  if (!pane || !pane.term || pane.minimized) return false;
  pane.term.focus();
  return true;
}
function keepsKeys(el) {
  if (!el || el === document.body || el === document.documentElement) return false;
  if (el.closest && el.closest(".xterm, dialog, #gate")) return true;
  const tag = el.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || !!el.isContentEditable;
}
function keyToData(ev) {
  if (ev.isComposing || ev.keyCode === 229 || ev.altKey || ev.metaKey) return "";
  if (ev.ctrlKey) {
    const key = (ev.key || "").toLowerCase();
    if (key === "v" || key.length !== 1 || key < "a" || key > "z") return "";
    return String.fromCharCode(key.charCodeAt(0) - 96);
  }
  const named = {
    Enter: "\r", Backspace: "\x7f", Tab: "\t", Escape: "\x1b",
    ArrowUp: "\x1b[A", ArrowDown: "\x1b[B", ArrowRight: "\x1b[C", ArrowLeft: "\x1b[D",
    Delete: "\x1b[3~", Home: "\x1b[H", End: "\x1b[F", PageUp: "\x1b[5~", PageDown: "\x1b[6~"
  };
  if (named[ev.key]) return named[ev.key];
  if (ev.key && ev.key.length === 1) return ev.key;
  return "";
}
function activate(id) {
  const pane = panes.get(id);
  if (!pane) return;
  activeId = id;
  if (pane.minimized) pane.minimized = false;
  zTop += 1;
  pane.place.z = zTop;
  for (const item of panes.values()) layoutPane(item);
  noteActivity(pane.sessionId || pane.windowId);
  markActive();
  if (pane.term) {
    pane.term.focus();
    window.__lastTerm = pane.term;
    sendResize(pane, "box");
    requestAnimationFrame(() => paintPane(pane));
  }
}
function screenText() {
  const term = window.__lastTerm;
  if (!term) return "";
  const buf = term.buffer.active;
  let out = "";
  for (let i = 0; i < buf.length; i++) out += buf.getLine(i).translateToString(true) + "\n";
  return out;
}
window.__screen = screenText;
function measureBox(pane) {
  const card = pane && pane.card;
  if (!card) return null;
  const bar = card.querySelector(".bar");
  const barH = bar ? bar.offsetHeight : 0;
  const fill = !!(pane.maximized || (isPhone() && !pane.minimized));
  if (fill && grid && grid.clientWidth > 8 && grid.clientHeight > barH + 8) {
    return {w: grid.clientWidth, h: grid.clientHeight - barH};
  }
  return {w: card.clientWidth, h: Math.max(0, card.clientHeight - barH)};
}
function desiredSize(pane) {
  const box = measureBox(pane);
  const core = pane.term && pane.term._core;
  const cell = core && core._renderService && core._renderService.dimensions.css.cell;
  if (!box || !cell || !(cell.width > 0) || !(cell.height > 0)) return null;
  const rawBar = core.viewport ? core.viewport.scrollBarWidth : 0;
  const bar = rawBar > 0 && rawBar < 24 ? rawBar : 0;
  return fitCells(box.w, box.h, cell.width, cell.height, bar);
}
function applyFit(pane) {
  const box = measureBox(pane);
  const core = pane.term._core;
  const cell = core && core._renderService && core._renderService.dimensions.css.cell;
  if (!box || !cell || !(cell.width > 0) || !(cell.height > 0)) {
    pane.fit.fit();
    return;
  }
  const rawBar = core.viewport ? core.viewport.scrollBarWidth : 0;
  const bar = rawBar > 0 && rawBar < 24 ? rawBar : 0;
  const decision = fitCells(box.w, box.h, cell.width, cell.height, bar);
  if (!decision) return;
  if (pane.term.cols !== decision.cols || pane.term.rows !== decision.rows) pane.term.resize(decision.cols, decision.rows);
}
function sendResize(pane, reason) {
  if (!pane || pane.minimized || !pane.term || !pane.fit || !pane.ws || pane.ws.readyState !== 1) return;
  const next = measureBox(pane);
  const force = reason === "force";
  if (reason === "lock") {
    if (!next || !(next.w > 8) || !(next.h > 8)) return;
    const same = pane.fitted && pane.fitted.w === next.w && pane.fitted.h === next.h;
    if (same && (pane.lockSends || 0) >= 2) return;
    pane.lockSends = same ? (pane.lockSends || 0) + 1 : 1;
  } else if (!force && !shouldRefit(reason || "box", pane.fitted, next)) {
    const want = desiredSize(pane);
    if (!want || (want.cols === pane.term.cols && want.rows === pane.term.rows)) return;
  } else pane.lockSends = 0;
  applyFit(pane);
  if (pane.term.cols < 2 || pane.term.rows < 2) return;
  pane.fitted = next;
  pane.ws.send(packJson(5, {cols: pane.term.cols, rows: pane.term.rows}));
}
function shellDesktop() { return params.get("shell") === "desktop"; }
function attachDesktopRenderer(term) {
  if (!shellDesktop()) return;
  try {
    if (window.Unicode11Addon) {
      term.loadAddon(new Unicode11Addon.Unicode11Addon());
      term.unicode.activeVersion = "11";
    }
  } catch (err) {}
  try {
    if (window.WebglAddon) {
      const addon = new WebglAddon.WebglAddon();
      addon.onContextLoss(() => { try { addon.dispose(); } catch (e) {} });
      term.loadAddon(addon);
      term.refresh(0, Math.max(0, term.rows - 1));
    }
  } catch (err) {}
}
function bindTerm(pane, host) {
  const term = new Terminal({
    allowProposedApi: shellDesktop(),
    fontFamily: shellDesktop() ? '"Cascadia Mono", "Cascadia Code", Consolas, monospace' : "Consolas, monospace",
    fontSize: shellDesktop() ? 14 : 15,
    drawBoldTextInBrightColors: shellDesktop(),
    cursorBlink: true,
    scrollback: 3000,
    convertEol: true,
    theme: {
      background: "#000000",
      foreground: "#e6e6e6",
      cursor: "#ffd866",
      cursorAccent: "#000000",
      selectionBackground: "#3a3320",
      black: "#000000",
      red: "#ff4d4d",
      green: "#7dce6a",
      yellow: "#e6c85a",
      blue: "#6cb6ff",
      magenta: "#d291e4",
      cyan: "#5fd7d7",
      white: "#e6e6e6",
      brightBlack: "#8a8a8a",
      brightRed: "#ff5555",
      brightGreen: "#9eeb8a",
      brightYellow: "#ffe08a",
      brightBlue: "#9ecbff",
      brightMagenta: "#f0b4ff",
      brightCyan: "#9aeeee",
      brightWhite: "#ffffff"
    }
  });
  const fit = new FitAddon.FitAddon();
  term.loadAddon(fit);
  term.open(host);
  attachDesktopRenderer(term);
  fit.fit();
  term.onData((data) => {
    if (activeId !== pane.key) return;
    if (/^\x1b[[>][0-9;?]*[cRn]$/.test(data)) return;
    if (pane.ws && pane.ws.readyState === 1) pane.ws.send(pack(4, new TextEncoder().encode(data)));
  });
  pane.term = term;
  pane.fit = fit;
  window.__lastTerm = term;
  host.addEventListener("mousedown", () => activate(pane.key));
}
function paintPane(pane) {
  if (!pane.term || pane.minimized) return;
  pane.term.refresh(0, pane.term.rows - 1);
}
const PLAIN_TOKEN = /(`[^`\n]+`|\*\*[^*\n]+\*\*|"[^"\n]*"|'[^'\n]*'|(?:错误|失败|异常|警告)|(?:Error|Exception|ERROR|FAIL|FAILED|Warning)\b|(?:[\w./\\-]+)\.(?:py|js|ts|tsx|css|json|md|exe|ps1)(?::\d+)?|\b[A-Za-z_][\w]*\(\)|OP_[A-Z0-9_]+|\b\d+(?:\.\d+)?(?:\s?(?:KiB|MiB|ms|字节))?|[\u2500-\u257F])/g;
function colorizePlain(text) {
  return text.replace(PLAIN_TOKEN, (token) => {
    let tone = "95";
    if (token.charAt(0) === "`") tone = "96";
    else if (token.charAt(0) === "*" || token.charAt(0) === '"' || token.charAt(0) === "'") tone = token.charAt(0) === "*" ? "91" : "92";
    else if (/错误|失败|异常|警告|Error|Exception|ERROR|FAIL|Warning/i.test(token)) tone = "91";
    else if (/\.\w+/.test(token)) tone = "93";
    else if (token.charAt(token.length - 1) === ")") tone = "96";
    else if (token.charAt(0) === "O") tone = "95";
    else if (token.charCodeAt(0) > 127) tone = "90";
    return "\x1b[" + tone + "m" + token + "\x1b[39m";
  });
}
function sgrKeepsColor(seq) {
  const body = seq.slice(2, -1);
  if (body === "" || body === "0") return false;
  let on = false;
  body.split(";").forEach((part) => {
    const n = parseInt(part, 10);
    if (part === "0" || n === 39) on = false;
    else if ((n >= 30 && n <= 38) || (n >= 90 && n <= 97)) on = true;
  });
  return on;
}
function colorizeEcho(text) {
  let out = "";
  let i = 0;
  let colored = false;
  while (i < text.length) {
    if (text.charCodeAt(i) !== 27) {
      let j = i + 1;
      while (j < text.length && text.charCodeAt(j) !== 27) j += 1;
      const plain = text.slice(i, j);
      out += colored ? plain : colorizePlain(plain);
      i = j;
      continue;
    }
    const start = i;
    i += 1;
    const next = text.charAt(i);
    if (next === "[") {
      i += 1;
      while (i < text.length) {
        const c = text.charCodeAt(i);
        i += 1;
        if (c >= 64 && c <= 126) break;
      }
      const seq = text.slice(start, i);
      if (seq.charAt(seq.length - 1) === "m") colored = sgrKeepsColor(seq);
      out += seq;
      continue;
    }
    if (next === "]") {
      i += 1;
      while (i < text.length) {
        if (text.charCodeAt(i) === 7) { i += 1; break; }
        if (text.charAt(i) === "\x1b" && text.charAt(i + 1) === "\\") { i += 2; break; }
        i += 1;
      }
      out += text.slice(start, i);
      continue;
    }
    if (next) i += 1;
    out += text.slice(start, i);
  }
  return out;
}
function armSizeLock(pane) {
  if (!pane || pane._lockTimer) return;
  setTimeout(() => sendResize(pane, "lock"), 120);
  pane._lockTimer = setTimeout(() => {
    pane._lockTimer = null;
    sendResize(pane, "lock");
  }, 700);
}
function noteTermOutput(pane, text) {
  const now = Date.now();
  const raw = typeof text === "string" ? text : "";
  const alt = raw.indexOf("\x1b[?1049h") >= 0 || raw.indexOf("\x1b[?1047h") >= 0 || raw.indexOf("\x1b[?47h") >= 0;
  const gap = now - (pane.lastOut || 0);
  pane.lastOut = now;
  if (alt || (raw.length > 2000 && gap > 600)) pane.lockSends = 0;
  if (alt || gap > 600) armSizeLock(pane);
}
function writeTerm(pane, text, reset) {
  if (!pane.term) return;
  noteTermOutput(pane, text);
  if (reset) pane.term.reset();
  pane.term.write(colorizeEcho(text), () => {
    if (pane._painted) return;
    pane._painted = true;
    requestAnimationFrame(() => paintPane(pane));
  });
  if (pane.key === activeId) window.__lastTerm = pane.term;
  noteActivity(pane.sessionId || pane.windowId);
}
function nextPlace() {
  const step = Math.max(0, panes.size - 1);
  const box = gridBox();
  const x = Math.min((step % 6) * 36, box.w / 4);
  const y = Math.min((step % 6) * 32, box.h / 4);
  return {x, y, w: Math.max(1, box.w - x - 2), h: Math.max(1, box.h - y - 2), z: zTop};
}
function trackPointer(ev, move, done) {
  ev.preventDefault();
  function onMove(e) { move(e); }
  function onUp(e) {
    window.removeEventListener("mousemove", onMove);
    window.removeEventListener("mouseup", onUp);
    if (done) done(e);
  }
  window.addEventListener("mousemove", onMove);
  window.addEventListener("mouseup", onUp);
}
function bindFrame(pane) {
  const card = pane.card;
  const bar = pane.title.parentElement;
  bar.addEventListener("mousedown", (ev) => {
    if (ev.target.closest("button")) return;
    activate(pane.key);
    if (pane.maximized || pane.minimized) return;
    const ox = pane.place.x;
    const oy = pane.place.y;
    const sx = ev.clientX;
    const sy = ev.clientY;
    trackPointer(ev, (e) => {
      pane.place.x = ox + e.clientX - sx;
      pane.place.y = Math.max(0, oy + e.clientY - sy);
      layoutPane(pane);
    });
  });
  bar.addEventListener("dblclick", (ev) => {
    if (ev.target.closest("button")) return;
    toggleMax(pane);
  });
  for (const edge of ["n", "s", "e", "w", "nw", "ne", "sw", "se"]) {
    const grip = document.createElement("div");
    grip.className = "grip " + edge;
    grip.addEventListener("mousedown", (ev) => beginResize(pane, ev, edge));
    card.append(grip);
  }
}
function restorePane(pane) {
  pane.maximized = false;
  const z = pane.place.z;
  if (pane.beforeMax) pane.place = pane.beforeMax;
  pane.place.z = z;
  layoutPane(pane);
  showMaxGlyph(pane);
}
function toggleMax(pane) {
  if (pane.minimized) pane.minimized = false;
  if (pane.maximized) restorePane(pane);
  else {
    for (const other of panes.values()) {
      if (other !== pane && other.maximized) restorePane(other);
    }
    pane.beforeMax = Object.assign({}, pane.place);
    pane.maximized = true;
    layoutPane(pane);
    showMaxGlyph(pane);
  }
  activate(pane.key);
  sendResize(pane, "force");
}
const CAPTION = {
  min: '<svg viewBox="0 0 12 12"><path d="M2 8.5h8" stroke="currentColor" stroke-width="1"/></svg>',
  max: '<svg viewBox="0 0 12 12"><rect x="2.5" y="2.5" width="7" height="7" fill="none" stroke="currentColor" stroke-width="1"/></svg>',
  restore: '<svg viewBox="0 0 12 12"><path d="M4.5 2.5h5.2V7" fill="none" stroke="currentColor" stroke-width="1"/><rect x="2.3" y="4.5" width="5.4" height="5" fill="none" stroke="currentColor" stroke-width="1"/></svg>',
  close: '<svg viewBox="0 0 12 12"><path d="M3 3l6 6M9 3L3 9" stroke="currentColor" stroke-width="1"/></svg>',
  cli: '<svg viewBox="0 0 12 12"><circle cx="6" cy="6" r="4.2" fill="none" stroke="currentColor" stroke-width="1"/><path d="M4.2 4.2l3.6 3.6M7.8 4.2L4.2 7.8" stroke="currentColor" stroke-width="1"/></svg>'
};
function captionButton(kind, label) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "win" + (kind === "close" || kind === "cli" ? " close " + kind : " " + kind);
  button.title = label;
  button.setAttribute("aria-label", label);
  button.innerHTML = CAPTION[kind];
  return button;
}
function showMaxGlyph(pane) {
  if (!pane.maxButton) return;
  const restore = !!pane.maximized;
  pane.maxButton.innerHTML = CAPTION[restore ? "restore" : "max"];
  const label = restore ? "还原" : "最大化";
  pane.maxButton.title = label;
  pane.maxButton.setAttribute("aria-label", label);
}
function setLinkDot(pane, on) {
  if (!pane.linkDot) return;
  pane.linkOn = !!on;
  pane.linkDot.classList.toggle("on", pane.linkOn);
  const label = pane.linkOn ? "已连接" : "已断开";
  pane.linkDot.title = label;
  pane.linkDot.setAttribute("aria-label", label);
  renderRate(pane);
}
function formatRate(bytes) {
  const kb = bytes / 1024;
  if (kb >= 1024) return (kb / 1024).toFixed(1) + " MB/s";
  if (kb >= 10) return Math.round(kb) + " KB/s";
  return kb.toFixed(1) + " KB/s";
}
function renderRate(pane) {
  if (!pane.rate) return;
  if (!pane.linkOn) {
    pane.rate.hidden = true;
    return;
  }
  pane.rate.hidden = false;
  pane.rate.textContent = "↓ " + formatRate(pane.rxRate || 0) + "  ↑ " + formatRate(pane.txRate || 0);
}
function noteBytes(pane, kind, data) {
  let n = 0;
  if (data && data.byteLength != null) n = data.byteLength;
  else if (typeof data === "string") n = new TextEncoder().encode(data).length;
  if (kind === "rx") pane.rxBytes = (pane.rxBytes || 0) + n;
  else pane.txBytes = (pane.txBytes || 0) + n;
}
function toggleLink(pane, ev) {
  ev.stopPropagation();
  if (pane.linkOn) {
    pane.linked = false;
    setLinkDot(pane, false);
    if (pane.ws && pane.ws.readyState < 2) pane.ws.close();
    return;
  }
  pane.linked = true;
  if (pane.sessionId) openPane(pane);
  else setLinkDot(pane, !!(control && control.readyState === 1));
}
function refreshBlankLinks() {
  for (const pane of panes.values()) {
    if (pane.sessionId || pane.linked === false) continue;
    setLinkDot(pane, !!(control && control.readyState === 1));
  }
}
function beginResize(pane, ev, edge) {
  ev.stopPropagation();
  activate(pane.key);
  if (pane.maximized) return;
  const origin = Object.assign({}, pane.place);
  const sx = ev.clientX;
  const sy = ev.clientY;
  trackPointer(ev, (e) => {
    const dx = e.clientX - sx;
    const dy = e.clientY - sy;
    let x = origin.x;
    let y = origin.y;
    let w = origin.w;
    let h = origin.h;
    const minW = minPaneWidth(pane);
    if (edge.includes("e")) w = Math.max(minW, origin.w + dx);
    if (edge.includes("s")) h = Math.max(160, origin.h + dy);
    if (edge.includes("w")) { w = Math.max(minW, origin.w - dx); x = origin.x + origin.w - w; }
    if (edge.includes("n")) { h = Math.max(160, origin.h - dy); y = Math.max(0, origin.y + origin.h - h); }
    pane.place.x = x;
    pane.place.y = y;
    pane.place.w = w;
    pane.place.h = h;
    layoutPane(pane);
  }, () => sendResize(pane));
}
function dressCard(pane, screenText) {
  zTop += 1;
  pane.place = nextPlace();
  pane.place.z = zTop;
  pane.maximized = false;
  pane.minimized = false;
  pane.beforeMax = null;
  const card = document.createElement("section");
  card.className = "card";
  const bar = document.createElement("div");
  bar.className = "bar";
  const title = document.createElement("span");
  title.textContent = pane.label;
  title.ondblclick = (ev) => {
    ev.stopPropagation();
    const win = catalog.find((item) => item.id === pane.windowId);
    const next = prompt("窗口标题", win ? win.title : "");
    if (!next) return;
    sendGroup({action: "title", window: pane.windowId, title: next});
  };
  const rate = document.createElement("div");
  rate.className = "rate";
  rate.hidden = true;
  const linkDot = document.createElement("button");
  linkDot.type = "button";
  linkDot.className = "win link";
  linkDot.innerHTML = "<i></i>";
  const minButton = captionButton("min", "最小化");
  const maxButton = captionButton("max", "最大化");
  bar.append(title, rate, linkDot, minButton, maxButton);
  pane.rate = rate;
  pane.linkDot = linkDot;
  pane.linked = true;
  setLinkDot(pane, false);
  linkDot.addEventListener("click", (ev) => toggleLink(pane, ev));
  const screen = document.createElement("div");
  screen.className = "screen";
  if (screenText) screen.textContent = screenText;
  card.append(bar, screen);
  grid.append(card);
  pane.card = card;
  pane.title = title;
  pane.screen = screen;
  pane.maxButton = maxButton;
  bindFrame(pane);
  layoutPane(pane);
  minButton.addEventListener("click", (ev) => {
    ev.stopPropagation();
    pane.minimized = true;
    pane.maximized = false;
    layoutPane(pane);
    if (activeId === pane.key) {
      const other = [...panes.keys()].find((id) => id !== pane.key && !panes.get(id).minimized);
      if (other) activate(other);
      else { activeId = ""; markActive(); }
    } else renderDock();
  });
  maxButton.addEventListener("pointerup", (ev) => {
    if (ev.button !== 0) return;
    ev.stopPropagation();
    toggleMax(pane);
  });
  maxButton.addEventListener("click", (ev) => ev.stopPropagation());
  return screen;
}
function ensurePane(win, session) {
  const known = panes.get(session.id);
  const label = win.title + " · " + session.shell + " " + session.id.slice(0, 4);
  if (known) {
    known.label = label;
    known.title.textContent = label;
    TabThemes.apply(known.title.parentElement, win.tab_theme);
    known.windowId = win.id;
    renderDock();
    return;
  }
  const pane = {key: session.id, label, windowId: win.id, sessionId: session.id, ws: null, term: null, fit: null};
  panes.set(session.id, pane);
  const screen = dressCard(pane);
  const bar = pane.title.parentElement;
  TabThemes.apply(bar, win.tab_theme);
  bar.oncontextmenu = (event) => showTabMenu(event, win.id);
  const shutCli = captionButton("cli", "关 CLI");
  const shutWin = captionButton("close", "关窗口");
  bar.append(shutCli, shutWin);
  bindTerm(pane, screen);
  screen.addEventListener("mousedown", () => activate(pane.key));
  shutCli.addEventListener("click", (ev) => {
    ev.stopPropagation();
    activate(session.id);
    if (control && control.readyState === 1) control.send(packJson(9, {session: session.id}));
  });
  shutWin.addEventListener("click", (ev) => {
    ev.stopPropagation();
    activate(session.id);
    if (control && control.readyState === 1) control.send(packJson(9, {window: win.id}));
  });
  openPane(pane);
  activate(session.id);
}
function openPane(pane) {
  const ws = new WebSocket(socketUrl());
  pane.ws = ws;
  ws.binaryType = "arraybuffer";
  const rawSend = ws.send.bind(ws);
  ws.send = (data) => {
    noteBytes(pane, "tx", data);
    return rawSend(data);
  };
  ws.onopen = () => {
    if (currentCode()) { ws.send(JSON.stringify({op: "phone", code: currentCode(), token})); return; }
    ws.send(packJson(1, {token, window: pane.windowId, channel: viewChannel}));
  };
  let live = false;
  let snap = null;
  let extra = "";
  let expectFocus = false;
  function showSnap(text) {
    live = true;
    expectFocus = false;
    writeTerm(pane, text, true);
    snap = null;
    extra = "";
    sendResize(pane);
  }
  ws.onmessage = (ev) => {
    noteBytes(pane, "rx", ev.data);
    if (typeof ev.data === "string") {
      const msg = JSON.parse(ev.data);
      if (msg.op === "ready") ws.send(packJson(1, {token, window: pane.windowId, channel: viewChannel}));
      return;
    }
    const [op, payload] = unpack(ev.data);
    let text = new TextDecoder().decode(payload);
    if (op === 3 || op === 8) {
      const cut = text.indexOf(String.fromCharCode(0));
      const head = cut >= 0 ? text.slice(0, cut) : "";
      if (/^[0-9a-f]{8}$/.test(head)) {
        if (head !== pane.sessionId) return;
        text = text.slice(cut + 1);
      }
    }
    if (!live) {
      if (op === 8) {
        if (expectFocus) showSnap(text);
        else { snap = text; extra = ""; }
      } else if (op === 3) {
        if (snap !== null || expectFocus) extra += text;
      } else if (op === 2) {
        const info = JSON.parse(text);
        if (info.error) showStatus(info.error);
        if (pane.linked !== false) setLinkDot(pane, true);
        if ((info.session || "") !== pane.sessionId) {
          snap = null;
          extra = "";
          expectFocus = true;
          ws.send(packJson(14, {session: pane.sessionId}));
        } else if (snap !== null) {
          showSnap(snap + extra);
        } else {
          live = true;
          sendResize(pane);
        }
      }
      return;
    }
    if (op === 3) writeTerm(pane, text, false);
    else if (op === 8) writeTerm(pane, text, true);
  };
  ws.onclose = () => {
    if (panes.get(pane.sessionId) !== pane || pane.ws !== ws) return;
    setLinkDot(pane, false);
    if (pane.linked === false) return;
    setTimeout(() => { if (panes.get(pane.sessionId) === pane && pane.linked !== false) openPane(pane); }, 1500);
  };
}
function ensureBlank(win) {
  const id = "blank:" + win.id;
  const label = win.title + " · 还没有 CLI";
  const known = panes.get(id);
  if (known) {
    known.label = label;
    known.title.textContent = label;
    TabThemes.apply(known.title.parentElement, win.tab_theme);
    known.windowId = win.id;
    renderDock();
    return;
  }
  const pane = {key: id, label, windowId: win.id, sessionId: "", ws: null, term: null, fit: null};
  panes.set(id, pane);
  dressCard(pane, "点「新窗口」打开终端。");
  TabThemes.apply(pane.title.parentElement, win.tab_theme);
  pane.title.parentElement.oncontextmenu = event => showTabMenu(event, win.id);
  pane.screen.addEventListener("mousedown", () => activate(id));
  const shutWin = captionButton("close", "关窗口");
  pane.title.parentElement.append(shutWin);
  shutWin.addEventListener("click", (ev) => {
    ev.stopPropagation();
    if (control && control.readyState === 1) control.send(packJson(9, {window: win.id}));
  });
  if (!activeId) activate(id);
}
function dropMissing(live) {
  for (const [id, pane] of [...panes]) {
    if (live.has(id)) continue;
    try { if (pane.ws) pane.ws.close(); } catch (err) {}
    if (pane.term) pane.term.dispose();
    pane.card.remove();
    panes.delete(id);
  }
  if (!panes.has(activeId)) activeId = panes.keys().next().value || "";
  for (const item of panes.values()) layoutPane(item);
  const current = panes.get(activeId);
  if (current) sendResize(current, "box");
  empty.hidden = panes.size > 0;
  markActive();
}
function shown(pane) {
  if (window.innerWidth <= 720 && pane.key !== activeId) return false;
  if (!viewGroup) return true;
  return windowGroup(pane.windowId) === viewGroup;
}
function windowGroup(windowId) {
  const win = catalog.find((item) => item.id === windowId);
  return win ? (win.group || "default") : "default";
}
function shortcutOf(ev) {
  const parts = [];
  if (ev.ctrlKey) parts.push("Ctrl");
  if (ev.altKey) parts.push("Alt");
  if (ev.shiftKey) parts.push("Shift");
  if (ev.metaKey) parts.push("Meta");
  const key = ev.key || "";
  if (!key || key === "Control" || key === "Alt" || key === "Shift" || key === "Meta") return "";
  parts.push(key.length === 1 ? key.toUpperCase() : key);
  return parts.join("+");
}
function sendGroup(obj) {
  if (control && control.readyState === 1) control.send(packJson(15, obj));
}
function selectGroup(group) {
  viewGroup = group.id;
  renderSide();
  for (const pane of panes.values()) layoutPane(pane);
  renderDock();
}
function focusWindow(win) {
  const group = groups.find((item) => item.id === (win.group || "default")) || groups[0];
  if (group) selectGroup(group);
  const session = (win.sessions || []).find((item) => item.id === win.focused) || (win.sessions || [])[0];
  const id = session ? session.id : "blank:" + win.id;
  if (panes.has(id)) activate(id);
}
let barSig = "";
function fillSelect(select, options, value, placeholder) {
  const current = value === undefined ? select.value : value;
  select.replaceChildren();
  if (placeholder) {
    const blank = document.createElement("option");
    blank.value = "";
    blank.textContent = placeholder;
    select.append(blank);
  }
  for (const option of options) {
    const node = document.createElement("option");
    node.value = option.value;
    node.textContent = option.label;
    select.append(node);
  }
  if ([...select.options].some((item) => item.value === current)) select.value = current;
}
function renderPresets() {
  const shell = document.getElementById("shell");
  if (!shell) return;
  const sig = presets.map((item) => item.id + item.label + item.command + item.shortcut).join("|");
  if (sig === barSig) return;
  barSig = sig;
  const shellValue = shell.value;
  fillSelect(shell, presets.map((item) => ({
    value: item.command,
    label: item.shortcut ? item.label + "  " + item.shortcut : item.label
  })), shellValue);
}
const activity = new Map();
const folded = new Set();
const phoneMedia = window.matchMedia("(max-width: 720px)");
function isPhone() { return phoneMedia.matches; }
let sideStored = localStorage.getItem("sma-side");
let sideClosed = sideStored === "1" || (sideStored === null && phoneMedia.matches);
let deskToolsHidden = localStorage.getItem("sma-tools") === "1";
let phoneToolsUntil = 0;
let toolsTimer = 0;
let sideSig = "";
function noteActivity(id) {
  if (!id) return;
  activity.set(id, Date.now());
  if (document.getElementById("menu")) return;
  const next = sideSignature();
  if (next === sideSig) return;
  renderSide();
}
function windowRank(win) {
  let best = activity.get(win.id) || 0;
  for (const session of win.sessions || []) best = Math.max(best, activity.get(session.id) || 0);
  return best;
}
function sessionsOf(group) {
  return catalog
    .filter((win) => (win.group || "default") === group.id)
    .map((win, index) => ({win, index}))
    .sort((a, b) => (windowRank(b.win) - windowRank(a.win)) || (b.index - a.index))
    .map((item) => item.win);
}
function sideSignature() {
  const rooms = listedProjects.map((item) => item.project + "=" + item.rooms.map((room) => room.code + room.machine).join(",")).join("|");
  const wins = catalog.map((win) => win.id + (win.title || "") + (win.tab_theme || "")).join(",");
  return rooms + "#" + projectName + "#" + machineName + "#" + relayCode + "#" + activeId + "#" + wins;
}
function sessionLabel(win) {
  const session = (win.sessions || []).find((item) => item.id === win.focused) || (win.sessions || [])[0];
  if (!session) return win.title || "空会话";
  return (win.title || "会话") + " · " + session.shell;
}
function minPaneWidth(pane) {
  const n = parseInt(pane.card && pane.card.style.minWidth, 10);
  return n > 0 ? n : 280;
}
function arrangeWindows(mode) {
  closeMenu();
  const list = [...panes.values()].filter((pane) => shown(pane) && !pane.minimized && pane.place);
  list.sort((a, b) => (a.place.z || 0) - (b.place.z || 0));
  if (list.length < 2) {
    showStatus("至少打开两个窗口再排列");
    return;
  }
  const dw = grid.clientWidth;
  const dh = grid.clientHeight;
  if (dw < 40 || dh < 40) return;
  const boxes = mode === "cascade" ? cascadeBoxes(list.length, dw, dh) : tileBoxes(list.length, mode, dw, dh);
  list.forEach((pane, i) => {
    const box = boxes[i];
    pane.maximized = false;
    pane.beforeMax = null;
    zTop += 1;
    pane.place.x = box.x;
    pane.place.y = box.y;
    pane.place.w = box.w;
    pane.place.h = box.h;
    pane.place.z = zTop;
    pane.card.style.minWidth = Math.min(280, box.w) + "px";
    showMaxGlyph(pane);
    layoutPane(pane);
  });
  const active = panes.get(activeId);
  if (active && list.includes(active)) {
    zTop += 1;
    active.place.z = zTop;
    layoutPane(active);
  }
  requestAnimationFrame(() => {
    for (const pane of list) {
      sendResize(pane);
      paintPane(pane);
    }
  });
  showStatus();
}
function cascadeBoxes(n, dw, dh) {
  const margin = 8;
  const span = Math.max(1, n - 1);
  const minW = Math.min(320, Math.max(160, dw - margin * 2 - 28));
  const minH = Math.min(220, Math.max(120, dh - margin * 2 - 24));
  const stepX = Math.min(28, Math.max(16, Math.floor((dw - margin * 2 - minW) / span)));
  const stepY = Math.min(26, Math.max(16, Math.floor((dh - margin * 2 - minH) / span)));
  let w = Math.min(Math.floor(dw * 0.74), dw - margin * 2 - stepX * span);
  let h = Math.min(Math.floor(dh * 0.78), dh - margin * 2 - stepY * span);
  w = Math.max(160, w);
  h = Math.max(120, h);
  const boxes = [];
  for (let i = 0; i < n; i++) {
    const x = margin + i * stepX;
    const y = margin + i * stepY;
    boxes.push({
      x: x,
      y: y,
      w: Math.max(120, Math.min(w, dw - x - margin)),
      h: Math.max(100, Math.min(h, dh - y - margin))
    });
  }
  return boxes;
}
function tileBoxes(n, mode, dw, dh) {
  const gap = 6;
  let cols = Math.ceil(Math.sqrt(n));
  let rows = Math.ceil(n / cols);
  if (mode === "rows") { cols = 1; rows = n; }
  else if (mode === "cols") { cols = n; rows = 1; }
  const boxes = [];
  for (let r = 0; r < rows; r++) {
    const count = Math.min(cols, n - r * cols);
    const cellW = Math.max(1, Math.floor((dw - gap * (count + 1)) / count));
    const cellH = Math.max(1, Math.floor((dh - gap * (rows + 1)) / rows));
    for (let c = 0; c < count; c++) {
      boxes.push({x: gap + c * (cellW + gap), y: gap + r * (cellH + gap), w: cellW, h: cellH});
    }
  }
  return boxes;
}
function arrangeItems() {
  return [
    {label: "层叠排列", keys: "Ctrl+Alt+L", run: () => arrangeWindows("cascade")},
    {label: "横向平铺", keys: "Ctrl+Alt+H", run: () => arrangeWindows("rows")},
    {label: "纵向平铺", keys: "Ctrl+Alt+V", run: () => arrangeWindows("cols")},
    {label: "网格平铺", keys: "Ctrl+Alt+G", run: () => arrangeWindows("grid")}
  ];
}
function menuFace(item) {
  const button = document.createElement("button");
  button.type = "button";
  if (item.danger) button.className = "danger";
  const name = document.createElement("span");
  name.textContent = item.label;
  button.append(name);
  if (item.keys) {
    const keys = document.createElement("span");
    keys.className = "keys";
    keys.textContent = item.keys;
    button.append(keys);
  }
  return button;
}
function closeMenu() {
  const menu = document.getElementById("menu");
  if (menu) menu.remove();
  const sub = document.getElementById("submenu");
  if (sub) sub.remove();
}
function openMenu(x, y, items) {
  closeMenu();
  const menu = document.createElement("div");
  menu.id = "menu";
  menu.className = "menu";
  for (const item of items) {
    const button = menuFace(item);
    button.onpointerenter = () => {
      const old = document.getElementById("submenu");
      if (old) old.remove();
      if (!item.children) return;
      const sub = document.createElement("div");
      sub.id = "submenu";
      sub.className = "menu";
      for (const child of item.children) {
        const choice = menuFace(child);
        choice.onclick = (ev) => { ev.stopPropagation(); closeMenu(); child.run(); };
        sub.append(choice);
      }
      document.body.append(sub);
      const rect = button.getBoundingClientRect();
      sub.style.left = Math.min(rect.right + 4, window.innerWidth - sub.offsetWidth - 8) + "px";
      sub.style.top = Math.min(rect.top, window.innerHeight - 8) + "px";
    };
    if (!item.children) button.onclick = (ev) => { ev.stopPropagation(); closeMenu(); item.run(); };
    menu.append(button);
  }
  document.body.append(menu);
  menu.style.left = Math.max(8, Math.min(x, window.innerWidth - menu.offsetWidth - 8)) + "px";
  menu.style.top = Math.max(8, Math.min(y, window.innerHeight - menu.offsetHeight - 8)) + "px";
}
function moreButton(items) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "more";
  button.textContent = "⋯";
  button.title = "菜单";
  button.onclick = (ev) => {
    ev.stopPropagation();
    const rect = button.getBoundingClientRect();
    openMenu(rect.left, rect.bottom + 4, items);
  };
  button.oncontextmenu = (ev) => ev.preventDefault();
  return button;
}
function projectItems(group) {
  const items = [
    {label: "新会话", children: presets.map((preset) => ({
      label: preset.shortcut ? preset.label + "  " + preset.shortcut : preset.label,
      run: () => { viewGroup = group.id; openShell("", preset.command); }
    }))},
    {label: "修改名称", run: () => {
      const name = prompt("项目名称", group.name);
      if (!name) return;
      sendGroup({action: "rename", id: group.id, name, channel: group.channel});
    }},
    {label: "推送频道", run: () => {
      const channel = prompt("推送频道", group.channel);
      if (!channel) return;
      sendGroup({action: "rename", id: group.id, name: group.name, channel});
    }}
  ];
  if (group.id !== "default") items.push({label: "删除 Project", danger: true, run: () => sendGroup({action: "delete", id: group.id})});
  return items;
}
function paneForWindow(win) {
  const session = (win.sessions || []).find((item) => item.id === win.focused) || (win.sessions || [])[0];
  const id = session ? session.id : "blank:" + win.id;
  return panes.get(id) || null;
}
function sessionItems(win) {
  const others = groups.filter((group) => group.id !== (win.group || "default"));
  const pane = paneForWindow(win);
  const items = [
    pane && pane.term ? {label: pane.maximized ? "还原" : "最大化", run: () => toggleMax(pane)} : null,
    {label: "修改名称", run: () => {
      const name = prompt("窗口名称", win.title || "");
      if (!name) return;
      sendGroup({action: "title", window: win.id, title: name});
    }},
    {label: "关闭窗口", danger: true, run: () => {
      if (control && control.readyState === 1) control.send(packJson(9, {window: win.id}));
    }}
  ];
  if (others.length) {
    items.push({label: "移到", children: others.map((group) => ({
      label: group.name,
      run: () => sendGroup({action: "move", window: win.id, group: group.id})
    }))});
  }
  return items.filter(Boolean);
}
function showTabMenu(event, windowId) {
  const win = catalog.find(item => item.id === windowId);
  if (!win) return;
  closeMenu();
  TabThemes.open(event, {
    theme: win.tab_theme,
    rename: () => {
      const name = prompt("窗口名称", win.title || "");
      if (name && name.trim()) sendGroup({action: "title", window: win.id, title: name});
    },
    select: theme => sendGroup({action: "tab-theme", window: win.id, theme})
  });
}
function rootItems() {
  return [
    {label: "新建 Project", run: () => {
      const name = prompt("Project 名称", "");
      if (!name) return;
      sendGroup({action: "create", name, channel: name});
    }},
    {label: "命令设定", run: () => openSettings()}
  ];
}
function treeMark(open, onToggle) {
  const mark = document.createElement("button");
  mark.type = "button";
  mark.className = "mark";
  mark.textContent = open ? "-" : "+";
  mark.title = open ? "收起" : "展开";
  mark.onclick = (ev) => {
    ev.stopPropagation();
    onToggle();
    renderSide();
  };
  return mark;
}
function appOf(win) {
  const session = (win.sessions || []).find((item) => item.id === win.focused) || (win.sessions || [])[0];
  const shellName = ((session && session.shell) || "空").trim();
  return (shellName.split(/[\\/]/).pop() || "空").split(" ")[0].replace(/\.exe$/i, "");
}
function applySide() {
  const side = document.getElementById("side");
  const button = document.getElementById("side-toggle");
  if (!side || !button) return;
  const phone = isPhone();
  side.classList.toggle("collapsed", sideClosed);
  button.textContent = sideClosed ? (phone ? "侧栏" : "+") : (phone ? "收起" : "-");
  button.title = sideClosed ? "展开侧栏" : "收起侧栏";
  button.setAttribute("aria-expanded", sideClosed ? "false" : "true");
}
function toolsHiddenNow() {
  if (isPhone()) return toolbarHidden(window.innerWidth, phoneToolsOpen);
  return deskToolsHidden;
}
function toolsBusy() {
  const el = document.activeElement;
  if (document.getElementById("menu")) return true;
  return !!(el && el.closest && el.closest("#tools"));
}
function placeToolsToggle() {
  const button = document.getElementById("tools-toggle");
  const side = document.getElementById("side");
  const tools = document.getElementById("tools");
  if (!button || !side) return;
  const hidden = toolsHiddenNow();
  button.textContent = hidden ? "展开菜单" : "收起菜单";
  button.title = hidden ? "展开顶部菜单" : "收起顶部菜单";
  button.setAttribute("aria-expanded", hidden ? "false" : "true");
  const rect = side.getBoundingClientRect();
  const phone = isPhone();
  let top = 8;
  let left = 8;
  if (!phone && !hidden && tools) {
    const bar = tools.getBoundingClientRect();
    top = Math.max(4, bar.top + (bar.height - button.offsetHeight) / 2);
  }
  if (phone || hidden) {
    top = Math.max(8, rect.top + 8);
    left = Math.max(8, rect.right + 8);
  }
  const reserve = button.offsetWidth + 16;
  if (left > window.innerWidth - reserve) left = Math.max(8, window.innerWidth - reserve);
  button.style.top = top + "px";
  button.style.left = left + "px";
  if (tools) tools.style.paddingLeft = hidden ? "" : (left + button.offsetWidth + 8) + "px";
}
function applyChrome() {
  const hiding = toolsHiddenNow();
  const ae = document.activeElement;
  const inTools = !!(ae && ae.closest && ae.closest("#tools"));
  if (hiding && inTools) ae.blur();
  document.body.classList.toggle("phone", isPhone());
  document.body.classList.toggle("tools-hidden", hiding);
  applySide();
  placeToolsToggle();
  if (hiding && inTools) focusActiveTerm();
}
function refitPanes(reason) {
  if (isPhone() && window.visualViewport) {
    document.body.style.height = window.visualViewport.height + "px";
    window.scrollTo(0, 0);
  } else document.body.style.height = "";
  const next = gridBox();
  if (!isPhone() && lastViewport && lastViewport.w > 0 && lastViewport.h > 0) {
    const scaleBox = box => {
      if (!box) return;
      box.x *= next.w / lastViewport.w;
      box.y *= next.h / lastViewport.h;
      box.w = Math.max(1, (box.w + 2) * next.w / lastViewport.w - 2);
      box.h = Math.max(1, (box.h + 2) * next.h / lastViewport.h - 2);
    };
    for (const pane of panes.values()) {
      scaleBox(pane.place);
      if (pane.beforeMax !== pane.place) scaleBox(pane.beforeMax);
    }
  }
  if (!isPhone()) lastViewport = next;
  for (const pane of panes.values()) layoutPane(pane);
  if (reason === "scroll" || reason === "chrome") return;
  for (const pane of panes.values()) sendResize(pane, reason || "box");
}
function showTools() {
  if (isPhone()) phoneToolsOpen = true;
  else {
    deskToolsHidden = false;
    localStorage.setItem("sma-tools", "0");
  }
  applyChrome();
  refitPanes(isPhone() ? "chrome" : "box");
}
function hideTools() {
  if (isPhone()) phoneToolsOpen = false;
  else {
    deskToolsHidden = true;
    localStorage.setItem("sma-tools", "1");
  }
  applyChrome();
  refitPanes(isPhone() ? "chrome" : "box");
}
function bumpTools() {}
function sectionLabel(text) {
  const head = document.createElement("div");
  head.className = "nav-label";
  const title = document.createElement("span");
  title.textContent = text;
  head.append(title);
  return head;
}
function currentProjects() {
  if (listedProjects.length) return listedProjects;
  return [{project: projectName || "默认", rooms: [{code: currentCode(), machine: machineName || "这台电脑"}]}];
}
function roomIsCurrent(room) {
  if (listedProjects.length) return !!room.code && room.code === currentCode();
  return true;
}
function enterRoom(code) {
  if (!code) return;
  if (code === relayCode && control && control.readyState === 1) return;
  relayCode = code;
  if (control) {
    const old = control;
    control = null;
    try { old.close(); } catch (err) {}
  }
  for (const pane of [...panes.values()]) {
    try { if (pane.ws) pane.ws.close(); } catch (err) {}
    if (pane.card) pane.card.remove();
  }
  panes.clear();
  catalog = [];
  activeId = "";
  const found = listedProjects.flatMap((item) => item.rooms.map((room) => Object.assign({project: item.project}, room))).find((room) => room.code === code);
  if (found) {
    machineName = found.machine || machineName;
    projectName = found.project || projectName;
  }
  token = sessionStorage.getItem(tokenKey(code)) || "";
  renderSide();
  if (!token) {
    document.getElementById("gate").classList.remove("hidden");
    const box = document.getElementById("auth-token");
    if (box) box.value = "";
    document.getElementById("gate-msg").textContent = "这台电脑要自己的 Token";
    return;
  }
  document.getElementById("gate").classList.add("hidden");
  connectControl();
}
function renderPicker() {
  if (!empty) return;
  if (currentCode() || !listedProjects.length) {
    empty.textContent = "用顶部的命令打开会话。左侧是项目、电脑和窗口。";
    empty.style.pointerEvents = "none";
    return;
  }
  empty.style.pointerEvents = "auto";
  empty.replaceChildren();
  for (const project of listedProjects) {
    const heading = document.createElement("div");
    heading.textContent = "项目 " + (project.project || "默认");
    empty.append(heading);
    for (const room of project.rooms) {
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = room.machine || "电脑";
      button.onclick = () => enterRoom(room.code);
      empty.append(button);
    }
  }
}
function renderSide() {
  const root = document.getElementById("groups");
  if (!root || document.getElementById("menu")) return;
  renderPresets();
  sideSig = sideSignature();
  applySide();
  root.replaceChildren();
  for (const project of currentProjects()) {
    root.append(sectionLabel("项目"));
    const prow = document.createElement("div");
    const mine = project.rooms.some(roomIsCurrent);
    prow.className = "row" + (mine ? " on" : "");
    const pname = document.createElement("button");
    pname.type = "button";
    pname.className = "name";
    pname.textContent = project.project || "默认";
    if (mine) {
      prow.title = "修改项目名称";
      pname.onclick = () => {
        const next = prompt("项目名称", project.project || "默认");
        if (!next || !next.trim()) return;
        sendGroup({action: "identity", project: next.trim()});
      };
    }
    prow.append(pname);
    root.append(prow);
    root.append(sectionLabel("电脑"));
    for (const room of project.rooms) {
      const row = document.createElement("div");
      const here = roomIsCurrent(room);
      row.className = "row app" + (here ? " on" : "");
      const name = document.createElement("button");
      name.type = "button";
      name.className = "name";
      name.textContent = room.machine || "电脑";
      name.onclick = () => { if (room.code) enterRoom(room.code); };
      row.append(name);
      root.append(row);
      if (!here) continue;
      root.append(sectionLabel("窗口"));
      if (!catalog.length) {
        const none = document.createElement("div");
        none.className = "row child";
        none.textContent = "还没有窗口";
        root.append(none);
        continue;
      }
      for (const win of catalog) {
        const session = (win.sessions || []).find((item) => item.id === win.focused) || (win.sessions || [])[0];
        const child = document.createElement("div");
        const current = session ? session.id : "blank:" + win.id;
        child.className = "row child" + (current === activeId ? " on" : "");
        const label = document.createElement("button");
        label.type = "button";
        label.className = "name";
        label.textContent = sessionLabel(win);
        label.onclick = () => focusWindow(win);
        const menuItems = sessionItems(win);
        child.append(label, moreButton(menuItems));
        TabThemes.apply(child, win.tab_theme);
        child.oncontextmenu = (ev) => showTabMenu(ev, win.id);
        root.append(child);
      }
    }
  }
  renderPicker();
}
function presetRow(preset) {
  const row = document.createElement("div");
  row.className = "preset";
  const label = document.createElement("input");
  label.placeholder = "名称";
  label.value = preset.label || "";
  const command = document.createElement("input");
  command.placeholder = "命令行";
  command.value = preset.command || "";
  const shortcut = document.createElement("input");
  shortcut.placeholder = "Alt 快捷键";
  shortcut.value = preset.shortcut || "";
  shortcut.addEventListener("keydown", (ev) => {
    ev.preventDefault();
    ev.stopPropagation();
    if (!(ev.altKey || ev.metaKey)) return;
    const spec = shortcutOf(ev);
    if (spec) shortcut.value = spec;
  });
  const del = document.createElement("button");
  del.type = "button";
  del.textContent = "删";
  del.onclick = () => row.remove();
  row.append(label, command, shortcut, del);
  row._fields = {label, command, shortcut, id: preset.id || ""};
  return row;
}
function openSettings() {
  const box = document.getElementById("preset-rows");
  box.replaceChildren();
  for (const preset of presets) box.append(presetRow(preset));
  const machineBox = document.getElementById("machine-name");
  const projectBox = document.getElementById("project-name");
  if (machineBox) machineBox.textContent = machineName || "电脑";
  if (projectBox) projectBox.value = projectName || "默认";
  document.getElementById("settings").showModal();
}
function chooseVisibleGroup() {
  const placed = catalog.some((win) => (win.group || "default") === viewGroup);
  if (viewGroup && placed) return;
  const hit = catalog.find((win) => (win.sessions || []).length) || catalog[0];
  if (!hit) return;
  const group = groups.find((item) => item.id === (hit.group || "default"));
  if (!group) return;
  viewGroup = group.id;
}
function sync(info) {
  if (info.windows) catalog = info.windows;
  if (info.groups) groups = info.groups;
  if (info.machine) machineName = info.machine;
  if (info.project) projectName = info.project;
  if (info.presets && info.presets.length) presets = info.presets;
  chooseVisibleGroup();
  if (info.service) {
    const relays = info.service.relays || [];
    const relayText = relays.length ? relays.map((item) => item.name + (item.up ? " 通" : " 断")).join(" ") : "未登记";
    conn = "在线 · 账户 " + (info.service.accounts || 0) + " · 窗口 " + (info.service.windows || 0) + " · 中继 " + relayText;
  }
  renderPresets();
  renderSide();
  for (const pane of panes.values()) layoutPane(pane);
  const live = new Set();
  for (const win of catalog) {
    const sessions = win.sessions || [];
    if (!sessions.length) {
      live.add("blank:" + win.id);
      ensureBlank(win);
    }
    for (const session of sessions) {
      live.add(session.id);
      ensurePane(win, session);
    }
  }
  dropMissing(live);
  if (info.error) showStatus(info.error);
}
setInterval(() => {
  for (const pane of panes.values()) {
    const rx = pane.rxBytes || 0;
    const tx = pane.txBytes || 0;
    pane.rxRate = Math.max(0, rx - (pane.rxMark || 0));
    pane.txRate = Math.max(0, tx - (pane.txMark || 0));
    pane.rxMark = rx;
    pane.txMark = tx;
    renderRate(pane);
  }
}, 1000);
async function discoverCode() {
  // Known room credentials come from the saved connection or explicit URL code.
  return true;
}
async function connectControl() {
  if (!token) return;
  if (control && (control.readyState === 0 || control.readyState === 1)) return;
  if (!(await discoverCode())) {
    setTimeout(connectControl, 2000);
    return;
  }
  if (!token) return;
  if (control && (control.readyState === 0 || control.readyState === 1)) return;
  control = new WebSocket(socketUrl());
  control.binaryType = "arraybuffer";
  control.onopen = () => {
    if (currentCode()) { control.send(JSON.stringify({op: "phone", code: currentCode(), token})); return; }
    conn = "已连接";
    showStatus();
    refreshBlankLinks();
    control.send(packJson(1, {token, channel: viewChannel}));
  };
  control.onclose = (ev) => {
    if (ev.code === 4001) {
      conn = "Token 不对";
      showStatus();
      token = "";
      sessionStorage.removeItem("sma-token");
      localStorage.removeItem("sma-token");
      document.getElementById("gate").classList.remove("hidden");
      return;
    }
    conn = "断开，2 秒后重连";
    showStatus();
    refreshBlankLinks();
    setTimeout(connectControl, 2000);
  };
  control.onmessage = (ev) => {
    if (typeof ev.data === "string") {
      const msg = JSON.parse(ev.data);
      if (msg.op === "ready") { conn = "中继已接通"; showStatus(); control.send(packJson(1, {token, channel: viewChannel})); }
      return;
    }
    const [op, payload] = unpack(ev.data);
    if (op === 2 || op === 7) sync(JSON.parse(new TextDecoder().decode(payload)));
  };
}
function openShell(windowId, command) {
  if (!control || control.readyState !== 1) {
    conn = "还没连上";
    showStatus();
    return;
  }
  const picked = document.getElementById("shell");
  const shell = command || (picked && picked.value) || (presets[0] && presets[0].command) || "powershell";
  const group = viewGroup || "default";
  control.send(packJson(6, {window: windowId || "", shell, group}));
  status.textContent = "正在打开终端…";
}
document.getElementById("new-window").onclick = () => openShell("");
document.getElementById("open-settings").onclick = () => openSettings();
function relayRow(item) {
  const row = document.createElement("div");
  row.className = "preset";
  const name = document.createElement("input");
  name.placeholder = "名称";
  name.value = item.name || "";
  const url = document.createElement("input");
  url.placeholder = "wss://117.50.48.22/ws";
  url.value = item.url || "";
  const code = document.createElement("input");
  code.placeholder = "code";
  code.value = item.code || "";
  const del = document.createElement("button");
  del.type = "button";
  del.textContent = "删";
  del.onclick = () => row.remove();
  row.append(name, url, code, del);
  row._fields = {name, url, code, id: item.id || ""};
  return row;
}
function renderRelaySelect() {
  const select = document.getElementById("relay");
  if (!select) return;
  const sig = relayBook.map((item) => item.id + item.name + item.url).join("|") + "#" + relayPick;
  if (select.dataset.sig === sig) return;
  select.dataset.sig = sig;
  select.replaceChildren();
  const local = document.createElement("option");
  local.value = "";
  local.textContent = "本机";
  select.append(local);
  for (const item of relayBook) {
    const option = document.createElement("option");
    option.value = item.id;
    option.textContent = item.name || item.url;
    select.append(option);
  }
  select.value = relayBook.some((item) => item.id === relayPick) ? relayPick : "";
}
function reconnectSockets() {
  try { if (control) control.onclose = null; if (control) control.close(); } catch (err) {}
  for (const pane of panes.values()) {
    try { if (pane.ws) pane.ws.onclose = null; if (pane.ws) pane.ws.close(); } catch (err) {}
  }
  connectControl();
  for (const pane of panes.values()) if (pane.linked !== false && pane.sessionId) openPane(pane);
}
document.getElementById("relay").onchange = () => {
  relayPick = document.getElementById("relay").value;
  localStorage.setItem("sma-relay-pick", relayPick);
  viewChannel = "";
  document.getElementById("relay").dataset.sig = "";
  reconnectSockets();
};
document.getElementById("edit-relays").onclick = () => {
  const box = document.getElementById("relay-rows");
  box.replaceChildren();
  for (const item of relayBook) box.append(relayRow(item));
  if (!relayBook.length) box.append(relayRow({name: "", url: "", code: ""}));
  document.getElementById("relay-box").showModal();
};
document.getElementById("add-relay").onclick = () => {
  document.getElementById("relay-rows").append(relayRow({name: "", url: "", code: ""}));
};
document.getElementById("close-relays").onclick = () => document.getElementById("relay-box").close();
document.getElementById("save-relays").onclick = () => {
  const items = [];
  for (const row of document.querySelectorAll("#relay-rows .preset")) {
    const name = row._fields.name.value.trim();
    const url = relayBase(row._fields.url.value);
    const code = row._fields.code.value.trim();
    if (!url || !code) continue;
    items.push({id: row._fields.id || Date.now().toString(36) + items.length, name: name || url, url, code});
  }
  relayBook = items;
  localStorage.setItem("sma-relays", JSON.stringify(relayBook));
  if (!relayBook.some((item) => item.id === relayPick)) relayPick = "";
  sendGroup({action: "relays", relays: relayBook.map((item) => ({name: item.name, url: item.url, code: item.code}))});
  document.getElementById("relay").dataset.sig = "";
  renderRelaySelect();
  document.getElementById("relay-box").close();
};
document.getElementById("rename-title").onclick = () => {
  const pane = panes.get(activeId);
  const title = document.getElementById("win-title").value.trim();
  if (pane && title) sendGroup({action: "title", window: pane.windowId, title});
};
document.getElementById("auto-title").onclick = () => {
  const pane = panes.get(activeId);
  if (pane) sendGroup({action: "autotitle", window: pane.windowId});
};
rememberThisRelay();
renderRelaySelect();
function enterWith(value) {
  token = (value || "").trim();
  if (!token) return;
  sessionStorage.setItem(tokenKey(), token);

  document.getElementById("gate").classList.add("hidden");
  if (control) {
    const old = control;
    control = null;
    try { old.close(); } catch (err) {}
  }
  connectControl();
}
async function authCall(op) {
  const email = document.getElementById("auth-name").value.trim();
  const password = document.getElementById("auth-pass").value;
  if (!(await discoverCode()) && currentCode() === "" && location.hostname !== "127.0.0.1" && location.hostname !== "localhost") {
    document.getElementById("gate-msg").textContent = "中继还没有办公室主机";
    return;
  }
  const ws = new WebSocket(socketUrl().split("?")[0]);
  const body = {op, email, password};
  if (currentCode()) body.code = currentCode();
  ws.onopen = () => ws.send(JSON.stringify(body));
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.error) {
      document.getElementById("gate-msg").textContent = msg.error;
      return;
    }
    if (msg.token) {
      sessionStorage.setItem("sma-user", msg.email || email);
      enterWith(msg.token);
    }
  };
  ws.onclose = () => {};
}
document.getElementById("do-login").onclick = () => authCall("login");
document.getElementById("do-register").onclick = () => authCall("register");
document.getElementById("do-token").onclick = () => enterWith(document.getElementById("auth-token").value);
document.getElementById("copy-token").onclick = async () => {
  try { await navigator.clipboard.writeText(token); conn = "Token 已复制"; showStatus(); }
  catch (err) { conn = "复制失败"; showStatus(); }
};
document.getElementById("arrange").addEventListener("pointerdown", (ev) => ev.stopPropagation());
document.getElementById("arrange").onclick = (ev) => {
  ev.stopPropagation();
  const open = document.getElementById("menu");
  const same = open && open.dataset.kind === "arrange";
  closeMenu();
  if (same) return;
  const rect = ev.currentTarget.getBoundingClientRect();
  openMenu(rect.left, rect.bottom + 4, arrangeItems());
  const menu = document.getElementById("menu");
  if (menu) menu.dataset.kind = "arrange";
};
document.getElementById("close-window").onclick = () => {
  const pane = panes.get(activeId);
  if (pane && control && control.readyState === 1) control.send(packJson(9, {window: pane.windowId}));
};
document.getElementById("close-cli").onclick = () => {
  const pane = panes.get(activeId);
  if (pane && pane.sessionId && control && control.readyState === 1) control.send(packJson(9, {session: pane.sessionId}));
};
document.getElementById("close-settings").onclick = () => document.getElementById("settings").close();
document.addEventListener("pointerdown", (ev) => {
  if (ev.target.closest && ev.target.closest("#menu, #submenu, .more")) return;
  closeMenu();
});
document.getElementById("add-preset").onclick = () => {
  document.getElementById("preset-rows").append(presetRow({label: "", command: "", shortcut: ""}));
};
document.getElementById("save-identity").onclick = () => {
  const project = document.getElementById("project-name").value.trim();
  sendGroup({action: "identity", project});
};
document.getElementById("save-presets").onclick = () => {
  const items = [];
  for (const row of document.querySelectorAll("#preset-rows .preset")) {
    items.push({
      id: row._fields.id,
      label: row._fields.label.value,
      command: row._fields.command.value,
      shortcut: row._fields.shortcut.value
    });
  }
  sendGroup({action: "presets", presets: items});
  document.getElementById("settings").close();
};
window.addEventListener("keydown", (ev) => {
  if (ev.repeat) return;
  const el = ev.target;
  if (el && el.closest && el.closest("input, select, dialog") && !el.closest(".xterm")) return;
  if (!ev.ctrlKey || !ev.altKey || ev.shiftKey || ev.metaKey) return;
  const mode = {l: "cascade", h: "rows", v: "cols", g: "grid"}[(ev.key || "").toLowerCase()];
  if (!mode) return;
  const spec = shortcutOf(ev).toLowerCase();
  if (presets.some((item) => (item.shortcut || "").toLowerCase() === spec)) return;
  ev.preventDefault();
  ev.stopPropagation();
  arrangeWindows(mode);
}, true);
window.addEventListener("keydown", (ev) => {
  if (ev.target.closest && ev.target.closest("#settings")) return;
  if (!(ev.altKey || ev.metaKey)) return;
  const spec = shortcutOf(ev).toLowerCase();
  const preset = presets.find((item) => (item.shortcut || "").toLowerCase() === spec);
  if (!preset) return;
  ev.preventDefault();
  openShell("", preset.command);
});
applyChrome();
document.getElementById("side-toggle").onclick = () => {
  sideClosed = !sideClosed;
  localStorage.setItem("sma-side", sideClosed ? "1" : "0");
  applyChrome();
  refitPanes(isPhone() ? "chrome" : "box");
};
document.getElementById("tools-toggle").onclick = () => {
  if (toolsHiddenNow()) showTools();
  else hideTools();
};
const toolsBar = document.getElementById("tools");
toolsBar.addEventListener("pointerdown", bumpTools);
toolsBar.addEventListener("focusin", bumpTools);
toolsBar.addEventListener("input", bumpTools);
toolsBar.addEventListener("change", bumpTools);
document.addEventListener("pointerdown", (ev) => {
  if (ev.pointerType !== "mouse" || ev.button !== 0) return;
  const button = ev.target.closest && ev.target.closest("button");
  if (!button || button.closest(".xterm, .bar, #tools, #tools-toggle, #side, #dock, .menu, dialog, #gate")) return;
  ev.preventDefault();
}, true);
document.addEventListener("pointerup", (ev) => {
  const button = ev.target.closest && ev.target.closest("button");
  if (!button || button.closest(".xterm, dialog")) return;
  requestAnimationFrame(() => {
    if (!keepsKeys(document.activeElement)) focusActiveTerm();
  });
});
window.addEventListener("keydown", (ev) => {
  if (ev.defaultPrevented || keepsKeys(ev.target)) return;
  if (ev.isComposing || ev.keyCode === 229) {
    focusActiveTerm();
    return;
  }
  const data = keyToData(ev);
  if (!data) return;
  const pane = panes.get(activeId);
  if (!pane || !pane.term || pane.minimized) return;
  pane.term.focus();
  pane.term.input(data, true);
  ev.preventDefault();
});
grid.addEventListener("pointerdown", (ev) => {
  if (!ev.target.closest(".card, button, input, select, textarea")) {
    ev.preventDefault();
    focusActiveTerm();
  }
  if (!isPhone()) return;
  const hideBar = !toolsHiddenNow();
  const hideSide = !sideClosed;
  if (!hideBar && !hideSide) return;
  if (hideBar) phoneToolsOpen = false;
  if (hideSide) {
    sideClosed = true;
    localStorage.setItem("sma-side", "1");
  }
  clearTimeout(toolsTimer);
  applyChrome();
  refitPanes("chrome");
});
let edgeSwipe = null;
window.addEventListener("touchstart", (ev) => {
  if (!isPhone() || !ev.touches || !ev.touches.length) return;
  const touch = ev.touches[0];
  if (touch.clientY > 28) { edgeSwipe = null; return; }
  edgeSwipe = touch.clientY;
}, {passive: true});
window.addEventListener("touchend", (ev) => {
  if (edgeSwipe === null || !ev.changedTouches || !ev.changedTouches.length) return;
  const endY = ev.changedTouches[0].clientY;
  if (endY - edgeSwipe > 24) showTools();
  edgeSwipe = null;
}, {passive: true});
if (phoneMedia.addEventListener) phoneMedia.addEventListener("change", () => { applyChrome(); refitPanes("box"); });
else if (phoneMedia.addListener) phoneMedia.addListener(() => { applyChrome(); refitPanes("box"); });
if (window.visualViewport) {
  visualViewport.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => { applyChrome(); refitPanes("box"); }, 150);
  });
  visualViewport.addEventListener("scroll", () => { if (isPhone()) window.scrollTo(0, 0); });
}
renderSide();
if (token) document.getElementById("gate").classList.add("hidden");

function gridBox() {
  if (!grid) return {w: window.innerWidth, h: window.innerHeight};
  return {w: grid.clientWidth, h: grid.clientHeight};
}
let lastViewport = gridBox();
window.addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => {
    const next = gridBox();
    let reason = "chrome";
    if (!lastViewport || next.w !== lastViewport.w || next.h !== lastViewport.h) reason = "box";
    applyChrome();
    refitPanes(reason);
  }, 150);
});
// Sidebar, toolbar and host-window changes can resize the grid without a window event.
const gridObserver = new ResizeObserver(() => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => refitPanes("box"), 50);
});
gridObserver.observe(grid);
if (token) connectControl();
</script>
</body>
</html>
"""

STATIC = {
    "/agent-modes/codex.json": ("agent-modes/codex.json", "application/json; charset=utf-8"),
    "/agent-modes/claude-code.json": ("agent-modes/claude-code.json", "application/json; charset=utf-8"),
    "/agent-modes/grok.json": ("agent-modes/grok.json", "application/json; charset=utf-8"),
    "/agent-modes/kimi.json": ("agent-modes/kimi.json", "application/json; charset=utf-8"),
    "/agent-modes/pi-dev.json": ("agent-modes/pi-dev.json", "application/json; charset=utf-8"),
    "/agent-modes/cursor-cli.json": ("agent-modes/cursor-cli.json", "application/json; charset=utf-8"),
    "/tab-themes.js": ("tab-themes.js", "text/javascript; charset=utf-8"),
    "/tab-themes.css": ("tab-themes.css", "text/css; charset=utf-8"),
    "/workspace.js": ("workspace.js", "text/javascript; charset=utf-8"),
    "/workspace.css": ("workspace.css", "text/css; charset=utf-8"),
    "/terminal-client.js": ("terminal-client.js", "text/javascript; charset=utf-8"),
    "/xterm.js": ("xterm.js", "text/javascript; charset=utf-8"),
    "/xterm.css": ("xterm.css", "text/css; charset=utf-8"),
    "/xterm-fit.js": ("xterm-fit.js", "text/javascript; charset=utf-8"),
    "/xterm-webgl.js": ("xterm-webgl.js", "text/javascript; charset=utf-8"),
    "/xterm-unicode11.js": ("xterm-unicode11.js", "text/javascript; charset=utf-8"),
}


def page_content(path: str):
    if path in {"/", "/index.html", "/legacy", "/legacy/"}:
        return PAGE, "text/html; charset=utf-8"
    if path in {"/workspace", "/workspace/"}:
        from .workspace import PAGE as workspace_page
        return workspace_page, "text/html; charset=utf-8"
    if path in STATIC:
        name, content_type = STATIC[path]
        return static_text(name), content_type
    return None


def static_text(name: str) -> str:
    from pathlib import Path

    if name == 'workspace.js':
        from .workspace import javascript
        return javascript()
    return (Path(__file__).parent / "static" / name).read_text(encoding="utf-8")


def text_response(connection, status: int, text: str, content_type: str):
    response = connection.respond(status, text)
    del response.headers["Content-Type"]
    response.headers["Content-Type"] = content_type
    response.headers["Cache-Control"] = "no-store"
    return response
