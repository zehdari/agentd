"""Branching a chat into a new one: editing one of your messages, or forking the conversation.
The new chat resumes the old session as a new one (claude --fork-session, codex exec fork), cut off
at a message, and starts with a copy of the old chat's log so the dashboard shows the history."""
import glob
import json
from pathlib import Path

from . import config

SENT_DIR = config.STATE_DIR / "sent"
LOGS = config.STATE_DIR / "logs"
CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"   # Claude Code's own session transcripts


def _transcripts(session_id: str) -> list[str]:
    return glob.glob(str(CLAUDE_PROJECTS / "*" / f"{glob.escape(session_id)}.jsonl"))


def transcript_parent(session_id: str, uuid: str) -> tuple[bool, str | None]:
    """(found, the message before it) for message `uuid` in a Claude Code session transcript.
    A conversation's first message has no message before it. One sent mid-run is filed as a
    queued_command attachment that points back to it."""
    for path in _transcripts(session_id):
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


def transcript_last(session_id: str) -> str | None:
    """The session's latest message, so a fork of the whole chat keeps to what it had when you forked it."""
    last = None
    for path in _transcripts(session_id):
        with open(path, "rb") as f:
            for raw in f:
                if b'"uuid"' not in raw:
                    continue
                try:
                    ev = json.loads(raw)
                except ValueError:
                    continue
                if ev.get("type") in ("user", "assistant") and ev.get("uuid") and not ev.get("isSidechain"):
                    last = ev["uuid"]
    return last


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


def log_size(thread_id: int) -> int:
    try:
        return (LOGS / f"{thread_id}.jsonl").stat().st_size
    except OSError:
        return 0


def copy_history(src: int, dst: int, cut: int, before: float) -> None:
    """Give a chat branched off another one the conversation so far: the log up to `cut` and the files sent before `before`."""
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


def fork_point(agent, uuid: str | None, codex: bool) -> tuple[str | None, int]:
    """Where a fork of `agent` branches off: (message to resume at, log bytes to copy).
    `uuid` forks from just before that message of yours; without it the fork takes the whole chat.
    Raises ValueError with a reason to show the user."""
    if not agent.session_id:
        raise ValueError("There's nothing to fork yet.")
    if not uuid:
        if codex:   # codex exec fork takes the whole session when the fork first runs
            return None, log_size(agent.thread_id)
        at = transcript_last(agent.session_id)
        if at is None:
            raise ValueError("Couldn't find this chat in Claude Code's sessions.")
        return at, log_size(agent.thread_id)
    if codex:
        raise ValueError("ChatGPT chats can only be forked whole.")
    found, at = transcript_parent(agent.session_id, uuid)
    if not found:
        raise ValueError("Couldn't find that message in Claude Code's session.")
    if not at:
        raise ValueError("That's the first message: start a new chat instead.")
    cut = log_cut(agent.thread_id, uuid)
    if cut is None:
        raise ValueError("Couldn't find that message in the chat's log.")
    return at, cut
