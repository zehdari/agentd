"""Localhost HTTP endpoints: hook events in, files out to Discord."""
import logging

import discord
from aiohttp import web

from . import config

log = logging.getLogger(__name__)


async def start_server(bot) -> web.AppRunner:
    async def hook(request: web.Request) -> web.Response:
        try:
            event = await request.json()
        except ValueError:
            return web.Response(status=400, text="expected JSON")
        bot.relay.submit(event)
        return web.Response(text="ok")

    async def send(request: web.Request) -> web.Response:
        try:
            body = await request.json()
            await bot.deliver(body.get("thread_id"), body.get("files") or [],
                              body.get("message") or "", bool(body.get("alert")))
        except (ValueError, OSError, discord.HTTPException) as e:
            return web.json_response({"ok": False, "error": str(e)}, status=400)
        return web.json_response({"ok": True})

    async def health(request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "discord": bot.ch_agents is not None, "running": len(bot.runs)})

    app = web.Application(client_max_size=8 * 1024 * 1024)
    app.add_routes([web.post("/hook", hook), web.post("/send", send), web.get("/health", health)])
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", config.PORT).start()
    log.info("listening on 127.0.0.1:%d", config.PORT)
    return runner
