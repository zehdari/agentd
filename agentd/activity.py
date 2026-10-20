"""Mirror Claude Code sessions agentd didn't start (terminal, Remote Control) into #activity.

Events arrive from the global hooks via hooks/relay.sh -> POST /hook. They are handled
one at a time, in arrival order, so each session's thread reads in sequence."""
import asyncio
import json
import logging
import time
from pathlib import Path

import discord

from .format import short, tool_full, tool_line, truncate

log = logging.getLogger(__name__)

OPENING_EVENTS = {"UserPromptSubmit", "PreToolUse", "Notification"}


def last_assistant_text(transcript_path: str | None) -> str:
    """Last text the assistant wrote, read from the tail of a session transcript."""
    if not transcript_path:
        return ""
    try:
        with open(transcript_path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 512 * 1024))
            lines = f.read().decode(errors="replace").splitlines()
    except OSError:
        return ""
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("type") != "assistant":
            continue
        content = entry.get("message", {}).get("content", [])
        texts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
        if any(t.strip() for t in texts):
            return "\n".join(texts).strip()
    return ""


class ActivityRelay:
    def __init__(self, bot):
        self.bot = bot
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=2000)
        self.worker: asyncio.Task | None = None

    def submit(self, event: dict) -> None:
        if self.worker is None:
            self.worker = asyncio.create_task(self._work())
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            log.warning("activity queue full; dropping %s", event.get("hook_event_name"))

    async def _work(self) -> None:
        while True:
            event = await self.queue.get()
            try:
                await self._handle(event)
            except Exception:
                log.exception("handling %s failed", event.get("hook_event_name"))

    async def _handle(self, ev: dict) -> None:
        if self.bot.ch_activity is None:
            return
        name, sid = ev.get("hook_event_name"), ev.get("session_id")
        if not name or not sid:
            return
        thread = await self._thread_for(sid, ev, create=name in OPENING_EVENTS)
        if thread is None:
            return
        feed = self.bot.feed_for(thread)
        if name != "Notification":
            self.bot.pending.pop(sid, None)  # it moved on, so whatever it asked was answered

        if name == "UserPromptSubmit":
            await feed.text(f"💬 **Prompt:** {truncate(ev.get('prompt', ''), 1500)}")
        elif name == "PreToolUse":
            name, inp = ev.get("tool_name", ""), ev.get("tool_input")
            await feed.tool(tool_line(name, inp), tool_full(name, inp))
        elif name == "Stop":
            text = ev.get("last_assistant_message") or last_assistant_text(ev.get("transcript_path"))
            await feed.text(f"✅ **Turn done**\n{truncate(text, 1800)}" if text else "✅ **Turn done**")
        elif name == "Notification":
            message = ev.get("message", "")
            await feed.send(f"🔔 {message}")
            if "permission" in (ev.get("notification_type", "") + message).lower():
                self.bot.pending[sid] = {"t": time.time(), "cwd": ev.get("cwd", ""), "message": message,
                                         "url": thread.jump_url}
                await self.bot.alert(f"🔔 Session needs you · {thread.jump_url}", message)
        elif name == "SessionEnd":
            await feed.send(f"⏹️ Session ended ({ev.get('reason', 'exit')})")

    async def _thread_for(self, sid: str, ev: dict, create: bool):
        tid = self.bot.state.activity.get(sid)
        if tid is not None:
            try:
                return await self.bot.get_thread(tid)
            except (discord.NotFound, discord.Forbidden):
                del self.bot.state.activity[sid]
        if not create:
            return None
        cwd = ev.get("cwd", "")
        label = Path(cwd).name or cwd or "session"
        topic = short(ev.get("prompt") or "", 60) or sid[:8]
        starter = await self.bot.ch_activity.send(f"🖥️ **{label}** · `{cwd}`\n-# session `{sid}`", silent=True)
        thread = await starter.create_thread(name=short(f"{label} · {topic}", 95), auto_archive_duration=1440)
        self.bot.state.activity[sid] = thread.id
        self.bot.state.save()
        return thread
