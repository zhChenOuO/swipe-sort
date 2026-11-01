"use strict";

const $ = (id) => document.getElementById(id);
const stage = $("stage");
const PREVIEW = Math.max(screen.width, screen.height) * (devicePixelRatio || 1) > 1600 ? 2400 : 1600;

const state = {
  folder: load("folder", ""),
  muted: load("muted", "1") === "1",
  queue: [],
  after: "",
  exhausted: false,
  loading: null,
  busy: false,
  folders: [],
  history: [],
  expanded: new Set(JSON.parse(load("expanded", "[]") || "[]")),
  stats: {},
  mode: load("mode", "sort") === "browse" ? "browse" : "sort",
  // 瀏覽模式:可以前後翻,不做任何決定
  browse: { items: [], index: 0, after: "", exhausted: false, loading: null },
};
const browsing = () => state.mode === "browse";

function load(k, d) { try { return localStorage.getItem("swipe-sort:" + k) ?? d; } catch { return d; } }
function save(k, v) { try { localStorage.setItem("swipe-sort:" + k, v); } catch {} }

async function api(path, body) {
  const opts = body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  };
  const r = await fetch(path, opts);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
  return data;
}

const q = (p, extra = "") => `path=${encodeURIComponent(p)}${extra}`;
const videoUrl = (p) => `/api/video?${q(p)}`;
const NATIVE_VIDEO = /\.(mp4|m4v|mov|webm)$/i;
const previewUrl = (p, size, trash) => `/api/preview?${q(p, `&size=${size}${trash ? "&trash=1" : ""}`)}`;
const baseName = (p) => p.slice(p.lastIndexOf("/") + 1);
// 外層 rtl 讓過長的路徑從左邊省略,內層 ltr 保持字的順序
function setPath(el, text) {
  const s = document.createElement("span");
  s.className = "ltr";
  s.textContent = text;
  el.replaceChildren(s);
}

function fmtSize(n) {
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(i && n < 10 ? 1 : 0)} ${u[i]}`;
}
const fmtDate = (t) => new Date(t * 1000).toLocaleString("zh-TW", { hour12: false });
const fmtTime = (s) => isFinite(s) ? `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}` : "-:--";

let toastTimer;
function toast(msg) {
  const t = $("toast");
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, 2500);
}

// ---------- 資料夾與計數 ----------

async function loadFolders() {
  const { folders, stats } = await api("/api/folders");
  state.folders = folders;
  state.stats = stats;
  if (!folders.some((f) => f.path === state.folder) && !stats.scanning) state.folder = "";
  for (let p = parentOf(state.folder); p; p = parentOf(p)) state.expanded.add(p);
  updateCounts();
  if (stats.scanning) {
    toast("正在掃描資料夾，請稍候…");
    setTimeout(() => loadFolders().then(reload).catch(() => {}), 2000);
  }
}

let foldersTimer;
function refreshFoldersSoon() {
  clearTimeout(foldersTimer);
  foldersTimer = setTimeout(() => loadFolders().catch(() => {}), 1500);
}

function updateCounts() {
  const f = state.folders.find((x) => x.path === state.folder);
  setPath($("folder-name"), state.folder || "全部");
  $("count").textContent = f ? `剩 ${f.pending}` : "";
  $("trash-count").textContent = state.stats.trashed ?? 0;
  renderTree();
}

// ---------- 資料夾樹 ----------

const parentOf = (p) => (p.includes("/") ? p.slice(0, p.lastIndexOf("/")) : "");
const isMobile = () => matchMedia("(max-width: 799px)").matches;

function renderTree() {
  const kids = new Map();
  for (const f of state.folders) {
    if (!f.path) continue;
    const p = parentOf(f.path);
    if (!kids.has(p)) kids.set(p, []);
    kids.get(p).push(f);
  }
  const rows = [];
  const add = (f, depth) => {
    const children = kids.get(f.path) || [];
    const open = !f.path || state.expanded.has(f.path);
    const row = document.createElement("div");
    row.className = `node${f.path === state.folder ? " sel" : ""}${f.pending ? "" : " done"}`;
    row.style.paddingLeft = `${8 + depth * 16}px`;
    const caret = document.createElement("span");
    caret.className = "caret";
    caret.textContent = children.length && f.path ? (open ? "▾" : "▸") : "";
    if (children.length && f.path) {
      caret.onclick = (e) => { e.stopPropagation(); toggleExpand(f.path); };
    }
    const n = document.createElement("span");
    n.className = "n";
    n.textContent = f.path ? baseName(f.path) : "全部";
    n.title = f.path || "全部";
    const c = document.createElement("span");
    c.className = "c";
    c.textContent = `${f.pending}/${f.total}`;
    row.append(caret, n, c);
    row.onclick = () => selectFolder(f.path);
    rows.push(row);
    if (open) for (const k of children) add(k, depth + 1);
  };
  const root = state.folders.find((f) => !f.path);
  if (root) add(root, 0);
  $("tree").replaceChildren(...rows);
}

function toggleExpand(path) {
  state.expanded.has(path) ? state.expanded.delete(path) : state.expanded.add(path);
  save("expanded", JSON.stringify([...state.expanded]));
  renderTree();
}

function selectFolder(path) {
  for (let p = parentOf(path); p; p = parentOf(p)) state.expanded.add(p);
  save("expanded", JSON.stringify([...state.expanded]));
  if (isMobile()) document.body.classList.remove("menu-open");
  if (path === state.folder) return renderTree();
  state.folder = path;
  save("folder", path);
  updateCounts();
  reload();
}

if (load("menu", "open") === "closed") document.body.classList.add("menu-closed");
$("menu-btn").onclick = () => {
  if (isMobile()) return document.body.classList.toggle("menu-open");
  const closed = document.body.classList.toggle("menu-closed");
  save("menu", closed ? "closed" : "open");
};
$("backdrop").onclick = () => document.body.classList.remove("menu-open");

function bump(path, folderDelta, trashDelta) {
  for (const f of state.folders) {
    if (!f.path || path.startsWith(f.path + "/")) f.pending += folderDelta;
  }
  state.stats.trashed = (state.stats.trashed ?? 0) + trashDelta;
  updateCounts();
}

// ---------- 佇列 ----------

function resetQueue() {
  state.queue = [];
  state.after = "";
  state.exhausted = false;
  state.loading = null;
  render();
  return refill();
}

function refill() {
  if (state.loading || state.exhausted || state.queue.length >= 8) return state.loading;
  const folder = state.folder;
  state.loading = api(`/api/queue?folder=${encodeURIComponent(folder)}&after=${encodeURIComponent(state.after)}&limit=30`)
    .then(({ items }) => {
      if (folder !== state.folder) return;
      const have = new Set(state.queue.map((i) => i.path));
      state.queue.push(...items.filter((i) => !have.has(i.path)));
      if (items.length) state.after = items[items.length - 1].path;
      if (items.length < 30) state.exhausted = true;
      state.loading = null;
      render();
    })
    .catch((e) => { state.loading = null; toast(`讀取失敗：${e.message}`); });
  return state.loading;
}

function preload() {
  for (const it of state.queue.slice(1, 5)) {
    if (it.kind === "image" && !it._pre) { it._pre = new Image(); it._pre.src = previewUrl(it.path, PREVIEW); }
  }
}

// ---------- 卡片 ----------

let rendered = null;

function makeCard(item) {
  const card = document.createElement("div");
  card.className = "card";
  card.dataset.path = item.path;
  const fail = (msg) => {
    const d = document.createElement("div");
    d.className = "err";
    d.textContent = `${msg}\n${baseName(item.path)}`;
    card.querySelector("img,video")?.remove();
    card.prepend(d);
  };
  if (item.kind === "video") {
    const v = document.createElement("video");
    v.src = videoUrl(item.path);
    v.poster = previewUrl(item.path, PREVIEW);
    v.playsInline = true;
    v.loop = true;
    v.muted = state.muted;
    v.preload = "metadata";
    const k = document.createElement("span");
    k.className = "kind";
    const converting = !NATIVE_VIDEO.test(item.path);
    k.textContent = converting ? "影片 · 轉檔中…" : "影片 · 載入中…";
    v.onloadeddata = () => { k.textContent = converting ? "影片（預覽版）" : "影片"; };
    v.onerror = () => { k.remove(); fail("這個影片沒辦法播放，還是可以滑"); };
    card.append(v, k);
  } else {
    const img = document.createElement("img");
    img.src = previewUrl(item.path, PREVIEW);
    img.alt = "";
    img.draggable = false;
    img.onerror = () => fail("無法顯示這張圖片，還是可以滑");
    card.append(img);
  }
  for (const [cls, text] of [["keep", "保留"], ["trash", "刪除"], ["undo", "還原上一張"], ["move", "搬移"]]) {
    const b = document.createElement("div");
    b.className = `badge ${cls}`;
    b.textContent = text;
    card.append(b);
  }
  return card;
}

function topCard() { return stage.querySelector(".card:not(.back):not(.gone)"); }

// 已經建好的卡片(尤其是預先載好的鄰近幾張)直接沿用,換張時才不會閃
function placeCards(cur, neighbors) {
  if (rendered === (cur?.path ?? null)) return;
  const existing = new Map([...stage.querySelectorAll(".card:not(.gone)")].map((c) => [c.dataset.path, c]));
  const curEl = cur && (existing.get(cur.path) || makeCard(cur));
  const backEls = neighbors.filter(Boolean).map((it) => existing.get(it.path) || makeCard(it));
  for (const c of existing.values()) if (c !== curEl && !backEls.includes(c)) c.remove();
  for (const b of backEls) { b.classList.add("back"); b.querySelector("video")?.pause(); stage.prepend(b); }
  if (curEl) {
    curEl.classList.remove("back", "snap");
    curEl.style.transform = "";
    stage.insertBefore(curEl, stage.querySelector(".card.gone"));
    const v = curEl.querySelector("video");
    if (v) { v.muted = state.muted; v.play().catch(() => {}); }
  }
  rendered = cur?.path ?? null;
  bindVideo();
}

function render() {
  if (browsing()) return renderBrowse();
  const [cur, next] = state.queue;
  placeCards(cur, [next]);
  $("empty-msg").textContent = "這個資料夾已經整理完了 🎉";
  $("empty").hidden = !!cur || !!state.loading || !state.exhausted || !!state.stats.scanning;
  setPath($("info"), cur ? `${cur.path} · ${fmtSize(cur.size)} · ${fmtDate(cur.mtime)}` : "");
  for (const id of ["trash-action", "keep-action"]) $(id).disabled = !cur;
  renderStrip();
  preload();
  refill();
}

// 縮圖的 <img> 重複使用,搬位置時不會重新載入
const thumbImgs = new Map();
function thumbImg(src) {
  let img = thumbImgs.get(src);
  if (!img) {
    img = document.createElement("img");
    img.src = src;
    img.alt = "";
    img.draggable = false;
    thumbImgs.set(src, img);
    if (thumbImgs.size > 40) thumbImgs.delete(thumbImgs.keys().next().value);
  }
  return img;
}

function thumb(cls, src, fallback) {
  const d = document.createElement("div");
  d.className = `st ${cls}`;
  if (src) {
    const img = thumbImg(src);
    img.onerror = () => { img.remove(); d.prepend(fallback); };
    d.append(img);
  } else {
    d.append(fallback);
  }
  return d;
}

const itemThumb = (it, cls) => thumb(cls, previewUrl(it.path, 200), it.kind === "video" ? "🎬" : "🖼");

function renderStrip() {
  const strip = $("strip");
  const prev = state.history.at(-1);
  let p;
  if (!prev) {
    p = thumb("prev none", null, "");
  } else if (prev.batch) {
    p = thumb("prev", null, "");
    p.textContent = `${prev.decision === "trash" ? "✕" : "✓"}${prev.batch}`;
    p.style.color = prev.decision === "trash" ? "var(--bad)" : "var(--good)";
    p.style.fontSize = "13px";
  } else {
    const it = prev.item;
    // 保留的還在原位,刪除的在垃圾桶,搬移的在新位置
    const loc = prev.decision === "keep" ? it.path : prev.trash_path;
    const src = loc ? previewUrl(loc, 200, prev.decision === "trash") : null;
    p = thumb("prev", src, it.kind === "video" ? "🎬" : "🖼");
    const m = document.createElement("span");
    m.className = `mark ${prev.decision}`;
    m.textContent = { keep: "✓", trash: "✕", move: "➜" }[prev.decision];
    p.append(m);
  }
  if (prev) { p.title = "上一張（點一下還原）"; p.onclick = undo; }
  const rest = state.queue.slice(0, 5).map((it, i) => itemThumb(it, i ? "next" : "cur"));
  strip.replaceChildren(p, ...rest);
}

function bindVideo() {
  const v = topCard()?.querySelector("video");
  $("seek-row").hidden = !v;
  if (!v) return;
  $("mute-btn").textContent = v.muted ? "🔇" : "🔊";
  const upd = () => {
    if (!dragging) $("seek").value = v.duration ? (v.currentTime / v.duration) * 1000 : 0;
    $("time").textContent = `${fmtTime(v.currentTime)} / ${fmtTime(v.duration)}`;
  };
  v.ontimeupdate = upd;
  v.onloadedmetadata = upd;
  upd();
}

$("seek").oninput = (e) => {
  const v = topCard()?.querySelector("video");
  if (v?.duration) v.currentTime = (e.target.value / 1000) * v.duration;
};
$("mute-btn").onclick = () => {
  const v = topCard()?.querySelector("video");
  state.muted = !state.muted;
  save("muted", state.muted ? "1" : "0");
  if (v) { v.muted = state.muted; if (!state.muted) v.play().catch(() => {}); }
  $("mute-btn").textContent = state.muted ? "🔇" : "🔊";
};

// ---------- 決定 ----------

async function decide(decision, target) {
  const item = state.queue[0];
  const card = topCard();
  if (!item || state.busy) return;
  state.busy = true;
  if (card) {
    card.classList.add("fly", "gone");
    const dir = decision === "keep" ? 1 : -1;
    card.style.transform = decision === "move"
      ? "translate(0, 120%) scale(.6)"
      : `translate(${dir * 150}%, 0) rotate(${dir * 25}deg)`;
    card.style.opacity = "0";
    card.querySelector("video")?.pause();
    setTimeout(() => card.remove(), 260);
  }
  bump(item.path, -1, decision === "trash" ? 1 : 0);
  const entry = { item, decision };
  state.history.push(entry);
  state.queue.shift();
  render();
  state.busy = false;
  try {
    const r = await api("/api/decide", { path: item.path, decision, target });
    if (decision === "move") toast(`已搬到 ${parentOf(r.trash_path) || "根目錄"}`);
    entry.trash_path = r.trash_path;
    if (state.history.at(-1) === entry) renderStrip();
  } catch (e) {
    toast(`沒處理成功：${e.message}`);
    state.history = state.history.filter((h) => h !== entry);
    bump(item.path, 1, decision === "trash" ? -1 : 0);
    state.queue.unshift(item);
    render();
  }
  refreshFoldersSoon();
}

async function undo() {
  try {
    const { item } = await api("/api/undo", {});
    if (!item) return toast("沒有可以還原的動作");
    const top = state.history.at(-1);
    if (item.batch) {
      if (top?.batch) state.history.pop();
      const what = item.undone === "trash" ? "從垃圾桶救回" : "取消保留";
      toast(`已整批${what}（${item.batch} 個）${item.failed?.length ? `，${item.failed.length} 個失敗` : ""}`);
      await loadFolders();
      return resetQueue();
    }
    if (top?.item?.path === item.path) state.history.pop();
    const label = { trash: "已從垃圾桶救回", keep: "已取消保留", move: "已搬回原位" }[item.undone];
    const inFolder = !state.folder || item.path.startsWith(state.folder + "/");
    if (inFolder && item.kind) {
      state.queue = state.queue.filter((i) => i.path !== item.path);
      state.queue.unshift({ path: item.path, kind: item.kind, size: item.size, mtime: item.mtime });
      render();
    }
    toast(`${label}：${baseName(item.path)}${inFolder ? "" : "（在其他資料夾）"}`);
    await loadFolders();
  } catch (e) {
    toast(`還原失敗：${e.message}`);
  }
}

// ---------- 瀏覽模式 ----------

function reload() {
  rendered = null;
  stage.querySelectorAll(".card").forEach((c) => c.remove());
  return browsing() ? resetBrowse() : resetQueue();
}

function setModeLabel() {
  $("mode-btn").querySelector(".ic").textContent = browsing() ? "✋" : "👁";
  $("mode-btn").querySelector(".lbl").textContent = browsing() ? " 整理" : " 瀏覽";
}

function setMode(mode) {
  state.mode = mode;
  save("mode", mode);
  document.body.classList.toggle("browse", browsing());
  setModeLabel();
  $("mode-btn").title = browsing() ? "切回整理模式" : "切到純瀏覽模式（只看不處理）";
  reload();
}

function resetBrowse() {
  state.browse = { items: [], index: 0, after: "", exhausted: false, loading: null };
  render();
  return browseMore();
}

function browseMore() {
  const b = state.browse;
  if (b.loading || b.exhausted) return b.loading;
  const folder = state.folder;
  b.loading = api(`/api/list?folder=${encodeURIComponent(folder)}&after=${encodeURIComponent(b.after)}&limit=100`)
    .then(({ items }) => {
      if (folder !== state.folder || b !== state.browse) return;
      b.items.push(...items);
      if (items.length) b.after = items[items.length - 1].path;
      if (items.length < 100) b.exhausted = true;
      b.loading = null;
      render();
    })
    .catch((e) => { b.loading = null; toast(`讀取失敗：${e.message}`); });
  return b.loading;
}

function renderBrowse() {
  const b = state.browse;
  const cur = b.items[b.index];
  placeCards(cur, [b.items[b.index + 1], b.items[b.index - 1]]);
  $("empty-msg").textContent = "這個資料夾沒有圖片或影片";
  $("empty").hidden = !!cur || !!b.loading || !b.exhausted || !!state.stats.scanning;
  const total = b.exhausted ? b.items.length : `${b.items.length}+`;
  setPath($("info"), cur
    ? `${cur.path} · ${fmtSize(cur.size)} · ${fmtDate(cur.mtime)}${cur.decision === "keep" ? " · ✓ 已保留" : ""} · ${b.index + 1}/${total}`
    : "");
  $("prev-action").disabled = b.index <= 0;
  $("next-action").disabled = !cur || (b.index >= b.items.length - 1 && b.exhausted);
  const from = Math.max(0, b.index - 2);
  $("strip").replaceChildren(...b.items.slice(from, b.index + 4).map((it, k) => {
    const i = from + k;
    const t = itemThumb(it, i === b.index ? "cur" : "next");
    if (it.decision === "keep") {
      const m = document.createElement("span");
      m.className = "mark keep";
      m.textContent = "✓";
      t.append(m);
    }
    t.style.cursor = "pointer";
    t.onclick = () => browseGo(i - b.index);
    return t;
  }));
  for (const it of b.items.slice(b.index + 1, b.index + 4)) {
    if (it.kind === "image" && !it._pre) { it._pre = new Image(); it._pre.src = previewUrl(it.path, PREVIEW); }
  }
  if (b.index >= b.items.length - 10) browseMore();
}

function browseGo(delta) {
  const b = state.browse;
  const ni = b.index + delta;
  if (!delta || state.busy) return;
  if (ni < 0) return toast("已經是第一張");
  if (ni >= b.items.length) {
    if (!b.exhausted) { browseMore(); return toast("載入中…"); }
    return toast("已經是最後一張");
  }
  const card = topCard();
  if (card) {
    card.classList.add("fly", "gone");
    card.style.transform = `translate(${delta > 0 ? -110 : 110}%, 0)`;
    card.style.opacity = "0";
    card.querySelector("video")?.pause();
    setTimeout(() => card.remove(), 260);
  }
  b.index = ni;
  render();
}

// ---------- 拖曳 ----------

let drag = null;
let dragging = false;

stage.addEventListener("pointerdown", (e) => {
  const card = topCard();
  if (!card || state.busy || e.button > 0) return;
  drag = { card, x: e.clientX, y: e.clientY, t: performance.now(), dx: 0, dy: 0 };
  card.classList.remove("snap");
  stage.setPointerCapture(e.pointerId);
});

stage.addEventListener("pointermove", (e) => {
  if (!drag) return;
  drag.dx = e.clientX - drag.x;
  drag.dy = e.clientY - drag.y;
  const { dx, dy } = drag;
  if (Math.abs(dx) > 6 || Math.abs(dy) > 6) { dragging = true; stage.classList.add("dragging"); }
  if (browsing()) {
    drag.card.style.transform = `translate(${dx}px, 0)`;
    return;
  }
  const vert = isVertical(dx, dy);
  drag.card.style.transform = vert
    ? `translate(${dx * 0.3}px, ${dy * 0.6}px)`
    : `translate(${dx}px, ${dy * 0.3}px) rotate(${dx / 18}deg)`;
  const p = Math.min(Math.abs(dx) / (stage.clientWidth * 0.25), 1);
  const q = Math.min(Math.abs(dy) / VERTICAL_DY, 1);
  drag.card.querySelector(".badge.keep").style.opacity = !vert && dx > 0 ? p : 0;
  drag.card.querySelector(".badge.trash").style.opacity = !vert && dx < 0 ? p : 0;
  drag.card.querySelector(".badge.undo").style.opacity = vert && dy < 0 ? q : 0;
  drag.card.querySelector(".badge.move").style.opacity = vert && dy > 0 ? q : 0;
});

const VERTICAL_DY = 90;
// 垂直距離明顯大於水平距離,才算上滑(還原)或下滑(搬移)
const isVertical = (dx, dy) => Math.abs(dy) > 20 && Math.abs(dy) > Math.abs(dx) * 1.2;

function endDrag(e) {
  if (!drag) return;
  const { card, dx, dy, t } = drag;
  stage.classList.remove("dragging");
  const v = Math.abs(dx) / Math.max(performance.now() - t, 1);
  drag = null;
  const vert = isVertical(dx, dy);
  const swiped = !vert && (Math.abs(dx) > stage.clientWidth * 0.25 || (Math.abs(dx) > 40 && v > 0.6));
  if (browsing()) {
    // 跟一般相簿一樣:往左滑看下一張;垂直拖動沒有作用,照常彈回原位
    if (swiped) {
      dragging = false;
      card.classList.add("snap");
      card.style.transform = "";
      return browseGo(dx < 0 ? 1 : -1);
    }
  } else if (vert && Math.abs(dy) > VERTICAL_DY) {
    dragging = false;
    card.classList.add("snap");
    card.style.transform = "";
    card.querySelectorAll(".badge").forEach((b) => { b.style.opacity = 0; });
    return dy < 0 ? undo() : openMove();
  } else if (swiped) {
    dragging = false;
    return decide(dx > 0 ? "keep" : "trash");
  }
  card.classList.add("snap");
  card.style.transform = "";
  card.querySelectorAll(".badge").forEach((b) => { b.style.opacity = 0; });
  if (!dragging && e.type === "pointerup") {
    const video = card.querySelector("video");
    if (video) video.paused ? video.play().catch(() => {}) : video.pause();
  }
  dragging = false;
}
stage.addEventListener("pointerup", endDrag);
stage.addEventListener("pointercancel", endDrag);

// ---------- 垃圾桶 ----------

async function showTrash() {
  $("sort-view").hidden = true;
  $("trash-view").hidden = false;
  topCard()?.querySelector("video")?.pause();
  await loadTrash();
}

async function loadTrash() {
  const { items, stats } = await api("/api/trash");
  state.stats = stats;
  updateCounts();
  $("trash-summary").textContent = `${items.length} 個 · ${fmtSize(stats.trash_bytes)}`;
  $("purge-btn").disabled = $("restore-all-btn").disabled = !items.length;
  $("grid").replaceChildren(...items.map((it) => {
    const tile = document.createElement("div");
    tile.className = "tile";
    const thumb = document.createElement("div");
    thumb.className = "thumb";
    const img = document.createElement("img");
    img.loading = "lazy";
    img.src = previewUrl(it.trash_path, 400, true);
    img.onerror = () => { img.replaceWith(it.kind === "video" ? "🎬" : "🖼"); };
    thumb.append(img);
    const name = document.createElement("div");
    name.className = "name";
    setPath(name, it.path);
    name.title = it.path;
    const btn = document.createElement("button");
    btn.textContent = "還原";
    btn.onclick = async () => {
      btn.disabled = true;
      try {
        await api("/api/restore", { path: it.path });
        tile.remove();
        await loadTrash();
      } catch (e) { toast(e.message); btn.disabled = false; }
    };
    tile.append(thumb, name, btn);
    return tile;
  }));
}

$("trash-btn").onclick = () => showTrash().catch((e) => toast(e.message));
$("back-btn").onclick = async () => {
  $("trash-view").hidden = true;
  $("sort-view").hidden = false;
  await loadFolders();
  await reload();
};
$("restore-all-btn").onclick = async () => {
  if (!confirm("把垃圾桶裡的檔案全部搬回原位？")) return;
  try {
    const r = await api("/api/restore", { all: true });
    toast(`還原了 ${r.restored} 個${r.failed.length ? `，${r.failed.length} 個失敗` : ""}`);
  } catch (e) { toast(e.message); }
  await loadTrash();
};
$("purge-btn").onclick = async () => {
  const n = state.stats.trashed ?? 0;
  if (!confirm(`永久刪除垃圾桶裡的 ${n} 個檔案（${fmtSize(state.stats.trash_bytes ?? 0)}）？\n這一步無法還原。`)) return;
  try {
    const r = await api("/api/purge", { confirm: "DELETE" });
    toast(`刪除了 ${r.deleted} 個，釋放 ${fmtSize(r.freed)}${r.failed.length ? `，${r.failed.length} 個失敗` : ""}`);
  } catch (e) { toast(e.message); }
  await loadTrash();
};

// ---------- 其他控制 ----------

$("rescan-btn").onclick = async () => {
  toast("掃描中…");
  try {
    const r = await api("/api/rescan", {});
    toast(`掃描完成，共 ${r.total} 個檔案`);
    await loadFolders();
    await reload();
  } catch (e) { toast(e.message); }
};
$("reset-kept-btn").onclick = async () => {
  try {
    const r = await api("/api/reset-kept", { folder: state.folder });
    toast(`重新列出 ${r.reset} 個`);
    await loadFolders();
    await resetQueue();
  } catch (e) { toast(e.message); }
};
async function decideAll(decision) {
  const f = state.folders.find((x) => x.path === state.folder);
  if (!f?.pending) return toast("這個資料夾已經沒有待處理的檔案");
  const name = state.folder || "全部";
  const what = decision === "keep" ? "全部標成保留" : "全部移到垃圾桶";
  if (!confirm(`把「${name}」剩下的 ${f.pending} 個檔案（含子資料夾）${what}？\n之後可以用「還原上一步」整批撤回。`)) return;
  toast("處理中…");
  try {
    const r = await api("/api/decide-all", { folder: state.folder, decision });
    if (r.count) state.history.push({ batch: r.count, decision });
    toast(`已${decision === "keep" ? "保留" : "移到垃圾桶"} ${r.count} 個${r.failed.length ? `，${r.failed.length} 個失敗` : ""}`);
    await loadFolders();
    await resetQueue();
  } catch (e) { toast(e.message); }
}
$("keep-all-btn").onclick = () => decideAll("keep");
$("trash-all-btn").onclick = () => decideAll("trash");
$("trash-action").onclick = () => decide("trash");
$("keep-action").onclick = () => decide("keep");
$("undo-action").onclick = undo;
$("move-action").onclick = () => openMove();
$("prev-action").onclick = () => browseGo(-1);
$("next-action").onclick = () => browseGo(1);
$("mode-btn").onclick = () => setMode(browsing() ? "sort" : "browse");

// ---------- 搬移 ----------

const recentTargets = () => { try { return JSON.parse(load("recent", "[]")) || []; } catch { return []; } };

function openMove() {
  if (browsing() || !state.queue[0] || state.busy) return;
  $("move-filter").value = "";
  renderMoveList();
  $("move-dialog").showModal();
  if (!isMobile()) $("move-filter").focus();
}

function renderMoveList() {
  const item = state.queue[0];
  if (!item) return;
  const here = parentOf(item.path);
  const q = $("move-filter").value.trim().replace(/^\/+|\/+$/g, "");
  const ql = q.toLowerCase();
  const all = state.folders.map((f) => f.path).filter((p) => p !== here);
  const match = (p) => !ql || p.toLowerCase().includes(ql);
  const opt = (path, label, cls = "") => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = `move-opt ${cls}`;
    setPath(b, label ?? (path || "（根目錄）"));
    b.onclick = () => moveTo(path);
    return b;
  };
  const head = (t) => { const d = document.createElement("div"); d.className = "move-head"; d.textContent = t; return d; };
  const rows = [];
  if (q && q !== here && !all.includes(q)) rows.push(opt(q, `＋ 新增資料夾並搬過去：${q}`, "new"));
  const recent = recentTargets().filter((p) => p !== here && match(p));
  if (recent.length) rows.push(head("最近用過"), ...recent.map((p) => opt(p)));
  const rest = all.filter(match);
  if (rest.length) rows.push(head("所有資料夾"), ...rest.slice(0, 300).map((p) => opt(p)));
  if (!rows.length) rows.push(head("沒有符合的資料夾"));
  $("move-list").replaceChildren(...rows);
}

function moveTo(path) {
  $("move-dialog").close();
  save("recent", JSON.stringify([path, ...recentTargets().filter((p) => p !== path)].slice(0, 8)));
  decide("move", path);
}

$("move-filter").oninput = renderMoveList;
$("move-filter").onkeydown = (e) => {
  if (e.key === "Enter") { e.preventDefault(); $("move-list").querySelector(".move-opt")?.click(); }
};
$("move-cancel").onclick = () => $("move-dialog").close();
$("move-dialog").onclick = (e) => { if (e.target === $("move-dialog")) $("move-dialog").close(); };

document.addEventListener("keydown", (e) => {
  if (!$("trash-view").hidden || $("move-dialog").open || e.metaKey || e.ctrlKey) return;
  if (browsing()) {
    if (e.key === "ArrowLeft" || e.key === "ArrowUp") { e.preventDefault(); browseGo(-1); }
    else if (e.key === "ArrowRight" || e.key === "ArrowDown") { e.preventDefault(); browseGo(1); }
    else if (e.key === " ") {
      const v = topCard()?.querySelector("video");
      if (v) { e.preventDefault(); v.paused ? v.play().catch(() => {}) : v.pause(); }
    }
    return;
  }
  if (e.key === "ArrowLeft") { e.preventDefault(); decide("trash"); }
  else if (e.key === "ArrowRight") { e.preventDefault(); decide("keep"); }
  else if (e.key === "ArrowUp" || e.key === "u" || e.key === "U" || e.key === "Backspace") { e.preventDefault(); undo(); }
  else if (e.key === "ArrowDown" || e.key === "m" || e.key === "M") { e.preventDefault(); openMove(); }
  else if (e.key === " ") {
    const v = topCard()?.querySelector("video");
    if (v) { e.preventDefault(); v.paused ? v.play().catch(() => {}) : v.pause(); }
  }
});

// iOS Safari 不理會 user-scalable=no,兩指縮放要在這裡擋
document.addEventListener("gesturestart", (e) => e.preventDefault());

document.body.classList.toggle("browse", browsing());
setModeLabel();
loadFolders().then(reload).catch((e) => toast(`連不上伺服器：${e.message}`));
