"""A one-glance summary of the machine for /status."""
import os
import shutil
import subprocess

from .format import fmt_duration


def _run(*cmd: str) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def snapshot() -> str:
    load1, load5, _ = os.getloadavg()
    meminfo = {}
    with open("/proc/meminfo") as f:
        for line in f:
            key, value = line.split(":", 1)
            meminfo[key] = int(value.split()[0]) * 1024
    mem_used = meminfo["MemTotal"] - meminfo["MemAvailable"]
    disk = shutil.disk_usage("/")
    with open("/proc/uptime") as f:
        uptime = float(f.read().split()[0])

    gib = 1024 ** 3
    lines = [
        f"🖥️ load {load1:.1f} / {load5:.1f} ({os.cpu_count()} cores) · up {fmt_duration(uptime)}",
        f"🧠 RAM {mem_used / gib:.1f} / {meminfo['MemTotal'] / gib:.0f} GiB · 💾 disk {disk.used / gib:.0f} / {disk.total / gib:.0f} GiB",
    ]
    gpus = _run("nvidia-smi", "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu",
                "--format=csv,noheader,nounits")
    for row in filter(None, gpus.splitlines()):
        name, util, used, total, temp = [x.strip() for x in row.split(",")]
        lines.append(f"🎮 {name}: {util}% · {int(used) / 1024:.1f} / {int(total) / 1024:.0f} GiB · {temp}°C")
    rc = _run("systemctl", "--user", "is-active", "claude-rc") or "unknown"
    lines.append(f"📱 Remote Control service: {rc}")
    return "\n".join(lines)
