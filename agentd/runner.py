"""One headless `claude -p` turn, streamed as JSON events."""
import asyncio
import json
import os
import shlex
import signal
import time
from collections import deque
from datetime import datetime, timezone

from . import config, models

SYSTEM_PROMPT = f"""You are running headless on {config.HOSTNAME}, a dedicated Linux machine that the user controls from Discord, usually from their phone.
- You have passwordless sudo and may administer the whole OS.
- The user sees your text replies and a one-line log of each tool call, but not tool output. Put anything they need to read in your reply.
- Send files (screenshots, plots, logs, videos) with `agentd-send PATH... -m "caption"`. Add `--alert` to ping the user's phone mid-task.
- Speak to the user with `agentd-say "text"`: it reads the text aloud in a calm voice and posts it as a voice message with the text as its caption. Use it when they ask you to talk to them or for a voice reply; keep it short and conversational, without markdown or code.
- Start long-running work (training, simulators, servers) so it outlives this turn: `systemd-run --user --collect --same-dir -E PATH="$PATH" --unit=NAME CMD`. Check on it with `systemctl --user status NAME` and `journalctl --user -u NAME`.
- End each turn with a short summary that reads well on a phone screen."""


class Run:
    def __init__(self, agent, prompt: str):
        self.agent = agent
        self.prompt = prompt
        self.proc: asyncio.subprocess.Process | None = None
        self.stopped = False
        self.stderr: deque[str] = deque(maxlen=40)
        # Tags of messages written to stdin that the agent hasn't read yet, oldest first. The bot
        # pops one per replayed user message and closes stdin once a turn ends with none left.
        self.unread: deque = deque()
        self.closed = False
        self.idle = False  # between turns
        self.bg_tasks = 0  # background tasks (e.g. Bash with run_in_background) still going
        self._stderr_task: asyncio.Task | None = None
        self.log_path = config.STATE_DIR / "logs" / f"{agent.thread_id}.jsonl"

    def argv(self) -> list[str]:
        args = [
            config.CLAUDE_BIN, "-p",
            "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
            "--replay-user-messages",
            "--thinking-display", "summarized",   # thinking text for the dashboard; omitted by default
            "--permission-mode", "bypassPermissions",
            "--append-system-prompt", SYSTEM_PROMPT,
        ]
        if self.agent.session_id:
            args += ["--resume", self.agent.session_id]
        elif self.agent.fork_from:   # an edited message: a copy of that conversation up to just before it
            args += ["--resume", self.agent.fork_from, "--fork-session", "--resume-session-at", self.agent.fork_at]
        else:
            args += ["--name", self.agent.title[:60]]
        if self.agent.model:
            args += ["--model", self.agent.model]
        if self.agent.effort:
            args += ["--effort", self.agent.effort]
        return args

    async def start(self) -> None:
        env = {
            **os.environ,
            "AGENTD_THREAD_ID": str(self.agent.thread_id),
            "AGENTD_PORT": str(config.PORT),
            "PATH": f"{config.ROOT / 'bin'}:{os.environ.get('PATH', '')}",
        }
        self.proc = await asyncio.create_subprocess_exec(
            *self.argv(),
            cwd=self.agent.cwd,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            limit=64 * 1024 * 1024,
        )
        self._stderr_task = asyncio.create_task(self._read_stderr())
        await self.send(self.prompt)

    async def send(self, text: str, tag=None) -> bool:
        """Give the agent a message. Mid-turn it is read after the agent's current step;
        returns False once the run is wrapping up, so the caller should queue it instead."""
        if self.closed or self.proc is None or self.proc.stdin.is_closing():
            return False
        msg = {"type": "user", "message": {"role": "user", "content": text}}
        self.proc.stdin.write(json.dumps(msg).encode() + b"\n")
        self.unread.append(tag)
        try:
            await self.proc.stdin.drain()
        except ConnectionError:
            pass
        return True

    def wrap_up(self) -> None:
        """Let claude exit if it's between turns with no unread messages and no background tasks.
        Until then stdin stays open: a background task finishing starts a new turn, and so does a message."""
        if self.idle and not self.unread and not self.bg_tasks:
            self.finish()

    def finish(self) -> None:
        """No more messages: claude exits after the current turn."""
        self.closed = True
        if self.proc and not self.proc.stdin.is_closing():
            self.proc.stdin.close()

    async def events(self):
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.log_path, "ab") as log:
            async for raw in self.proc.stdout:
                log.write(raw)
                log.flush()
                try:
                    ev = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                kind, sub = ev.get("type"), ev.get("subtype")
                if kind == "result":
                    self.idle = True
                elif kind == "system" and sub == "init":
                    self.idle = False
                elif kind == "system" and sub == "background_tasks_changed":
                    self.bg_tasks = len(ev.get("tasks") or [])
                    if not self.bg_tasks:
                        # Finishing a task normally starts a turn that reports on it. If none comes, wrap up anyway.
                        asyncio.get_running_loop().call_later(15, self.wrap_up)
                yield ev

    async def wait(self) -> tuple[int, str]:
        rc = await self.proc.wait()
        if self._stderr_task:
            await self._stderr_task
        return rc, "".join(self.stderr)

    def stop(self) -> None:
        self.stopped = self.closed = True
        asyncio.create_task(self._kill())

    async def _kill(self) -> None:
        # Signal the whole process group so tool subprocesses go too.
        for sig, grace in ((signal.SIGINT, 5), (signal.SIGTERM, 5), (signal.SIGKILL, 0)):
            if self.proc is None or self.proc.returncode is not None:
                return
            try:
                os.killpg(self.proc.pid, sig)
            except ProcessLookupError:
                return
            if grace:
                try:
                    await asyncio.wait_for(self.proc.wait(), grace)
                    return
                except asyncio.TimeoutError:
                    pass

    async def _read_stderr(self) -> None:
        async for line in self.proc.stderr:
            self.stderr.append(line.decode(errors="replace"))


def make_run(agent, prompt: str) -> "Run":
    return CodexRun(agent, prompt) if models.is_codex(agent.model) else Run(agent, prompt)


def unwrap_shell(command: str) -> str:
    """`/bin/bash -lc 'ls -la'` -> `ls -la`, as Codex reports the commands it runs."""
    try:
        words = shlex.split(command)
    except ValueError:
        return command
    if len(words) == 3 and words[0].rsplit("/", 1)[-1] in ("bash", "sh", "zsh") and words[1] in ("-lc", "-c"):
        return words[2]
    return command


class CodexRun(Run):
    """One `codex exec` turn, with its JSONL events translated into the stream-json shapes that
    `claude -p` produces, so the bot, the logs and the dashboard treat both the same way.
    Codex can't take messages mid-turn, so those queue up and start the next turn."""

    def __init__(self, agent, prompt: str):
        super().__init__(agent, prompt)
        self.started = time.time()
        self.failure: str | None = None
        self.tools_seen: set[str] = set()

    def argv(self) -> list[str]:
        args = [models.codex_bin() or "codex", "exec"]
        if self.agent.session_id:
            args += ["resume"]
        args += ["--json", "--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox",
                 "-c", f"developer_instructions={json.dumps(SYSTEM_PROMPT)}"]
        if self.agent.model:
            args += ["-m", self.agent.model]
        if self.agent.effort:
            args += ["-c", f"model_reasoning_effort={json.dumps(self.agent.effort)}"]
        if self.agent.session_id:
            args += [self.agent.session_id]
        return args + ["-"]   # the prompt comes on stdin

    async def start(self) -> None:
        env = {
            **os.environ,
            "AGENTD_THREAD_ID": str(self.agent.thread_id),
            "AGENTD_PORT": str(config.PORT),
            "PATH": f"{config.ROOT / 'bin'}:{os.environ.get('PATH', '')}",
        }
        self.proc = await asyncio.create_subprocess_exec(
            *self.argv(), cwd=self.agent.cwd, env=env,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=True, limit=64 * 1024 * 1024,
        )
        self._stderr_task = asyncio.create_task(self._read_stderr())
        self.proc.stdin.write(self.prompt.encode())
        self.proc.stdin.close()
        self.closed = True

    async def send(self, text: str, tag=None) -> bool:
        return False

    def wrap_up(self) -> None:
        pass

    def finish(self) -> None:
        self.closed = True

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _tool(self, name: str, inp: dict) -> dict:
        return {"type": "assistant", "timestamp": self._now(), "parent_tool_use_id": None,
                "message": {"role": "assistant", "content": [{"type": "tool_use", "name": name, "input": inp}]}}

    def _translate(self, ev: dict) -> list[dict]:
        kind = ev.get("type")
        if kind == "thread.started":
            return [{"type": "system", "subtype": "init", "session_id": ev.get("thread_id"),
                     "model": self.agent.model, "provider": "codex", "timestamp": self._now()},
                    {"type": "user", "isReplay": True, "timestamp": self._now(),
                     "message": {"role": "user", "content": self.prompt}}]
        if kind == "turn.completed":
            return [{"type": "result", "subtype": "success", "is_error": False, "num_turns": 1,
                     "duration_ms": int((time.time() - self.started) * 1000), "usage": ev.get("usage"),
                     "timestamp": self._now()}]
        if kind == "turn.failed":
            self.failure = (ev.get("error") or {}).get("message") or "Codex turn failed"
            return [{"type": "result", "subtype": "error", "is_error": True, "num_turns": 1, "result": self.failure,
                     "duration_ms": int((time.time() - self.started) * 1000), "timestamp": self._now()}]
        if kind == "error":
            self.failure = ev.get("message") or "Codex error"
            return []
        if kind not in ("item.started", "item.completed"):
            return []
        item = ev.get("item") or {}
        itype, iid = item.get("type"), item.get("id")
        if itype == "reasoning":
            text = item.get("text") or ""
            if kind != "item.completed" or not text.strip():
                return []
            return [{"type": "assistant", "timestamp": self._now(), "parent_tool_use_id": None,
                     "message": {"role": "assistant", "content": [{"type": "thinking", "thinking": text}]}}]
        if itype == "agent_message":
            text = item.get("text") or ""
            if kind != "item.completed" or not text.strip():
                return []
            return [{"type": "assistant", "timestamp": self._now(), "parent_tool_use_id": None,
                     "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}]
        out = []
        if iid not in self.tools_seen:   # announce each tool call once, as soon as it starts
            self.tools_seen.add(iid)
            if itype == "command_execution":
                out.append(self._tool("Bash", {"command": unwrap_shell(item.get("command") or "")}))
            elif itype == "file_change":
                for c in item.get("changes") or []:
                    name = {"add": "Write", "delete": "Delete"}.get(c.get("kind"), "Edit")
                    out.append(self._tool(name, {"file_path": c.get("path", "")}))
            elif itype == "mcp_tool_call":
                out.append(self._tool(f"mcp__{item.get('server', 'mcp')}__{item.get('tool', '')}",
                                      item.get("arguments") if isinstance(item.get("arguments"), dict) else {}))
            elif itype == "web_search":
                out.append(self._tool("WebSearch", {"query": item.get("query", "")}))
            elif itype == "error":
                out.append({"type": "assistant", "timestamp": self._now(), "parent_tool_use_id": None,
                            "message": {"role": "assistant", "content": [
                                {"type": "text", "text": f"⚠️ Codex: {item.get('message', '')}"}]}})
            else:
                self.tools_seen.discard(iid)
        if kind == "item.completed" and itype == "command_execution" and item.get("exit_code") not in (0, None):
            out.append({"type": "user", "timestamp": self._now(), "message": {"role": "user", "content": [
                {"type": "tool_result", "is_error": True,
                 "content": f"exit {item.get('exit_code')}: {(item.get('aggregated_output') or '').strip()[-300:]}"}]}})
        return out

    async def events(self):
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        ended = False
        with open(self.log_path, "ab") as log:
            async for raw in self.proc.stdout:
                try:
                    ev = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                for out in self._translate(ev):
                    ended = ended or out["type"] == "result"
                    log.write(json.dumps(out).encode() + b"\n")
                    log.flush()
                    yield out
            if not ended and self.failure and not self.stopped:
                out = {"type": "result", "subtype": "error", "is_error": True, "num_turns": 0,
                       "result": self.failure, "duration_ms": int((time.time() - self.started) * 1000),
                       "timestamp": self._now()}
                log.write(json.dumps(out).encode() + b"\n")
                yield out
