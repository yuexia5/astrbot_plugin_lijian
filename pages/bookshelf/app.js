/* 璃笺书架 —— 页面逻辑 */

const bridge = window.AstrBotPluginPage;
const $ = (id) => document.getElementById(id);

const state = {
  notes: [],
  tab: "all",
  q: "",
  opening: false,
  current: null,
  editing: false,
  timers: [],
  template: "",
  apiBase: "",
  signature: "",
  pollTimer: null,
  pendingRefresh: false,
};

const PALETTES = [
  ["#7d3b2c", "#5c2a1f"],
  ["#2f5d4c", "#1f4436"],
  ["#37506b", "#273c53"],
  ["#6b5a30", "#4c4120"],
  ["#3d6b5e", "#2b4f44"],
  ["#5b3a6b", "#3f2a4d"],
  ["#7a4a2b", "#57331c"],
  ["#4c5a6a", "#37424f"],
  ["#8a6a3c", "#634c28"],
  ["#405a8c", "#2d4370"],
  ["#5c6b4a", "#414f34"],
  ["#7d4a4a", "#5c3535"],
];

/* ---------------- 工具 ---------------- */

function escapeHtml(value) {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function hashStr(value) {
  let hash = 0;
  for (const ch of value) hash = (hash * 31 + ch.charCodeAt(0)) | 0;
  return Math.abs(hash);
}

function wordCount(value) {
  const cjk = (value.match(/[\u4e00-\u9fff]/g) || []).length;
  const words = (value.replace(/[\u4e00-\u9fff]/g, " ").match(/[A-Za-z0-9_]+/g) || []).length;
  return cjk + words;
}

function isoToday() {
  const date = new Date();
  const pad = (n) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

function fmtTime(iso) {
  if (!iso) return "";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  const pad = (n) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function noteDate(note) {
  if (note.diary_date) return note.diary_date;
  // created_at 是 UTC，直接截前 10 位在东八区凌晨会显示成前一天。
  const date = new Date(note.created_at || "");
  if (Number.isNaN(date.getTime())) return (note.created_at || "").slice(0, 10);
  const pad = (n) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

function splitTagList(value) {
  return String(value || "")
    .split(/[,，、;；]/)
    .map((tag) => tag.trim())
    .filter(Boolean);
}

function sourceLabel(note) {
  if (note.source === "astrbot") return "AstrBot 写入";
  if (note.source === "import") return "导入";
  if (note.source === "quick") return "快捷便签";
  if (note.source === "page") return "书架";
  return "本地";
}

function wait(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function reduceMotion() {
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

let toastTimer = null;
function toast(message) {
  const el = $("toast");
  el.textContent = message;
  el.hidden = false;
  requestAnimationFrame(() => el.classList.add("show"));
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    el.classList.remove("show");
    setTimeout(() => (el.hidden = true), 300);
  }, 2200);
}

async function downloadBlob(filename, content, mime) {
  try {
    const blob = new Blob([content], { type: mime });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 4000);
  } catch (error) {
    toast("导出失败：" + error.message);
  }
}

/* ---------------- 书 ---------------- */

function bookStyle(note) {
  const hash = hashStr(note.id + note.title);
  const [c1, c2] = PALETTES[hash % PALETTES.length];
  const w = note.kind === "diary" ? 26 + (hash % 5) * 4 : 22 + (hash % 4) * 4;
  const hgt = note.kind === "diary" ? 158 + (hash % 5) * 9 : 128 + (hash % 4) * 10;
  const base = Math.max(11, Math.round(w * 0.42));
  const len = Math.max(1, (note.title || "").length);
  const fit = Math.max(6, Math.floor((hgt - 22) / len));
  return { c1, c2, w, hgt, fs: Math.min(base, fit) };
}

function bookEl(note) {
  const style = bookStyle(note);
  const el = document.createElement("button");
  el.className = "book";
  el.dataset.id = note.id;
  el.title = note.title + (noteDate(note) ? "\n" + noteDate(note) : "");
  el.style.setProperty("--w", style.w + "px");
  el.style.setProperty("--h", style.hgt + "px");
  el.style.setProperty("--c1", style.c1);
  el.style.setProperty("--c2", style.c2);
  el.style.setProperty("--fs", style.fs + "px");
  const decorative = note.kind === "note" ? '<span class="book-mark">笺</span>' : "";
  el.innerHTML = `
    <span class="book-band top"></span>
    <span class="book-filigree"></span>
    <span class="book-title">${escapeHtml(note.title || "未命名")}</span>
    ${decorative}
    <span class="book-band bottom"></span>
    <span class="book-pages"></span>
    ${note.pinned ? '<span class="book-ribbon"></span>' : ""}
    ${note.source === "astrbot" ? '<span class="book-ai">✦</span>' : ""}
  `;
  el.addEventListener("click", () => openBook(el, note));
  return el;
}

function findBookEl(noteId) {
  return document.querySelector(`.book[data-id="${CSS.escape(noteId)}"]`);
}

/* ---------------- 书架渲染 ---------------- */

function chunk(items, size) {
  const rows = [];
  for (let index = 0; index < items.length; index += size) rows.push(items.slice(index, index + size));
  return rows;
}

function shelfRow(label, books) {
  const section = document.createElement("section");
  section.className = "shelf-row";
  if (label) {
    const plaque = document.createElement("div");
    plaque.className = "shelf-label";
    plaque.textContent = label;
    section.appendChild(plaque);
  }
  const board = document.createElement("div");
  board.className = "shelf-board";
  const container = document.createElement("div");
  container.className = "books";
  books.forEach((note) => container.appendChild(bookEl(note)));
  section.appendChild(container);
  section.appendChild(board);
  return section;
}

function zone(title, rows) {
  const wrapper = document.createElement("div");
  wrapper.className = "shelf-zone";
  const heading = document.createElement("h2");
  heading.className = "zone-title";
  heading.textContent = title;
  wrapper.appendChild(heading);
  rows.forEach(([label, books]) => wrapper.appendChild(shelfRow(label, books)));
  return wrapper;
}

function renderShelf() {
  const shelves = $("shelves");
  shelves.innerHTML = "";
  const empty = $("empty");

  // 搜索已经下沉到后端，这里只按标签页筛一遍，保证切换 tab 不用重新请求。
  const visible = state.notes.filter((note) => {
    if (state.tab === "archived") return note.archived;
    if (note.archived) return false;
    if (state.tab === "diary" && note.kind !== "diary") return false;
    if (state.tab === "note" && note.kind !== "note") return false;
    return true;
  });

  const searched = visible;

  const byDate = (a, b) => {
    if (Boolean(b.pinned) !== Boolean(a.pinned)) return Number(b.pinned) - Number(a.pinned);
    return noteDate(b).localeCompare(noteDate(a)) || b.updated_at.localeCompare(a.updated_at);
  };
  searched.sort(byDate);

  if (searched.length === 0) {
    shelves.hidden = true;
    empty.hidden = false;
    $("emptyText").textContent =
      state.q.trim()
        ? "没有找到匹配的书"
        : state.tab === "archived"
          ? "归档箱里空空如也"
          : state.tab === "diary"
            ? "还没有日记，从今天开始吧"
            : state.tab === "note"
              ? "还没有便签"
              : "书架上空空如也";
    $("emptyNew").hidden = state.tab === "archived";
    return;
  }

  empty.hidden = true;
  shelves.hidden = false;

  if (state.tab === "archived") {
    const rows = chunk(searched, 12).map((books) => [null, books]);
    shelves.appendChild(zone("旧档书橱", rows));
    return;
  }

  if (state.tab !== "note") {
    const diaries = searched.filter((note) => note.kind === "diary");
    const byYear = new Map();
    diaries.forEach((note) => {
      const year = (noteDate(note) || "").slice(0, 4) || "未定年";
      if (!byYear.has(year)) byYear.set(year, []);
      byYear.get(year).push(note);
    });
    const rows = [];
    [...byYear.keys()]
      .sort((a, b) => b.localeCompare(a))
      .forEach((year) => chunk(byYear.get(year), 12).forEach((books) => rows.push([year, books])));
    if (rows.length) shelves.appendChild(zone("时光书橱", rows));
  }

  if (state.tab !== "diary") {
    const notes = searched.filter((note) => note.kind === "note");
    if (notes.length) {
      const rows = chunk(notes, 12).map((books) => [null, books]);
      shelves.appendChild(zone("便签文集", rows));
    }
  }
}

async function fetchNotes(q) {
  // light=1：只要摘要。正文在打开书的时候单独拉，列表页不必背着整库正文。
  const params = { include_archived: "1", light: "1" };
  const keyword = (q || "").trim();
  if (keyword) params.q = keyword;
  return await bridge.apiGet("notes", params);
}

async function reload() {
  try {
    const result = await fetchNotes(state.q);
    state.notes = Array.isArray(result.notes) ? result.notes : [];
  } catch (error) {
    toast("读取书架失败：" + error.message);
    state.notes = [];
  }
  renderShelf();
  updateStats();
}

async function updateStats() {
  try {
    const stats = await bridge.apiGet("stats");
    $("statsText").textContent = `${stats.notes || 0} 册 · ${stats.words || 0} 字`;
  } catch {
    $("statsText").textContent = "";
  }
}

/* ---------------- 取出与打开动画 ---------------- */

function prepareBook3d(note) {
  const style = bookStyle(note);
  const book = $("book3d");
  book.style.setProperty("--c1", style.c1);
  book.style.setProperty("--c2", style.c2);
  const title = note.title || "未命名";
  $("book3d").querySelectorAll(".cover-title").forEach((el) => el.remove());
  document.querySelectorAll(".cover-left, .cover-right").forEach((cover) => {
    const span = document.createElement("span");
    span.className = "cover-title";
    span.textContent = title;
    cover.appendChild(span);
  });

  const clean = String(note.body_md || "").replace(/^---[\s\S]*?---/, "").trim();
  const text = clean.replace(/[#>*_`~\-[\]()|]/g, "").replace(/\s+/g, " ").trim();
  const first = text.slice(0, 120);
  const rest = text.slice(120, 320);
  $("coverLeft").innerHTML = `
    <div class="content-page-title">${escapeHtml(title)}</div>
    <div class="content-page-text">${escapeHtml(first || "……")}</div>
  `;
  $("coverRight").innerHTML = `
    <div class="content-page-text">${escapeHtml(rest || "……")}</div>
  `;
}

async function openBook(el, note) {
  if (state.opening) return;
  state.opening = true;
  state.editing = false;
  try {
    const result = await bridge.apiGet("notes/" + note.id);
    state.current = result.note || note;
  } catch (error) {
    toast("读取失败：" + error.message);
    state.opening = false;
    return;
  }
  prepareBook3d(state.current);
  renderRead();

  $("book3d").classList.remove("is-open", "fade");
  $("reader").classList.remove("is-open", "show-content");

  if (el && !reduceMotion()) {
    const rect = el.getBoundingClientRect();
    const tx = window.innerWidth / 2 - (rect.left + rect.width / 2);
    const ty = window.innerHeight / 2 - (rect.top + rect.height / 2);
    const scale = Math.max(2.2, Math.min(window.innerWidth, window.innerHeight) / (Math.max(rect.width, 1) * 3));
    el.style.setProperty("--tx", tx + "px");
    el.style.setProperty("--ty", ty + "px");
    el.style.setProperty("--s", scale);
    document.body.classList.add("is-opening");
    el.classList.add("is-out");
    await wait(500);
    el.classList.add("is-gone");
    await wait(260);
  } else {
    document.body.classList.add("is-opening");
    await wait(80);
  }

  $("reader").hidden = false;
  requestAnimationFrame(() => $("reader").classList.add("is-open"));
  await wait(120);
  if (!reduceMotion()) $("book3d").classList.add("is-open");
  await wait(1080);
  $("reader").classList.add("show-content");
  $("book3d").classList.add("fade");
  await wait(520);
  state.opening = false;
}

async function closeBook() {
  if (state.opening) return;
  state.opening = true;
  state.editing = false;
  const noteId = state.current ? state.current.id : null;

  $("reader").classList.remove("show-content");
  $("book3d").classList.remove("fade");
  if (!reduceMotion()) $("book3d").classList.remove("is-open");
  await wait(280);
  $("reader").classList.remove("is-open");
  await wait(320);
  $("reader").hidden = true;
  document.body.classList.remove("is-opening");
  state.current = null;

  if (noteId) {
    const el = findBookEl(noteId);
    if (el) {
      el.classList.remove("is-gone");
      await wait(50);
      el.classList.remove("is-out");
    }
  }
  await wait(560);
  state.opening = false;
  // 看书期间 AI 写过东西的话，合上书再刷新一次
  if (state.pendingRefresh) {
    state.pendingRefresh = false;
    await reload();
  }
}

/* ---------------- 阅读 / 编辑 ---------------- */

function metaHtml(note) {
  const kind = note.kind === "diary" ? "日记" : "便签";
  const tags = (note.tags || [])
    .map((tag) => `<span class="chip">#${escapeHtml(tag)}</span>`)
    .join("");
  const pinned = note.pinned ? `<span class="chip">已置顶</span>` : "";
  const archived = note.archived ? `<span class="chip">已归档</span>` : "";
  return `
    <span class="chip kind">${kind}</span>
    <span>${escapeHtml(noteDate(note))}</span>
    ${pinned}${archived}
    <span>来源：${escapeHtml(sourceLabel(note))}</span>
    ${tags}
  `;
}

function renderRead() {
  const note = state.current;
  if (!note) return;
  $("readerTitle").value = note.title;
  $("readerMeta").innerHTML = metaHtml(note);
  $("readerBody").innerHTML = renderMarkdown(note.body_md);
  $("readerFooter").innerHTML = `
    <span>${wordCount(note.body_md)} 字</span>
    <span>${fmtTime(note.updated_at)}</span>
  `;
  $("editBtn").textContent = "编辑";
  $("editorPanel").hidden = true;
  $("readerBody").hidden = false;
  $("readerFooter").hidden = false;
  $("readerTitle").readOnly = true;

  const isArchived = Boolean(note.archived);
  $("archiveBtn").textContent = isArchived ? "恢复" : "归档";
  $("archiveBtn").classList.toggle("danger", !isArchived);
  $("pinBtn").textContent = note.pinned ? "取消置顶" : "置顶";
  $("pinBtn").classList.toggle("is-active", Boolean(note.pinned));
}

function enterEdit() {
  const note = state.current;
  if (!note) return;
  state.editing = true;
  $("editorTags").value = (note.tags || []).join(", ");
  $("editorDate").value = note.diary_date || "";
  $("editorBody").value = note.body_md;
  $("readerTitle").readOnly = false;
  $("readerTitle").focus();
  $("readerBody").hidden = true;
  $("readerFooter").hidden = true;
  $("editorPanel").hidden = false;
  $("editBtn").textContent = "阅读";
}

async function saveEdit() {
  const note = state.current;
  if (!note) return;
  const title = $("readerTitle").value.trim() || "未命名";
  const tags = splitTagList($("editorTags").value);
  const diaryDate = $("editorDate").value || null;
  try {
    const result = await bridge.apiPost("notes/" + note.id, {
      title,
      body_md: $("editorBody").value,
      tags,
      diary_date: diaryDate,
    });
    state.current = result.note || { ...note, title, body_md: $("editorBody").value, tags, diary_date: diaryDate };
    renderRead();
    toast("已保存");
    await reload();
  } catch (error) {
    toast("保存失败：" + error.message);
  }
}

async function archiveCurrent() {
  const note = state.current;
  if (!note) return;
  try {
    if (note.archived) {
      await bridge.apiPost("notes/" + note.id + "/restore", {});
      toast("已放回书架");
    } else {
      await bridge.apiPost("notes/" + note.id + "/archive", {});
      toast("已移入归档");
    }
  } catch (error) {
    toast("操作失败：" + error.message);
    return;
  }
  await reload();
  await closeBook();
}

async function exportCurrent() {
  const note = state.current;
  if (!note) return;
  try {
    const result = await bridge.apiGet("notes/" + note.id + "/markdown");
    await downloadBlob(result.filename || "note.md", result.content, "text/markdown");
    toast("已导出《" + note.title + "》");
  } catch (error) {
    toast("导出失败：" + error.message);
  }
}

async function togglePin() {
  const note = state.current;
  if (!note) return;
  const action = note.pinned ? "unpin" : "pin";
  try {
    const result = await bridge.apiPost("notes/" + note.id + "/" + action, {});
    state.current = result.note || { ...note, pinned: !note.pinned };
    renderRead();
    toast(result.message || "已更新");
    await reload();
  } catch (error) {
    toast("操作失败：" + error.message);
  }
}

function applyTab() {
  document.querySelectorAll("#tabs button").forEach((el) => {
    el.classList.toggle("is-active", el.dataset.tab === state.tab);
  });
}

async function randomBook() {
  if (state.opening) return;
  const params = {};
  if (state.current) params.exclude = state.current.id;
  try {
    const result = await bridge.apiGet("random", params);
    const note = result.note;
    if (!note) {
      toast("书架上还没有可翻的书");
      return;
    }
    // 翻到的这本可能不在当前标签页里，先切到能看见它的地方
    if (state.tab === "archived" && !note.archived) state.tab = "all";
    else if (state.tab === "diary" && note.kind !== "diary") state.tab = "all";
    else if (state.tab === "note" && note.kind !== "note") state.tab = "all";
    applyTab();
    if (!state.notes.some((item) => item.id === note.id)) await reload();
    const el = findBookEl(note.id);
    await openBook(el, note);
  } catch (error) {
    toast("没翻到：" + error.message);
  }
}

/* ---------------- 图片 ---------------- */

function insertAtCursor(textarea, text) {
  const start = textarea.selectionStart ?? textarea.value.length;
  const end = textarea.selectionEnd ?? textarea.value.length;
  const before = textarea.value.slice(0, start);
  const after = textarea.value.slice(end);
  const prefix = before && !before.endsWith("\n") ? "\n" : "";
  textarea.value = before + prefix + text + after;
  const caret = (before + prefix + text).length;
  textarea.selectionStart = caret;
  textarea.selectionEnd = caret;
  textarea.focus();
}

async function insertImage(file) {
  if (!file) return;
  if (!/^image\/(png|jpeg|gif|webp)$/i.test(file.type)) {
    toast("只支持 png、jpg、gif、webp");
    return;
  }
  toast("正在上传…");
  try {
    const result = await bridge.upload("media", file);
    if (!result || !result.url) throw new Error("服务端没有返回图片地址");
    const alt = (file.name || "图片").replace(/\.[^.]+$/, "");
    insertAtCursor($("editorBody"), `![${alt}](${result.url})\n`);
    toast("图片已插入");
  } catch (error) {
    toast("上传失败：" + error.message);
  }
}

/* ---------------- 导出与备份 ---------------- */

async function exportZip() {
  try {
    await bridge.download("export/all", {}, "lijian-export.zip");
    toast("已导出全部日记");
  } catch (error) {
    toast("导出失败：" + error.message);
  }
}

async function exportJson() {
  try {
    const result = await bridge.apiGet("export/json");
    await downloadBlob(
      result.filename || "lijian-backup.json",
      result.content || "{}",
      "application/json"
    );
    toast(`已备份 ${result.count || 0} 册`);
  } catch (error) {
    toast("备份失败：" + error.message);
  }
}

async function restoreJson(file) {
  if (!file) return;
  let data;
  try {
    data = JSON.parse(await file.text());
  } catch {
    toast("这个文件不是合法 JSON");
    return;
  }
  const count = Array.isArray(data && data.notes) ? data.notes.length : 0;
  if (!count) {
    toast("备份里没有条目");
    return;
  }
  if (!window.confirm(`将导入 ${count} 条，同 id 的会被覆盖。继续？`)) return;
  try {
    const result = await bridge.apiPost("import/json", data);
    toast(result.message || "导入完成");
    await reload();
  } catch (error) {
    toast("恢复失败：" + error.message);
  }
}

async function copyWeekly() {
  try {
    const result = await bridge.apiGet("weekly");
    const text = result.digest_md || "";
    if (!text.trim()) {
      toast("本周还没有日记");
      return;
    }
    await navigator.clipboard.writeText(text);
    toast(`已复制本周素材（${result.count || 0} 篇）`);
  } catch (error) {
    toast("复制失败：" + error.message);
  }
}

/* ---------------- 自动刷新 ---------------- */

async function checkSignature() {
  // 页面在后台、正在新建/编辑、或者正开着书的时候都不打扰，
  // 等回到书架再一次性刷新。
  if (document.hidden) return;
  if (state.opening || state.editing) return;
  if (!$("reader").hidden) {
    state.pendingRefresh = true;
    return;
  }
  try {
    const result = await bridge.apiGet("signature");
    const next = result.signature || "";
    if (!state.signature) {
      state.signature = next;
      return;
    }
    if (next !== state.signature) {
      state.signature = next;
      await reload();
    }
  } catch {
    // 网络抖一下就跳过，下一轮再试
  }
}

function startWatcher() {
  stopWatcher();
  state.pollTimer = setInterval(checkSignature, 6000);
}

function stopWatcher() {
  if (state.pollTimer) {
    clearInterval(state.pollTimer);
    state.pollTimer = null;
  }
}

/* ---------------- 新建 ---------------- */

async function newDiary() {
  if (state.opening) return;
  const today = isoToday();
  // 模板里可以写 {date}，换成当天日期；模板为空就还是空白日记。
  const template = String(state.template || "").replace(/\{date\}/g, today);
  const bodyMd = "# 日记 " + today + "\n\n" + template;
  try {
    const result = await bridge.apiPost("notes", {
      title: "日记 " + today,
      body_md: bodyMd,
      kind: "diary",
      diary_date: today,
      tags: ["日记"],
      source: "page",
    });
    const note = result.note;
    await reload();
    state.tab = state.tab === "archived" ? "all" : state.tab;
    renderShelf();
    const el = findBookEl(note.id);
    if (el) {
      await openBook(el, note);
      enterEdit();
    } else {
      await openBook(null, note);
      enterEdit();
    }
  } catch (error) {
    toast("新建失败：" + error.message);
  }
}

/* ---------------- Markdown 渲染 ---------------- */

function safeLink(text, href) {
  const url = String(href || "").trim();
  if (/^(https?:|mailto:|#)/i.test(url)) {
    return `<a href="${escapeHtml(url)}" target="_blank" rel="noreferrer">${text}</a>`;
  }
  return text;
}

// 本地图片在正文里写成 media/<32位hex>.<ext>，只认这个形状，别的原样显示文字。
const MEDIA_REF = /^(?:\.\/)?media\/([0-9a-f]{32}\.(?:png|jpg|jpeg|gif|webp))$/i;

function guessApiBase() {
  const matched = location.pathname.match(/^(.*\/plugins\/extensions\/[^/]+)/);
  return matched ? matched[1] : "";
}

function mediaUrl(name) {
  // 优先用后端告诉我们的前缀；拿不到就从当前地址推；再不行退回固定路径。
  const base =
    state.apiBase ||
    guessApiBase() ||
    "/api/v1/plugins/extensions/astrbot_plugin_lijian";
  return base + "/media/" + name;
}

function safeImg(alt, src) {
  const url = String(src || "").trim();
  if (/^(https?:|data:image\/)/i.test(url)) {
    return `<img src="${escapeHtml(url)}" alt="${escapeHtml(alt || "")}" loading="lazy" />`;
  }
  const local = url.match(MEDIA_REF);
  if (local) {
    return `<img src="${escapeHtml(mediaUrl(local[1]))}" alt="${escapeHtml(alt || "")}" loading="lazy" />`;
  }
  return escapeHtml(alt || "图片");
}

function inline(text) {
  let out = escapeHtml(text);
  out = out.replace(/`([^`]+)`/g, "<code>$1</code>");
  out = out.replace(/!\[([^\]]*)\]\(([^)\s]+)(?:\s+"[^"]*")?\)/g, (match, alt, src) => safeImg(alt, src));
  out = out.replace(/\[([^\]]+)\]\(([^)\s]+)(?:\s+"[^"]*")?\)/g, (match, text2, href) => safeLink(text2, href));
  out = out.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  out = out.replace(/__([^_]+)__/g, "<strong>$1</strong>");
  out = out.replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");
  out = out.replace(/~~([^~]+)~~/g, "<del>$1</del>");
  return out;
}

function splitRow(line) {
  let cells = line.trim();
  if (cells.startsWith("|")) cells = cells.slice(1);
  if (cells.endsWith("|")) cells = cells.slice(0, -1);
  return cells.split("|").map((cell) => cell.trim());
}

function renderMarkdown(markdown) {
  const source = String(markdown || "").replace(/\r\n/g, "\n");
  const codeBlocks = [];
  const noCode = source.replace(/```([\w+-]*)\n([\s\S]*?)```/g, (match, lang, code) => {
    codeBlocks.push({ lang, code: code.replace(/\n$/, "") });
    return `\u0000CODE${codeBlocks.length - 1}\u0000`;
  });

  const lines = noCode.split("\n");
  const html = [];
  let paragraph = [];
  let listOpen = null;

  const flushParagraph = () => {
    if (paragraph.length) {
      html.push("<p>" + paragraph.map(inline).join("<br/>") + "</p>");
      paragraph = [];
    }
  };
  const closeList = () => {
    if (listOpen) {
      html.push("</" + listOpen + ">");
      listOpen = null;
    }
  };

  let index = 0;
  while (index < lines.length) {
    const line = lines[index];
    const trimmed = line.trim();

    if (!trimmed) {
      flushParagraph();
      closeList();
      index += 1;
      continue;
    }

    const codeMatch = trimmed.match(/^\u0000CODE(\d+)\u0000$/);
    if (codeMatch) {
      flushParagraph();
      closeList();
      const block = codeBlocks[Number(codeMatch[1])];
      html.push(
        `<pre class="code-block"><code>${escapeHtml(block.code)}</code></pre>`
      );
      index += 1;
      continue;
    }

    const headingMatch = trimmed.match(/^(#{1,6})\s+/);
    if (headingMatch) {
      flushParagraph();
      closeList();
      const level = headingMatch[1].length;
      html.push(`<h${level}>${inline(trimmed.slice(headingMatch[0].length))}</h${level}>`);
      index += 1;
      continue;
    }

    if (/^-{3,}\s*$/.test(trimmed)) {
      flushParagraph();
      closeList();
      html.push("<hr/>");
      index += 1;
      continue;
    }

    if (trimmed.startsWith(">")) {
      flushParagraph();
      closeList();
      const quote = [];
      while (index < lines.length && lines[index].trim().startsWith(">")) {
        quote.push("<p>" + inline(lines[index].trim().replace(/^>\s?/, "")) + "</p>");
        index += 1;
      }
      html.push("<blockquote>" + quote.join("") + "</blockquote>");
      continue;
    }

    const ulMatch = line.match(/^\s*[-*+]\s+/);
    if (ulMatch) {
      flushParagraph();
      if (listOpen !== "ul") {
        closeList();
        html.push("<ul>");
        listOpen = "ul";
      }
      html.push("<li>" + inline(line.slice(ulMatch[0].length)) + "</li>");
      index += 1;
      continue;
    }

    const olMatch = line.match(/^\s*\d+[.)]\s+/);
    if (olMatch) {
      flushParagraph();
      if (listOpen !== "ol") {
        closeList();
        html.push("<ol>");
        listOpen = "ol";
      }
      html.push("<li>" + inline(line.slice(olMatch[0].length)) + "</li>");
      index += 1;
      continue;
    }

    const nextTrimmed = lines[index + 1] ? lines[index + 1].trim() : "";
    if (trimmed.includes("|") && /^\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)+\|?\s*$/.test(nextTrimmed)) {
      flushParagraph();
      closeList();
      const headerCells = splitRow(trimmed);
      const alignCells = splitRow(nextTrimmed);
      const table = ["<table>", "<thead><tr>"];
      headerCells.forEach((cell, cellIndex) => {
        const align = alignCells[cellIndex] || "";
        const alignAttr = /^:.*:$/.test(align)
          ? ' style="text-align:center"'
          : /^:/.test(align)
            ? ' style="text-align:left"'
            : /:$/.test(align)
              ? ' style="text-align:right"'
              : "";
        table.push(`<th${alignAttr}>${inline(cell)}</th>`);
      });
      table.push("</tr></thead><tbody>");
      index += 2;
      while (index < lines.length && lines[index].trim().startsWith("|") && lines[index].trim().includes("|")) {
        table.push("<tr>");
        splitRow(lines[index]).forEach((cell) => table.push(`<td>${inline(cell)}</td>`));
        table.push("</tr>");
        index += 1;
      }
      table.push("</tbody></table>");
      html.push(table.join(""));
      continue;
    }

    paragraph.push(line);
    index += 1;
  }

  flushParagraph();
  closeList();
  return html.join("\n");
}

/* ---------------- 设置弹窗 ---------------- */

async function openSettings() {
  const modal = $("settingsModal");
  modal.hidden = false;
  try {
    const status = await bridge.apiGet("status");
    const bridgeInfo = status.bridge
      ? status.bridge.running
        ? `运行中 ${status.bridge.host}:${status.bridge.port}`
        : "未运行"
      : "未启用";
    $("statusList").innerHTML = `
      <div><dt>名称</dt><dd>${escapeHtml(status.name || "璃笺")} v${escapeHtml(status.version || "")}</dd></div>
      <div><dt>数据库</dt><dd>${escapeHtml(status.db_path || "")}</dd></div>
      <div><dt>写入接口</dt><dd>${bridgeInfo}</dd></div>
      <div><dt>条目</dt><dd>${status.stats ? status.stats.notes : "-"} 册</dd></div>
    `;
    $("tokenField").value = status.token || "";
  } catch (error) {
    $("statusList").innerHTML = `<div><dt>状态</dt><dd>${escapeHtml(error.message)}</dd></div>`;
  }
}

function closeSettings() {
  $("settingsModal").hidden = true;
}

/* ---------------- 事件绑定与初始化 ---------------- */

function bindEvents() {
  $("tabs").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-tab]");
    if (!button) return;
    state.tab = button.dataset.tab;
    applyTab();
    renderShelf();
  });

  let searchTimer = null;
  $("searchInput").addEventListener("input", (event) => {
    const value = event.target.value;
    clearTimeout(searchTimer);
    // 搜索交给后端做，中文两个字也能命中，不用把整库正文拉到页面里。
    searchTimer = setTimeout(async () => {
      state.q = value;
      await reload();
    }, 260);
  });

  $("newBtn").addEventListener("click", () => newDiary());
  $("emptyNew").addEventListener("click", () => newDiary());
  $("randomBtn").addEventListener("click", () => randomBook());

  $("backBtn").addEventListener("click", () => closeBook());
  $("editBtn").addEventListener("click", () => (state.editing ? renderRead() : enterEdit()));
  $("cancelBtn").addEventListener("click", () => renderRead());
  $("saveBtn").addEventListener("click", () => saveEdit());
  $("archiveBtn").addEventListener("click", () => archiveCurrent());
  $("exportOneBtn").addEventListener("click", () => exportCurrent());
  $("pinBtn").addEventListener("click", () => togglePin());

  $("insertImageBtn").addEventListener("click", () => $("imageInput").click());
  $("imageInput").addEventListener("change", async (event) => {
    const file = event.target.files && event.target.files[0];
    event.target.value = "";
    await insertImage(file);
  });
  $("jsonInput").addEventListener("change", async (event) => {
    const file = event.target.files && event.target.files[0];
    event.target.value = "";
    await restoreJson(file);
  });

  $("exportBtn").addEventListener("click", (event) => {
    event.stopPropagation();
    const menu = $("exportMenu");
    menu.hidden = !menu.hidden;
    $("exportBtn").setAttribute("aria-expanded", String(!menu.hidden));
  });
  $("exportMenu").addEventListener("click", async (event) => {
    const button = event.target.closest("button[data-export]");
    if (!button) return;
    $("exportMenu").hidden = true;
    $("exportBtn").setAttribute("aria-expanded", "false");
    const action = button.dataset.export;
    if (action === "zip") await exportZip();
    else if (action === "json") await exportJson();
    else if (action === "restore") $("jsonInput").click();
    else if (action === "weekly") await copyWeekly();
  });
  document.addEventListener("click", (event) => {
    if (!event.target.closest(".menu-wrap")) {
      $("exportMenu").hidden = true;
      $("exportBtn").setAttribute("aria-expanded", "false");
    }
  });

  $("editorBody").addEventListener("keydown", (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
      event.preventDefault();
      saveEdit();
    }
  });

  $("settingsBtn").addEventListener("click", openSettings);
  $("closeModal").addEventListener("click", closeSettings);
  $("settingsModal").addEventListener("mousedown", (event) => {
    if (event.target === $("settingsModal")) closeSettings();
  });

  $("copyTokenBtn").addEventListener("click", async () => {
    const value = $("tokenField").value;
    if (!value) return;
    try {
      await navigator.clipboard.writeText(value);
      toast("令牌已复制");
    } catch {
      $("tokenField").select();
      toast("请手动复制令牌");
    }
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !$("reader").hidden) closeBook();
    if (event.key === "Escape" && !$("settingsModal").hidden) closeSettings();
  });

  bridge.onContext(() => {});
}

async function loadStatus() {
  try {
    const status = await bridge.apiGet("status");
    state.template = status.diary_template || "";
    state.apiBase = status.api_base || guessApiBase();
  } catch {
    state.apiBase = guessApiBase();
  }
}

async function init() {
  try {
    await bridge.ready();
  } catch {
    // 即使 bridge 未就绪也尽量加载页面
  }
  bindEvents();
  await loadStatus();
  await reload();
  // 先记下当前签名，之后每次轮询拿它比对，AI 写进来的东西才能自己冒出来
  await checkSignature();
  startWatcher();
  window.addEventListener("beforeunload", stopWatcher);
}

init();
