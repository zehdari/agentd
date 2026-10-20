"use strict";
/* agentd dashboard: polls the JSON API and renders the overview or one agent's page. */

const $ = (sel, root = document) => root.querySelector(sel);
// The current page. Links switch pages in place (no reload), so these change; see navigate().
let AGENT_ID = null, SYSTEM = false, SECTION = null;
const SECTION_TITLES = { usage: "Usage", jobs: "Jobs", files: "Files", machine: "Machine", settings: "Settings" };
function parseRoute() {
  const m = location.pathname.match(/^\/agents\/(\d+)/);
  AGENT_ID = m ? m[1] : null;
  SYSTEM = location.pathname === "/system";
  const s = location.pathname.match(/^\/(usage|jobs|files|machine|settings)$/);   // the desktop rail's own pages
  SECTION = s ? s[1] : null;
}
parseRoute();
const pollMs = () => (AGENT_ID ? 2500 : 4000);

// Last data seen per page, so switching pages draws instantly and then refreshes.
const cache = { overview: null, agents: {} };
try { cache.overview = JSON.parse(localStorage.getItem("agentd-overview") || "null"); } catch { /* ignore */ }

const ui = {
  filter: "all",
  showAll: false,
  expanded: new Set(),      // tool-call groups opened on the agent page
  confirm: null,            // key of a destructive button waiting for its second tap
  confirmTimer: null,
  convoKey: "",
  loadingOlder: false,
  firstDetail: true,
  newOpen: false,
  usageBusy: false, usageAt: 0, usageError: "",
  sending: [],              // messages posted but not yet acknowledged by the server: {text, files, pct}
  attach: [],               // files waiting to go with the next reply
  newFiles: [],             // files for the new-agent form
  details: false,           // the agent page's side panel
  data: null,
};

// ------------------------------------------------------------------ helpers

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
const pad = n => String(n).padStart(2, "0");
function hm(t) { const d = new Date(t * 1000); return `${pad(d.getHours())}:${pad(d.getMinutes())}`; }
function day(t) { return new Date(t * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric" }); }
function isToday(t) { return new Date(t * 1000).toDateString() === new Date().toDateString(); }
function when(t) { return t ? (isToday(t) ? hm(t) : `${day(t)}, ${hm(t)}`) : ""; }
function dur(sec) {
  sec = Math.max(0, Math.round(sec));
  if (sec < 60) return `${sec} s`;
  const m = Math.round(sec / 60);
  if (m < 60) return `${m} min`;
  return `${Math.floor(m / 60)} h ${m % 60} min`;
}
function ago(t, now) {
  const s = now - t;
  if (s < 45) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 6 * 3600) return `${Math.floor(s / 3600)} h ago`;
  return when(t);
}
const gib = b => (b / 1024 ** 3);
function size(b) {
  if (b == null) return "";
  if (b < 1024 ** 2) return `${Math.max(1, Math.round(b / 1024))} KB`;
  if (b < 1024 ** 3) return `${(b / 1024 ** 2).toFixed(1)} MB`;
  return `${gib(b).toFixed(1)} GB`;
}
const fileUrl = p => `/api/file?path=${encodeURIComponent(p)}`;
const IMG_RE = /\.(png|jpe?g|gif|webp|avif|bmp|svg)$/i;
// Attached files are kept as <time>-name in the agent's inbox; show the name they had.
const baseName = p => p.split("/").pop().replace(/^\d{9,}(\.\d+)?-/, "");

async function api(path, body) {
  const opts = body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json", "X-Agentd": "1" }, body: JSON.stringify(body),
  };
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `${res.status} ${res.statusText}`);
  return data;
}

// POST a form with files, reporting upload progress (fetch can't).
function upload(path, form, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", path);
    xhr.setRequestHeader("X-Agentd", "1");
    xhr.upload.onprogress = e => { if (e.lengthComputable && onProgress) onProgress(e.loaded / e.total); };
    xhr.onload = () => {
      let data = {};
      try { data = JSON.parse(xhr.responseText); } catch { /* not JSON */ }
      if (xhr.status >= 200 && xhr.status < 300) resolve(data);
      else reject(new Error(data.error || (xhr.status === 413 ? "Too big to upload." : `${xhr.status} ${xhr.statusText}`)));
    };
    xhr.onerror = () => reject(new Error("The upload was cut off."));
    xhr.send(form);
  });
}

// ------------------------------------------------------------------ attachments

const previews = new WeakMap();   // File -> object URL for its thumbnail
function chipsHtml(files, list) {
  return files.map((f, i) => {
    const img = f.type.startsWith("image/");
    if (img && !previews.has(f)) previews.set(f, URL.createObjectURL(f));
    return `<li class="chip">${img ? `<img src="${previews.get(f)}" alt="">` : icon("files", "i ft")}
      <span><b>${esc(f.name)}</b><small>${esc(size(f.size))}</small></span>
      <button type="button" class="chip-x" data-unattach="${list}:${i}" aria-label="Remove ${esc(f.name)}">${icon("close", "i i-sm")}</button></li>`;
  }).join("");
}
function renderChips() {
  const c = $("#attach-chips");
  if (c) { c.innerHTML = chipsHtml(ui.attach, "attach"); c.hidden = !ui.attach.length; }
  const n = $("#new-chips");
  if (n) { n.innerHTML = chipsHtml(ui.newFiles, "newFiles"); n.hidden = !ui.newFiles.length; }
}
function addFiles(list, files) {
  // Pasted screenshots all arrive as "image.png"; give them distinct names.
  const named = [...files].map((f, i) => /^image\.\w+$/.test(f.name)
    ? new File([f], `pasted-${new Date().toISOString().slice(0, 19).replace(/[T:]/g, "-")}${files.length > 1 ? `-${i + 1}` : ""}.${f.name.split(".").pop()}`, { type: f.type })
    : f);
  ui[list].push(...named);
  renderChips();
}
function clearFiles(list) {
  for (const f of ui[list].splice(0)) { const u = previews.get(f); if (u) URL.revokeObjectURL(u); }
  renderChips();
}
function formWith(fields, files) {
  const form = new FormData();
  for (const [k, v] of Object.entries(fields)) form.append(k, v);
  for (const f of files) form.append("files", f, f.name);
  return form;
}

// Files the user attached, or that an agent sent: image thumbnails, other files as cards.
function fileTiles(paths, exists) {
  return paths.map((p, k) => {
    const name = baseName(p);
    const img = IMG_RE.test(name);
    // Gone when this entry was loaded; tapping checks again, since the file may be there now.
    if (exists && !exists[k]) return `<a class="attach gone" href="${fileUrl(p)}" data-view="${img ? "image" : "file"}" data-recheck data-name="${esc(name)}">${icon(img ? "image" : "files", "i ft")}<span><b>${esc(name)}</b> deleted</span></a>`;
    return img ? `<a class="thumb" href="${fileUrl(p)}" data-view="image" data-name="${esc(name)}"><img src="${fileUrl(p)}" alt="${esc(name)}" loading="lazy"><span>${esc(name)}</span></a>`
      : `<a class="attach" href="${fileUrl(p)}" data-view="file" data-name="${esc(name)}">${icon("files", "i ft")}<span><b>${esc(name)}</b></span></a>`;
  }).join("");
}

const ICONS = {
  clip: '<path d="m20 11.5-7.8 7.8a5 5 0 0 1-7.1-7.1l8-8a3.3 3.3 0 0 1 4.7 4.7l-7.9 7.9a1.7 1.7 0 0 1-2.4-2.4l7.3-7.3"/>',
  agents: '<circle cx="12" cy="12" r="9"/><path d="m15.5 8.5-2 5-5 2 2-5z"/>',
  jobs: '<path d="m12 3 9 5-9 5-9-5z"/><path d="m3 13 9 5 9-5"/>',
  files: '<path d="M14 3H6v18h12V7z"/><path d="M14 3v4h4"/>',
  machine: '<rect x="3" y="4" width="18" height="12" rx="1"/><path d="M8 20h8M12 16v4"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  usage: '<path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/>',
  sidebar: '<rect x="3" y="4" width="18" height="16" rx="1"/><path d="M9 4v16"/>',
  close: '<path d="M6 6l12 12M18 6 6 18"/>',
  download: '<path d="M12 4v11M7 10l5 5 5-5M5 20h14"/>',
  system: '<path d="M4 6h10M18 6h2M4 12h4M12 12h8M4 18h12M20 18h0"/><circle cx="16" cy="6" r="2"/><circle cx="10" cy="12" r="2"/><circle cx="18" cy="18" r="2"/>',
  settings: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>',
  stop: '<rect x="6" y="6" width="12" height="12" rx="1"/>',
  send: '<path d="M5 12h13M13 6l6 6-6 6"/>',
  copy: '<rect x="8" y="8" width="12" height="12" rx="1"/><path d="M16 8V4H4v12h4"/>',
  chev: '<path d="m9 6 6 6-6 6"/>',
  back: '<path d="m15 6-6 6 6 6"/>',
  reply: '<path d="M9 7 4 12l5 5"/><path d="M4 12h10a6 6 0 0 1 6 6"/>',
  term: '<path d="m5 8 4 4-4 4M12 16h7"/>',
  image: '<rect x="3" y="4" width="18" height="16" rx="1"/><circle cx="9" cy="10" r="2"/><path d="m21 17-5-5-9 8"/>',
  check: '<path d="m5 12 5 5 9-10"/>',
  ext: '<path d="M14 4h6v6M20 4l-9 9"/><path d="M18 14v6H4V6h6"/>',
  moon: '<path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
  auto: '<circle cx="12" cy="12" r="9"/><path d="M12 3v18a9 9 0 0 0 0-18z" fill="currentColor"/>',
};
function icon(name, cls = "i") {
  return `<svg class="${cls}" viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">${ICONS[name]}</svg>`;
}

const STATE_LABEL = { running: "Running", waiting: "Waiting on you", failed: "Failed", done: "Done", interrupted: "Interrupted" };
const stateChip = (st, extra = "") => `<span class="state state-${st}"><i></i>${STATE_LABEL[st] || st}${extra}</span>`;

// Links in already-escaped text: [label](url) and bare http(s) URLs, in one pass so a
// markdown link's href is never re-linked. Trailing punctuation stays outside the link.
const LINK_RE = /\[([^\]]+)\]\((https?:[^)\s]+)\)|https?:\/\/[^\s<]+/g;
const extLink = (href, label) => `<a href="${href}" target="_blank" rel="noopener">${label}</a>`;
function linkify(s) {
  return s.replace(LINK_RE, (m, label, href) => {
    if (label) return extLink(href, label);
    const tail = m.match(/(?:[.,;:!?)\]*_]|&(?:gt|quot|#39);)+$/);
    const url = tail ? m.slice(0, -tail[0].length) : m;
    return extLink(url, url) + (tail ? tail[0] : "");
  });
}

// Markdown tables: a header row, a |---|:-:| separator, then body rows.
const TABLE_SEP = /^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$/;
const cells = line => line.trim().replace(/^\|/, "").replace(/(?<!\\)\|$/, "").split(/(?<!\\)\|/).map(c => c.trim().replace(/\\\|/g, "|"));
function mdTable(rows, inline) {
  const head = cells(rows[0]);
  const align = cells(rows[1]).map(c => c.startsWith(":") && c.endsWith(":") ? "center" : c.endsWith(":") ? "right" : "");
  const td = (tag, c, i) => `<${tag}${align[i] ? ` style="text-align:${align[i]}"` : ""}>${inline(c)}</${tag}>`;
  const body = rows.slice(2).map(r => {
    const cs = cells(r);
    return `<tr>${head.map((_, i) => td("td", cs[i] ?? "", i)).join("")}</tr>`;
  });
  return `<div class="md-table"><table><thead><tr>${head.map((c, i) => td("th", c, i)).join("")}</tr></thead><tbody>${body.join("")}</tbody></table></div>`;
}

// Tiny, safe markdown: code fences, inline code, bold, links, bullet lists, tables, paragraphs.
function md(text) {
  const parts = String(text).split(/```[^\n]*\n?/);
  return parts.map((part, i) => {
    if (i % 2) return `<pre class="code"><code>${esc(part.replace(/\n$/, ""))}</code></pre>`;
    const inline = s => linkify(esc(s)
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>"));
    const LIST = /^\s*([-*•]|\d+[.)])\s+/;
    return part.split(/\n{2,}/).filter(b => b.trim()).map(block => {
      const html = [];
      let para = [], list = null;
      const flushPara = () => { if (para.length) html.push(`<p>${para.map(inline).join("<br>")}</p>`); para = []; };
      const flushList = () => { if (list) html.push(`<${list.tag}>${list.items.map(t => `<li>${inline(t)}</li>`).join("")}</${list.tag}>`); list = null; };
      const lines = block.split("\n");
      for (let n = 0; n < lines.length; n++) {
        const line = lines[n];
        if (line.includes("|") && lines[n + 1]?.includes("|") && TABLE_SEP.test(lines[n + 1])) {
          flushPara(); flushList();
          let end = n + 2;
          while (end < lines.length && lines[end].includes("|")) end++;
          html.push(mdTable(lines.slice(n, end), inline));
          n = end - 1;
        } else if (LIST.test(line)) {
          flushPara();
          const tag = /^\s*\d/.test(line) ? "ol" : "ul";
          if (!list || list.tag !== tag) { flushList(); list = { tag, items: [] }; }
          list.items.push(line.replace(LIST, ""));
        } else if (/^#{1,4}\s/.test(line)) {
          flushPara(); flushList();
          html.push(`<p class="md-h">${inline(line.replace(/^#+\s*/, ""))}</p>`);
        } else if (list && /^\s{2,}\S/.test(line)) {
          list.items[list.items.length - 1] += " " + line.trim();
        } else {
          flushList();
          para.push(line);
        }
      }
      flushPara(); flushList();
      return html.join("");
    }).join("");
  }).join("");
}

// ------------------------------------------------------------------ the track line (signature)

function track(a, now, win, { w = 240, h = 30, big = false } = {}) {
  const lo = now - win;
  if (!a.segs.length) return `<span class="track-off">← ${esc(day(a.updated))}</span>`;
  const x = t => ((t - lo) / win * w).toFixed(1);
  const mid = h * 0.62;
  const st = a.status;
  const p = [`<svg class="track track-${st}" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" role="img" aria-label="${esc(STATE_LABEL[st] || st)}, ${a.tool_count} tool calls">`];
  for (let m = Math.ceil(lo / 900) * 900; m <= now; m += 900) p.push(`<line class="grat" x1="${x(m)}" x2="${x(m)}" y1="0" y2="${h}"/>`);
  for (const [s, e] of a.segs) p.push(`<line class="course" x1="${x(s)}" x2="${x(e)}" y1="${mid}" y2="${mid}"/>`);
  const ticks = a.ticks;
  ticks.forEach((t, i) => {
    const gap = (ticks[i + 1] ?? (st === "running" ? now : t + 5)) - t;   // longer call, taller tick
    const len = (2.5 + Math.min(7, Math.log2(1 + gap / 4))) * (big ? 1.6 : 1);
    p.push(`<line class="sound" x1="${x(t)}" x2="${x(t)}" y1="${mid - 2}" y2="${(mid - 2 - len).toFixed(1)}"/>`);
  });
  for (const s of a.steers) p.push(`<path class="steer" d="M${(+x(s) - 3).toFixed(1)} ${h - 1} L${x(s)} ${mid + 3} L${(+x(s) + 3).toFixed(1)} ${h - 1} Z"/>`);
  const first = a.segs[0][0], last = a.segs[a.segs.length - 1][1];
  if (first > lo + 1) p.push(`<circle class="origin" cx="${x(first)}" cy="${mid}" r="2.4"/>`);
  const ex = +x(last);
  if (st === "running") p.push(`<circle class="halo" cx="${ex}" cy="${mid}" r="6"/><circle class="head" cx="${ex}" cy="${mid}" r="3.2"/>`);
  else if (st === "waiting") p.push(`<path class="head" d="M${ex} ${mid - 4.5} L${ex + 4.5} ${mid} L${ex} ${mid + 4.5} L${ex - 4.5} ${mid} Z"/>`);
  else if (st === "failed") p.push(`<path class="head" d="M${ex - 3.5} ${mid - 3.5} L${ex + 3.5} ${mid + 3.5} M${ex + 3.5} ${mid - 3.5} L${ex - 3.5} ${mid + 3.5}"/>`);
  p.push("</svg>");
  return p.join("");
}

function axis(now, win) {
  const lo = now - win, labels = [];
  // Stop short of the right edge so the last time label never runs into "now".
  for (let m = Math.ceil(lo / 1800) * 1800; m < now - win * 0.22; m += 1800) {
    labels.push(`<span style="left:${((m - lo) / win * 100).toFixed(2)}%">${hm(m)}</span>`);
  }
  labels.push('<span class="now" style="left:100%">now</span>');
  return `<div class="axis" aria-hidden="true">${labels.join("")}</div>`;
}

// ------------------------------------------------------------------ soundings (machine load)

let soundId = 0;
function soundings(name, hist, key, now, win, { w = 340, h = 64 } = {}) {
  const pts = hist.filter(s => s[key] != null && s.t >= now - win);
  if (pts.length < 2) return `<div class="sound-empty">Collecting the last 2 h…</div>`;
  const x = t => ((t - (now - win)) / win * w), y = v => h - v / 100 * h;
  const line = pts.map((s, i) => `${i ? "L" : "M"}${x(s.t).toFixed(1)} ${y(s[key]).toFixed(1)}`).join(" ");
  const area = `M${x(pts[0].t).toFixed(1)} ${h} ${line.replace(/^M/, "L")} L${x(pts[pts.length - 1].t).toFixed(1)} ${h} Z`;
  const pk = pts.reduce((a, b) => (b[key] > a[key] ? b : a));
  const pkx = x(pk.t), id = `s${++soundId}`;
  return `<svg class="sound-chart" viewBox="0 0 ${w} ${h + 2}" preserveAspectRatio="none" role="img" aria-label="${name} over the last 2 hours, peak ${Math.round(pk[key])}%">
  <defs><clipPath id="${id}"><path d="${area}"/></clipPath></defs>
  <g clip-path="url(#${id})">
    <rect class="band b0" x="0" y="${y(30)}" width="${w}" height="${h - y(30)}"/>
    <rect class="band b1" x="0" y="${y(70)}" width="${w}" height="${y(30) - y(70)}"/>
    <rect class="band b2" x="0" y="0" width="${w}" height="${y(70)}"/>
  </g>
  <line class="contour" x1="0" x2="${w}" y1="${y(30)}" y2="${y(30)}"/>
  <line class="contour" x1="0" x2="${w}" y1="${y(70)}" y2="${y(70)}"/>
  <path class="shore" d="${line}"/>
  <text class="depth" x="${w - 2}" y="${y(70) - 3}" text-anchor="end">70</text>
  <text class="depth" x="${w - 2}" y="${y(30) - 3}" text-anchor="end">30</text>
  <text class="peak" x="${pkx.toFixed(1)}" y="${Math.max(10, y(pk[key]) - 4).toFixed(1)}" text-anchor="${pkx > w - 30 ? "end" : pkx < 30 ? "start" : "middle"}">${Math.round(pk[key])}</text>
</svg>`;
}

// ------------------------------------------------------------------ shell

function renderShell(d) {
  const m = d.machine;
  if (m) {
    $("#host").textContent = m.host;
    $("#host-meta").textContent = `agentd · up ${dur(m.uptime).replace(/ \d+ min$/, "")}`;
    document.title = AGENT_ID && d.title ? `${d.title.slice(0, 40)} · ${m.host}` : `Agents · ${m.host}`;
  }
  const running = d.agents ? d.agents.filter(a => a.status === "running").length : null;
  // Desktop rail: agents plus a page per section. (The phone has no rail; it uses the gear and the System page.)
  const jobs = d.jobs ? d.jobs.filter(j => j.active).length : null;
  const nav = [["agents", "Agents", "/", running, !SYSTEM && !SECTION],
    ...Object.entries(SECTION_TITLES).map(([k, label]) => [k, label, `/${k}`, k === "jobs" ? jobs : null, SECTION === k])];
  $("#nav").innerHTML = nav.map(([k, label, href, n, on]) =>
    `<a class="nav${on ? " is-on" : ""}" href="${href}"${on ? ' aria-current="page"' : ""}>${icon(k)}<span>${label}</span>${n ? `<b>${n}</b>` : ""}</a>`).join("");
  if (d.services) {
    $("#services").innerHTML = `<h3>Services</h3>` + d.services.map(s =>
      `<p><i class="dot ${s.state === "active" ? "ok" : "bad"}"></i>${esc(s.name)}<span>${esc(s.state === "active" ? (s.detail || "active") : s.state)}</span></p>`).join("");
  }
}

function renderTopbar() {
  if (SYSTEM || SECTION) {
    $("#topbar").innerHTML = `<a class="back icon-back" href="/" aria-label="All agents">${icon("back")}</a><h1>${SYSTEM ? "System" : SECTION_TITLES[SECTION]}</h1>`;
    return;
  }
  const left = AGENT_ID ? `<a class="back" href="/">${icon("back")}<span>Agents</span></a>` : `<h1>Agents</h1>`;
  $("#topbar").innerHTML = `${left}
    <a class="icon-btn system-btn" href="/system" aria-label="System: usage, machine, files" title="System">${icon("system")}</a>
    <button class="btn primary new-btn" type="button" id="new-toggle" aria-expanded="${ui.newOpen}">${icon("plus")}<span>New agent</span></button>`;
}

// Models the new-agent form offers (Claude and ChatGPT), from /api/models.
let MODELS = null;
async function loadModels() {
  try {
    const r = await fetch("/api/models", { cache: "no-store" });
    if (r.ok) MODELS = await r.json();
  } catch { /* the form falls back to "default" only */ }
  if (ui.newOpen && $("#new-form") && !$("#new-form textarea").value) { $("#new-agent").innerHTML = ""; renderNewAgent(); }
}
const findModel = id => MODELS && MODELS.groups.flatMap(g => g.models).find(m => m.id === id);
const withDefault = (word, value) => value ? `${word} (${value})` : word;

function modelOptions() {
  if (!MODELS) return `<option value="">default</option>`;
  const def = MODELS.default;
  const head = `<option value="">${esc(withDefault("default", def.label))}</option>`;
  return head + MODELS.groups.filter(g => g.models.length).map(g =>
    `<optgroup label="${esc(g.name)}">${g.models.map(m => `<option value="${esc(m.id)}">${esc(m.name)}</option>`).join("")}</optgroup>`).join("");
}

function effortOptions(modelId, current = "") {
  const m = findModel(modelId || (MODELS && MODELS.default.model));
  const levels = m ? m.efforts : ["low", "medium", "high", "xhigh", "max"];
  const def = m ? m.default_effort : null;
  return `<option value="">${esc(withDefault("default", def))}</option>` +
    levels.map(e => `<option${e === current ? " selected" : ""}>${esc(e)}</option>`).join("");
}

function renderNewAgent() {
  const box = $("#new-agent");
  if (!ui.newOpen) { box.innerHTML = ""; return; }
  if (box.firstChild) return;
  if (!MODELS) loadModels();
  box.innerHTML = `<form class="new-panel" id="new-form">
    <h2>New agent</h2>
    <label class="field wide"><span>Task</span><textarea name="text" rows="3" placeholder="What should it do? Start with a path like ~/nereus to set its directory."></textarea></label>
    <div class="new-files">
      <button class="btn sm" type="button" id="new-attach">${icon("clip")}<span>Attach files</span></button>
      <input type="file" id="new-attach-input" multiple hidden>
      <ul class="chips" id="new-chips"${ui.newFiles.length ? "" : " hidden"}>${chipsHtml(ui.newFiles, "newFiles")}</ul>
    </div>
    <div class="new-row">
      <label class="field"><span>Directory</span><input name="cwd" placeholder="~ (home)" autocomplete="off" spellcheck="false"></label>
      <label class="field"><span>Model</span><select name="model">${modelOptions()}</select></label>
      <label class="field"><span>Effort</span><select name="effort">${effortOptions("")}</select></label>
    </div>
    <p class="form-error" id="new-error" role="alert" hidden></p>
    <div class="new-actions"><button class="btn primary" type="submit">${icon("send")}<span>Start agent</span></button><button class="btn" type="button" id="new-cancel">Cancel</button><span class="hint">${webToDiscord() ? "It gets its own thread in #agents too." : "Stays on the dashboard (not posted to Discord)."}</span></div>
  </form>`;
  $("textarea", box).focus();
}

// ------------------------------------------------------------------ overview

function waitingBand(d) {
  const agents = d.agents.filter(a => a.status === "waiting");
  const items = [
    ...agents.map(a => ({ t: a.updated, html: `<li>
      <span class="w-mark" aria-hidden="true"></span>
      <div class="w-body">
        <a class="w-title" href="/agents/${a.id}">${esc(a.title)}</a>
        ${a.reply ? `<p class="w-quote">${esc(a.reply)}</p>` : ""}
        <p class="w-meta"><code>${esc(a.cwd)}</code> · ${a.raw_status === "error" ? "failed" : "finished"} ${esc(ago(a.updated, d.now))} · ${a.turns} turns</p>
      </div>
      <div class="w-act"><a class="btn" href="/agents/${a.id}#reply">${icon("reply")}<span>Reply</span></a>
        <button class="icon-btn" type="button" data-seen="${a.id}" aria-label="Mark as read" title="Mark as read">${icon("check")}</button></div>
    </li>` })),
    ...d.pending.map(p => ({ t: p.t, html: `<li>
      <span class="w-mark" aria-hidden="true"></span>
      <div class="w-body">
        <span class="w-title">Session needs permission</span>
        <p class="w-quote">${esc(p.message)}</p>
        <p class="w-meta"><code>${esc(p.cwd)}</code> · terminal or Remote Control · asked ${esc(ago(p.t, d.now))}</p>
      </div>
      <div class="w-act"><a class="btn" href="${esc(p.url)}" target="_blank" rel="noopener">Open in Discord</a></div>
    </li>` })),
  ].sort((a, b) => b.t - a.t);
  if (!items.length) return `<p class="all-clear">${icon("check", "i i-sm")}Nothing is waiting on you.</p>`;
  return `<section class="waiting" aria-labelledby="w-h">
    <h2 id="w-h">Waiting on you <span class="count">${items.length}</span></h2>
    <ul class="${items.length === 1 ? "single" : ""}">${items.map(i => i.html).join("")}</ul>
  </section>`;
}

function agentRow(a, d) {
  const st = a.status;
  const effort = a.effort ? ` · ${esc(a.effort)}` : "";
  const time = st === "running" ? `since ${hm(a.started)}` : ago(a.updated, d.now);
  let action;
  if (st === "running") {
    const key = `stop:${a.id}`;
    action = ui.confirm === key
      ? `<button class="btn danger sm" type="button" data-stop="${a.id}">Stop?</button>`
      : `<button class="icon-btn danger" type="button" data-arm="${key}" aria-label="Stop agent" title="Stop">${icon("stop")}</button>`;
  } else if (st === "waiting") {
    action = `<a class="icon-btn" href="/agents/${a.id}#reply" aria-label="Reply" title="Reply">${icon("reply")}</a>`;
  } else {
    action = `<a class="icon-btn" href="/agents/${a.id}" aria-label="Open" title="Open">${icon("chev")}</a>`;
  }
  const doing = st === "running" && a.now_doing
    ? `<span class="doing">${icon("term", "i i-sm")}<code>${esc(a.now_doing)}</code></span>` : "";
  const queued = a.queued ? ` · ${a.queued} queued` : "";
  return `<tr class="row-${st}">
    <td class="c-state">${stateChip(st)}</td>
    <td class="c-task"><a href="/agents/${a.id}" class="task">${esc(a.title)}</a>
      <span class="sub"><code>${esc(a.cwd)}</code><span>${esc(a.model_label || a.model || "default")}${effort}</span><span class="when">${esc(time)}${queued}</span></span>${doing}</td>
    <td class="c-track">${track(a, d.now, d.window)}</td>
    <td class="c-num">${a.turns}</td>
    <td class="c-act">${action}</td>
  </tr>${(d.jobs || []).filter(j => j.active && j.agent === a.id).map(j => jobSubRow(j, d.now)).join("")}`;
}

// A long job (systemd-run unit) shown under the agent that started it.
function jobSubRow(j, now) {
  const key = `job:${j.unit}`;
  const act = ui.confirm === key
    ? `<button class="btn danger sm" type="button" data-stopjob="${esc(j.unit)}">Stop?</button>`
    : `<button class="icon-btn danger" type="button" data-arm="${esc(key)}" aria-label="Stop job ${esc(j.unit)}" title="Stop job">${icon("stop")}</button>`;
  return `<tr class="job-row"><td class="c-state"></td>
    <td class="c-task" colspan="3"><span class="job-line">${icon("jobs", "i i-sm")}<code class="unit">${esc(j.unit)}</code>
      <span class="sub">long job · running ${esc(dur(now - (j.started || now)))}</span></span></td>
    <td class="c-act">${act}</td></tr>`;
}

function agentsSection(d) {
  const groups = {
    all: d.agents, running: d.agents.filter(a => a.status === "running"),
    waiting: d.agents.filter(a => a.status === "waiting"),
    finished: d.agents.filter(a => !["running", "waiting"].includes(a.status)),
  };
  const list = groups[ui.filter] || d.agents;
  const shown = ui.showAll ? list : list.slice(0, 15);
  const tab = (k, label) => `<button role="tab" type="button" data-filter="${k}" aria-selected="${ui.filter === k}">${label} <b>${groups[k].length}</b></button>`;
  const empty = {
    all: "No agents yet. Start one with New agent, or post a task in #agents.",
    running: "Nothing is running.", waiting: "Nothing is waiting on you.", finished: "No finished agents yet.",
  }[ui.filter];
  return `<section class="agents" aria-labelledby="a-h">
    <div class="sec-head">
      <h2 id="a-h" class="sr">Agents</h2>
      <div class="tabs" role="tablist">${tab("all", "All")}${tab("running", "Running")}${tab("waiting", "Waiting")}${tab("finished", "Finished")}</div>
      <span class="legend"><svg viewBox="0 0 20 10" class="lg" aria-hidden="true"><line x1="1" x2="19" y1="7" y2="7" class="lg-course"/><line x1="6" x2="6" y1="5" y2="1" class="lg-sound"/><line x1="11" x2="11" y1="5" y2="2" class="lg-sound"/></svg> each tick is a tool call</span>
    </div>
    ${shown.length ? `<table class="grid">
      <thead><tr><th class="c-state">State</th><th>Task</th><th class="c-track">${axis(d.now, d.window)}</th><th class="c-num">Turns</th><th class="c-act"><span class="sr">Actions</span></th></tr></thead>
      <tbody>${shown.map(a => agentRow(a, d)).join("")}</tbody>
    </table>` : `<p class="empty">${empty}</p>`}
    ${list.length > shown.length ? `<button class="link more" type="button" data-showall>Show all ${list.length}</button>` : ""}
    ${otherJobs(d)}
  </section>`;
}

function machineSection(m, now, win, chart) {
  const h = m.history, g = m.gpu;
  const gauge = (name, small, val, key) => `<div class="gauge">
    <div class="g-head"><span class="g-name">${name} <small>${small}</small></span><span class="g-val">${val}</span></div>
    ${soundings(name, h, key, now, win, chart)}</div>`;
  return `<section class="machine" id="machine" aria-labelledby="m-h">
    <h2 id="m-h">Machine <span class="h-aside">last 2 h</span></h2>
    ${g ? gauge("GPU", esc(g.name), `<em>${Math.round(g.util)}</em>% · ${g.mem.toFixed(1)}/${Math.round(g.mem_total)} GiB · ${Math.round(g.temp)} °C`, "gpu") : ""}
    ${gauge("CPU", `${m.cores} cores`, `<em>${m.cpu == null ? "–" : Math.round(m.cpu)}</em>% · load ${m.load.toFixed(1)}`, "cpu")}
    ${gauge("Memory", "", `<em>${gib(m.ram_used).toFixed(1)}</em> / ${Math.round(gib(m.ram_total))} GiB`, "ram")}
    <div class="disk"><span class="g-name">Disk <small>/</small></span><span class="bar"><i style="width:${(100 * m.disk_used / m.disk_total).toFixed(1)}%"></i></span><span class="g-val"><em>${Math.round(gib(m.disk_used))}</em> / ${Math.round(gib(m.disk_total))} GiB</span></div>
  </section>`;
}

function jobRow(j, now, own = false) {
  const key = `job:${j.unit}`;
  const from = j.agent && !own ? `from <a href="/agents/${j.agent}">${esc(j.agent_title || "agent")}</a> · ` : "";
  const state = j.active ? `${dur(now - (j.started || now))}` : (j.gone ? "finished" : `${esc(j.state)}${j.result && j.result !== "success" ? ` · ${esc(j.result)}` : ""}`);
  const ended = !j.active && j.ended ? ` ${esc(when(j.ended))}` : "";
  const act = j.active
    ? (ui.confirm === key ? `<button class="btn danger sm" type="button" data-stopjob="${esc(j.unit)}">Stop?</button>`
      : `<button class="icon-btn danger" type="button" data-arm="${esc(key)}" aria-label="Stop job" title="Stop">${icon("stop")}</button>`)
    : "";
  return `<li class="${j.active ? "" : "is-past"}"><span class="state state-${j.active ? "running" : "done"}"><i></i></span>
    <div><code class="unit">${esc(j.unit)}</code><span class="sub">${from}${state}${ended}</span></div>${act}</li>`;
}

function jobsSection(jobs, now) {
  return `<section class="jobs" id="jobs" aria-labelledby="j-h">
    <h2 id="j-h">Long jobs <span class="h-aside">systemd --user</span></h2>
    ${jobs.length ? `<ul class="list">${jobs.map(j => jobRow(j, now)).join("")}</ul>`
      : `<p class="empty">No long jobs. Agents start them with <code>systemd-run --user</code> so they outlive a turn.</p>`}
  </section>`;
}

function fileItem(f, withAgent = true) {
  const name = f.exists ? `<a class="fname" href="${fileUrl(f.path)}" data-view="${f.image ? "image" : "file"}" data-name="${esc(f.name)}">${esc(f.name)}</a>` : `<span class="fname gone">${esc(f.name)}</span>`;
  const meta = [withAgent ? `<a href="/agents/${f.agent}">${esc(f.agent_title)}</a>` : "", esc(when(f.t)), f.exists ? size(f.size) : "deleted"].filter(Boolean).join(" · ");
  return `<li>${icon(f.image ? "image" : "files", "i ft")}<div>${name}<span class="sub">${meta}</span></div></li>`;
}

function filesSection(files) {
  return `<section class="files" id="files" aria-labelledby="f-h">
    <h2 id="f-h">Sent to you</h2>
    ${files.length ? `<ul class="list">${files.map(f => fileItem(f)).join("")}</ul>`
      : `<p class="empty">Files agents send with <code>agentd-send</code> show up here.</p>`}
  </section>`;
}

const WINDOW_LABEL = { five_hour: "5-hour window", seven_day: "This week", seven_day_opus: "This week · Opus", seven_day_sonnet: "This week · Sonnet" };
function planName(p) {
  if (!p || !p.type) return "";
  const mult = /(\d+)x/.exec(p.tier || "");
  return `${p.type.charAt(0).toUpperCase()}${p.type.slice(1)}${mult ? ` ${mult[1]}×` : ""}`;
}
function resetText(t, now) {
  if (!t) return "";
  const s = t - now;
  if (s <= 0) return "resets now";
  if (s < 3600) return `resets in ${Math.round(s / 60)} min`;
  if (s < 86400) return `resets ${hm(t)}`;
  return `resets ${new Date(t * 1000).toLocaleDateString(undefined, { weekday: "short" })} ${hm(t)}`;
}

function usageSection(u, now) {
  if (!u) return "";
  const c = u.claude;
  const meterList = ws => (ws || []).map(w => {
    const pct = Math.round((w.used || 0) * 100);
    return `<div class="meter${pct >= 90 ? " hot" : pct >= 70 ? " warm" : ""}">
      <div class="g-head"><span class="g-name">${esc(WINDOW_LABEL[w.key] || w.key.replace(/_/g, " "))}</span>
        <span class="g-val"><em>${pct}</em>% · ${esc(resetText(w.resets, now))}</span></div>
      <span class="bar"><i style="width:${Math.min(100, Math.max(pct, pct ? 1 : 0))}%"></i></span></div>`;
  }).join("");
  const meters = meterList(c.windows);
  const x = u.codex || {};
  const CODEX_PLANS = { prolite: "Pro Lite", pro: "Pro", plus: "Plus", free: "Free", team: "Team", business: "Business", enterprise: "Enterprise", edu: "Edu" };
  const codexPlan = x.plan ? CODEX_PLANS[x.plan] || x.plan.charAt(0).toUpperCase() + x.plan.slice(1) : "";
  const codexBody = !x.installed ? `<p class="empty">Codex isn’t installed on this machine.</p>`
    : x.windows && x.windows.length ? `${x.limited ? `<p class="u-alert">Codex is rate limited right now.</p>` : ""}${meterList(x.windows)}
        ${x.error ? `<p class="u-note">${esc(x.error)} Showing the reading from ${esc(ago(x.as_of, now))}.</p>` : ""}`
    : `<p class="empty">${esc(x.error || "Checking…")}</p>`;
  const limited = c.status && c.status !== "allowed";
  return `<section class="usage" id="usage" aria-labelledby="u-h">
    <h2 id="u-h">Usage <span class="h-aside">${ui.usageBusy ? "checking…" : c.as_of ? `as of ${esc(ago(c.as_of, now))}` : ""}</span>
      <button class="link u-refresh" type="button" id="usage-refresh"${ui.usageBusy ? " disabled" : ""}>Refresh</button></h2>
    ${ui.usageError ? `<p class="u-note">${esc(ui.usageError)} Showing the last reading from an agent run.</p>` : ""}
    <div class="u-block">
      <div class="u-head"><b>Claude</b><span>${esc(planName(c.plan))}</span></div>
      ${limited ? `<p class="u-alert">Rate limited right now (${esc(c.status)}).</p>` : ""}
      ${meters || `<p class="empty">No reading yet. It updates whenever an agent runs.</p>`}
      <p class="u-foot">agentd agents: <b>$${c.cost_day.toFixed(2)}</b> today · <b>$${c.cost_week.toFixed(2)}</b> this week <span>(API-equivalent, not billed on your plan)</span></p>
    </div>
    <div class="u-block">
      <div class="u-head"><b>Codex</b><span>${esc(codexPlan || (x.installed ? "" : "not on Cam-PC"))}</span></div>
      ${codexBody}
    </div>
  </section>`;
}

// Fetch fresh plan limits when Usage is on screen (at most once a minute; the server also limits it).
async function refreshUsage(force = false) {
  if (ui.usageBusy || (!force && Date.now() - (ui.usageAt || 0) < 60000)) return;
  ui.usageBusy = true;
  ui.usageAt = Date.now();
  rerender();
  try {
    const r = await api("/api/usage/refresh", {});
    ui.usageError = r.error || "";
  } catch (err) { ui.usageError = err.message; }
  ui.usageBusy = false;
  refresh();
}
function usageInView() {
  const el = document.getElementById("usage");
  if (!el) return false;
  const r = el.getBoundingClientRect();
  return r.top < innerHeight && r.bottom > 0;
}
let usageScrollTimer = null;
addEventListener("scroll", () => {
  clearTimeout(usageScrollTimer);
  usageScrollTimer = setTimeout(() => { if (!AGENT_ID && usageInView()) refreshUsage(); }, 300);
}, { passive: true });
addEventListener("hashchange", () => { if (location.hash === "#usage") refreshUsage(); });

// Running jobs no listed agent started (from a terminal, or an agent off the list).
function otherJobs(d) {
  const ids = new Set(d.agents.map(a => a.id));
  const jobs = (d.jobs || []).filter(j => j.active && !ids.has(j.agent));
  return jobs.length ? `<div class="other-jobs"><h3>Other long jobs</h3><ul class="list">${jobs.map(j => jobRow(j, d.now)).join("")}</ul></div>` : "";
}

function renderOverview(d) {
  $("#view").innerHTML = `${waitingBand(d)}
    ${agentsSection(d)}`;
}

function servicesSection(services) {
  return `<section aria-labelledby="sv-h"><h2 id="sv-h">Services</h2>
    <ul class="list">${(services || []).map(s => `<li class="svc"><i class="dot ${s.state === "active" ? "ok" : "bad"}"></i>
      <div><b>${esc(s.name)}</b><span class="sub">${esc(s.state)}${s.detail ? ` · ${esc(s.detail)}` : ""}</span></div></li>`).join("")}</ul></section>`;
}

// Whether agents started here get a Discord thread (Settings → Discord). Off by default.
const webToDiscord = () => !!(cache.overview && cache.overview.settings && cache.overview.settings.web_to_discord);

function discordSection() {
  const on = webToDiscord();
  const opt = (v, label) => `<button type="button" data-web-discord="${v}" aria-pressed="${on === v}">${label}</button>`;
  return `<section aria-labelledby="dc-h"><h2 id="dc-h">Discord</h2>
    <p class="set-label" id="dc-l">Post agents started here to Discord</p>
    <div class="segmented" role="group" aria-labelledby="dc-l">${opt(false, "Off")}${opt(true, "On")}</div>
    <p class="set-note">${on
      ? "New agents you start on the dashboard get a thread in #agents, with their replies, tool log and done pings."
      : "New agents you start on the dashboard stay here: no thread in #agents and no Discord pings."}
      Agents started from Discord keep their threads either way, and replies you send them from here show up there too.</p></section>`;
}

function appearanceSection() {
  const cur = localStorage.getItem("agentd-theme") || "auto";
  const opt = (v, label) => `<button type="button" data-theme-set="${v}" aria-pressed="${cur === v}">${label}</button>`;
  return `<section aria-labelledby="ap-h"><h2 id="ap-h">Appearance</h2>
    <div class="segmented" role="group" aria-label="Theme">${opt("auto", "Match phone")}${opt("light", "Light")}${opt("dark", "Dark")}</div></section>`;
}

// One rail section on its own page (desktop; the phone reaches these through System).
function renderSection(d) {
  const body = {
    usage: () => usageSection(d.usage, d.now),
    jobs: () => jobsSection(d.jobs, d.now),
    files: () => filesSection(d.files),
    machine: () => machineSection(d.machine, d.now, d.window, { w: 900, h: 120 }),
    settings: () => `<div class="settings">${discordSection()}${appearanceSection()}</div>`,
  }[SECTION]();
  $("#view").innerHTML = `<div class="one-page page-${SECTION}">${body}</div>`;
}

function renderSystem(d) {
  $("#view").innerHTML = `<div class="system">
    <div class="sys-col">${usageSection(d.usage, d.now)}${machineSection(d.machine, d.now, d.window)}</div>
    <div class="sys-col">${jobsSection(d.jobs, d.now)}${filesSection(d.files)}${servicesSection(d.services)}${discordSection()}${appearanceSection()}</div>
  </div>`;
}

// ------------------------------------------------------------------ agent page

const TOOL_WORDS = [
  [["Bash"], "ran", "command", "commands"],
  [["Read", "NotebookRead"], "read", "file", "files"],
  [["Edit", "MultiEdit", "Write", "NotebookEdit"], "edited", "file", "files"],
  [["Grep", "Glob"], "searched", "time", "times"],
  [["WebFetch", "WebSearch"], "looked up", "page", "pages"],
  [["Agent", "Task"], "ran", "subagent", "subagents"],
];
function toolSummary(group) {
  const counts = new Map();
  let other = 0;
  for (const c of group) {
    const kind = TOOL_WORDS.find(([names]) => names.includes(c.name));
    if (kind) counts.set(kind, (counts.get(kind) || 0) + 1); else other++;
  }
  const parts = [...counts].map(([[, verb, one, many], n]) => `${verb} ${n} ${n === 1 ? one : many}`);
  if (other) parts.push(`used ${other} other tool${other === 1 ? "" : "s"}`);
  const s = parts.join(", ");
  return s.charAt(0).toUpperCase() + s.slice(1);
}

function chatTime(t) {
  const d = new Date(t * 1000);
  return isToday(t) ? `Today ${hm(t)}` : `${d.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" })}, ${hm(t)}`;
}

function convoHtml(d) {
  const out = [];
  const entries = d.entries;
  let lastT = 0;
  const stamp = t => {   // a quiet time divider when the conversation picks up after a pause
    if (t && t - lastT > 20 * 60) out.push(`<p class="chat-time">${esc(chatTime(t))}</p>`);
    if (t) lastT = t;
  };
  for (let i = 0; i < entries.length; i++) {
    const e = entries[i];
    if (e.kind === "tool") {
      const start = i;
      while (i + 1 < entries.length && entries[i + 1].kind === "tool") i++;
      const group = entries.slice(start, i + 1);
      stamp(group[0].t);
      const live = d.status === "running" && i === entries.length - 1;
      const open = ui.expanded.has(d.entries_from + start);   // keyed by log position, so loading older entries keeps it
      const last = group[group.length - 1];
      out.push(`<div class="tools-row${live ? " live" : ""}${open ? " open" : ""}">
        <button class="tools-toggle" type="button" data-expand="${d.entries_from + start}" aria-expanded="${open}">
          ${icon("chev", "i i-sm chev")}<span class="tools-sum">${esc(toolSummary(group))}</span>
          ${live ? `<span class="tools-now"><code>${esc(last.name)} ${esc(last.detail.split("\n")[0])}</code></span>` : ""}
        </button>
        ${open ? `<ul class="calls full">${group.map(c => {
          const full = c.detail.length > 4000 ? `${c.detail.slice(0, 4000)}\n…` : c.detail;
          return `<li class="${c.nested ? "nested" : ""}"><span class="tool">${esc(c.name)}${c.t ? ` <time>${hm(c.t)}</time>` : ""}</span><pre>${esc(full)}</pre></li>`;
        }).join("")}</ul>` : ""}
      </div>`);
    } else if (e.kind === "you") {
      stamp(e.t);
      out.push(`<div class="bubble-row">${e.paths ? `<div class="thumbs mine">${fileTiles(e.paths, e.exists)}</div>` : ""}
        ${e.text ? `<div class="bubble"><div class="md">${md(e.text)}</div></div>` : ""}
        ${e.steer ? `<p class="bubble-note">sent while it was working</p>` : ""}</div>`);
    } else if (e.kind === "claude") {
      stamp(e.t);
      out.push(`<div class="reply md">${md(e.text)}</div>`);
    } else if (e.kind === "file") {
      stamp(e.t);
      out.push(`<div class="reply files-msg">${e.caption ? `<p class="files-cap">${esc(e.caption)}</p>` : ""}<div class="thumbs">${fileTiles(e.paths, e.exists)}</div></div>`);
    } else if (e.kind === "result" && !e.ok) {
      out.push(`<p class="chat-error">The run failed${e.detail ? `: ${esc(e.detail)}` : "."}</p>`);
    }
  }
  const OUT_NOTE = {
    starting: "Sent · starting the agent…",
    sent: "Sent · it reads this after its current step",
    queued: "Queued · it goes in when the current turn ends",
    sending: "Sending…",
  };
  for (const m of [...(d.outbox || []), ...ui.sending.map(s => ({ ...s, state: "sending" }))]) {
    const files = (m.files || []).length ? `<ul class="pending-files">${m.files.map(n => `<li>${icon("clip", "i i-sm")}${esc(n)}</li>`).join("")}</ul>` : "";
    const note = m.state === "sending" && m.files.length ? (m.pct < 1 ? `Uploading ${Math.floor(m.pct * 100)}%…` : "Sending…") : OUT_NOTE[m.state] || "Sent";
    out.push(`<div class="bubble-row pending">${files}${m.text ? `<div class="bubble"><div class="md">${md(m.text)}</div></div>` : ""}
      <p class="bubble-note">${note}</p></div>`);
  }
  if (d.status === "running") {
    out.push(`<p class="working"><span class="dots" aria-hidden="true"><i></i><i></i><i></i></span>Working · ${dur(d.now - d.started)}${d.queued ? ` · ${d.queued} message${d.queued > 1 ? "s" : ""} queued` : ""}</p>`);
  }
  if (d.entries_from > 0) {
    out.unshift(`<button class="convo-more" type="button" data-older${ui.loadingOlder ? " disabled" : ""}>${ui.loadingOlder ? "Loading earlier messages…" : "Load earlier messages"}</button>`);
  }
  if (!out.length) out.push(`<p class="empty">No conversation yet.</p>`);
  return out.join("");
}

function detailsHtml(d) {
  const effortSel = `<label class="select"><span>Effort</span><select id="effort">${["default", ...d.efforts].map(e =>
    `<option value="${esc(e)}"${(d.effort || "default") === e ? " selected" : ""}>${esc(e === "default" ? withDefault(e, d.default_effort) : e)}</option>`).join("")}</select></label>`;
  const mix = Object.entries(d.tools).sort((a, b) => b[1] - a[1]).slice(0, 5).map(([k, v]) => `${esc(k)} ${v}`).join(" · ");
  return `
    <section aria-labelledby="di-h">
      <h2 id="di-h">Details</h2>
      <p class="d-full-title">${esc(d.title)}</p>
      <dl class="kv">
        <dt>Directory</dt><dd><code>${esc(d.cwd)}</code></dd>
        <dt>Model</dt><dd>${esc(d.model_label || d.model || "default")}</dd>
        <dt>Started</dt><dd>${esc(when(d.created))}</dd>
        <dt>Turns</dt><dd>${d.turns} · ${d.tool_count} tool calls</dd>
        ${mix ? `<dt>Tools</dt><dd>${mix}</dd>` : ""}
      </dl>
      <div class="d-actions">${effortSel}
        ${d.url ? `<a class="btn" href="${esc(d.url)}" target="_blank" rel="noopener">${icon("ext")}<span>Discord thread</span></a>` : ""}</div>
    </section>
    <section aria-labelledby="dt-h"><h2 id="dt-h">Last 2 hours</h2>
      ${d.segs.length ? `<div class="d-track">${axis(d.now, d.window)}${track(d, d.now, d.window, { w: 320, h: 36, big: true })}</div>
        <p class="d-track-key"><span><i class="k-steer"></i>your message mid-run</span><span>each tick is a tool call</span></p>`
        : `<p class="empty">No activity · last ${esc(when(d.updated))}</p>`}</section>
    <section aria-labelledby="dj-h"><h2 id="dj-h">Jobs started</h2>
      ${d.jobs.length ? `<ul class="list">${d.jobs.map(j => jobRow(j, d.now, true)).join("")}</ul>` : `<p class="empty">None in the last day.</p>`}</section>
    <section aria-labelledby="df-h"><h2 id="df-h">Files sent</h2>
      ${d.file_list.length ? `<ul class="list">${d.file_list.map(f => fileItem(f, false)).join("")}</ul>` : `<p class="empty">None yet.</p>`}</section>
    ${d.resume ? `<section aria-labelledby="ds-h"><h2 id="ds-h">Continue at the machine</h2>
      <pre class="cmd"><code id="resume">${esc(d.resume)}</code></pre>
      <button class="btn sm" type="button" id="copy-resume">${icon("copy")}<span>Copy</span></button></section>` : ""}`;
}

// Unsent replies, per agent, so leaving the chat doesn't lose them.
const draftKey = id => `agentd-draft:${id}`;
function saveDraft(id, text) {
  try { text ? localStorage.setItem(draftKey(id), text) : localStorage.removeItem(draftKey(id)); } catch { /* full */ }
}
function fitComposer(ta) { ta.style.height = "auto"; ta.style.height = Math.min(ta.scrollHeight, 220) + "px"; }

function renderDetail(d) {
  const view = $("#view");
  document.body.classList.add("p-chat");
  if (!$(".chat", view) || view.dataset.agent !== String(d.id)) {
    view.dataset.agent = String(d.id);
    view.innerHTML = `<div class="chat-layout">
        <section class="chat" aria-label="Conversation"><div class="convo"></div>
          <form class="composer" id="composer"><label class="sr" for="m">Message this agent</label>
            <ul class="chips" id="attach-chips" hidden></ul>
            <button class="attach-btn" type="button" id="attach-btn" aria-label="Attach files" title="Attach files">${icon("clip")}</button>
            <input type="file" id="attach-input" multiple hidden>
            <textarea id="m" rows="1" placeholder="Reply to Claude"></textarea>
            <button class="send" type="submit" aria-label="Send">${icon("send")}</button></form>
          <p class="hint" id="composer-hint"></p>
        </section>
        <aside class="details" id="details"></aside>
      </div>`;
    // Keep room under the chat for the composer as it grows with the message.
    new ResizeObserver(([e]) => document.documentElement.style.setProperty("--composer-h", `${Math.ceil(e.target.offsetHeight)}px`))
      .observe($("#composer"));
    renderChips();
    const draft = localStorage.getItem(draftKey(d.id));
    if (draft) { $("#m").value = draft; fitComposer($("#m")); }
  }
  $("#m").placeholder = d.provider === "codex" ? "Reply to ChatGPT" : "Reply to Claude";
  const stop = d.status === "running"
    ? (ui.confirm === `stop:${d.id}` ? `<button class="btn danger sm" type="button" data-stop="${d.id}">${icon("stop")}<span>Stop now?</span></button>`
      : `<button class="btn danger sm" type="button" data-arm="stop:${d.id}" aria-label="Stop">${icon("stop")}<span>Stop</span></button>`) : "";
  $("#topbar").innerHTML = `<a class="back" href="/" aria-label="All agents">${icon("back")}</a>
    <div class="chat-title"><b>${esc(d.title)}</b><span>${stateChip(d.status)}<code>${esc(d.cwd)}</code></span></div>
    <div class="chat-actions">${stop}
      <button class="btn sm" type="button" id="details-toggle" aria-expanded="${ui.details}" aria-controls="details">Details</button></div>`;
  document.body.classList.toggle("details-open", ui.details);

  const outKey = (d.outbox || []).map(m => m.state).join(",") + "|" + ui.sending.map(s => Math.floor(s.pct * 100)).join(",");
  const key = `${d.entries_from}:${ui.loadingOlder}:${d.total_entries}:${d.status}:${d.queued}:${outKey}:${[...ui.expanded].join(",")}:${Math.floor(d.now / 60)}`;
  if (key !== ui.convoKey) {
    const nearBottom = window.innerHeight + window.scrollY > document.body.scrollHeight - 240;
    ui.convoKey = key;
    $(".convo").innerHTML = convoHtml(d);
    if (ui.firstDetail ? location.hash !== "#top" : nearBottom) window.scrollTo(0, document.body.scrollHeight);
  }
  $("#composer-hint").textContent = (d.status === "running"
    ? "It reads this after its current step." : "Continues the same session.") + (d.url ? " Also posted in Discord." : "");
  if (ui.firstDetail && location.hash === "#reply") $("#m").focus();
  ui.firstDetail = false;
  $("#details").innerHTML = detailsHtml(d);
}

// ------------------------------------------------------------------ loop

// Append newly logged entries to what we already have (the conversation log only grows).
function mergeDetail(prev, d) {
  const have = prev ? prev.entries_from + prev.entries.length : -1;
  if (prev && d.entries_from === have) return { ...d, entries_from: prev.entries_from, entries: prev.entries.concat(d.entries) };
  ui.expanded.clear();
  return d;
}

// Older entries load only when the reader scrolls up near the top (or taps the button), a page at a time.
async function loadOlder() {
  const id = AGENT_ID, d = cache.agents[id];
  if (!d || d.entries_from <= 0 || ui.loadingOlder) return;
  ui.loadingOlder = true;
  ui.convoKey = "";
  renderDetail(d);
  try {
    const r = await api(`/api/agents/${id}/history?before=${d.entries_from}`);
    const cur = cache.agents[id];
    if (cur && cur.entries_from === d.entries_from) {
      cache.agents[id] = { ...cur, entries_from: r.entries_from, entries: r.entries.concat(cur.entries) };
    }
  } catch { /* the button stays; try again on the next scroll or tap */ }
  ui.loadingOlder = false;
  if (AGENT_ID !== id || !cache.agents[id]) return;
  const fromBottom = document.body.scrollHeight - window.scrollY;   // keep the reader's place
  ui.convoKey = "";
  ui.data = cache.agents[id];
  renderDetail(ui.data);
  window.scrollTo(0, document.body.scrollHeight - fromBottom);
}
addEventListener("scroll", () => {
  if (AGENT_ID && !ui.firstDetail && window.scrollY < 900 && cache.agents[AGENT_ID]?.entries_from > 0) loadOlder();
}, { passive: true });

let timer = null, failures = 0, seq = 0;
async function refresh() {
  clearTimeout(timer);
  const mine = ++seq;   // a newer refresh (or a page switch) makes this one's result stale
  try {
    if (AGENT_ID) {
      const id = AGENT_ID, prev = cache.agents[id];
      const after = prev ? prev.entries_from + prev.entries.length : 0;
      const [raw, o] = await Promise.all([api(`/api/agents/${id}?after=${after}`), cache.overview ? null : api("/api/overview")]);
      if (o) cache.overview = o;
      const d = mergeDetail(cache.agents[id], raw);
      cache.agents[id] = d;
      if (mine !== seq) return;
      if (cache.overview) renderShell(cache.overview);
      ui.data = d;
      document.title = `${d.title.slice(0, 48)} · agentd`;
      renderDetail(d);
      if (d.status === "waiting" && !document.hidden) api(`/api/agents/${id}/seen`, {}).catch(() => {});
    } else {
      const d = await api("/api/overview");
      cache.overview = d;
      try { localStorage.setItem("agentd-overview", JSON.stringify(d)); } catch { /* full */ }
      if (mine !== seq) return;
      ui.data = d;
      renderShell(d);
      const typing = document.activeElement && /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName)
        && $("#view").contains(document.activeElement);
      if (!typing) SYSTEM ? renderSystem(d) : SECTION ? renderSection(d) : renderOverview(d);
      if (SYSTEM || SECTION) document.title = `${SYSTEM ? "System" : SECTION_TITLES[SECTION]} · ${d.machine.host}`;
      if (!ui.scrolledToHash && location.hash.length > 1) {   // content didn't exist when the browser tried
        ui.scrolledToHash = true;
        const el = document.getElementById(location.hash.slice(1));
        if (el) el.scrollIntoView();
      }
      if (usageInView()) refreshUsage();
    }
    failures = 0;
    $("#offline").hidden = true;
  } catch (err) {
    console.error(err);
    failures++;
    if (failures > 1) {
      $("#offline").hidden = false;
      $("#offline").textContent = `Can’t reach agentd (${err.message || err}). Retrying…`;
    }
    if (mine === seq && String(err.message).startsWith("404")) $("#view").innerHTML = `<p class="empty">That agent doesn’t exist. <a href="/">Back to all agents</a></p>`;
  }
  if (mine === seq) timer = setTimeout(refresh, document.hidden ? 30000 : pollMs());
}
document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });

// Switch pages without reloading: draw from cache straight away, then fetch fresh data.
function navigate(url, push = true) {
  if (push) history.pushState({}, "", url);
  parseRoute();
  ui.firstDetail = true;
  ui.convoKey = "";
  ui.confirm = null;
  ui.scrolledToHash = false;
  ui.newOpen = false;
  renderNewAgent();
  document.body.classList.toggle("p-chat", !!AGENT_ID);
  if (!AGENT_ID) document.body.classList.remove("details-open");
  if (AGENT_ID) ui.expanded.clear();
  renderTopbar();
  const cached = AGENT_ID ? cache.agents[AGENT_ID] : cache.overview;
  ui.data = cached || null;
  if (cached) {
    if (cache.overview) renderShell(cache.overview);
    rerender();
  } else if (AGENT_ID) {
    const a = cache.overview && cache.overview.agents.find(x => x.id === AGENT_ID);
    $("#view").innerHTML = `<p class="loading">${a ? esc(a.title) : "Loading…"}</p>`;
  } else {
    $("#view").innerHTML = `<p class="loading">Loading…</p>`;
  }
  if (!AGENT_ID) {
    const el = location.hash.length > 1 && document.getElementById(location.hash.slice(1));
    if (el) { el.scrollIntoView(); ui.scrolledToHash = true; } else window.scrollTo(0, 0);
  }
  refresh();
}

document.addEventListener("click", e => {
  const a = e.target.closest("a[href]");
  if (!a || e.defaultPrevented || e.button || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
  if (a.target || a.hasAttribute("download") || a.dataset.view) return;
  const url = new URL(a.href, location.href);
  if (url.origin !== location.origin || url.pathname.startsWith("/api/") || url.pathname.startsWith("/static/")) return;
  e.preventDefault();
  if (url.pathname === location.pathname && url.hash) {   // same page, different section
    history.replaceState({}, "", url);
    const el = document.getElementById(url.hash.slice(1));
    if (el) el.scrollIntoView({ behavior: "smooth" });
    return;
  }
  navigate(url.pathname + url.search + url.hash);
});
addEventListener("popstate", e => {
  if (ui.ignorePop) { ui.ignorePop = false; return; }   // the file viewer closing itself
  if (e.state && e.state.viewer) return;
  const dlg = document.getElementById("viewer");
  if (dlg && dlg.open) return;   // the viewer's own back handler closes it
  navigate(location.pathname + location.hash, false);
});

// Warm the cache when a finger lands on an agent link, so the page is ready when the tap completes.
let prefetching = null;
document.addEventListener("pointerdown", e => {
  const a = e.target.closest('a[href^="/agents/"]');
  const m = a && a.getAttribute("href").match(/^\/agents\/(\d+)/);
  if (!m || cache.agents[m[1]] || prefetching === m[1]) return;
  prefetching = m[1];
  api(`/api/agents/${m[1]}?after=0`).then(d => { if (!cache.agents[m[1]]) cache.agents[m[1]] = d; }).catch(() => {}).finally(() => { prefetching = null; });
}, { passive: true });

function rerender() {
  if (!ui.data) return;
  AGENT_ID ? renderDetail(ui.data) : SYSTEM ? renderSystem(ui.data) : SECTION ? renderSection(ui.data) : renderOverview(ui.data);
}

// ------------------------------------------------------------------ actions

function arm(key) {
  ui.confirm = key;
  clearTimeout(ui.confirmTimer);
  ui.confirmTimer = setTimeout(() => { ui.confirm = null; rerender(); }, 4000);
  rerender();
}

async function act(fn, btn) {
  if (btn) btn.disabled = true;
  try { await fn(); } catch (err) { alert(err.message); }
  ui.confirm = null;
  ui.convoKey = "";
  refresh();
}

document.addEventListener("click", e => {
  const t = e.target.closest("button, a");
  if (!t) return;
  if (t.id === "attach-btn") $("#attach-input").click();
  else if (t.id === "new-attach") $("#new-attach-input").click();
  else if (t.dataset.unattach) {
    const [list, i] = t.dataset.unattach.split(":");
    const [f] = ui[list].splice(+i, 1);
    if (previews.has(f)) URL.revokeObjectURL(previews.get(f));
    renderChips();
  } else if (t.id === "new-toggle" || t.id === "new-cancel") {
    ui.newOpen = !ui.newOpen;
    $("#new-toggle") && $("#new-toggle").setAttribute("aria-expanded", ui.newOpen);
    renderNewAgent();
  } else if (t.id === "usage-refresh") {
    refreshUsage(true);
  } else if (t.id === "details-toggle") {
    ui.details = !ui.details;
    rerender();
  } else if (t.dataset.arm) arm(t.dataset.arm);
  else if (t.dataset.stop) act(() => api(`/api/agents/${t.dataset.stop}/stop`, {}), t);
  else if (t.dataset.stopjob) act(() => api(`/api/jobs/${encodeURIComponent(t.dataset.stopjob)}/stop`, {}), t);
  else if (t.dataset.seen) act(() => api(`/api/agents/${t.dataset.seen}/seen`, {}), t);
  else if (t.dataset.filter) { ui.filter = t.dataset.filter; ui.showAll = false; rerender(); }
  else if (t.hasAttribute("data-showall")) { ui.showAll = true; rerender(); }
  else if (t.hasAttribute("data-older")) loadOlder();
  else if (t.dataset.expand) {
    const k = +t.dataset.expand;
    ui.expanded.has(k) ? ui.expanded.delete(k) : ui.expanded.add(k);
    rerender();
  } else if (t.id === "copy-resume") {
    const text = $("#resume").textContent;
    const done = () => { t.querySelector("span").textContent = "Copied"; setTimeout(() => rerender(), 1500); };
    if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(text).then(done);
    else {
      const ta = Object.assign(document.createElement("textarea"), { value: text });
      document.body.append(ta); ta.select(); document.execCommand("copy"); ta.remove(); done();
    }
  } else if (t.id === "theme" || t.dataset.themeSet) {
    const order = ["auto", "light", "dark"];
    const cur = localStorage.getItem("agentd-theme") || "auto";
    const next = t.dataset.themeSet || order[(order.indexOf(cur) + 1) % 3];
    localStorage.setItem("agentd-theme", next);
    const dark = next === "dark" || (next === "auto" && matchMedia("(prefers-color-scheme: dark)").matches);
    document.documentElement.dataset.theme = dark ? "dark" : "light";
    renderTheme();
    if (SYSTEM || SECTION) rerender();
  } else if (t.dataset.webDiscord) {
    const on = t.dataset.webDiscord === "true";
    act(async () => {
      const settings = await api("/api/settings", { web_to_discord: on });
      if (cache.overview) cache.overview.settings = settings;
    }, t);
  }
});

document.addEventListener("change", e => {
  if (e.target.id === "attach-input" || e.target.id === "new-attach-input") {
    addFiles(e.target.id === "attach-input" ? "attach" : "newFiles", e.target.files);
    e.target.value = "";
    if (e.target.id === "attach-input") $("#m").focus();
    return;
  }
  if (e.target.name === "model" && e.target.form && e.target.form.id === "new-form") {
    const eff = e.target.form.elements.effort;
    eff.innerHTML = effortOptions(e.target.value, eff.value);
  }
  if (e.target.id === "effort") act(() => api(`/api/agents/${AGENT_ID}/effort`, { level: e.target.value }));
});

document.addEventListener("submit", async e => {
  e.preventDefault();
  if (e.target.id === "composer") {
    const ta = $("#m"), text = ta.value.trim();
    if (!text && !ui.attach.length) return;
    const btn = $("button[type=submit]", e.target);
    btn.disabled = true;
    // Show it straight away; the server's copy (with its real status) replaces it on the next refresh.
    const files = ui.attach.splice(0), id = AGENT_ID;
    ta.value = "";
    saveDraft(id, "");
    ta.style.height = "";
    renderChips();
    const pending = { text, files: files.map(f => f.name), pct: 0 };
    ui.sending.push(pending);
    ui.convoKey = "";
    rerender();
    window.scrollTo(0, document.body.scrollHeight);
    try {
      if (files.length) {
        await upload(`/api/agents/${AGENT_ID}/message`, formWith({ text }, files), pct => {
          pending.pct = pct;
          if (ui.data) renderDetail(ui.data);
        });
      } else {
        await api(`/api/agents/${AGENT_ID}/message`, { text });
      }
    } catch (err) {
      if (!ta.value) { ta.value = text; saveDraft(id, text); }
      ui.attach.unshift(...files);
      renderChips();
      alert(`Couldn’t send: ${err.message}`);
    }
    for (const f of files) if (!ui.attach.includes(f) && previews.has(f)) URL.revokeObjectURL(previews.get(f));
    ui.sending = ui.sending.filter(s => s !== pending);
    btn.disabled = false;
    ui.convoKey = "";
    refresh();
  } else if (e.target.id === "new-form") {
    const fields = Object.fromEntries(new FormData(e.target)), err = $("#new-error");
    const btn = $("button[type=submit]", e.target), label = $("span", btn);
    err.hidden = true;
    if (!fields.text.trim() && !ui.newFiles.length) {
      err.textContent = "Write a task for the agent, or attach files.";
      err.hidden = false;
      return;
    }
    btn.disabled = true;
    try {
      const r = ui.newFiles.length
        ? await upload("/api/agents", formWith(fields, ui.newFiles), pct => { label.textContent = pct < 1 ? `Uploading ${Math.floor(pct * 100)}%…` : "Starting…"; })
        : await api("/api/agents", fields);
      clearFiles("newFiles");
      location.href = `/agents/${r.id}`;
    } catch (x) { err.textContent = x.message; err.hidden = false; btn.disabled = false; label.textContent = "Start agent"; }
  }
});

document.addEventListener("keydown", e => {
  // With a physical keyboard Enter sends and Shift+Enter makes a new line; on a phone Enter is a new
  // line and you tap Send. Any fine pointer or hover means a mouse/trackpad, so likely a keyboard too.
  if (e.target.id === "m" && e.key === "Enter" && !e.shiftKey && !e.isComposing && matchMedia("(any-hover: hover), (any-pointer: fine)").matches) {
    e.preventDefault();
    $("#composer").requestSubmit();
  }
  if (e.target.name === "text" && e.key === "Enter" && (e.ctrlKey || e.metaKey)) $("#new-form").requestSubmit();
});
document.addEventListener("input", e => {
  if (e.target.id === "m") { fitComposer(e.target); if (AGENT_ID) saveDraft(AGENT_ID, e.target.value); }
});

// Paste screenshots straight into the reply box or the task; drop files on the chat or the form.
document.addEventListener("paste", e => {
  const list = e.target.id === "m" ? "attach" : e.target.closest && e.target.closest("#new-form") ? "newFiles" : null;
  const files = e.clipboardData && [...e.clipboardData.files];
  if (!list || !files || !files.length) return;
  e.preventDefault();
  addFiles(list, files);
});
const dropTarget = e => e.target.closest && (e.target.closest(".chat") ? "attach" : e.target.closest("#new-form") ? "newFiles" : null);
let dragDepth = 0;
for (const type of ["dragenter", "dragover", "dragleave", "drop"]) {
  document.addEventListener(type, e => {
    if (!e.dataTransfer || ![...e.dataTransfer.types].includes("Files")) return;
    const list = dropTarget(e);
    if (type === "dragenter") dragDepth++;
    if (type === "dragleave") dragDepth--;
    if (type === "drop") dragDepth = 0;
    document.body.classList.toggle("dragging", dragDepth > 0 && type !== "drop");
    e.preventDefault();   // a file dropped anywhere else would open in place of the dashboard
    if (type === "drop" && list) addFiles(list, e.dataTransfer.files);
  });
}

function renderTheme() {
  const cur = localStorage.getItem("agentd-theme") || "auto";
  $("#theme").innerHTML = `${icon(cur === "auto" ? "auto" : cur === "dark" ? "moon" : "sun", "i i-sm")}<span>Theme: ${cur}</span>`;
}
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", ev => {
  if ((localStorage.getItem("agentd-theme") || "auto") === "auto") document.documentElement.dataset.theme = ev.matches ? "dark" : "light";
});

renderTopbar();
renderTheme();

// Desktop: the left rail can be hidden; the choice is kept in this browser.
function renderRailToggle() {
  const hidden = document.documentElement.dataset.rail === "hidden";
  const b = $("#rail-toggle");
  b.innerHTML = icon("sidebar");
  b.setAttribute("aria-label", hidden ? "Show sidebar" : "Hide sidebar");
  b.title = hidden ? "Show sidebar" : "Hide sidebar";
  b.setAttribute("aria-expanded", !hidden);
}
renderRailToggle();
$("#rail-toggle").addEventListener("click", () => {
  const hide = document.documentElement.dataset.rail !== "hidden";
  if (hide) document.documentElement.dataset.rail = "hidden"; else delete document.documentElement.dataset.rail;
  try { localStorage.setItem("agentd-rail", hide ? "hidden" : "shown"); } catch { /* full */ }
  renderRailToggle();
});
if (!AGENT_ID && cache.overview) {   // last known state while the first fetch is in flight
  ui.data = cache.overview;
  renderShell(cache.overview);
  rerender();
}
refresh();
loadModels();

// ------------------------------------------------------------------ file viewer
// Files open over the page, not in a new tab: in the home-screen app a new tab has no way back.

function openViewer(href, name, kind) {
  let dlg = $("#viewer");
  if (!dlg) {
    dlg = document.createElement("dialog");
    dlg.id = "viewer";
    dlg.className = "viewer";
    document.body.append(dlg);
    dlg.addEventListener("click", e => { if (e.target === dlg || e.target.classList.contains("viewer-body")) closeViewer(); });
    dlg.addEventListener("close", () => {
      dlg.innerHTML = "";
      if (history.state && history.state.viewer) { ui.ignorePop = true; history.back(); }
    });
  }
  const pdf = kind === "file" && /\.pdf$/i.test(name);
  const body = kind === "image" ? `<img src="${href}" alt="${esc(name)}">`
    : pdf ? `<div class="pdf-pages"><p class="pdf-note">Loading…</p></div>`
    : `<iframe src="${href}" title="${esc(name)}"></iframe>`;
  dlg.innerHTML = `<div class="viewer-bar"><span>${esc(name)}</span>
      <a class="icon-btn" href="${href}" download="${esc(name)}" aria-label="Download">${icon("download")}</a>
      <button class="icon-btn" type="button" id="viewer-close" aria-label="Close">${icon("close")}</button></div>
    <div class="viewer-body${pdf ? " pdf" : ""}" tabindex="-1" autofocus>${body}</div>`;
  dlg.showModal();
  history.pushState({ viewer: true }, "");   // so the back gesture closes it
  if (pdf) showPdf(dlg.querySelector(".pdf-pages"), new URL(href, location.href).searchParams.get("path"));
}

// Phone browsers only draw a PDF's first page in an iframe, so pages come from the server as images.
async function showPdf(box, path) {
  const q = `path=${encodeURIComponent(path)}`;
  try {
    const { pages } = await api(`/api/pdf/pages?${q}`);
    if (!pages.length) throw new Error("no pages");
    box.innerHTML = pages.map(([w, h], i) =>
      `<img src="/api/pdf/page?${q}&page=${i + 1}" width="${w}" height="${h}" loading="lazy" alt="Page ${i + 1}">`).join("")
      + (pages.length > 1 ? `<p class="pdf-note">${pages.length} pages</p>` : "");
  } catch (err) {
    box.innerHTML = `<p class="pdf-note">Couldn’t render this PDF (${esc(err.message || String(err))}). Use the download button.</p>`;
  }
}
function closeViewer() {
  const dlg = $("#viewer");
  if (dlg && dlg.open) dlg.close();
}
addEventListener("popstate", () => {
  const dlg = $("#viewer");
  if (dlg && dlg.open) { history.replaceState(null, ""); dlg.close(); }
});
document.addEventListener("click", e => {
  const a = e.target.closest("a[data-view]");
  if (a && "recheck" in a.dataset) {
    e.preventDefault();
    fetch(a.getAttribute("href"), { method: "HEAD" }).then(r => {
      if (!r.ok) return;
      a.classList.remove("gone");
      delete a.dataset.recheck;
      a.querySelector("span").innerHTML = `<b>${esc(a.dataset.name || "")}</b>`;
      openViewer(a.getAttribute("href"), a.dataset.name || "", a.dataset.view);
    }).catch(() => {});
  } else if (a) {
    e.preventDefault();
    openViewer(a.getAttribute("href"), a.dataset.name || "", a.dataset.view);
  } else if (e.target.closest("#viewer-close")) closeViewer();
}, true);
