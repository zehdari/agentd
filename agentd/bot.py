"""Discord front end: post a task -> an agent thread with a live tool log, alerts when done."""
import asyncio
import io
import logging
import os
import re
import signal
import time
from pathlib import Path
from typing import Optional

import discord
from discord import app_commands

from . import config, models
from .activity import ActivityRelay
from .format import ATTACHED, fmt_duration, short, tables_to_code, tool_full, tool_line, tool_result_text, truncate
from .runner import CodexRun, Run, make_run
from .server import start_server
from .web import record_sent, start_web
from .state import Agent, State
from .sysinfo import snapshot
from .ui import NULL_FEED, Feed, FullView

log = logging.getLogger("agentd")

EFFORTS = models.CLAUDE_EFFORTS
DIR_PREFIX = re.compile(r"^(~?/\S*)\s+(.+)$", re.S)
STATUS_ICONS = {"idle": "✅", "running": "⏳", "error": "❌", "interrupted": "⚠️"}
STOP_BUTTON = "🛑"
STEER = "📨"  # sent to the running agent, which reads it after its current step
READ = "👀"


def resolve_dir(text: str | None) -> Path | None:
    if not text:
        return config.DEFAULT_DIR
    path = Path(os.path.expanduser(text))
    return path.resolve() if path.is_dir() else None


def split_dir(text: str) -> tuple[Path, str]:
    """'~/proj fix the tests' -> (~/proj, 'fix the tests') when ~/proj exists."""
    text = text.strip()
    m = DIR_PREFIX.match(text)
    if m and (path := resolve_dir(m[1])):
        return path, m[2].strip()
    return config.DEFAULT_DIR, text


async def react(msg, emoji: str, remove: bool = False) -> None:
    if msg is None:
        return
    try:
        if remove:
            await msg.remove_reaction(emoji, msg._state.user)
        else:
            await msg.add_reaction(emoji)
    except discord.HTTPException:
        pass


class AgentBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(
            intents=intents,
            allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=True),
        )
        self.tree = app_commands.CommandTree(self)
        self.state = State(config.STATE_DIR / "state.json")
        self.runs: dict[int, Run] = {}
        self.active: set[int] = set()  # threads with a run loop in progress
        self.queued: dict[int, list[tuple[str, discord.Message | None]]] = {}
        self.feeds: dict[int, Feed] = {}
        self.stop_buttons: dict[int, int] = {}  # trigger message id -> thread id, while that run is going
        self.owner_ids = set(config.OWNER_IDS)
        self.owner_mention = ""
        self.guild = None
        self.ch_agents = self.ch_alerts = self.ch_activity = None
        self.relay = ActivityRelay(self)
        # Terminal/Remote Control sessions waiting on a permission prompt: session id -> details.
        self.pending: dict[str, dict] = {}
        # Messages the agent hasn't read yet (until they show up in its log), for the dashboard.
        self.outbox: dict[int, list[dict]] = {}
        self.dashboard = None
        self._booted = False

    # ---- lifecycle -------------------------------------------------------

    async def setup_hook(self) -> None:
        register_commands(self)
        self.add_view(FullView())
        await start_server(self)
        if config.WEB_PORT:
            await start_web(self)

    async def on_ready(self) -> None:
        if self._booted:
            return
        self.guild = self.get_guild(config.GUILD_ID) if config.GUILD_ID else next(iter(self.guilds), None)
        if self.guild is None:
            invite = discord.utils.oauth_url(self.user.id, permissions=discord.Permissions(administrator=True),
                                             scopes=("bot", "applications.commands"))
            log.error("The bot isn't in a server yet. Add it with: %s", invite)
            return
        self._booted = True
        info = await self.application_info()
        if info.team:
            self.owner_ids |= {m.id for m in info.team.members}
            self.owner_mention = f"<@{info.team.owner_id}>"
        else:
            self.owner_ids.add(info.owner.id)
            self.owner_mention = info.owner.mention
        await self.ensure_channels()
        self.tree.copy_global_to(guild=self.guild)
        await self.tree.sync(guild=self.guild)
        await self.recover_interrupted()
        await self.ch_alerts.send(f"🟢 agentd online on **{config.HOSTNAME}**", silent=True)
        log.info("ready in %s as %s", self.guild.name, self.user)

    async def on_guild_join(self, guild: discord.Guild) -> None:
        await self.on_ready()

    async def ensure_channels(self) -> None:
        guild = self.guild
        category = discord.utils.get(guild.categories, name="Claude") or await guild.create_category("Claude")

        async def channel(name: str, topic: str):
            return discord.utils.get(guild.text_channels, name=name) or await guild.create_text_channel(
                name, category=category, topic=topic)

        self.ch_agents = await channel(
            "agents", "Post a task to start an agent (prefix a path like ~/proj to set its directory). "
                      "Reply in its thread to continue. /status /stop /info /sh")
        self.ch_alerts = await channel("alerts", "Pings from terminal/Remote Control sessions that need you, "
                                                 "and agentd-send --alert outside an agent thread.")
        self.ch_activity = await channel("activity", "Live mirror of terminal and Remote Control sessions.")

    async def recover_interrupted(self) -> None:
        for agent in self.state.agents.values():
            if agent.status != "running":
                continue
            agent.status = "interrupted"
            if not agent.discord:
                continue
            try:
                thread = await self.get_thread(agent.thread_id)
                await thread.send("⚠️ agentd restarted while this was running, so the run was cut off. "
                                  "Reply to continue where it left off.", silent=True)
            except discord.HTTPException:
                pass
        self.state.save()

    # ---- helpers ---------------------------------------------------------

    async def get_thread(self, thread_id: int):
        return self.get_channel(thread_id) or await self.fetch_channel(thread_id)

    def feed_for(self, thread) -> Feed:
        if thread is None:  # an agent without a Discord thread
            return NULL_FEED
        feed = self.feeds.get(thread.id)
        if feed is None:
            feed = self.feeds[thread.id] = Feed(thread)
        return feed

    async def alert(self, head: str, body: str = "") -> None:
        text = f"{self.owner_mention} {head}"
        if body.strip():
            text += "\n>>> " + truncate(body.strip(), 400)
        try:
            await self.ch_alerts.send(text)
        except discord.HTTPException:
            log.exception("alert failed")

    async def deliver(self, thread_id, files: list[str], message: str, alert: bool) -> None:
        """Back end of `agentd-send`: post files/message to an agent's thread (or #alerts)."""
        agent = self.state.agents.get(int(thread_id)) if thread_id else None
        if agent is not None:
            record_sent(agent.thread_id, files, message)   # the dashboard shows it from here
        if agent is not None and not agent.discord:
            return  # no thread to post to; the dashboard shows files sent with agentd-send
        if self.ch_alerts is None:
            raise ValueError("agentd is not connected to Discord yet")
        for path in files:
            size = os.path.getsize(path)
            if size > config.MAX_UPLOAD_BYTES:
                raise ValueError(f"{path} is {size / 1e6:.1f} MB; Discord's limit is "
                                 f"{config.MAX_UPLOAD_BYTES / 1e6:.0f} MB. Compress or downscale it.")
        target = await self.get_thread(int(thread_id)) if thread_id else self.ch_alerts
        message = tables_to_code(message)
        batches = [files[i:i + 10] for i in range(0, len(files), 10)] or [[]]
        for i, batch in enumerate(batches):
            content = message if i == 0 else None
            ping = alert and i == 0
            if ping:
                content = f"{self.owner_mention} {content or ''}"
            kwargs = {"files": [discord.File(p) for p in batch]} if batch else {}
            if target is self.ch_alerts:
                await target.send(content, **kwargs)
            else:
                # Ping in the thread itself rather than cross-posting to #alerts.
                await self.feed_for(target).send(content, silent=not ping, **kwargs)

    # ---- agents ----------------------------------------------------------

    async def start_agent(self, text: str, cwd: Path, model: str | None, effort: str | None = None, *,
                          origin: discord.Message | None = None, attachments=(), discord_thread: bool = True) -> Agent:
        """Start an agent in a new #agents thread, or with no Discord presence at all
        (discord_thread=False, for the dashboard)."""
        title = short(text, 90) or "agent"
        if not discord_thread:
            thread = trigger = None
        elif origin is not None:
            thread = await origin.create_thread(name=title, auto_archive_duration=10080)
            trigger = origin
        else:
            files = f"\n-# 📎 {truncate(', '.join(a.filename for a in attachments), 200)}" if attachments else ""
            trigger = await self.ch_agents.send(f">>> {truncate(text, 1700)}{files}", silent=True)
            thread = await trigger.create_thread(name=title, auto_archive_duration=10080)
        if not effort and not models.is_codex(model):
            effort = config.DEFAULT_EFFORT
        tid = thread.id if thread is not None else self.state.local_id()
        agent = Agent(thread_id=tid, cwd=str(cwd), title=title, model=model, effort=effort,
                      discord=thread is not None)
        self.state.agents[tid] = agent
        self.state.save()
        await self.feed_for(thread).send(
            f"-# 📁 `{cwd}` · model: {models.label(model)} · effort: {agent.effort or 'default'}")
        await self.submit(agent, thread, text, trigger, attachments)
        return agent

    async def submit(self, agent: Agent, thread, text: str, trigger, attachments=()) -> None:
        agent.seen = time.time()
        if attachments:
            paths = await self.save_attachments(agent, attachments)
            text = f"{text}\n\n{ATTACHED}{', '.join(paths)}]".strip()
        note = {"t": time.time(), "text": text, "state": "starting"}
        self.outbox.setdefault(agent.thread_id, []).append(note)
        if agent.thread_id in self.active:
            # Steer the running agent rather than waiting for it to finish.
            run = self.runs.get(agent.thread_id)
            if run and not run.closed:
                await react(trigger, STEER)
                if await run.send(text, trigger):
                    note["state"] = "sent"
                    return
                await react(trigger, STEER, remove=True)
            self.queued.setdefault(agent.thread_id, []).append((text, trigger))
            note["state"] = "queued"
            await react(trigger, "📥")
            return
        self.active.add(agent.thread_id)
        asyncio.create_task(self.run_loop(agent, thread, text, trigger))

    async def save_attachments(self, agent: Agent, attachments) -> list[str]:
        inbox = config.STATE_DIR / "inbox" / str(agent.thread_id)
        inbox.mkdir(parents=True, exist_ok=True)
        paths = []
        for a in attachments:
            path = inbox / f"{int(time.time())}-{Path(a.filename).name}"
            n = 1
            while path.exists():   # same name twice in one second
                n += 1
                path = inbox / f"{int(time.time())}.{n}-{Path(a.filename).name}"
            await a.save(path)
            paths.append(str(path))
        return paths

    async def run_loop(self, agent: Agent, thread, text: str, trigger) -> None:
        try:
            while True:
                await self.run_once(agent, thread, text, trigger)
                pending = self.queued.pop(agent.thread_id, [])
                if not pending:
                    break
                text = "\n\n".join(t for t, _ in pending)
                trigger = pending[-1][1]
                for _, t in pending:
                    await react(t, "📥", remove=True)
        finally:
            self.active.discard(agent.thread_id)

    async def run_once(self, agent: Agent, thread, text: str, trigger) -> None:
        feed = self.feed_for(thread)
        tid = agent.thread_id
        await react(trigger, "⏳")
        await react(trigger, STOP_BUTTON)
        if trigger is not None:
            self.stop_buttons[trigger.id] = tid
        agent.status, agent.updated = "running", time.time()
        self.state.save()

        run = make_run(agent, text)
        self.runs[tid] = run
        result, rc, err = None, -1, ""
        turns, duration_ms = 0, 0
        try:
            await run.start()
            async for ev in run.events():
                kind = ev.get("type")
                if kind == "system" and ev.get("subtype") == "init":
                    agent.session_id = ev.get("session_id") or agent.session_id
                    self.state.save()
                elif kind == "assistant":
                    nested = ev.get("parent_tool_use_id") is not None
                    for block in ev.get("message", {}).get("content", []):
                        if block.get("type") == "text" and block.get("text", "").strip() and not nested:
                            await feed.text(block["text"])
                        elif block.get("type") == "tool_use":
                            name, inp = block.get("name", ""), block.get("input")
                            await feed.tool(("↳ " if nested else "") + tool_line(name, inp), tool_full(name, inp))
                elif kind == "user" and ev.get("isReplay") and not ev.get("origin"):   # origin: Claude Code's own notices
                    read = run.unread.popleft() if run.unread else None
                    if read is not None:
                        await react(read, STEER, remove=True)
                        await react(read, READ)
                elif kind == "user":
                    content = ev.get("message", {}).get("content", [])
                    for block in content if isinstance(content, list) else []:
                        if block.get("type") == "tool_result" and block.get("is_error") and not run.stopped:
                            await feed.tool(f"⚠️ `{short(tool_result_text(block.get('content')), 150).replace('`', 'ˋ')}`")
                elif kind == "result":
                    result = ev
                    turns += ev.get("num_turns", 0)
                    duration_ms += ev.get("duration_ms", 0)
                    # A message that came in too late for this turn starts another one, and so does
                    # a background task finishing; until then the run stays open for steering.
                    run.wrap_up()
                    if run.bg_tasks and not run.closed:
                        await feed.send(f"-# ⏳ Waiting on {run.bg_tasks} background task"
                                        f"{'s' * (run.bg_tasks != 1)}. Message anytime to steer.")
            rc, err = await run.wait()
        except Exception as e:
            log.exception("run in thread %s failed", tid)
            err = f"{type(e).__name__}: {e}"
            if run.proc and run.proc.returncode is None:
                run.stop()
        finally:
            self.runs.pop(tid, None)
            if trigger is not None:
                self.stop_buttons.pop(trigger.id, None)

        for msg in run.unread:  # never read: the run was stopped or crashed
            await react(msg, STEER, remove=True)
        await react(trigger, "⏳", remove=True)
        if trigger is not None:
            try:
                await trigger.clear_reaction(STOP_BUTTON)
            except discord.HTTPException:
                pass
        if run.stopped:
            agent.status = "idle"
            await react(trigger, "⏹️")
            await feed.send("⏹️ Stopped.")
        elif result and not result.get("is_error"):
            agent.status = "idle"
            agent.turns += turns
            await react(trigger, "✅")
            # Agent threads ping in place; #alerts is for things that have no thread of their own.
            await feed.send(f"{self.owner_mention} ✅ done · {fmt_duration(duration_ms / 1000)}"
                            f" · {turns} turns", silent=False)
        else:
            agent.status = "error"
            detail = (result or {}).get("result") or err.strip() or f"{'codex' if isinstance(run, CodexRun) else 'claude'} exited with code {rc}"
            await react(trigger, "❌")
            await feed.send(f"{self.owner_mention} ❌ **Run failed**\n```\n"
                            f"{truncate(detail, 1500).replace('```', 'ˋˋˋ')}\n```", silent=False)
        agent.updated = time.time()
        self.state.save()

    def stop_agent(self, thread_id: int) -> tuple[bool, int]:
        """Stop the run in a thread and drop its queue. Returns (was running, queued messages dropped)."""
        self.outbox.pop(thread_id, None)
        queued = self.queued.pop(thread_id, [])
        for _, msg in queued:
            asyncio.create_task(react(msg, "📥", remove=True))
        dropped = len(queued)
        run = self.runs.get(thread_id)
        if run is None:
            return False, dropped
        dropped += sum(msg is not None for msg in run.unread)
        run.stop()
        return True, dropped

    # ---- messages --------------------------------------------------------

    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        if str(payload.emoji) != STOP_BUTTON or payload.user_id not in self.owner_ids:
            return
        tid = self.stop_buttons.get(payload.message_id)
        if tid is None or tid not in self.runs or self.runs[tid].stopped:
            return
        stopped, dropped = self.stop_agent(tid)
        if stopped:
            await self.feed_for(await self.get_thread(tid)).send(stopping_text(dropped))

    async def on_message(self, msg: discord.Message) -> None:
        if msg.author.bot or self.ch_agents is None or msg.author.id not in self.owner_ids:
            return
        if msg.channel.id == self.ch_agents.id:
            if not msg.content.strip() and not msg.attachments:
                return
            cwd, text = split_dir(msg.content)
            await self.start_agent(text or "Look at the attached files.", cwd, config.DEFAULT_MODEL,
                                   origin=msg, attachments=msg.attachments)
        elif isinstance(msg.channel, discord.Thread) and msg.channel.id in self.state.agents:
            agent = self.state.agents[msg.channel.id]
            await self.submit(agent, msg.channel, msg.content, msg, msg.attachments)

    async def status_text(self) -> str:
        lines = [f"**{config.HOSTNAME}**", await asyncio.to_thread(snapshot), ""]
        running = [a for a in self.state.agents.values() if a.thread_id in self.runs]
        lines.append(f"**Running ({len(running)})**")
        lines += [f"⏳ {where(a)} · `{a.cwd}`" for a in running] or ["-# nothing running"]
        recent = sorted((a for a in self.state.agents.values() if a.thread_id not in self.runs),
                        key=lambda a: a.updated, reverse=True)[:8]
        if recent:
            lines.append("**Recent**")
            lines += [f"{STATUS_ICONS.get(a.status, '•')} {where(a)} · <t:{int(a.updated)}:R>" for a in recent]
        return truncate("\n".join(lines), 1990)


def where(agent: Agent) -> str:
    """A link to the agent's thread, or its title if it only lives on the dashboard."""
    return f"<#{agent.thread_id}>" if agent.discord else f"🖥️ {short(agent.title, 60)} (dashboard)"


def stopping_text(dropped: int) -> str:
    note = f" (and dropped {dropped} queued message{'s' * (dropped != 1)})" if dropped else ""
    return f"⏹️ Stopping…{note}"


def register_commands(bot: AgentBot) -> None:
    tree = bot.tree

    async def allowed(inter: discord.Interaction) -> bool:
        if inter.user.id in bot.owner_ids:
            return True
        await inter.response.send_message("Not authorized.", ephemeral=True)
        return False

    def thread_agent(inter: discord.Interaction) -> Agent | None:
        return bot.state.agents.get(inter.channel_id)

    effort_choices = [app_commands.Choice(name=e, value=e) for e in EFFORTS]
    model_choices = [app_commands.Choice(name=models.label(m), value=m) for m in models.CLAUDE_MODELS] + [
        app_commands.Choice(name=f"{m['name']} (ChatGPT)", value=m["id"]) for m in models.codex_models()]

    @tree.command(description="Start a new agent")
    @app_commands.describe(task="What the agent should do",
                           directory="Working directory (default: home)",
                           model=f"Model (default: {config.DEFAULT_MODEL or 'Claude Code default'})",
                           effort="Thinking effort (default: Claude Code's default)")
    @app_commands.choices(model=model_choices[:25], effort=effort_choices)
    async def new(inter: discord.Interaction, task: str, directory: Optional[str] = None,
                  model: Optional[app_commands.Choice[str]] = None,
                  effort: Optional[app_commands.Choice[str]] = None):
        if not await allowed(inter):
            return
        cwd = resolve_dir(directory)
        if cwd is None:
            await inter.response.send_message(f"No such directory: `{directory}`", ephemeral=True)
            return
        await inter.response.defer(ephemeral=True, thinking=True)
        agent = await bot.start_agent(task, cwd, model.value if model else config.DEFAULT_MODEL,
                                      effort.value if effort else None)
        await inter.followup.send(f"Started → <#{agent.thread_id}>", ephemeral=True)

    @tree.command(description="Set thinking effort for the agent in this thread (applies from its next turn)")
    @app_commands.choices(level=effort_choices + [app_commands.Choice(name="default", value="default")])
    async def effort(inter: discord.Interaction, level: app_commands.Choice[str]):
        if not await allowed(inter):
            return
        agent = thread_agent(inter)
        if agent is None:
            await inter.response.send_message(
                "Use this inside an agent thread. For new agents, pick `effort` on /new, "
                "or set AGENTD_DEFAULT_EFFORT in ~/agents/.env.", ephemeral=True)
            return
        agent.effort = None if level.value == "default" else level.value
        bot.state.save()
        running = " (the current turn keeps its old setting)" if agent.thread_id in bot.runs else ""
        await inter.response.send_message(f"🧠 Effort → **{level.value}**{running}")

    @tree.command(description="Stop the agent in this thread")
    async def stop(inter: discord.Interaction):
        if not await allowed(inter):
            return
        agent = thread_agent(inter)
        if agent is None:
            await inter.response.send_message("Use this inside an agent thread.", ephemeral=True)
            return
        stopped, dropped = bot.stop_agent(agent.thread_id)
        if not stopped:
            await inter.response.send_message("Nothing is running here.", ephemeral=True)
            return
        await inter.response.send_message(stopping_text(dropped))

    @tree.command(description="Machine health and agent overview")
    async def status(inter: discord.Interaction):
        if not await allowed(inter):
            return
        await inter.response.defer(thinking=True)
        await inter.followup.send(await bot.status_text())

    @tree.command(description="Session details for the agent in this thread")
    async def info(inter: discord.Interaction):
        if not await allowed(inter):
            return
        agent = thread_agent(inter)
        if agent is None:
            await inter.response.send_message("Use this inside an agent thread.", ephemeral=True)
            return
        resume = models.resume_command(agent) or "(no session yet)"
        await inter.response.send_message(
            f"**Status:** {agent.status} · **model:** {models.label(agent.model)} · "
            f"**effort:** {agent.effort or 'default'} · **turns:** {agent.turns}\n"
            f"**Directory:** `{agent.cwd}`\n**Session:** `{agent.session_id}`\n"
            f"**Continue at the machine:**\n```\n{resume}\n```", ephemeral=True)

    @tree.command(description="Run a shell command on the machine (as you, from home)")
    @app_commands.describe(command="Command for bash -lc", timeout="Seconds before it's killed")
    async def sh(inter: discord.Interaction, command: str, timeout: app_commands.Range[int, 1, 3600] = 120):
        if not await allowed(inter):
            return
        await inter.response.defer(thinking=True)
        proc = await asyncio.create_subprocess_exec(
            "bash", "-lc", command, cwd=str(Path.home()),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, start_new_session=True)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            os.killpg(proc.pid, signal.SIGKILL)
            await proc.wait()
            out = f"(killed after {timeout}s)".encode()
        text = out.decode(errors="replace").strip()
        header = f"`$ {short(command, 150).replace('`', 'ˋ')}` → exit {proc.returncode}"
        if len(text) <= 1800:
            await inter.followup.send(f"{header}\n```\n{text.replace('```', 'ˋˋˋ') or '(no output)'}\n```")
        else:
            await inter.followup.send(header, file=discord.File(io.BytesIO(out), filename="output.txt"))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not config.TOKEN:
        raise SystemExit("DISCORD_TOKEN is not set. Copy ~/agents/.env.example to ~/agents/.env and fill it in.")
    AgentBot().run(config.TOKEN, log_handler=None)
