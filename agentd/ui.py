"""Ordered, rate-friendly writer for one Discord thread."""
import asyncio
import json
import logging

import discord

from . import config
from .format import LIMIT, chunk, tables_to_code

log = logging.getLogger(__name__)

EDIT_INTERVAL = 2.0
FULL_DIR = config.STATE_DIR / "full"  # untruncated tool calls behind each log message's 📜 button


EMBED_LIMIT = 4000


def full_embed(fulls: list[str]) -> discord.Embed:
    text = "\n\n".join(fulls)
    if len(text) > EMBED_LIMIT:
        text = text[:EMBED_LIMIT - 20]
        if text.count("```") % 2:
            text += "\n```"
        text += "\n…"
    return discord.Embed(description=text)


def load_fulls(msg_id: int) -> tuple[list[str], bool] | None:
    try:
        data = json.loads((FULL_DIR / f"{msg_id}.json").read_text())
    except (OSError, ValueError):
        return None
    if isinstance(data, list):  # older format
        return data, False
    return data.get("fulls", []), data.get("expanded", False)


def save_fulls(msg_id: int, fulls: list[str], expanded: bool) -> None:
    try:
        FULL_DIR.mkdir(parents=True, exist_ok=True)
        (FULL_DIR / f"{msg_id}.json").write_text(json.dumps({"fulls": fulls, "expanded": expanded}))
    except OSError:
        log.exception("saving full tool calls failed")


class FullView(discord.ui.View):
    """📜 button on a tool log message: expands the calls that were cut off in place (as an embed
    under the log) and collapses them again. Persistent (registered with add_view), so old
    buttons keep working after a restart."""

    def __init__(self, expanded: bool = False):
        super().__init__(timeout=None)
        self.toggle.label = "Collapse" if expanded else "Expand"

    @discord.ui.button(label="Expand", emoji="📜", style=discord.ButtonStyle.secondary, custom_id="agentd:full")
    async def toggle(self, inter: discord.Interaction, button: discord.ui.Button) -> None:
        if inter.user.id not in getattr(inter.client, "owner_ids", ()):
            await inter.response.send_message("Not authorized.", ephemeral=True)
            return
        saved = load_fulls(inter.message.id)
        if saved is None:
            await inter.response.send_message("The full text for this log wasn't saved.", ephemeral=True)
            return
        fulls, expanded = saved
        feed = getattr(inter.client, "feeds", {}).get(inter.channel_id)
        if feed and feed.msg and feed.msg.id == inter.message.id:
            fulls = feed.fulls  # still being written; use the live copy
            feed.expanded = not expanded
        save_fulls(inter.message.id, fulls, not expanded)
        await inter.response.edit_message(embed=None if expanded else full_embed(fulls),
                                          view=FullView(not expanded))


class Feed:
    """Tool calls collect into one message that is edited in place (at most every
    EDIT_INTERVAL seconds); text is posted as new messages, so order is preserved."""

    def __init__(self, thread):
        self.thread = thread
        self.lock = asyncio.Lock()
        self.msg = None
        self.lines: list[str] = []
        self.fulls: list[str] = []  # untruncated versions of lines that were cut off
        self.expanded = False  # whether the 📜 embed is showing on the current message
        self.dirty = False
        self.flush_task: asyncio.Task | None = None

    async def tool(self, line: str, full: str | None = None) -> None:
        """Add a line to the tool log. `full` is the untruncated call, shown by the 📜 button."""
        async with self.lock:
            if self.msg and len("\n".join(self.lines + [line])) > LIMIT:
                await self._flush()
                self.msg, self.lines, self.fulls, self.expanded = None, [], [], False
            self.lines.append(line)
            if full:
                self.fulls.append(full)
            if self.msg is None:
                self.msg = await self._send("\n".join(self.lines), **({"view": FullView()} if full else {}))
                self.dirty = False
                self._save_fulls()
            elif full and len(self.fulls) == 1:
                self.dirty = True
                await self._flush()  # put the 📜 button on now
            else:
                self.dirty = True
                if not self.flush_task or self.flush_task.done():
                    self.flush_task = asyncio.create_task(self._flush_later())

    async def text(self, text: str, **kwargs):
        async with self.lock:
            await self._close_log()
            last = None
            for part in chunk(tables_to_code(text)):
                last = await self._send(part, **kwargs)
            return last

    async def send(self, content: str | None = None, **kwargs):
        async with self.lock:
            await self._close_log()
            return await self._send(content, **kwargs)

    async def _flush_later(self) -> None:
        await asyncio.sleep(EDIT_INTERVAL)
        async with self.lock:
            await self._flush()

    async def _flush(self) -> None:
        if self.msg and self.dirty:
            kwargs = {"view": FullView()} if self.fulls and not self.msg.components else {}
            if self.expanded:
                kwargs["embed"] = full_embed(self.fulls)
            try:
                self.msg = await self.msg.edit(content="\n".join(self.lines), **kwargs)
            except discord.HTTPException:
                log.exception("editing tool log failed")
            self.dirty = False
            self._save_fulls()

    def _save_fulls(self) -> None:
        if self.msg and self.fulls:
            save_fulls(self.msg.id, self.fulls, self.expanded)

    async def _close_log(self) -> None:
        await self._flush()
        self.msg, self.lines, self.fulls, self.expanded = None, [], [], False

    async def _send(self, content, **kwargs):
        kwargs.setdefault("silent", True)
        try:
            return await self.thread.send(content, **kwargs)
        except discord.HTTPException:
            log.exception("sending to thread %s failed", getattr(self.thread, "id", "?"))
            return None


class NullFeed:
    """Feed for an agent with no Discord thread: everything is dropped (the dashboard reads the log)."""

    async def tool(self, line: str, full: str | None = None) -> None:
        pass

    async def text(self, text: str, **kwargs):
        return None

    async def send(self, content: str | None = None, **kwargs):
        return None


NULL_FEED = NullFeed()
