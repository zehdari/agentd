# Product

<!-- impeccable:product-schema 1 -->

> Inferred from the README, state.json, and the 2026-10-19 dashboard request. The user asked for a mockup without an interview, so every section below is an assumption until they confirm it.

## Platform

web

## Stack
Inferred: static HTML/CSS mockup only (the user asked for "just a mockup with screenshots"). A live build would sit behind agentd's localhost HTTP server (`agentd/server.py`) and be reached over Tailscale.

## Users
One person: the owner of Cam-PC, usually on their phone, sometimes at a desk. They hand Claude agents tasks across the whole OS (robotics sim work, datasets, system fixes, repo chores) and check back between other things.

## Product Purpose
agentd lets the owner start, follow, steer, and stop Claude Code agents on Cam-PC from anywhere. Discord is the current control surface. A web dashboard would add an at-a-glance view Discord can't give: every agent's state on one screen, machine health, and long jobs.

## Positioning
A personal control room for one machine and one operator. Not a team tool or an orchestration platform.

## Operating Context
- Agents run `claude -p` with bypassPermissions and passwordless sudo, one Discord thread each.
- Agent state: running, idle, queued, stopped, failed. Each has a title (its first prompt), cwd, model, effort, turn count, and session ID.
- Steering mid-run: messages are sent (📨), read (👀), or queued (📥).
- Long jobs run as `systemd-run --user` units that outlive agent turns.
- Agents send files back with `agentd-send`. Alerts go to `#alerts`; other sessions' hook events go to `#activity`.
- Machine: RTX 3090 24 GB, 16 cores, 46 GiB RAM, 1.8 TB disk, Ubuntu 22.04-era, ROS 2 Humble.

## Capabilities and Constraints
Existing commands: /new, /effort, /stop, /info, /status, /sh. Remote Control (`claude-rc`) runs as a separate service. The dashboard must not invent capabilities agentd lacks; anything new is marked as a proposal.

## Evidence on Hand
Real agent titles and turn counts in `~/.local/state/agentd/state.json`. No usage-cost data is collected.

## Product Principles
- Glanceable from a phone: what's running, what needs me, is the machine OK.
- Show the agent's own work (its tool calls and files), not summaries about it.
- Stopping is always one tap away.
- Full machine control means the interface must be unambiguous about what is acting where.
