#!/usr/bin/env bash
# One-time setup for controlling this machine's Claude agents from a phone.
# Run as your normal user (not with sudo); it asks for your password once.
# Safe to re-run.
set -euo pipefail

[ "$(id -u)" -eq 0 ] && { echo "Run this as your normal user, not root."; exit 1; }
AGENTS="$HOME/agents"
U="$USER"
step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

step "Passwordless sudo for $U"
tmp=$(mktemp)
echo "$U ALL=(ALL) NOPASSWD:ALL" > "$tmp"
sudo visudo -cf "$tmp" >/dev/null   # validate before installing; a broken sudoers file locks out sudo
sudo install -m 440 -o root -g root "$tmp" "/etc/sudoers.d/90-$U-nopasswd"
rm -f "$tmp"

step "Pausing automatic apt updates until reboot (they hold the apt locks for a long time)"
sudo systemctl stop apt-daily.timer apt-daily-upgrade.timer apt-daily.service apt-daily-upgrade.service

step "Packages: tmux, openssh-server, jq"
# Wait (up to 15 min) for unattended-upgrades instead of failing on the dpkg lock.
APT=(sudo apt-get -o DPkg::Lock::Timeout=900)
"${APT[@]}" update -qq
"${APT[@]}" install -y -qq tmux openssh-server jq
"${APT[@]}" install -y -qq --no-install-recommends tigervnc-standalone-server tigervnc-tools xfce4 xfce4-terminal dbus-x11 novnc   # remote desktop

step "Never sleep or suspend"
sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target >/dev/null
gsettings set org.gnome.settings-daemon.plugins.power sleep-inactive-ac-type 'nothing' 2>/dev/null || true

step "Keep user services running without a login session"
sudo loginctl enable-linger "$U"

step "Tailscale"
# Same steps as tailscale.com/install.sh, but with apt waiting on background apt jobs.
if ! command -v tailscale >/dev/null; then
  . /etc/os-release
  curl -fsSL "https://pkgs.tailscale.com/stable/ubuntu/$VERSION_CODENAME.noarmor.gpg" \
    | sudo tee /usr/share/keyrings/tailscale-archive-keyring.gpg >/dev/null
  curl -fsSL "https://pkgs.tailscale.com/stable/ubuntu/$VERSION_CODENAME.tailscale-keyring.list" \
    | sudo tee /etc/apt/sources.list.d/tailscale.list >/dev/null
  "${APT[@]}" update -qq
  "${APT[@]}" install -y -qq tailscale
fi
tailscale version | head -1

step "Stray git repo in home directory"
if [ -d "$HOME/.git" ]; then
  mv "$HOME/.git" "$HOME/.git-home-backup"
  echo "moved ~/.git -> ~/.git-home-backup (it only held a 'first commit'; real history is on github.com/zehdari/swarm)"
else
  echo "none"
fi

step "Python environment for agentd"
if [ ! -x "$AGENTS/.venv/bin/python" ]; then
  uv venv -q --python /usr/bin/python3 "$AGENTS/.venv"
fi
uv pip install -q --python "$AGENTS/.venv/bin/python" -r "$AGENTS/requirements.txt"
mkdir -p "$HOME/.local/bin"
ln -sf "$AGENTS/bin/agentd-send" "$HOME/.local/bin/agentd-send"
ln -sf "$AGENTS/bin/agentd-say" "$HOME/.local/bin/agentd-say"

step "Text-to-speech for agentd-say (Kokoro)"
if [ ! -x "$AGENTS/tts/.venv/bin/python" ]; then
  uv venv -q --python 3.12 "$AGENTS/tts/.venv"
fi
command -v espeak-ng >/dev/null || sudo apt-get install -y -qq espeak-ng
uv pip install -q --python "$AGENTS/tts/.venv/bin/python" --torch-backend=cu128 -r "$AGENTS/tts/requirements.txt"

step "Claude Code settings: activity hooks + no bypass-mode warning dialog"
python3 - "$AGENTS/hooks/relay.sh" <<'EOF'
import json, os, shutil, sys, time
relay_cmd = sys.argv[1]
path = os.path.expanduser("~/.claude/settings.json")
settings = json.load(open(path)) if os.path.exists(path) else {}
if os.path.exists(path):
    shutil.copy(path, f"{path}.bak-{int(time.time())}")
relay = {"type": "command", "command": relay_cmd, "timeout": 5}
hooks = settings.setdefault("hooks", {})
for event in ["UserPromptSubmit", "PreToolUse", "Stop", "Notification", "SessionEnd"]:
    groups = hooks.setdefault(event, [])
    if not any(h.get("command") == relay_cmd for g in groups for h in g.get("hooks", [])):
        groups.append({"matcher": "*", "hooks": [relay]} if event == "PreToolUse" else {"hooks": [relay]})
settings["skipDangerousModePermissionPrompt"] = True
json.dump(settings, open(path, "w"), indent=2)
print("updated", path)
EOF

step "Services"
mkdir -p "$HOME/.config/systemd/user"
cp "$AGENTS/systemd/agentd.service" "$AGENTS/systemd/claude-rc.service" "$AGENTS/systemd/agentd-tts.service" "$AGENTS/systemd/agentd-desktop.service" "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
systemctl --user enable --now agentd-tts.service
systemctl --user enable --now claude-rc.service
systemctl --user enable --now agentd-desktop.service
systemctl --user enable agentd.service
if grep -q '^DISCORD_TOKEN=.\+' "$AGENTS/.env" 2>/dev/null; then
  systemctl --user restart agentd.service
  echo "agentd started"
else
  echo "agentd enabled but not started: put your bot token in $AGENTS/.env, then: systemctl --user start agentd"
fi

cat <<EOF

Done. Remaining one-time steps:
  1. Approve Remote Control once:   tmux -L claude-rc attach   -> answer 'y', then detach with Ctrl-b d
  2. Connect Tailscale:             sudo tailscale up           (then install Tailscale on your phone)
  3. Discord bot:                   see $AGENTS/README.md ("Discord setup")
EOF
