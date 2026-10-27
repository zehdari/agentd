"""Web dashboard: every agent, machine health and long jobs on one screen.

Listens on localhost at AGENTD_WEB_PORT; Caddy serves it to the tailnet as https://agentd.zehdari.com
(see /etc/caddy/Caddyfile). Requests from other tailnet devices are only accepted when
`tailscale whois` says they belong to this machine's own Tailscale user."""
import asyncio
import glob
import hashlib
import ipaddress
import json
import logging
import os
import re
import shlex
import shutil
import time
from collections import deque
from datetime import datetime
from pathlib import Path

from aiohttp import ClientError, ClientSession, ClientTimeout, web

from . import config, models, push
from .format import ATTACHED, short, split_attached

log = logging.getLogger(__name__)

STATIC = Path(__file__).parent / "web"
WINDOW = 2 * 3600            # the overview's tracks all share this window
WAITING_FOR = 48 * 3600      # finished agents count as waiting on you for this long, until seen
SAMPLE_EVERY = 30
TAILNET = [ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fd7a:115c:a1e0::/48")]
UNIT_NAME = re.compile(r"^[\w@.:-]+$")
INBOX = config.STATE_DIR / "inbox"     # files you attach, from Discord or here
MAX_UPLOAD = int(os.environ.get("AGENTD_WEB_MAX_UPLOAD_MB", "2048")) * 1024 * 1024   # per message
SENT_DIR = config.STATE_DIR / "sent"   # <thread>.jsonl: what agentd-send actually delivered
LOGS = config.STATE_DIR / "logs"       # <thread>.jsonl: each agent's raw stream-json
CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"   # Claude Code's own session transcripts
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
PDF_CACHE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "agentd" / "pdf"
# Remote desktop (the Desktop page): noVNC in the browser, bridged here so the dashboard's own check guards it.
NOVNC = Path(os.environ.get("AGENTD_NOVNC", "/usr/share/novnc"))
DESKTOP_SOCK = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / "agentd-desktop.sock"
# The GPU switch on the Desktop page: `winvm gpu|nogpu` (~/winvm) moves the RTX 3090 between Linux and the VM.
WINVM = os.environ.get("AGENTD_WINVM", str(Path.home() / ".local" / "bin" / "winvm"))
SWITCH_UNIT = "winvm-switch"
WINDOWS_VNC = os.environ.get("AGENTD_WINDOWS_VNC", "http://127.0.0.1:8006/websockify")   # dockurr/windows' websockify
PDF_WIDTH = 1400                       # px; phones zoom in from here
PDF_MAX_PAGES = 300


def sent_since() -> float:
    """When agentd started recording sends. Older sends are only known from the command text."""
    marker = SENT_DIR / ".since"
    try:
        return float(marker.read_text())
    except (OSError, ValueError):
        SENT_DIR.mkdir(parents=True, exist_ok=True)
        now = time.time()
        marker.write_text(str(now))
        return now


SENT_SINCE = sent_since()


def record_sent(thread_id, paths: list[str], caption: str) -> None:
    """Called by agentd-send's back end once the files are known to exist."""
    if not thread_id or not paths:
        return
    entry = {"t": time.time(), "paths": [str(Path(p).resolve()) for p in paths], "caption": caption}
    with open(SENT_DIR / f"{int(thread_id)}.jsonl", "a") as f:
        f.write(json.dumps(entry) + "\n")


async def run(*cmd: str, timeout: float = 5) -> str:
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
        return out.decode(errors="replace")
    except (OSError, asyncio.TimeoutError):
        return ""


def inbox_name(path: str) -> str:
    """The name a file had when you attached it, without the inbox's time prefix."""
    return re.sub(r"^\d{9,}(\.\d+)?-", "", Path(path).name)


def tilde(path: str) -> str:
    home = str(Path.home())
    return "~" + path[len(home):] if path == home or path.startswith(home + "/") else path


def parse_ts(value) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text")
    return ""


def tool_detail(name: str, inp: dict) -> str:
    inp = inp or {}
    for key in ("command", "file_path", "notebook_path", "pattern", "url", "query", "description", "prompt"):
        if inp.get(key):
            return str(inp[key])
    return json.dumps(inp, ensure_ascii=False) if inp else ""


SHELL_OPS = {"&&", "||", ";", "|", "&", "(", ")", ";;", "|&"}
COMMAND_START = SHELL_OPS | {"sudo", "exec", "nohup", "then", "else", "do", "time"}


def commands(command: str):
    """Yield the word list of each simple command in a shell snippet, line by line."""
    for line in command.splitlines():
        try:
            lex = shlex.shlex(line, posix=True, punctuation_chars=True)
            lex.whitespace_split = True
            words = list(lex)
        except ValueError:
            continue
        cur: list[str] = []
        for w in words + [";"]:
            if w in SHELL_OPS:
                if cur:
                    yield cur
                cur = []
            else:
                cur.append(w)


def invocation(words: list[str], program: str) -> list[str] | None:
    """The arguments if this simple command runs `program` (allowing sudo/env-style prefixes)."""
    for i, w in enumerate(words):
        if Path(w).name == program:
            return words[i + 1:]
        if w not in COMMAND_START and "=" not in w:
            return None
    return None


def sent_files(command: str, cwd: str) -> tuple[list[str], str]:
    """Paths and caption from `agentd-send` calls inside a Bash command."""
    paths, caption, base = [], "", cwd
    for words in commands(command):
        if words and words[0] == "cd" and len(words) > 1:
            base = str(Path(base, os.path.expanduser(words[1])))
            continue
        args = invocation(words, "agentd-send")
        if args is None:
            continue
        skip = False
        for i, w in enumerate(args):
            if skip:
                skip = False
            elif w in ("-m", "--message"):
                caption = args[i + 1] if i + 1 < len(args) else ""
                skip = True
            elif not w.startswith("-") and ("." in Path(w).name or "/" in w):
                full = str(Path(base, os.path.expanduser(w)))
                matches = sorted(glob.glob(full)) if glob.has_magic(full) else [full]
                paths += [str(Path(m).resolve()) for m in matches]
    return paths, caption


def started_units(command: str) -> list[str]:
    """Unit names from `systemd-run --unit=NAME` calls (not from text that merely mentions one)."""
    units = []
    for words in commands(command):
        args = invocation(words, "systemd-run")
        for i, w in enumerate(args or []):
            if w.startswith("--unit="):
                units.append(w.split("=", 1)[1])
            elif w in ("--unit", "-u") and i + 1 < len(args):
                units.append(args[i + 1])
    return [u.removesuffix(".service") for u in units if UNIT_NAME.match(u)]


# ---------------------------------------------------------------- per-agent log index

PAGE = 150   # conversation entries per load; older ones come in as the page scrolls up


def entry_list(entries: list[dict]) -> list[dict]:
    return [{**e, "exists": [Path(p).is_file() for p in e["paths"]]} if e.get("paths") else e
            for e in entries]


# ---------------------------------------------------------------- PDFs as page images
# Phone browsers only draw the first page of a PDF in an iframe, so the viewer shows rendered pages.

PDF_MAX_PIXELS = 12_000_000   # iOS Safari won't decode much bigger images
PDF_SIZE = re.compile(r"^Page\s+(\d+) size:\s*([\d.]+) x ([\d.]+)", re.M)
pdf_jobs = asyncio.Semaphore(2)


async def pdf_info(path: str) -> list[tuple[float, float]]:
    """Each page's (width, height) in points."""
    out = await run("pdfinfo", "-f", "1", "-l", str(PDF_MAX_PAGES), path, timeout=20)
    return [(float(w), float(h)) for _, w, h in PDF_SIZE.findall(out)]


def pdf_scale(w: float, h: float) -> tuple[int, int]:
    x = PDF_WIDTH
    if x * x * h / w > PDF_MAX_PIXELS:
        x = int((PDF_MAX_PIXELS * w / h) ** 0.5)
    return x, max(1, round(x * h / w))


async def pdf_page(path: str, n: int) -> Path | None:
    st = Path(path).stat()
    key = hashlib.sha1(f"{path}\0{st.st_mtime_ns}\0{st.st_size}".encode()).hexdigest()[:20]
    out = PDF_CACHE / f"{key}-{n}.png"
    if out.is_file():
        return out
    sizes = await pdf_info(path)
    if not 1 <= n <= len(sizes):
        return None
    x, y = pdf_scale(*sizes[n - 1])
    PDF_CACHE.mkdir(parents=True, exist_ok=True)
    tmp = PDF_CACHE / f".{key}-{n}-{os.getpid()}"
    async with pdf_jobs:
        await run("pdftoppm", "-png", "-f", str(n), "-l", str(n), "-singlefile",
                  "-scale-to-x", str(x), "-scale-to-y", str(y), path, str(tmp), timeout=90)
    try:
        Path(f"{tmp}.png").replace(out)
    except OSError:
        return None
    return out


class AgentLog:
    """Incrementally parsed stream-json log of one agent thread."""

    def __init__(self, path: Path, cwd: str):
        self.path, self.cwd = path, cwd
        self.offset = 0
        self.sent_path = SENT_DIR / path.name
        self.sent_offset = 0
        self.unsent: deque = deque()     # agentd-send records not yet placed among the entries
        self.entries: list[dict] = []
        self.ticks: list[tuple[float, str]] = []
        self.steers: list[float] = []
        self.segs: list[list] = []      # [start, end, open]
        self.tools: dict[str, int] = {}
        self.files: list[dict] = []
        self.units: dict[str, float] = {}
        self.last_t: float | None = None
        self.last_reply: tuple[float, str] | None = None
        self.last_tool: str = ""
        self.model: str | None = None              # what the last run actually used, e.g. claude-opus-5-5
        self.rate: tuple[float, dict] | None = None   # latest plan-limit reading (rate_limit_event)
        self.costs: list[list] = []                   # [time, API-equivalent $] per result
        self.subagents: dict[str, dict] = {}          # Agent tool_use id -> what it's doing (Details → Subagents)
        self.tokens: dict[str, dict] = {}             # model -> token totals for this chat, subagents included
        self._tok_last: dict[str, dict] = {}          # the last running totals the CLI reported, per model
        self.context: tuple[int, int] | None = None   # (tokens in the latest request, the model's context window)
        self._last_cost = 0.0
        self._new_run = True

    def refresh(self) -> "AgentLog":
        try:
            size = self.path.stat().st_size
        except OSError:
            return self
        if size < self.offset:
            self.__init__(self.path, self.cwd)
        self._read_sent()
        if size != self.offset:
            with open(self.path, "rb") as f:
                f.seek(self.offset)
                data = f.read()
            end = data.rfind(b"\n") + 1
            self.offset += end
            for raw in data[:end].splitlines():
                try:
                    ev = json.loads(raw)
                    self._place_sent(parse_ts(ev.get("timestamp")))
                    self._event(ev)
                except (ValueError, AttributeError, TypeError):
                    continue
        self._place_sent(None)   # anything recorded so far happened before whatever the log says next
        return self

    def _read_sent(self) -> None:
        try:
            if self.sent_path.stat().st_size <= self.sent_offset:
                return
            with open(self.sent_path, "rb") as f:
                f.seek(self.sent_offset)
                data = f.read()
        except OSError:
            return
        end = data.rfind(b"\n") + 1
        self.sent_offset += end
        for raw in data[:end].splitlines():
            try:
                rec = json.loads(raw)
                self.unsent.append((float(rec["t"]), list(rec["paths"]), str(rec.get("caption") or "")))
            except (ValueError, KeyError, TypeError):
                continue

    def _place_sent(self, before: float | None) -> None:
        """Add the sends recorded before time `before` (all of them for None) as file entries."""
        while self.unsent and (before is None or self.unsent[0][0] <= before):
            t, paths, caption = self.unsent.popleft()
            self.entries.append({"kind": "file", "t": t, "paths": paths, "caption": caption})
            self.files += [{"path": p, "t": t, "caption": caption} for p in paths]

    def _at(self, t: float | None) -> float | None:
        if t is None:
            return self.last_t
        self.last_t = t
        if self._new_run or not self.segs or not self.segs[-1][2]:
            self.segs.append([t, t, True])
            self._new_run = False
        else:
            self.segs[-1][1] = t
        return t

    def _count_tokens(self, ev: dict) -> None:
        """Add a run's tokens. Claude Code (modelUsage, per model) and Codex (usage) both report running
        totals for the session, so count what they grew by; a total that went down is a new session."""
        if ev.get("modelUsage"):
            reports = {m: {"in": u.get("inputTokens", 0), "cache_read": u.get("cacheReadInputTokens", 0),
                           "cache_write": u.get("cacheCreationInputTokens", 0), "out": u.get("outputTokens", 0),
                           "reasoning": u.get("thinkingTokens", 0)} for m, u in ev["modelUsage"].items()}
            main = ev["modelUsage"].get(self.model or "") or {}
            if main.get("contextWindow") and self.context:
                self.context = (self.context[0], main["contextWindow"])
        elif ev.get("usage") and models.is_codex(self.model):
            u = ev["usage"]
            cached = u.get("cached_input_tokens") or 0
            reports = {self.model: {"in": (u.get("input_tokens") or 0) - cached, "cache_read": cached,
                                    "cache_write": u.get("cache_write_input_tokens") or 0,
                                    "out": u.get("output_tokens") or 0, "reasoning": u.get("reasoning_output_tokens") or 0}}
        else:
            return
        for model, cur in reports.items():
            last = self._tok_last.get(model)
            grew = last is not None and all(cur[k] >= last.get(k, 0) for k in cur)
            total = self.tokens.setdefault(model, dict.fromkeys(cur, 0))
            for k, v in cur.items():
                total[k] += v - last[k] if grew else v
            self._tok_last[model] = cur

    def _subagent_task(self, ev: dict) -> None:
        """Claude Code's task events: a subagent's progress, and its status and report when it ends."""
        sub = self.subagents.get(ev.get("tool_use_id") or "") or next(
            (s for s in self.subagents.values() if s["agent_id"] and s["agent_id"] == ev.get("task_id")), None)
        if sub is None:
            return
        sub["agent_id"] = sub["agent_id"] or ev.get("task_id")
        if ev.get("subtype") == "task_progress":
            usage = ev.get("usage") or {}
            sub["progress"] = ev.get("description") or sub["progress"]
            sub["tools"] = max(sub["tools"], usage.get("tool_uses") or 0)
            return
        status = ev.get("status") or (ev.get("patch") or {}).get("status")
        if status and status != "running":
            sub["status"] = {"killed": "stopped", "error": "failed"}.get(status, status)
            sub["end"] = sub["end"] or ((ev.get("patch") or {}).get("end_time") or 0) / 1000 or self.last_t
        if ev.get("summary"):
            sub["summary"] = short(ev["summary"], 1500)

    def _event(self, ev: dict) -> None:
        kind = ev.get("type")
        if kind == "system" and ev.get("subtype") == "init":
            self.model = ev.get("model") or self.model
            for sub in self.subagents.values():
                if sub["status"] == "running":
                    sub["status"] = "stopped"
            if ev.get("provider") != "codex":
                models.saw_claude(self.model)
            self._new_run = True
            return
        if kind == "system" and ev.get("subtype") in ("task_progress", "task_updated", "task_notification"):
            self._subagent_task(ev)
            return
        if kind == "rate_limit_event":
            self.rate = (self.last_t or time.time(), ev.get("rate_limit_info") or {})
            return
        if kind == "user" and ev.get("isReplay"):
            if ev.get("origin"):   # Claude Code's own notices (a background task finished), not you
                self._at(parse_ts(ev.get("timestamp")))
                return
            in_run = bool(self.segs and self.segs[-1][2] and not self._new_run)
            t = self._at(parse_ts(ev.get("timestamp")))
            text, paths = split_attached(text_of(ev.get("message", {}).get("content")).strip())
            if text or paths:
                # uuid: Claude Code's id for the message, so it can be edited into a new chat.
                # first: the chat's opening message, which can be edited for Codex agents too.
                first = not any(e["kind"] == "you" for e in self.entries)
                self.entries.append({"kind": "you", "t": t, "text": text, "steer": in_run,
                                     **({"paths": paths} if paths else {}),
                                     **({"uuid": ev["uuid"]} if ev.get("uuid") else {}), **({"first": True} if first else {})})
                if in_run and t:
                    self.steers.append(t)
            return
        if kind == "assistant":
            t = self._at(parse_ts(ev.get("timestamp")))
            nested = ev.get("parent_tool_use_id") is not None
            usage = ev.get("message", {}).get("usage")
            if usage and not nested:
                used = sum(usage.get(k) or 0 for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
                if used:
                    self.context = (used, self.context[1] if self.context else 0)
            for block in ev.get("message", {}).get("content", []) or []:
                btype = block.get("type")
                if btype == "text" and block.get("text", "").strip() and not nested:
                    self.entries.append({"kind": "claude", "t": t, "text": block["text"].strip()})
                    self.last_reply = (t, block["text"].strip())
                elif btype == "thinking" and block.get("thinking", "").strip() and not nested:
                    self.entries.append({"kind": "thinking", "t": t, "text": block["thinking"].strip()})
                elif btype == "tool_use":
                    name = block.get("name", "")
                    if name.startswith("mcp__"):
                        name = ":".join(name.split("__")[1:])
                    detail = tool_detail(name, block.get("input"))
                    self.entries.append({"kind": "tool", "t": t, "name": name, "detail": detail, "nested": nested})
                    if name in ("Agent", "Task"):
                        inp = block.get("input") or {}
                        self.subagents[block.get("id", "")] = {
                            "id": block.get("id", ""), "agent_id": None, "session": ev.get("session_id"),
                            "description": inp.get("description") or short(inp.get("prompt", ""), 80),
                            "type": inp.get("subagent_type") or "general-purpose", "model": inp.get("model"),
                            "prompt": short(inp.get("prompt", ""), 600), "background": bool(inp.get("run_in_background")),
                            "status": "running", "t": t, "end": None, "progress": "", "tools": 0, "summary": ""}
                    if (sub := self.subagents.get(ev.get("parent_tool_use_id") or "")) is not None:
                        sub["tools"] += 1
                        sub["progress"] = f"{name} {short(detail, 140)}"
                    self.tools[name] = self.tools.get(name, 0) + 1
                    self.last_tool = f"{name} {short(detail, 140)}"
                    if t:
                        self.ticks.append((t, name))
                    if name == "Bash":
                        cmd = (block.get("input") or {}).get("command", "")
                        if "agentd-send" in cmd and (t or 0) < SENT_SINCE:   # newer sends are recorded
                            paths, caption = sent_files(cmd, self.cwd)
                            if paths:
                                item = {"kind": "file", "t": t, "paths": paths, "caption": caption}
                                self.entries.append(item)
                                self.files += [{"path": p, "t": t, "caption": caption} for p in paths]
                        if "systemd-run" in cmd:
                            for unit in started_units(cmd):
                                self.units[unit] = t or time.time()
            return
        if kind == "user":
            t = self._at(parse_ts(ev.get("timestamp")))
            res = ev.get("tool_use_result")
            content = ev.get("message", {}).get("content")
            for block in content if isinstance(content, list) and isinstance(res, dict) else []:
                sub = self.subagents.get(block.get("tool_use_id") or "") if block.get("type") == "tool_result" else None
                if sub is None:
                    continue
                sub["agent_id"] = res.get("agentId") or sub["agent_id"]
                sub["model"] = res.get("resolvedModel") or sub["model"]
                if res.get("status") == "async_launched":
                    sub["background"] = True
                elif sub["status"] == "running":   # it ran in the foreground; this is its report
                    sub["status"] = "failed" if block.get("is_error") else "completed"
                    sub["end"] = t
                    sub["summary"] = short(text_of(res.get("content")) or text_of(block.get("content")), 1500)
            return
        if kind == "result":
            self._count_tokens(ev)
            cost = ev.get("total_cost_usd")
            if cost is not None:   # a running total for the whole session, even across --resume
                delta = cost - self._last_cost if cost >= self._last_cost else cost
                self._last_cost = cost
                self.costs.append([self.last_t or time.time(), delta])
            self.entries.append({"kind": "result", "t": self.last_t, "ok": not ev.get("is_error"),
                                 "turns": ev.get("num_turns", 0), "ms": ev.get("duration_ms", 0),
                                 "detail": short(ev.get("result") or "", 300) if ev.get("is_error") else ""})
            if self.segs:
                self.segs[-1][2] = False
            self._new_run = True


# ---------------------------------------------------------------- a subagent's own conversation

def subagent_entries(log_path: Path, sub: dict, limit: int = 400) -> list[dict]:
    """What one subagent said and did: its Claude Code transcript when there is one, else its
    messages in the parent's stream (parent_tool_use_id)."""
    events = []
    if sub.get("agent_id") and sub.get("session"):
        for path in glob.glob(str(CLAUDE_PROJECTS / "*" / glob.escape(sub["session"]) / "subagents"
                                  / f"agent-{glob.escape(sub['agent_id'])}.jsonl")):
            with open(path, "rb") as f:
                events = [ev for raw in f if (ev := _json(raw))]
            break
    if not events:
        needle = sub["id"].encode()
        with open(log_path, "rb") as f:
            events = [ev for raw in f if needle in raw and (ev := _json(raw)) and ev.get("parent_tool_use_id") == sub["id"]]
        events.insert(0, {"type": "user", "message": {"content": sub.get("prompt", "")}})
    out: list[dict] = []
    for ev in events:
        kind, t = ev.get("type"), parse_ts(ev.get("timestamp"))
        content = (ev.get("message") or {}).get("content")
        if kind == "user" and isinstance(content, str) and content.strip() and not any(e["kind"] == "you" for e in out):
            out.append({"kind": "you", "t": t, "text": content.strip()})
        elif kind == "assistant":
            for block in content or []:
                btype = block.get("type")
                if btype == "text" and block.get("text", "").strip():
                    out.append({"kind": "claude", "t": t, "text": block["text"].strip()})
                elif btype == "tool_use":
                    name = block.get("name", "")
                    if name.startswith("mcp__"):
                        name = ":".join(name.split("__")[1:])
                    out.append({"kind": "tool", "t": t, "name": name, "detail": tool_detail(name, block.get("input"))})
    return out[:1] + out[1:][-limit:] if len(out) > limit + 1 else out


def _json(raw: bytes) -> dict | None:
    try:
        ev = json.loads(raw)
        return ev if isinstance(ev, dict) else None
    except ValueError:
        return None


# ---------------------------------------------------------------- editing a message into a new chat

def transcript_parent(session_id: str, uuid: str) -> tuple[bool, str | None]:
    """(found, the message before it) for message `uuid` in a Claude Code session transcript.
    A conversation's first message has no message before it. One sent mid-run is filed as a
    queued_command attachment that points back to it."""
    for path in glob.glob(str(CLAUDE_PROJECTS / "*" / f"{glob.escape(session_id)}.jsonl")):
        with open(path, "rb") as f:
            for raw in f:
                if uuid.encode() not in raw:
                    continue
                try:
                    ev = json.loads(raw)
                except ValueError:
                    continue
                if ev.get("uuid") == uuid or (ev.get("attachment") or {}).get("source_uuid") == uuid:
                    return True, ev.get("parentUuid")
    return False, None


def log_cut(thread_id: int, uuid: str) -> int | None:
    """Where message `uuid` starts in an agent's log, in bytes: everything before it is the history it follows."""
    offset = 0
    try:
        with open(LOGS / f"{thread_id}.jsonl", "rb") as f:
            for raw in f:
                if uuid.encode() in raw:
                    try:
                        ev = json.loads(raw)
                    except ValueError:
                        ev = {}
                    if ev.get("type") == "user" and ev.get("uuid") == uuid:
                        return offset
                offset += len(raw)
    except OSError:
        pass
    return None


def copy_history(src: int, dst: int, cut: int, before: float) -> None:
    """Give a chat edited off another one the conversation so far: the log up to `cut` and the files sent before `before`."""
    LOGS.mkdir(parents=True, exist_ok=True)
    with open(LOGS / f"{src}.jsonl", "rb") as f:
        (LOGS / f"{dst}.jsonl").write_bytes(f.read(cut))
    try:
        sent = [raw for raw in (SENT_DIR / f"{src}.jsonl").read_bytes().splitlines(keepends=True)
                if json.loads(raw).get("t", 0) < before]
    except (OSError, ValueError):
        sent = []
    if sent:
        (SENT_DIR / f"{dst}.jsonl").write_bytes(b"".join(sent))


# ---------------------------------------------------------------- machine

class Machine:
    def __init__(self):
        self.history: deque = deque(maxlen=WINDOW // SAMPLE_EVERY + 1)
        self.gpu: dict | None = None
        self._cpu_prev: tuple[int, int] | None = None
        self.task: asyncio.Task | None = None

    def start(self) -> None:
        self.task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        while True:
            try:
                await self.sample()
            except Exception:
                log.exception("machine sample failed")
            await asyncio.sleep(SAMPLE_EVERY)

    def _cpu(self) -> float:
        with open("/proc/stat") as f:
            vals = [int(x) for x in f.readline().split()[1:]]
        idle, total = vals[3] + vals[4], sum(vals)
        prev, self._cpu_prev = self._cpu_prev, (idle, total)
        if not prev or total == prev[1]:
            return 0.0
        return 100 * (1 - (idle - prev[0]) / (total - prev[1]))

    @staticmethod
    def mem() -> tuple[int, int]:
        info = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, v = line.split(":", 1)
                info[k] = int(v.split()[0]) * 1024
        return info["MemTotal"] - info["MemAvailable"], info["MemTotal"]

    async def sample(self) -> None:
        cpu = self._cpu()
        used, total = self.mem()
        out = await run("nvidia-smi", "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu",
                        "--format=csv,noheader,nounits")
        gpu = None
        row = out.strip().splitlines()[0] if out.strip() else ""
        if row:
            try:
                name, util, mused, mtotal, temp = [x.strip() for x in row.split(",")]
                gpu = {"name": name.replace("NVIDIA GeForce ", ""), "util": float(util), "mem": float(mused) / 1024,
                       "mem_total": float(mtotal) / 1024, "temp": float(temp)}
            except ValueError:
                gpu = None
        self.gpu = gpu
        self.history.append({"t": time.time(), "cpu": round(cpu, 1), "ram": round(100 * used / total, 1),
                             "gpu": gpu["util"] if gpu else None})

    def snapshot(self) -> dict:
        used, total = self.mem()
        disk = shutil.disk_usage("/")
        with open("/proc/uptime") as f:
            uptime = float(f.read().split()[0])
        last = self.history[-1] if self.history else {}
        return {
            "host": config.HOSTNAME, "uptime": uptime, "load": os.getloadavg()[0], "cores": os.cpu_count(),
            "cpu": last.get("cpu"), "ram_used": used, "ram_total": total,
            "disk_used": disk.used, "disk_total": disk.total, "gpu": self.gpu,
            "history": list(self.history),
        }


# ---------------------------------------------------------------- dashboard

class Dashboard:
    def __init__(self, bot):
        self.bot = bot
        self.logs: dict[int, AgentLog] = {}
        self.machine = Machine()
        self.own_login: str | None = None
        self.whois: dict[str, tuple[float, bool]] = {}
        self.tailscale_ip = ""
        self.live_usage: tuple[float, list[dict]] | None = None   # from Anthropic's usage endpoint
        self.codex: dict = {}                                      # last Codex limits reading
        self.codex_checked = 0.0
        self.usage_checked = 0.0

    # ---- data ------------------------------------------------------------

    def log_for(self, agent) -> AgentLog:
        al = self.logs.get(agent.thread_id)
        if al is None:
            al = self.logs[agent.thread_id] = AgentLog(config.STATE_DIR / "logs" / f"{agent.thread_id}.jsonl", agent.cwd)
        return al.refresh()

    def jump_url(self, thread_id: int) -> str:
        guild = self.bot.guild.id if self.bot.guild else "@me"
        return f"https://discord.com/channels/{guild}/{thread_id}"

    def status(self, agent) -> str:
        if agent.thread_id in self.bot.runs or agent.thread_id in self.bot.active:
            return "running"
        if agent.updated > (agent.seen or 0) and time.time() - agent.updated < WAITING_FOR:
            return "waiting"
        return {"error": "failed", "interrupted": "interrupted"}.get(agent.status, "done")

    def started(self, agent, al, live: bool) -> float:
        """When the current (or last) run began. A run just started from a new message hasn't
        logged anything yet, so the log's last segment is still the previous turn's."""
        t = al.segs[-1][0] if al.segs else agent.created
        return max(t, self.bot.run_started.get(agent.thread_id, 0)) if live else t

    def agent_summary(self, agent, now: float) -> dict:
        al = self.log_for(agent)
        status = self.status(agent)
        lo = now - WINDOW
        live = status == "running"
        segs = [[max(s, lo), now if (o and live) else e] for s, e, o in al.segs
                if (now if (o and live) else e) >= lo]
        reply = al.last_reply
        return {
            "id": str(agent.thread_id), "title": agent.title, "cwd": tilde(agent.cwd), "model": agent.model,
            "model_label": models.label(agent.model if models.is_codex(agent.model) else al.model or agent.model),
            "provider": "codex" if models.is_codex(agent.model) else "claude", "default_effort": models.default_effort(agent.model),
            "effort": agent.effort, "status": status, "raw_status": agent.status, "turns": agent.turns,
            "created": agent.created, "updated": agent.updated,
            "started": self.started(agent, al, live),
            "ticks": [t for t, _ in al.ticks if t >= lo], "steers": [t for t in al.steers if t >= lo], "segs": segs,
            "tool_count": sum(al.tools.values()), "now_doing": al.last_tool if status == "running" else "",
            "reply": short(reply[1], 320) if reply else "", "reply_t": reply[0] if reply else None,
            "files": len(al.files), "url": self.jump_url(agent.thread_id) if agent.discord else None,
            "queued": len(self.bot.queued.get(agent.thread_id, [])),
        }

    async def jobs(self) -> list[dict]:
        """Long jobs, cached for a few seconds: every poll from every open page asks for them."""
        cached = getattr(self, "_jobs_cache", None)
        if cached and time.time() - cached[0] < 4:
            return cached[1]
        jobs = await self._jobs()
        self._jobs_cache = (time.time(), jobs)
        return jobs

    async def _jobs(self) -> list[dict]:
        referenced: dict[str, tuple[float, int]] = {}
        for agent in self.bot.state.agents.values():
            for unit, t in self.log_for(agent).units.items():
                if unit not in referenced or t > referenced[unit][0]:
                    referenced[unit] = (t, agent.thread_id)
        out = await run("systemctl", "--user", "list-units", "--type=service", "--state=running",
                        "--no-legend", "--plain")
        running = [line.split()[0].removesuffix(".service") for line in out.splitlines() if line.strip()]
        names = sorted(set(running) | {u for u, (t, _) in referenced.items() if time.time() - t < 24 * 3600})
        if not names:
            return []
        out = await run("systemctl", "--user", "show", *[n + ".service" for n in names],
                        "-p", "Id,ActiveState,SubState,Transient,ExecMainStartTimestamp,ExecMainExitTimestamp,"
                              "Result,LoadState,Description")
        jobs = []
        for block in out.strip().split("\n\n"):
            props = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
            name = props.get("Id", "").removesuffix(".service")
            if not name:
                continue
            ref = referenced.get(name)
            if props.get("Transient") != "yes" and not ref:
                continue
            started = _systemd_time(props.get("ExecMainStartTimestamp")) or (ref[0] if ref else None)
            active = props.get("ActiveState") == "active"
            jobs.append({
                "unit": name, "active": active, "state": props.get("SubState") or props.get("ActiveState"),
                "result": props.get("Result"), "gone": props.get("LoadState") == "not-found",
                "started": started, "ended": _systemd_time(props.get("ExecMainExitTimestamp")),
                "agent": str(ref[1]) if ref else None,
                "agent_title": short(self.bot.state.agents[ref[1]].title, 60) if ref and ref[1] in self.bot.state.agents else None,
            })
        jobs.sort(key=lambda j: (not j["active"], -(j["started"] or 0)))
        return jobs

    @staticmethod
    def recent_jobs(jobs: list[dict], now: float) -> list[dict]:
        """Everything running, plus the last few that finished in the past 6 hours."""
        done = [j for j in jobs if not j["active"] and (j["ended"] or j["started"] or 0) > now - 6 * 3600]
        return [j for j in jobs if j["active"]] + done[:3]

    def files(self, limit: int = 12) -> list[dict]:
        items = []
        for agent in self.bot.state.agents.values():
            for f in self.log_for(agent).files:
                items.append({**f, "agent": str(agent.thread_id), "agent_title": short(agent.title, 60)})
        items.sort(key=lambda f: f["t"] or 0, reverse=True)
        out = []
        for f in items[:limit]:
            p = Path(f["path"])
            out.append({**f, "name": p.name, "exists": p.is_file(), "size": p.stat().st_size if p.is_file() else None,
                        "image": p.suffix.lower() in (".png", ".jpg", ".jpeg", ".gif", ".webp")})
        return out

    def known_file(self, path: str) -> bool:
        if Path(path).resolve().is_relative_to(INBOX.resolve()):
            return True
        for agent in self.bot.state.agents.values():
            if any(f["path"] == path for f in self.log_for(agent).files):
                return True
        return False

    async def services(self) -> list[dict]:
        cached = getattr(self, "_services_cache", None)
        if cached and time.time() - cached[0] < 15:
            return cached[1]
        self._services_cache = (time.time(), await self._services())
        return self._services_cache[1]

    async def _services(self) -> list[dict]:
        rc = (await run("systemctl", "--user", "is-active", "claude-rc")).strip() or "unknown"
        ts = (await run("systemctl", "is-active", "tailscaled")).strip() or "unknown"
        return [{"name": "agentd", "state": "active"}, {"name": "claude-rc", "state": rc},
                {"name": "tailscaled", "state": ts, "detail": self.tailscale_ip}]

    def pending(self) -> list[dict]:
        cutoff = time.time() - 6 * 3600
        return [{"sid": sid, **p, "cwd": tilde(p.get("cwd", ""))}
                for sid, p in sorted(self.bot.pending.items(), key=lambda kv: kv[1]["t"], reverse=True)
                if p["t"] > cutoff]

    async def refresh_usage(self) -> str | None:
        """Ask Anthropic for the plan's current limits, as Claude Code's /usage does, with its own login.
        Returns an error message, or None. At most once a minute."""
        if time.time() - self.usage_checked < 60:
            return None
        self.usage_checked = time.time()
        try:
            oauth = json.loads((Path.home() / ".claude" / ".credentials.json").read_text())["claudeAiOauth"]
        except (OSError, ValueError, KeyError):
            return "Claude Code isn't logged in on this machine."
        if oauth.get("expiresAt", 0) / 1000 < time.time():
            return "Claude Code's login token has expired; it renews the next time an agent runs."
        try:
            async with ClientSession(timeout=ClientTimeout(total=10)) as http:
                async with http.get("https://api.anthropic.com/api/oauth/usage", headers={
                        "Authorization": f"Bearer {oauth['accessToken']}", "anthropic-beta": "oauth-2025-04-20"}) as r:
                    if r.status != 200:
                        return f"Anthropic answered {r.status}."
                    data = await r.json()
        except (ClientError, asyncio.TimeoutError, ValueError) as e:
            return f"Couldn't reach Anthropic ({type(e).__name__})."
        windows = [{"key": k, "used": w["utilization"] / 100, "resets": parse_ts(w.get("resets_at"))}
                   for k, w in data.items() if k.startswith(("five_hour", "seven_day"))
                   and isinstance(w, dict) and isinstance(w.get("utilization"), (int, float))]
        if windows:
            self.live_usage = (time.time(), windows)
        return None

    async def refresh_codex(self) -> None:
        """Codex plan limits via its app-server (`account/rateLimits/read`). At most once a minute."""
        codex = shutil.which("codex") or str(Path.home() / ".local" / "bin" / "codex")
        if not Path(codex).exists() or time.time() - self.codex_checked < 60:
            return
        self.codex_checked = time.time()
        try:
            proc = await asyncio.create_subprocess_exec(
                codex, "app-server", stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, limit=4 * 1024 * 1024)
        except OSError as e:
            self.codex = {"error": f"Couldn't start Codex ({e})."}
            return
        try:
            for msg in ({"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "agentd", "version": "1"}}},
                        {"method": "initialized"},
                        # Refreshes an expired ChatGPT token first; only a real turn would otherwise.
                        {"id": 3, "method": "account/read", "params": {"refreshToken": True}},
                        {"id": 2, "method": "account/rateLimits/read", "params": None}):
                proc.stdin.write(json.dumps(msg).encode() + b"\n")
            await proc.stdin.drain()
            while True:
                line = await asyncio.wait_for(proc.stdout.readline(), 20)
                if not line:
                    raise ValueError("Codex closed the connection")
                msg = json.loads(line)
                if msg.get("id") == 2:
                    break
        except (asyncio.TimeoutError, ValueError, ConnectionError) as e:
            self.codex = {**self.codex, "error": f"Couldn't read Codex limits ({e or type(e).__name__})."}
            return
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
        if "error" in msg:
            text = msg["error"].get("message", "")
            self.codex = {"error": "Codex isn't signed in on this machine." if "authentication" in text else text}
            return
        snap = (msg.get("result") or {}).get("rateLimits") or {}
        names = {300: "5-hour window", 10080: "This week"}
        windows = []
        for w in (snap.get("primary"), snap.get("secondary")):
            if w:
                mins = w.get("windowDurationMins")
                windows.append({"key": names.get(mins) or (f"{mins // 60}-hour window" if mins else "Limit"),
                                "used": (w.get("usedPercent") or 0) / 100, "resets": w.get("resetsAt")})
        self.codex = {"as_of": time.time(), "plan": snap.get("planType"), "windows": windows,
                      "limited": bool(snap.get("rateLimitReachedType"))}

    def usage(self) -> dict:
        """Claude plan limits from the newest reading in any agent log, plus agentd's own spend."""
        now = time.time()
        latest, cost_day, cost_week = None, 0.0, 0.0
        midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        for agent in self.bot.state.agents.values():
            al = self.log_for(agent)
            if al.rate and (latest is None or al.rate[0] > latest[0]):
                latest = al.rate
            for t, c in al.costs:
                if t >= now - 7 * 86400:
                    cost_week += c
                if t >= midnight:
                    cost_day += c
        plan = {}
        try:
            oauth = json.loads((Path.home() / ".claude" / ".credentials.json").read_text()).get("claudeAiOauth", {})
            plan = {"type": oauth.get("subscriptionType"), "tier": oauth.get("rateLimitTier")}  # never the tokens
        except (OSError, ValueError):
            pass
        claude = {"plan": plan, "cost_day": round(cost_day, 2), "cost_week": round(cost_week, 2)}
        if latest:
            info = latest[1]
            windows = info.get("unifiedWindows") or {}
            claude.update({
                "as_of": latest[0], "status": info.get("status"),
                "overage": info.get("overageStatus"), "overage_reason": info.get("overageDisabledReason"),
                "windows": [{"key": k, "used": w.get("utilization"), "resets": w.get("resetsAt")}
                            for k, w in windows.items() if isinstance(w, dict)],
            })
        if self.live_usage and self.live_usage[0] > claude.get("as_of", 0):
            claude.update({"as_of": self.live_usage[0], "windows": self.live_usage[1], "live": True})
        installed = bool(shutil.which("codex")) or (Path.home() / ".local" / "bin" / "codex").exists()
        return {"claude": claude, "codex": {"installed": installed, **self.codex}}

    async def overview(self) -> dict:
        now = time.time()
        agents = sorted(self.bot.state.agents.values(), key=lambda a: a.updated, reverse=True)
        summaries = [self.agent_summary(a, now) for a in agents]
        order = {"running": 0, "waiting": 1}
        summaries.sort(key=lambda s: (order.get(s["status"], 2), -s["updated"]))
        return {
            "now": now, "window": WINDOW, "agents": summaries, "pending": self.pending(),
            "machine": self.machine.snapshot(), "jobs": self.recent_jobs(await self.jobs(), now),
            "files": self.files(),
            "services": await self.services(), "usage": self.usage(),
            "settings": self.bot.state.settings,
        }

    async def agent_detail(self, agent, after: int = 0) -> dict:
        """`after`: how many entries the page already has; only newer ones are sent (the log only grows)."""
        now = time.time()
        al = self.log_for(agent)
        jobs = [j for j in await self.jobs() if j["agent"] == str(agent.thread_id)]
        files = [f for f in self.files(200) if f["agent"] == str(agent.thread_id)]
        resume = models.resume_command(agent)
        # Messages it hasn't read yet: drop each one once it shows up in the log (queued ones arrive joined).
        mine = [e for e in al.entries if e["kind"] == "you"]
        def logged(m) -> bool:
            text, paths = split_attached(m["text"])
            return any((e["t"] or 0) >= m["t"] - 30 and text.strip() in e["text"]
                       and set(paths) <= set(e.get("paths", [])) for e in mine)
        outbox = [m for m in getattr(self.bot, "outbox", {}).get(agent.thread_id, [])
                  if now - m["t"] < 6 * 3600 and not logged(m)]
        if hasattr(self.bot, "outbox"):
            self.bot.outbox[agent.thread_id] = outbox
        total = len(al.entries)
        start = after if 0 < after <= total and total - after <= 400 else max(0, total - PAGE)
        return {
            "now": now, "window": WINDOW, **self.agent_summary(agent, now), "session_id": agent.session_id,
            "resume": resume, "tools": al.tools, "total_entries": total, "entries_from": start,
            "entries": entry_list(al.entries[start:]),
            "jobs": jobs, "file_list": files, "efforts": models.efforts_for(agent.model),
            "subagents": [{**sub, "model_label": models.label(sub["model"]) if sub["model"] else ""}
                          for sub in al.subagents.values()],
            "tokens": [{"model": models.label(m), **t} for m, t in
                       sorted(al.tokens.items(), key=lambda kv: -sum(kv[1].values()))],
            "context": al.context,
            # the title is cut to 90 characters; the Details panel shows the whole first message
            "prompt": short(next((e["text"] for e in al.entries if e["kind"] == "you" and e["text"]), agent.title), 1000),
            "parent": ({"id": str(agent.parent), "title": parent.title}
                       if (parent := self.bot.state.agents.get(agent.parent or 0)) else None),
            "outbox": [{"t": m["t"], "text": split_attached(m["text"])[0], "state": m["state"],
                        "files": [inbox_name(p) for p in split_attached(m["text"])[1]]}
                       for m in outbox],
        }

    def agent_history(self, agent, before: int) -> dict:
        """The PAGE entries just before index `before`, for scrolling back through a long conversation."""
        al = self.log_for(agent)
        before = max(0, min(before, len(al.entries)))
        start = max(0, before - PAGE)
        return {"entries_from": start, "entries": entry_list(al.entries[start:before])}

    # ---- access ----------------------------------------------------------

    async def init_tailscale(self) -> None:
        out = await run("tailscale", "status", "--json")
        try:
            data = json.loads(out)
            me = data["Self"]
            self.own_login = data["User"][str(me["UserID"])]["LoginName"]
            self.tailscale_ip = next((ip for ip in me.get("TailscaleIPs", []) if "." in ip), "")
        except (ValueError, KeyError, TypeError):
            log.warning("tailscale status unavailable; the dashboard only answers on localhost")

    async def allowed(self, remote: str | None) -> bool:
        try:
            ip = ipaddress.ip_address((remote or "").split("%")[0])
        except ValueError:
            return False
        if getattr(ip, "ipv4_mapped", None):
            ip = ip.ipv4_mapped
        if ip.is_loopback:
            return True
        if not self.own_login or not any(ip in net for net in TAILNET):
            return False
        cached = self.whois.get(str(ip))
        if cached and time.time() - cached[0] < 300:
            return cached[1]
        out = await run("tailscale", "whois", "--json", str(ip), timeout=10)
        try:
            login = json.loads(out)["UserProfile"]["LoginName"]
        except (ValueError, KeyError, TypeError):
            log.warning("dashboard: tailscale whois %s failed; not caching", ip)
            return False
        ok = login == self.own_login
        if not ok:
            log.warning("dashboard: refused %s (%s)", ip, login)
        self.whois[str(ip)] = (time.time(), ok)
        return ok


def _systemd_time(value: str | None) -> float | None:
    if not value or value in ("n/a", "0"):
        return None
    try:
        return datetime.strptime(" ".join(value.split()[1:3]), "%Y-%m-%d %H:%M:%S").timestamp()
    except (ValueError, IndexError):
        return None


# ---------------------------------------------------------------- HTTP

class Upload:
    """A file attached on the dashboard, staged on disk. Saves like a discord.Attachment,
    so the bot files it in the agent's inbox the same way."""

    def __init__(self, filename: str, tmp: Path, size: int):
        self.filename, self.tmp, self.size = filename, tmp, size

    async def save(self, path) -> None:
        os.replace(self.tmp, path)


def clean_name(name: str) -> str:
    name = re.sub(r"[\x00-\x1f\x7f\]]", "", Path(name.replace("\\", "/")).name).strip(" .")
    return name[-120:] or "file"


async def read_form(request) -> tuple[dict, list[Upload]]:
    """The fields and files of a multipart POST (or the fields of a JSON one). Files stream to disk."""
    if not request.content_type.startswith("multipart/"):
        try:
            data = await request.json()
        except ValueError:
            data = {}
        return (data if isinstance(data, dict) else {}), []
    staging = INBOX / ".incoming"
    staging.mkdir(parents=True, exist_ok=True)
    fields, uploads, total = {}, [], 0
    try:
        reader = await request.multipart()
        while (part := await reader.next()) is not None:
            if not part.filename:
                if part.name:
                    fields[part.name] = await part.text()
                continue
            tmp = staging / f"{time.time_ns()}-{len(uploads)}"
            up = Upload(clean_name(part.filename), tmp, 0)
            uploads.append(up)
            with open(tmp, "wb") as f:
                while chunk := await part.read_chunk(1 << 20):
                    total += len(chunk)
                    if total > MAX_UPLOAD:
                        raise web.HTTPRequestEntityTooLarge(max_size=MAX_UPLOAD, actual_size=total)
                    up.size += len(chunk)
                    await asyncio.to_thread(f.write, chunk)
    except BaseException:
        discard(uploads)
        raise
    return fields, uploads


def discard(uploads: list[Upload]) -> None:
    for up in uploads:
        up.tmp.unlink(missing_ok=True)

def make_app(bot) -> web.Application:
    dash = Dashboard(bot)
    bot.dashboard = dash

    @web.middleware
    async def guard(request: web.Request, handler):
        # Behind Caddy every request comes from loopback; judge the client Caddy forwarded for instead
        # (Caddy replaces any X-Forwarded-For the client sent, so it can't be spoofed through the proxy).
        client = request.remote
        if (fwd := request.headers.get("X-Forwarded-For")) and await dash.allowed(client):
            client = fwd.split(",")[-1].strip()
        if not await dash.allowed(client):
            log.warning("dashboard: 403 %s %s", client, request.path)
            return web.Response(status=403, text="Not allowed. Connect from your own Tailscale devices.")
        if request.method == "POST" and request.headers.get("X-Agentd") != "1":
            return web.Response(status=400, text="missing X-Agentd header")
        try:
            resp = await handler(request)
        except web.HTTPException:
            raise
        except Exception as e:
            log.exception("dashboard: %s %s failed", request.method, request.path)
            return web.json_response({"error": f"{type(e).__name__}: {e}"}, status=500)
        if resp.status >= 400:
            log.warning("dashboard: %d %s %s from %s", resp.status, request.method, request.path, request.remote)
        path = request.path
        if path.startswith(("/static/fonts/", "/static/icons/")):
            resp.headers.setdefault("Cache-Control", "public, max-age=604800, immutable")
        elif path.startswith("/api/"):
            resp.headers.setdefault("Cache-Control", "no-store")
            if isinstance(resp, web.Response) and resp.body is not None and len(resp.body) > 1024 \
                    and "gzip" in request.headers.get("Accept-Encoding", ""):
                resp.enable_compression()
        else:
            resp.headers.setdefault("Cache-Control", "no-cache")   # revalidate (ETag/304) so updates show up
        return resp

    def agent_or_404(request):
        try:
            agent = bot.state.agents.get(int(request.match_info["id"]))
        except ValueError:
            agent = None
        if agent is None:
            raise web.HTTPNotFound(text="no such agent")
        return agent

    async def body(request) -> dict:
        try:
            data = await request.json()
            return data if isinstance(data, dict) else {}
        except ValueError:
            return {}

    async def page(request):
        return web.FileResponse(STATIC / "index.html")

    async def overview(request):
        return web.json_response(await dash.overview())

    async def agent_detail(request):
        try:
            after = int(request.query.get("after", "0"))
        except ValueError:
            after = 0
        return web.json_response(await dash.agent_detail(agent_or_404(request), after))

    async def subagent(request):
        agent = agent_or_404(request)
        al = dash.log_for(agent)
        sub = al.subagents.get(request.match_info["sub"])
        if sub is None:
            raise web.HTTPNotFound(text="no such subagent")
        entries = await asyncio.to_thread(subagent_entries, al.path, sub)
        return web.json_response({"now": time.time(), "entries": entries,
                                  "sub": {**sub, "model_label": models.label(sub["model"]) if sub["model"] else ""}})

    async def agent_history(request):
        try:
            before = int(request.query.get("before", "0"))
        except ValueError:
            raise web.HTTPBadRequest(text="before must be a number")
        return web.json_response(dash.agent_history(agent_or_404(request), before))

    def known_path(request) -> str:
        path = request.query.get("path", "")
        if not dash.known_file(path) or not Path(path).is_file():
            raise web.HTTPNotFound(text="unknown file")
        return path

    async def file(request):
        return web.FileResponse(known_path(request))

    async def pdf_pages(request):
        sizes = await pdf_info(known_path(request))
        return web.json_response({"pages": [pdf_scale(w, h) for w, h in sizes]})

    async def pdf_png(request):
        path = known_path(request)
        try:
            n = int(request.query.get("page", "1"))
        except ValueError:
            raise web.HTTPBadRequest(text="bad page")
        out = await pdf_page(path, n)
        if out is None:
            raise web.HTTPNotFound(text="no such page")
        return web.FileResponse(out, headers={"Cache-Control": "private, max-age=86400"})

    async def new_agent(request):
        data, uploads = await read_form(request)
        try:
            return await start_new(data, uploads)
        finally:
            discard(uploads)   # anything not already filed in the agent's inbox

    async def start_new(data, uploads):
        from .bot import resolve_dir
        text = str(data.get("text", "")).strip()
        if not text and not uploads:
            return web.json_response({"error": "Write a task for the agent."}, status=400)
        text = text or "Look at the attached files."
        cwd = resolve_dir(str(data.get("cwd") or "").strip() or None)
        if cwd is None:
            return web.json_response({"error": f"No such directory: {data.get('cwd')}"}, status=400)
        model = data.get("model") if models.valid(data.get("model")) else config.DEFAULT_MODEL
        effort = data.get("effort") if data.get("effort") in models.efforts_for(model) else None
        mirror = bool(bot.state.settings.get("web_to_discord"))
        if mirror and bot.ch_agents is None:
            return web.json_response({"error": "agentd isn't connected to Discord yet."}, status=503)
        agent = await bot.start_agent(text, cwd, model, effort, attachments=uploads, discord_thread=mirror)
        return web.json_response({"id": str(agent.thread_id)})

    async def message(request):
        agent = agent_or_404(request)
        data, uploads = await read_form(request)
        try:
            text = str(data.get("text", "")).strip()
            if not text and not uploads:
                return web.json_response({"error": "Empty message."}, status=400)
            thread = trigger = None
            if agent.discord:
                thread = await bot.get_thread(agent.thread_id)
                trigger = await post_to_discord(thread, text, uploads)
            await bot.submit(agent, thread, text, trigger, uploads)
            return web.json_response({"ok": True, "steered": agent.thread_id in bot.runs})
        finally:
            discard(uploads)

    async def edit(request):
        """Edit one of your messages into a new chat: the conversation up to just before it, then the new text.
        The original chat is left as it is."""
        from .bot import resolve_dir
        agent = agent_or_404(request)
        data = await body(request)
        text, uuid = str(data.get("text", "")).strip(), str(data.get("uuid") or "")
        if uuid and not UUID.match(uuid):
            return web.json_response({"error": "bad message id"}, status=400)
        mine = [e for e in dash.log_for(agent).entries if e["kind"] == "you"]
        entry = next((e for e in mine if e.get("uuid") == uuid), None) if uuid else (mine[0] if mine else None)
        if entry is None:
            return web.json_response({"error": "That message isn't in this chat."}, status=404)
        paths = [p for p in entry.get("paths", []) if Path(p).is_file()]   # its files go along
        if not text and not paths:
            return web.json_response({"error": "Write a message."}, status=400)
        cwd = resolve_dir(agent.cwd)
        if cwd is None:
            return web.json_response({"error": f"Its directory is gone: {agent.cwd}"}, status=409)
        mirror = bool(bot.state.settings.get("web_to_discord"))
        if mirror and bot.ch_agents is None:
            return web.json_response({"error": "agentd isn't connected to Discord yet."}, status=503)
        at = cut = None
        if uuid and not models.is_codex(agent.model):
            found, at = (await asyncio.to_thread(transcript_parent, agent.session_id, uuid)
                         if agent.session_id else (False, None))
            if not found:
                return web.json_response({"error": "Couldn't find that message in Claude Code's session."}, status=409)
            if at:   # not the opening message, so there's history to keep
                cut = await asyncio.to_thread(log_cut, agent.thread_id, uuid)
                if cut is None:
                    return web.json_response({"error": "Couldn't find that message in the chat's log."}, status=409)
        elif entry is not mine[0]:
            return web.json_response({"error": "Only a ChatGPT chat's first message can be edited."}, status=400)

        def setup(new):
            new.parent = agent.thread_id
            if at:
                new.fork_from, new.fork_at = agent.session_id, at
                copy_history(agent.thread_id, new.thread_id, cut, entry["t"] or time.time())

        prompt = text or "Look at the attached files."
        if paths:
            prompt += f"\n\n{ATTACHED}{', '.join(paths)}]"
        title = f"{short(agent.title.removesuffix(' (edited)'), 80)} (edited)" if at else short(prompt, 90)
        new = await bot.start_agent(prompt, cwd, agent.model, agent.effort, discord_thread=mirror,
                                    title=title, setup=setup)
        return web.json_response({"id": str(new.thread_id)})

    async def post_to_discord(thread, text: str, uploads: list[Upload]):
        """Mirror a dashboard message into the agent's thread, with the files that fit under Discord's cap."""
        import discord
        feed = bot.feed_for(thread)
        content = f"**You** (dashboard): {text[:1700]}" if text else "**You** (dashboard)"
        fit, budget = [], config.MAX_UPLOAD_BYTES
        for up in uploads[:10]:
            if up.size <= budget:
                fit.append(up)
                budget -= up.size
        left = [up.filename for up in uploads if up not in fit]
        if left:
            content += f"\n-# 📎 on {config.HOSTNAME} only (too big for Discord): {short(', '.join(left), 200)}"
        if fit:
            try:
                return await feed.send(content, files=[discord.File(up.tmp, filename=up.filename) for up in fit])
            except discord.HTTPException:
                log.warning("dashboard: couldn't post attachments to Discord", exc_info=True)
                content += f"\n-# 📎 {short(', '.join(up.filename for up in fit), 200)}"
        return await feed.send(content)

    async def stop(request):
        from .bot import stopping_text
        agent = agent_or_404(request)
        stopped, dropped = bot.stop_agent(agent.thread_id)
        if stopped and agent.discord:
            thread = await bot.get_thread(agent.thread_id)
            await bot.feed_for(thread).send(stopping_text(dropped) + " (from the dashboard)")
        return web.json_response({"ok": True, "stopped": stopped})

    async def delete(request):
        agent = agent_or_404(request)
        tid = agent.thread_id
        if dash.status(agent) == "running":
            return web.json_response({"error": "It's still running. Stop it first."}, status=409)
        bot.stop_agent(tid)   # drops anything still queued for it
        del bot.state.agents[tid]
        bot.state.save()
        dash.logs.pop(tid, None)
        for path in (config.STATE_DIR / "logs" / f"{tid}.jsonl", SENT_DIR / f"{tid}.jsonl"):
            path.unlink(missing_ok=True)
        shutil.rmtree(INBOX / str(tid), ignore_errors=True)
        if agent.discord:   # archived, not deleted: messages there no longer reach an agent
            import discord
            try:
                thread = await bot.get_thread(tid)
                await thread.send("-# 🗑️ Deleted from the dashboard. Messages here no longer reach an agent.")
                await thread.edit(archived=True)
            except discord.HTTPException:
                log.warning("dashboard: couldn't archive thread %s", tid, exc_info=True)
        return web.json_response({"ok": True})

    async def effort(request):
        agent = agent_or_404(request)
        level = (await body(request)).get("level")
        if level not in models.efforts_for(agent.model) + ["default"]:
            return web.json_response({"error": "unknown effort level"}, status=400)
        agent.effort = None if level == "default" else level
        bot.state.save()
        if agent.discord:
            thread = await bot.get_thread(agent.thread_id)
            await bot.feed_for(thread).send(f"🧠 Effort → **{level}** (from the dashboard)")
        return web.json_response({"ok": True})

    async def model_catalog(request):
        return web.json_response(models.catalog())

    async def seen(request):
        agent = agent_or_404(request)
        agent.seen = time.time()
        bot.state.save()
        return web.json_response({"ok": True})

    async def settings(request):
        data = await body(request)
        if "web_to_discord" in data:
            bot.state.settings["web_to_discord"] = bool(data["web_to_discord"])
            bot.state.save()
        return web.json_response(bot.state.settings)

    async def push_key(request):
        return web.json_response({"key": push.public_key(), "devices": push.count()})

    async def push_subscribe(request):
        data = await body(request)
        try:
            push.subscribe(data.get("sub") or {}, request.headers.get("User-Agent", ""))
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        return web.json_response({"ok": True, "devices": push.count()})

    async def push_unsubscribe(request):
        push.unsubscribe((await body(request)).get("endpoint", ""))
        return web.json_response({"ok": True, "devices": push.count()})

    async def push_test(request):
        await push.notify("🔔 agentd", "Test notification from Cam-PC.", "/settings", tag="test")
        return web.json_response({"ok": True})

    async def service_worker(request):
        resp = web.FileResponse(STATIC / "sw.js")
        resp.headers["Service-Worker-Allowed"] = "/"
        return resp

    async def refresh_usage(request):
        error, _ = await asyncio.gather(dash.refresh_usage(), dash.refresh_codex())
        return web.json_response({"ok": error is None, "error": error})

    async def stop_job(request):
        unit = request.match_info["unit"]
        if not UNIT_NAME.match(unit) or not any(j["unit"] == unit for j in await dash.jobs()):
            raise web.HTTPNotFound(text="no such job")
        await run("systemctl", "--user", "stop", unit + ".service", timeout=30)
        return web.json_response({"ok": True})

    def same_origin(request) -> bool:
        # A WebSocket isn't bound by CORS, so without this any page open on a tailnet device could drive the desktop.
        origin = request.headers.get("Origin", "")
        return bool(origin) and origin.split("://", 1)[-1].rstrip("/") == request.host

    async def remote_desktop(request):
        """Bridge noVNC's WebSocket to a VNC server: the Linux desktop (agentd-desktop.service, on a private
        unix socket) or the Windows VM (its container's websockify on localhost)."""
        which = request.match_info["which"]
        if which not in ("linux", "windows"):
            raise web.HTTPNotFound(text="no such desktop")
        if not same_origin(request):
            log.warning("dashboard: desktop websocket from foreign origin %r", request.headers.get("Origin"))
            raise web.HTTPForbidden(text="cross-origin")
        ws = web.WebSocketResponse(protocols=("binary",), compress=False, max_msg_size=0, heartbeat=30)
        await ws.prepare(request)
        log.info("dashboard: remote desktop %s opened", which)
        try:
            if which == "linux":
                await bridge_unix(ws)
            else:
                await bridge_ws(ws)
        except (OSError, ClientError, asyncio.TimeoutError) as e:
            log.warning("dashboard: remote desktop %s: %s", which, e)
        finally:
            await ws.close()
            log.info("dashboard: remote desktop %s closed", which)
        return ws

    async def bridge_unix(ws):
        if not DESKTOP_SOCK.exists():   # start it on demand if it was stopped
            await run("systemctl", "--user", "start", "agentd-desktop.service", timeout=30)
            for _ in range(50):
                if DESKTOP_SOCK.exists():
                    break
                await asyncio.sleep(0.2)
        reader, writer = await asyncio.open_unix_connection(str(DESKTOP_SOCK))

        async def up():
            async for msg in ws:
                if msg.type == web.WSMsgType.BINARY:
                    writer.write(msg.data)
                    await writer.drain()
            writer.close()

        async def down():
            while data := await reader.read(1 << 16):
                await ws.send_bytes(data)

        await pump(up(), down())
        writer.close()

    async def bridge_ws(ws):
        async with ClientSession() as session:
            async with session.ws_connect(WINDOWS_VNC, protocols=("binary",), max_msg_size=0,
                                          timeout=ClientTimeout(total=None, connect=10)) as far:
                async def up():
                    async for msg in ws:
                        if msg.type == web.WSMsgType.BINARY:
                            await far.send_bytes(msg.data)

                async def down():
                    async for msg in far:
                        if msg.type == web.WSMsgType.BINARY:
                            await ws.send_bytes(msg.data)

                await pump(up(), down())

    async def pump(*coros):
        """Run both directions until either side hangs up."""
        tasks = [asyncio.ensure_future(c) for c in coros]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                if t.exception() and not isinstance(t.exception(), (ConnectionError, ClientError)):
                    raise t.exception()
        finally:
            for t in tasks:
                t.cancel()

    async def open_desktop(request):
        """linux.zehdari.com / windows.zehdari.com land here: pick the viewer for this device and go."""
        which = request.match_info["which"]
        if which not in ("linux", "windows"):
            raise web.HTTPNotFound(text="no such desktop")
        return web.Response(content_type="text/html", text=f"""<!doctype html><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{which.title()} desktop</title>
<script src="/static/desktop.js"></script><script>location.replace(desktopUrl("{which}"));</script>""")

    async def gpu_status() -> dict:
        out, unit, log_ = await asyncio.gather(
            run(WINVM, "status", timeout=20),
            run("systemctl", "--user", "show", SWITCH_UNIT, "-p", "ActiveState", "-p", "Result", "-p", "LoadState"),
            run("journalctl", "--user", "-u", SWITCH_UNIT, "-n", "8", "-o", "cat", "--no-pager", timeout=10))
        driver = re.search(r"3090 driver:\s*(\S+)", out)
        vm = re.search(r"VM:\s*(.+)", out)
        props = dict(line.split("=", 1) for line in unit.splitlines() if "=" in line)
        switching = props.get("ActiveState") in ("active", "activating", "deactivating")
        driver = driver.group(1) if driver else ""
        return {
            "owner": "windows" if driver == "vfio-pci" else "linux" if driver == "nvidia" else "unknown",
            "driver": driver, "vm": vm.group(1).strip() if vm else "",
            "switching": switching, "failed": props.get("ActiveState") == "failed",
            "log": [line for line in log_.splitlines() if line.strip()][-6:],
        }

    async def gpu(request):
        return web.json_response(await gpu_status())

    async def gpu_switch(request):
        """Move the 3090 to Windows ("windows": winvm gpu) or back to Linux ("linux": winvm nogpu). It takes a few
        minutes and restarts the VM and gdm, so it runs as its own systemd unit, not under agentd."""
        if not same_origin(request):
            raise web.HTTPForbidden(text="cross-origin")
        to = (await body(request)).get("to")
        if to not in ("windows", "linux"):
            return web.json_response({"error": "to must be windows or linux"}, status=400)
        st = await gpu_status()
        if st["switching"]:
            return web.json_response({"error": "A GPU switch is already running."}, status=409)
        if st["owner"] == to:
            return web.json_response({"error": f"The GPU is already with {to.title()}."}, status=409)
        await run("systemctl", "--user", "reset-failed", SWITCH_UNIT)   # a failed earlier run keeps the name taken
        proc = await asyncio.create_subprocess_exec(
            "systemd-run", "--user", f"--unit={SWITCH_UNIT}", f"--description=winvm: GPU to {to}",
            "-E", f"PATH={os.environ.get('PATH', '')}", WINVM, "gpu" if to == "windows" else "nogpu",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        _, err = await proc.communicate()
        if proc.returncode:
            return web.json_response({"error": err.decode(errors="replace").strip() or "systemd-run failed"}, status=500)
        log.info("dashboard: GPU switch to %s started", to)
        return web.json_response({"ok": True})

    app = web.Application(middlewares=[guard])
    app.add_routes([
        web.get("/", page), web.get("/agents/{id}", page), web.get("/system", page),
        *[web.get(f"/{name}", page) for name in ("usage", "jobs", "files", "machine", "desktop", "settings")],
        web.get("/rd/ws/{which}", remote_desktop), web.get("/rd/open/{which}", open_desktop), web.static("/rd/novnc", NOVNC),
        web.get("/api/overview", overview), web.get("/api/agents/{id}", agent_detail),
        web.get("/api/agents/{id}/history", agent_history), web.get("/api/agents/{id}/subagents/{sub}", subagent),
        web.get("/api/file", file), web.get("/api/pdf/pages", pdf_pages), web.get("/api/pdf/page", pdf_png),
        web.get("/api/models", model_catalog),
        web.post("/api/agents", new_agent), web.post("/api/agents/{id}/message", message),
        web.post("/api/agents/{id}/edit", edit),
        web.post("/api/agents/{id}/stop", stop), web.post("/api/agents/{id}/delete", delete),
        web.post("/api/agents/{id}/effort", effort),
        web.post("/api/agents/{id}/seen", seen), web.post("/api/jobs/{unit}/stop", stop_job),
        web.get("/api/gpu", gpu), web.post("/api/gpu", gpu_switch),
        web.post("/api/usage/refresh", refresh_usage), web.post("/api/settings", settings),
        web.get("/api/push/key", push_key), web.post("/api/push/subscribe", push_subscribe),
        web.post("/api/push/unsubscribe", push_unsubscribe), web.post("/api/push/test", push_test),
        web.get("/sw.js", service_worker),
        web.static("/static", STATIC),
    ])

    async def on_startup(app):
        shutil.rmtree(INBOX / ".incoming", ignore_errors=True)   # uploads cut off by a restart
        await dash.init_tailscale()
        dash.machine.start()

    app.on_startup.append(on_startup)
    return app


async def start_web(bot) -> web.AppRunner:
    runner = web.AppRunner(make_app(bot), access_log=None)
    await runner.setup()
    # Loopback only; Caddy terminates HTTPS on the Tailscale IP. Access is still checked per request.
    for host in ("127.0.0.1", "::1"):
        await web.TCPSite(runner, host, config.WEB_PORT).start()
    log.info("dashboard on port %d", config.WEB_PORT)
    return runner
