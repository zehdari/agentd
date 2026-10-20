"""Agents and activity threads, persisted so they survive restarts."""
import json
import time
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

MAX_ACTIVITY_SESSIONS = 300


@dataclass
class Agent:
    thread_id: int
    cwd: str
    title: str
    model: str | None = None
    effort: str | None = None
    session_id: str | None = None
    status: str = "idle"  # idle | running | error | interrupted
    turns: int = 0
    created: float = field(default_factory=time.time)
    updated: float = field(default_factory=time.time)
    seen: float = 0.0  # last time the user looked at or replied to it (dashboard "waiting on you")
    # False for agents started on the dashboard with Discord mirroring off: no thread, and
    # thread_id is just a local id.
    discord: bool = True


class State:
    def __init__(self, path: Path):
        self.path = path
        self.agents: dict[int, Agent] = {}
        # Claude Code session id -> Discord thread id, for sessions not started by agentd.
        self.activity: dict[str, int] = {}
        # Dashboard settings. web_to_discord: give agents started on the dashboard a Discord thread.
        self.settings: dict = {"web_to_discord": False}
        if path.exists():
            data = json.loads(path.read_text())
            known = {f.name for f in fields(Agent)}
            self.agents = {
                int(k): Agent(**{"seen": v.get("updated", 0.0), **{kk: vv for kk, vv in v.items() if kk in known}})
                for k, v in data.get("agents", {}).items()
            }
            self.activity = data.get("activity", {})
            self.settings.update(data.get("settings", {}))

    def save(self) -> None:
        while len(self.activity) > MAX_ACTIVITY_SESSIONS:
            self.activity.pop(next(iter(self.activity)))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(
            {"agents": {str(k): asdict(a) for k, a in self.agents.items()}, "activity": self.activity,
             "settings": self.settings},
            indent=1,
        ))
        tmp.replace(self.path)

    def local_id(self) -> int:
        """An id for an agent with no Discord thread: microseconds since the epoch, which is
        far below any Discord snowflake, so the two never collide."""
        tid = int(time.time() * 1_000_000)
        while tid in self.agents:
            tid += 1
        return tid
