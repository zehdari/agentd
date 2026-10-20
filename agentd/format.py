"""Turning Claude Code events into short Discord-friendly text."""
import json
import re
import unicodedata

LIMIT = 1900
TOOL_DETAIL_CHARS = 160

ICONS = {
    "Bash": "💻", "Read": "📖", "Edit": "✏️", "MultiEdit": "✏️", "Write": "📝",
    "NotebookEdit": "📓", "Grep": "🔎", "Glob": "🔎", "WebFetch": "🌐",
    "WebSearch": "🌐", "Agent": "🤖", "Task": "🤖", "TodoWrite": "☑️",
}


def short(text, n: int) -> str:
    """One line, at most n characters."""
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1] + "…"


def truncate(text: str, n: int) -> str:
    """At most n characters, keeping line breaks."""
    return text if len(text) <= n else text[: n - 1] + "…"


def tool_parts(name: str, inp: dict | None) -> tuple[str, str, str]:
    """(icon, display name, full detail) for a tool call."""
    inp = inp or {}
    if name == "Bash":
        detail = "$ " + inp.get("command", "")
    elif name in ("Read", "Edit", "MultiEdit", "Write"):
        detail = inp.get("file_path", "")
    elif name == "NotebookEdit":
        detail = inp.get("notebook_path", "")
    elif name in ("Grep", "Glob"):
        detail = inp.get("pattern", "") + (f"  in {inp['path']}" if inp.get("path") else "")
    elif name == "WebFetch":
        detail = inp.get("url", "")
    elif name == "WebSearch":
        detail = inp.get("query", "")
    elif name in ("Agent", "Task"):
        detail = inp.get("description") or inp.get("prompt", "")
    elif name == "TodoWrite":
        detail = f"{len(inp.get('todos', []))} items"
    else:
        detail = json.dumps(inp, ensure_ascii=False) if inp else ""
    if name.startswith("mcp__"):
        name = ":".join(name.split("__")[1:])
    return ICONS.get(name, "🔧"), name, detail


def tool_line(name: str, inp: dict | None) -> str:
    icon, name, detail = tool_parts(name, inp)
    detail = short(detail, TOOL_DETAIL_CHARS).replace("`", "ˋ")
    return f"{icon} **{name}** `{detail}`" if detail else f"{icon} **{name}**"


def tool_full(name: str, inp: dict | None) -> str | None:
    """The whole tool call as a code block, or None if tool_line already shows all of it."""
    icon, name, detail = tool_parts(name, inp)
    detail = str(detail).strip()
    if len(detail) <= TOOL_DETAIL_CHARS and "\n" not in detail:
        return None
    lang = "bash" if name == "Bash" else ""
    return f"{icon} **{name}**\n```{lang}\n{detail.replace('```', 'ˋˋˋ')}\n```"


def tool_result_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(c.get("text", "") for c in content if isinstance(c, dict))
    return ""


def chunk(text: str, limit: int = LIMIT) -> list[str]:
    """Split text into Discord-sized messages, re-opening code fences across splits."""
    parts, cur = [], ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:
            if cur:
                parts.append(cur)
                cur = ""
            parts.append(line[:limit])
            line = line[limit:]
        if len(cur) + len(line) > limit:
            parts.append(cur)
            cur = ""
        cur += line
    if cur.strip():
        parts.append(cur)

    out, carry = [], ""
    for part in parts:
        part = carry + part
        carry = ""
        if part.count("```") % 2:
            lang = part[part.rfind("```") + 3:].split("\n", 1)[0].strip()
            part = part.rstrip("\n") + "\n```"
            carry = f"```{lang}\n"
        out.append(part)
    return out


def fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    return f"{seconds // 3600}h {seconds % 3600 // 60}m"


# Discord has no table syntax, so markdown tables become aligned monospace blocks.
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$")
_INLINE_MD = re.compile(r"\*\*(.+?)\*\*|`([^`]+)`|\[([^\]]+)\]\([^)]+\)")


def _cells(line: str) -> list[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|") and not line.endswith("\\|"):
        line = line[:-1]
    cells = re.split(r"(?<!\\)\|", line)
    return [_INLINE_MD.sub(lambda m: next(g for g in m.groups() if g), c.strip()).replace("\\|", "|")
            for c in cells]


def _width(s: str) -> int:
    w = 0
    for ch in s:
        if unicodedata.combining(ch) or ch in "\ufe0f\u200d":
            continue
        w += 2 if unicodedata.east_asian_width(ch) in "WF" else 1
    return w


def _pad(s: str, n: int, align: str) -> str:
    gap = n - _width(s)
    if align == "r":
        return " " * gap + s
    if align == "c":
        return " " * (gap // 2) + s + " " * (gap - gap // 2)
    return s + " " * gap


def _render_table(rows: list[str]) -> str:
    head, sep, body = _cells(rows[0]), rows[1], [_cells(r) for r in rows[2:]]
    ncol = len(head)
    aligns = []
    for c in sep.strip().strip("|").split("|")[:ncol]:
        c = c.strip()
        aligns.append("c" if c.startswith(":") and c.endswith(":") else "r" if c.endswith(":") else "l")
    aligns += ["l"] * (ncol - len(aligns))
    body = [(r + [""] * ncol)[:ncol] for r in body]
    widths = [max(_width(r[i]) for r in [head] + body) for i in range(ncol)]
    line = lambda r: "  ".join(_pad(c, widths[i], aligns[i]) for i, c in enumerate(r)).rstrip()
    out = [line(head), "  ".join("─" * w for w in widths)] + [line(r) for r in body]
    return "```\n" + "\n".join(out) + "\n```"


def tables_to_code(text: str) -> str:
    """Rewrite markdown tables (outside code fences) as aligned code blocks."""
    lines, out, i, fenced = text.split("\n"), [], 0, False
    while i < len(lines):
        line = lines[i]
        if line.lstrip().startswith("```"):
            fenced = not fenced
        if (not fenced and "|" in line and i + 1 < len(lines) and _TABLE_SEP.match(lines[i + 1])
                and "|" in lines[i + 1]):
            j = i + 2
            while j < len(lines) and "|" in lines[j] and lines[j].strip():
                j += 1
            out.append(_render_table(lines[i:j]))
            i = j
            continue
        out.append(line)
        i += 1
    return "\n".join(out)


# How files the user attached are named in the prompt an agent gets (from Discord or the dashboard).
ATTACHED = "[The user attached these files: "
_ATTACHED_RE = re.compile(r"^" + re.escape(ATTACHED) + r"(.*)\]$", re.M)


def split_attached(text: str) -> tuple[str, list[str]]:
    """A prompt's text without its attachment notes, and the attached paths."""
    paths = [p for m in _ATTACHED_RE.finditer(text) for p in re.split(r", (?=/)", m.group(1))]
    return (_ATTACHED_RE.sub("", text).strip(), paths) if paths else (text, [])
