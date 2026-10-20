"""Which models agents can run on: Claude (via `claude`) and ChatGPT (via `codex`), and their effort levels."""
import json
import re
import shutil
from pathlib import Path

from . import config

CLAUDE_MODELS = ["fable", "opus", "sonnet", "haiku"]
CLAUDE_EFFORTS = ["low", "medium", "high", "xhigh", "max"]
CODEX_CACHE = Path.home() / ".codex" / "models_cache.json"

# What each alias currently resolves to. Updated from the model id in every Claude run's init event,
# so this only matters until an agent has run on that alias.
CLAUDE_RESOLVED = {"fable": "claude-fable-5-1", "opus": "claude-opus-5-5",
                   "sonnet": "claude-sonnet-5-5", "haiku": "claude-haiku-5-5"}

_codex: tuple[float, list[dict]] = (0.0, [])


def saw_claude(model_id: str | None) -> None:
    """Note which version an alias resolved to, from a run's init event."""
    for alias in CLAUDE_MODELS:
        if model_id and f"-{alias}-" in model_id:
            CLAUDE_RESOLVED[alias] = model_id


def pretty_claude(model_id: str) -> str:
    """claude-opus-5-5 -> Opus 5.5 (dated ids and [1m] suffixes too)."""
    m = re.match(r"claude-([a-z]+)-(\d+(?:-\d)?)(?:-\d{8})?(\[1m\])?$", model_id)
    if not m:
        return model_id
    return f"{m[1].capitalize()} {m[2].replace('-', '.')}" + (" (1M)" if m[3] else "")


def codex_bin() -> str | None:
    path = shutil.which("codex") or str(Path.home() / ".local" / "bin" / "codex")
    return path if Path(path).exists() else None


def codex_models() -> list[dict]:
    """The models Codex offers this account (its own cache, refreshed whenever Codex runs)."""
    global _codex
    if not codex_bin():
        return []
    try:
        mtime = CODEX_CACHE.stat().st_mtime
    except OSError:
        return []
    if mtime != _codex[0]:
        try:
            data = json.loads(CODEX_CACHE.read_text())
            models = [{"id": m["slug"], "name": m.get("display_name") or m["slug"],
                       "efforts": [lvl["effort"] for lvl in m.get("supported_reasoning_levels") or []],
                       "default_effort": m.get("default_reasoning_level")}
                      for m in data.get("models", []) if m.get("visibility") == "list" and m.get("slug")]
        except (OSError, ValueError, KeyError, TypeError):
            models = []
        _codex = (mtime, models)
    return _codex[1]


def codex_model(model: str | None) -> dict | None:
    return next((m for m in codex_models() if m["id"] == model), None)


def is_codex(model: str | None) -> bool:
    return bool(model) and (model.startswith("gpt-") or codex_model(model) is not None)


def valid(model: str | None) -> bool:
    return model in CLAUDE_MODELS or codex_model(model) is not None


def efforts_for(model: str | None) -> list[str]:
    if is_codex(model):
        m = codex_model(model)
        return m["efforts"] if m and m["efforts"] else CLAUDE_EFFORTS
    return CLAUDE_EFFORTS


def default_effort(model: str | None) -> str | None:
    """What 'default' effort means for this model."""
    if is_codex(model):
        m = codex_model(model)
        return m["default_effort"] if m else None
    if config.DEFAULT_EFFORT:
        return config.DEFAULT_EFFORT
    try:
        settings = json.loads((Path.home() / ".claude" / "settings.json").read_text())
    except (OSError, ValueError):
        return None
    if settings.get("effortLevel"):
        return settings["effortLevel"]
    name = model or "opus"
    for full, s in (settings.get("modelSettings") or {}).items():
        if name in full and isinstance(s, dict) and s.get("effortLevel"):
            return s["effortLevel"]
    return None


def label(model: str | None) -> str:
    """Display name with version: opus -> Opus 5.5, gpt-6-luna -> GPT-6-Luna."""
    m = codex_model(model)
    if m:
        return m["name"]
    if not model:
        return "default"
    return pretty_claude(CLAUDE_RESOLVED.get(model, model))


def resume_command(agent) -> str:
    """How to pick the agent's session up in a terminal."""
    if not agent.session_id:
        return ""
    cli = "codex resume" if is_codex(agent.model) else "claude --resume"
    return f"cd {agent.cwd} && {cli} {agent.session_id}"


def catalog() -> dict:
    """Everything the dashboard's pickers need."""
    default = config.DEFAULT_MODEL
    return {
        "default": {"model": default, "label": label(default), "effort": default_effort(default)},
        "groups": [
            {"name": "Claude", "models": [{"id": m, "name": label(m), "efforts": CLAUDE_EFFORTS,
                                           "default_effort": default_effort(m)} for m in CLAUDE_MODELS]},
            {"name": "ChatGPT", "models": codex_models()},
        ],
    }
