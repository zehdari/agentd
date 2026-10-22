# agents: control this machine's Claude agents from your phone

Three ways in, all running on this machine:

| | What it's for |
|---|---|
| **Discord** (`agentd`) | Main control panel. Start agents, follow their work live, get pinged when they finish, upload or receive files, check machine health. |
| **Claude app → Code tab** (`claude-rc` service) | Remote Control of full interactive sessions anywhere on the OS. Their activity also shows up in Discord `#activity`. |
| **SSH over Tailscale** | Backup for everything else: `tmux`, `nvidia-smi`, restarting things by hand. |

## Setup

1. **Run the setup script** in a terminal on this machine. It asks for your password once.
   ```bash
   bash ~/agents/setup/setup.sh
   ```
   It sets up passwordless sudo, never-sleep, lingering user services, tmux/ssh/Tailscale, the Claude Code hooks and the two services. It is safe to re-run.
2. **Approve Remote Control once:** `tmux -L claude-rc attach`, answer `y`, then detach with `Ctrl-b d`.
3. **Tailscale:** `sudo tailscale up`, then install Tailscale on your phone and sign in to the same account.
4. **Discord setup** (about 5 minutes):
   1. Create a private Discord server for this (e.g. "Cam-PC").
   2. Go to https://discord.com/developers/applications and choose **New Application**. Under **Bot**, choose **Reset Token** and copy the token. On the same page, turn on **Message Content Intent**.
   3. Under **OAuth2 → URL Generator**, tick the scopes `bot` and `applications.commands`, and the bot permission `Administrator`. This is fine on a private server. Open the URL and add the bot to your server.
   4. `cp ~/agents/.env.example ~/agents/.env`, paste the token after `DISCORD_TOKEN=`, then `systemctl --user restart agentd`.

   The bot creates a **Claude** category with `#agents`, `#alerts` and `#activity`, and says hi in `#alerts`. Only you, the app's owner, can control it.
5. On your phone, open Discord and set `#alerts` notifications to **All Messages**. Mute `#activity` if it's too chatty. Agent threads ping you with an @mention, so `#agents` can stay on the default (mentions only).

## Using it

- **Start an agent:** post the task in `#agents`. To set the working directory, start the message with a path:
  `~/ball_balancer retrain with the new reward and send me the reward curve`
  Or use `/new task:… directory:… model:… effort:…`.
- **Thinking effort** (`low` → `max`): choose it on `/new`, change it mid-conversation with `/effort` inside a thread (it applies from the next turn), or set a default with `AGENTD_DEFAULT_EFFORT` in `.env`. If none of these is set, agents use your Claude Code default, which is the `effortLevel` in `~/.claude/settings.json`.
- **Each agent gets a thread** showing its replies and a live, condensed log of its tool calls. Long commands are cut off in the log; tap **📜 Expand** under it to show them whole in place, and **Collapse** to hide them again. Reactions on your message show where it's at: ⏳ running, 📨/👀 steering message sent/read, 📥 queued, ✅ done, ❌ failed, ⏹️ stopped. While it runs, your message also has a 🛑 reaction: tap it to stop the agent (same as `/stop`). When a run finishes or fails, the thread @mentions you, once.
- **Reply in the thread** to continue the same session. Attach files and the agent gets them.
- **Steer it mid-run:** reply while it's working and the message goes straight to the running agent, which reads it after its current step (one tool call), the same as typing into Claude Code while it works. 📨 means sent, 👀 means the agent has read it. If it was already wrapping up, it handles your message as a follow-up turn right after. If the agent ends its turn while waiting on a background task (e.g. "I'll tell you when the upload finishes"), the run stays open: it reports back when the task finishes, and anything you send in the meantime gets answered straight away. A message only waits in the queue (📥) if it arrives in the second or so between runs.
- **`/stop`** stops the agent in the current thread. **`/info`** shows its session ID and the command to pick it up at the keyboard (`claude --resume …`).
- **`/status`** shows load, RAM, disk, GPU, running agents and recent ones.
- **`/sh command:…`** runs a quick shell command without an agent.
- `#alerts` is only for things with no agent thread: a terminal/Remote Control session that needs permission, or `agentd-send --alert` from a plain shell.
- **Agents send you files** with `agentd-send plot.png -m "reward curve"`. Add `--alert` to ping you (in the agent's thread). It also works from any shell on the machine; without an agent thread it posts to `#alerts`.
- **Agents talk to you** with `agentd-say "text"`: a voice message with the text as its caption, made by the `agentd-tts` service (port 8781). It speaks with Higgs TTS 3 through voiceclone's TTS server when that runs the higgs engine, cloning a stock British voice from `~/voiceclone/data/stock/<voice>/` (`clarke`, `yearsley`, `evers`, `broom_salesman`, `chadwick`), and falls back to Kokoro (`bm_george`) otherwise. Set `AGENTD_VOICE` / `AGENTD_FALLBACK_VOICE` in `.env`, then `systemctl --user restart agentd-tts`. `agentd-say --voice NAME` overrides it once; `-o out.mp3` just writes the file.
- **Long jobs** (training, Isaac Sim): agents are told to start them with `systemd-run --user …`, so they survive the agent's turn and agentd restarts.

## Web dashboard

agentd also serves a dashboard at **http://cam-pc:8780** (or `http://<tailscale IP>:8780`). Open it on your phone with Tailscale connected; adding it to your home screen makes it feel like an app.

- **Pull down to refresh** at the top of the Agents and System pages, like a native app.
- **Waiting on you:** agents that finished since you last looked, and terminal/Remote Control sessions waiting on a permission prompt.
- **Agents:** each agent's state, current command and a timeline of the last 2 hours. Each tick is a tool call, and ▲ marks a message you sent mid-run. Stop agents from here (tap twice).
- **Agent page:** the conversation with tool calls and the files it sent (images inline). Reply or steer from the message box (it is also posted in the Discord thread). Attach files with the paperclip, by pasting a screenshot, or by dropping them on the chat; the new-agent form takes files too. They land in the agent's inbox like Discord uploads, and ones under Discord's 10 MB cap are mirrored into the thread, change effort, copy the resume command.
- **Edit a message into a new chat:** tap **Edit** under any message you sent (with a mouse, point at it), change it and tap **Start new chat**. The new chat picks up the conversation from just before that message, so the agent remembers everything up to there and nothing after. Files attached to the message go along. The original chat stays as it was, and the new one links back to it under Details → Edited from. In ChatGPT chats only the first message can be edited, which starts over.
- **Voice messages** play in the chat (play/pause, tap the bar to seek). New ones play by themselves while the dashboard is open, after you've tapped the page once (phones require that). Turn this off under Settings → Voice.
- **Machine, long jobs, files:** GPU/CPU/RAM over the last 2 hours, disk, `systemd-run` jobs agents started (with stop), and files sent with `agentd-send`.
- **New agent:** starts an agent like posting in `#agents` does. By default it stays on the dashboard: no Discord thread and no Discord pings.
- **Settings** (desktop rail; on the phone, at the bottom of System): **Discord → Post agents started here to Discord** gives new dashboard agents a thread in `#agents` like before. Off by default. Agents started from Discord keep their threads either way, and replies you send them from the dashboard also show up there.

Only this machine and devices logged in to your own Tailscale account get in (checked with `tailscale whois` on every new device). Everyone else gets a 403. Set `AGENTD_WEB_PORT=0` in `.env` to turn it off. Theme follows your phone (light/dark); the toggle at the bottom of the side rail overrides it.

## Agents have full control

Agents run with `bypassPermissions` and passwordless sudo. Anyone who controls your Discord account controls this machine, so turn on 2FA for Discord. Agents can read everything in your home directory, including `~/.ssh` and `~/.git-credentials`.

## Operating it

```bash
systemctl --user status agentd claude-rc     # health
journalctl --user -u agentd -f               # bot logs
tmux -L claude-rc attach                     # Remote Control console (Ctrl-b d to detach)
systemctl --user restart agentd              # after editing code or .env (cuts off running agents)
```

State lives in `~/.local/state/agentd/`: `state.json`, raw stream logs per thread in `logs/`, and uploaded files in `inbox/`.

## Layout

- `agentd/bot.py`: Discord client, slash commands, agent run loop
- `agentd/runner.py`: one `claude -p` run with stream-json in and out (so messages can be fed in mid-turn), plus the system prompt agents get
- `agentd/activity.py`: mirrors hook events from other sessions into `#activity`
- `agentd/server.py`: localhost HTTP (`/hook`, `/send`, `/health`)
- `agentd/web.py` + `agentd/web/`: the dashboard (JSON API and a plain HTML/CSS/JS front end)
- `hooks/relay.sh`: global Claude Code hook that forwards events to agentd
- `tts/server.py`: the `agentd-tts` text-to-speech service behind `bin/agentd-say`
- `bin/agentd-send`, `bin/agentd-say`, `bin/claude-rc-loop`, `systemd/*.service`, `setup/setup.sh`
