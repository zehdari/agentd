// Voice: dictation in the message box (the mic), and voice mode, where you talk with an agent
// hands-free: it listens until you stop talking, sends what you said (with a note telling the agent
// it's a spoken conversation), reads the reply aloud, then listens again.
// Speech goes both ways through the agentd-tts service: Whisper to listen, Higgs or Kokoro to speak.
// Uses app.js's globals: $, api, icon, esc, player, audioMode, SILENCE, cache, ui, AGENT_ID, navigate, refresh.

// ------------------------------------------------------------------ microphone

let actx = null;
// One AudioContext for metering, started from a tap: iOS keeps contexts made outside one suspended.
function audioCtx() {
  actx = actx || new (window.AudioContext || window.webkitAudioContext)();
  if (actx.state !== "running") actx.resume().catch(() => {});
  return actx;
}

function micError(err) {
  if (!navigator.mediaDevices || !window.MediaRecorder) return "This browser can’t record here. Voice needs the https:// address (agentd.zehdari.com).";
  if (err && err.name === "NotAllowedError") return "Microphone access is blocked. Allow it for this site in the browser’s settings.";
  if (err && err.name === "NotFoundError") return "No microphone found.";
  return `Couldn’t start the microphone: ${err && err.message || err}`;
}

// Start recording. level() is the input loudness (RMS); stop() resolves with the recording and
// releases the mic, so replies play through the speaker rather than the iPhone's earpiece.
async function openMic() {
  if (!navigator.mediaDevices || !window.MediaRecorder) throw new Error("unsupported");
  const stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
  const ctx = audioCtx();
  const source = ctx.createMediaStreamSource(stream);
  const analyser = ctx.createAnalyser();
  analyser.fftSize = 1024;
  source.connect(analyser);
  const buf = new Float32Array(analyser.fftSize);
  const rec = new MediaRecorder(stream), chunks = [];
  rec.ondataavailable = e => { if (e.data.size) chunks.push(e.data); };
  rec.start(250);
  return {
    level() {
      analyser.getFloatTimeDomainData(buf);
      let sum = 0;
      for (const x of buf) sum += x * x;
      return Math.sqrt(sum / buf.length);
    },
    stop() {
      return new Promise(resolve => {
        const done = () => {
          stream.getTracks().forEach(t => t.stop());
          source.disconnect();
          resolve(new Blob(chunks, { type: rec.mimeType || "audio/webm" }));
        };
        if (rec.state === "inactive") done();
        else { rec.onstop = done; rec.stop(); }
      });
    },
  };
}

async function transcribe(blob) {
  const res = await fetch("/api/stt", { method: "POST", headers: { "X-Agentd": "1", "Content-Type": blob.type || "application/octet-stream" }, body: blob });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return (data.text || "").trim();
}

// ------------------------------------------------------------------ dictation

let dictation = null;   // {mic, raf} while recording, "busy" while transcribing

async function toggleDictation(btn) {
  if (dictation === "busy") return;
  if (!dictation) {
    audioCtx();
    try {
      dictation = { mic: await openMic() };
    } catch (err) {
      dictation = null;
      alert(micError(err));
      return;
    }
    btn.classList.add("rec");
    btn.setAttribute("aria-label", "Stop dictating");
    const meter = () => {   // the button's ring follows your voice
      if (!dictation || dictation === "busy") return;
      btn.style.setProperty("--lvl", Math.min(1, dictation.mic.level() * 12).toFixed(2));
      dictation.raf = requestAnimationFrame(meter);
    };
    meter();
    return;
  }
  const { mic, raf } = dictation;
  cancelAnimationFrame(raf);
  dictation = "busy";
  btn.classList.remove("rec");
  btn.classList.add("busy");
  btn.setAttribute("aria-label", "Transcribing");
  try {
    const text = await transcribe(await mic.stop());
    const ta = $("#m");
    if (text && ta) {
      ta.value = ta.value.trim() ? `${ta.value.replace(/\s+$/, "")} ${text}` : text;
      ta.dispatchEvent(new Event("input", { bubbles: true }));   // saves the draft and grows the box
      ta.focus();
    }
  } catch (err) {
    alert(`Couldn’t transcribe that: ${err.message}`);
  } finally {
    dictation = null;
    btn.classList.remove("busy");
    btn.setAttribute("aria-label", "Dictate");
    btn.style.removeProperty("--lvl");
  }
}

// The send button opens voice mode while the message box is empty, like the phone apps.
function syncSend() {
  const btn = $("#composer .send"), ta = $("#m");
  if (!btn || !ta) return;
  const talk = !ta.value.trim() && !ui.attach.length;
  if (btn.classList.contains("talk") === talk && btn.firstChild) return;
  btn.classList.toggle("talk", talk);
  btn.type = talk ? "button" : "submit";
  btn.setAttribute("aria-label", talk ? "Talk (voice mode)" : "Send");
  btn.title = talk ? "Talk (voice mode)" : "";
  btn.innerHTML = icon(talk ? "talk" : "send");
}
window.syncSend = syncSend;

// ------------------------------------------------------------------ voice mode

const vm = {
  on: false, state: "off", id: null, newOpts: null,
  mark: 0,               // replies from this log position on get spoken
  spoken: new Set(),     // log positions already spoken (or skipped)
  queue: [], speaking: false, stopPlay: null,
  mic: null, vad: null, heard: false,
  you: "", reply: "", note: "", doing: "", lock: null,
};
const vmFast = () => localStorage.getItem("agentd-vm-fast") !== "off";   // Kokoro by default: Higgs takes seconds a sentence
window.voiceActive = () => vm.on;

const VM_STATES = {
  listening: ["Listening", () => vm.heard ? "Tap when you’re done, or just stop talking" : "Go ahead"],
  transcribing: ["Got it", () => ""],
  waiting: ["Thinking", () => vm.doing || ""],
  speaking: ["Speaking", () => "Tap to interrupt"],
  paused: ["Paused", () => "Tap to talk"],
};

function vmEl() {
  let el = $("#vm");
  if (el) return el;
  el = document.createElement("div");
  el.id = "vm";
  el.className = "vm";
  el.hidden = true;
  el.setAttribute("role", "dialog");
  el.setAttribute("aria-label", "Voice mode");
  el.innerHTML = `
    <div class="vm-top"><span class="vm-title"></span>
      <button class="icon-btn vm-close" type="button" id="vm-close" aria-label="End voice mode">${icon("close")}</button></div>
    <div class="vm-center">
      <button class="vm-orb" type="button" id="vm-orb" aria-label="Talk"><span class="vm-core"></span></button>
      <p class="vm-state" aria-live="polite"></p>
      <p class="vm-sub"></p>
    </div>
    <div class="vm-captions"><p class="vm-you"></p><p class="vm-reply"></p></div>
    <div class="vm-bottom">
      <label class="vm-fast"><input type="checkbox" id="vm-fast"> Fast voice</label>
      <button class="btn sm" type="button" id="vm-end">End</button>
    </div>`;
  document.body.appendChild(el);
  return el;
}

function vmRender() {
  const el = $("#vm");
  if (!el || !vm.on) return;
  const [label, sub] = VM_STATES[vm.state] || ["", () => ""];
  el.dataset.state = vm.state;
  const d = vm.id && cache.agents[vm.id];
  $(".vm-title", el).textContent = d ? d.title.replace(/^🎙️ /, "") : "New chat";
  $(".vm-state", el).textContent = label;
  $(".vm-sub", el).textContent = vm.note || sub();
  $(".vm-you", el).textContent = vm.you ? `“${vm.you}”` : "";
  $(".vm-reply", el).textContent = vm.reply.length > 320 ? `${vm.reply.slice(0, 320)}…` : vm.reply;
  $("#vm-fast", el).checked = vmFast();
  $("#vm-orb", el).setAttribute("aria-label", { listening: "Done talking", speaking: "Interrupt", paused: "Talk" }[vm.state] || "Talk");
}

function setState(state, note = "") {
  vm.state = state;
  vm.note = note;
  vmRender();
}

function vmOpen() {
  audioCtx();
  // iOS only lets audio start later, outside a tap, on an element that has played during one.
  if (player.paused) { audioMode("playback"); player.src = SILENCE; player.play().then(() => player.pause()).catch(() => {}); }
  const form = $("#new-form");
  vm.newOpts = !AGENT_ID && form ? Object.fromEntries([...new FormData(form)].filter(([k, v]) => k !== "text" && v)) : null;
  Object.assign(vm, { on: true, id: AGENT_ID, queue: [], spoken: new Set(), you: "", reply: "", doing: "" });
  const d = AGENT_ID && cache.agents[AGENT_ID];
  vm.mark = d ? d.total_entries : 0;
  vmEl().hidden = false;
  document.body.classList.add("vm-open");
  keepAwake();
  if (d && d.status === "running") setState("waiting"); else listen();
  refresh();   // poll faster from now on
}

function vmClose() {
  vm.on = false;
  clearInterval(vm.vad);
  if (vm.mic) { vm.mic.stop(); vm.mic = null; }
  vm.queue = [];
  if (vm.stopPlay) vm.stopPlay();
  vm.state = "off";
  if (vm.lock) { vm.lock.release().catch(() => {}); vm.lock = null; }
  audioMode("auto");
  const el = $("#vm");
  if (el) el.hidden = true;
  document.body.classList.remove("vm-open");
}

function keepAwake() {   // the phone sleeping would cut the conversation off
  if (!vm.on || vm.lock || !navigator.wakeLock) return;
  navigator.wakeLock.request("screen").then(l => {
    vm.lock = l;
    l.addEventListener("release", () => { vm.lock = null; });
  }).catch(() => {});
}
document.addEventListener("visibilitychange", () => { if (!document.hidden) keepAwake(); });

// Listen until you've said something and then gone quiet for a moment. The level is compared with
// the room's noise, measured in the first moments.
async function listen() {
  if (!vm.on) return;
  stopSpeaking();
  vm.heard = false;
  setState("listening");
  try {
    audioMode("play-and-record");
    vm.mic = await openMic();
  } catch (err) {
    setState("paused", micError(err));
    return;
  }
  if (!vm.on || vm.state !== "listening") { const m = vm.mic; vm.mic = null; if (m) m.stop(); return; }
  const t0 = performance.now(), orb = $("#vm-orb");
  let floor = 0, samples = 0, voiced = 0, last = 0, began = 0;
  clearInterval(vm.vad);
  vm.vad = setInterval(() => {
    if (!vm.mic) return;
    const now = performance.now(), lvl = vm.mic.level();
    if (orb) orb.style.setProperty("--lvl", Math.min(1, lvl * 12).toFixed(2));
    if (now - t0 < 350) { floor += lvl; samples++; return; }
    const threshold = Math.min(0.05, Math.max(0.012, floor / Math.max(samples, 1) * 2.5));
    if (lvl > threshold) {
      voiced += 50;
      last = now;
      if (voiced >= 200 && !vm.heard) { vm.heard = true; began = now; vmRender(); }
    } else voiced = Math.max(0, voiced - 50);
    if (vm.heard && (now - last > 1500 || now - began > 120000)) finishListening(true);
    else if (!vm.heard && now - t0 > 60000) finishListening(false);   // nothing said for a minute: pause
  }, 50);
}

async function finishListening(send) {
  clearInterval(vm.vad);
  const mic = vm.mic;
  vm.mic = null;
  if (!mic) return;
  $("#vm-orb")?.style.removeProperty("--lvl");
  if (!send) { mic.stop(); setState("paused"); return; }
  setState("transcribing");
  let text;
  try {
    text = await transcribe(await mic.stop());
  } catch (err) {
    if (vm.on) setState("paused", `Couldn’t transcribe that: ${err.message}`);
    return;
  }
  if (!vm.on) return;
  if (!text) { listen(); return; }
  vm.you = text;
  vm.reply = "";
  setState("waiting");
  try {
    if (vm.id) {
      await api(`/api/agents/${vm.id}/message`, { text, voice: true });
    } else {   // started from the new agent form: the first thing you say starts the chat
      const r = await api("/api/agents", { ...(vm.newOpts || {}), text, voice: true });
      vm.id = r.id;
      vm.mark = 0;
      navigate(`/agents/${r.id}`);
      return;
    }
  } catch (err) {
    setState("paused", `Couldn’t send that: ${err.message}`);
    return;
  }
  refresh();
}

// After every poll of the chat: queue new replies to be spoken; once the agent is done and has
// had its say, listen again.
window.voiceUpdate = d => {
  if (!vm.on || String(d.id) !== String(vm.id)) return;
  d.entries.forEach((e, i) => {
    const k = d.entries_from + i;
    if (e.kind === "claude" && k >= vm.mark && !vm.spoken.has(k)) {
      vm.spoken.add(k);
      vm.queue.push(e.text);
    }
  });
  const running = d.status === "running";
  vm.doing = d.stalled ? "No reply for a while. It may be stuck." : running ? (d.now_doing || "").split("\n")[0].slice(0, 90) : "";
  if (vm.state === "waiting" && vm.queue.length) speakNext();
  else if (vm.state === "waiting" && !running && !(d.outbox || []).length) {
    if (d.status === "failed") vm.note = "The run failed.";
    listen();
  } else vmRender();
};

// Replies are spoken a sentence or two at a time, fetching the next part while one plays, so
// the slower Higgs voice can start talking before it has made the whole reply.
async function speakNext() {
  if (vm.speaking) return;
  vm.speaking = true;
  setState("speaking");
  while (vm.on && vm.state === "speaking" && vm.queue.length) {
    const text = speakable(vm.queue.shift());
    if (!text) continue;
    vm.reply = text;
    vmRender();
    const parts = speechParts(text);
    let next = say(parts[0]);
    for (let i = 0; i < parts.length && vm.state === "speaking"; i++) {
      const cur = next;
      next = i + 1 < parts.length ? say(parts[i + 1]) : null;
      const url = await cur.catch(() => null);
      if (url && vm.state === "speaking") await play(url);
      if (url) URL.revokeObjectURL(url);
    }
  }
  vm.speaking = false;
  if (vm.on && vm.state === "speaking") {
    vm.state = "waiting";
    if (cache.agents[vm.id]) window.voiceUpdate(cache.agents[vm.id]);
  }
}

async function say(text) {
  const res = await fetch("/api/speak", {
    method: "POST", headers: { "Content-Type": "application/json", "X-Agentd": "1" },
    body: JSON.stringify({ text, fast: vmFast() }),
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return URL.createObjectURL(await res.blob());
}

function play(url) {
  return new Promise(resolve => {
    const done = () => {
      player.removeEventListener("ended", done);
      player.removeEventListener("error", done);
      vm.stopPlay = null;
      resolve();
    };
    vm.stopPlay = () => { player.pause(); done(); };
    player.addEventListener("ended", done);
    player.addEventListener("error", done);
    audioMode("playback");
    player.src = url;
    player.play().catch(done);
  });
}

function stopSpeaking() {
  vm.queue = [];
  if (vm.stopPlay) vm.stopPlay();
}

// What a reply sounds like read out: no markdown, code or links, which the agent was asked to avoid anyway.
function speakable(md) {
  return md
    .replace(/```[\s\S]*?```/g, " (there's code in the chat) ")
    .replace(/`([^`]*)`/g, "$1")
    .replace(/!\[[^\]]*\]\([^)]*\)/g, "")
    .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
    .replace(/https?:\/\/\S+/g, "a link")
    .replace(/^\s*\|.*\|\s*$/gm, "")
    .replace(/^\s{0,3}#{1,6}\s+/gm, "")
    .replace(/^\s*(?:[-*+]|\d+[.)])\s+/gm, "")
    .replace(/(\*\*|__|\*|_|~~)(\S(?:.*?\S)?)\1/g, "$2")
    .replace(/\s*\n+\s*/g, ". ").replace(/([.!?:;,])\.\s/g, "$1 ").replace(/\s+/g, " ")
    .trim();
}

// A short first part so speech starts quickly, then a couple of sentences at a time.
function speechParts(text) {
  const sentences = text.match(/[^.!?]+(?:[.!?]+["”’)]*|$)\s*/g) || [text];
  const parts = [];
  let cur = "";
  for (const s of sentences) {
    cur += s;
    if (cur.length >= (parts.length ? 180 : 40)) { parts.push(cur.trim()); cur = ""; }
  }
  if (cur.trim()) parts.push(cur.trim());
  return parts.length ? parts : [text];
}

function orbTap() {
  audioCtx();
  if (vm.state === "listening") { if (vm.heard) finishListening(true); else finishListening(false); }
  else if (vm.state === "speaking" || vm.state === "waiting" || vm.state === "paused") {
    // Interrupting skips what's left of the reply; talking while it works steers it.
    if (vm.state === "speaking" && cache.agents[vm.id]) {
      const d = cache.agents[vm.id];
      d.entries.forEach((e, i) => { if (e.kind === "claude") vm.spoken.add(d.entries_from + i); });
    }
    listen();
  }
}

// ------------------------------------------------------------------ wiring

document.addEventListener("click", e => {
  const t = e.target.closest("button, input");
  if (!t) return;
  if (t.id === "mic-btn") toggleDictation(t);
  else if (t.matches("#composer .send.talk") || t.id === "new-talk") { e.preventDefault(); vmOpen(); }
  else if (t.id === "vm-orb") orbTap();
  else if (t.id === "vm-close" || t.id === "vm-end") vmClose();
  else if (t.id === "vm-fast") localStorage.setItem("agentd-vm-fast", t.checked ? "on" : "off");
});
document.addEventListener("input", e => { if (e.target.id === "m") syncSend(); });
document.addEventListener("keydown", e => { if (e.key === "Escape" && vm.on) vmClose(); });
