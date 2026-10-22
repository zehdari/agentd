"""Local text-to-speech for agents (Kokoro-82M): POST /tts {"text", "voice"?, "speed"?} -> MP3.

Runs as the agentd-tts user service on 127.0.0.1:8781; `agentd-say` is the client.
"""
import asyncio
import os
import subprocess

import numpy as np
from aiohttp import web
from kokoro import KModel, KPipeline

PORT = int(os.environ.get("AGENTD_TTS_PORT", "8781"))
VOICE = os.environ.get("AGENTD_VOICE", "am_michael")
SPEED = float(os.environ.get("AGENTD_VOICE_SPEED", "0.85"))
RATE = 24000
GAP = np.zeros(int(RATE * 0.35), dtype=np.float32)   # pause between Kokoro's sentence chunks

model = KModel(repo_id="hexgrad/Kokoro-82M").to("cuda").eval()
pipelines: dict[str, KPipeline] = {}   # one per language; the voice name's first letter picks it
lock = asyncio.Lock()


def synth(text: str, voice: str, speed: float) -> bytes:
    lang = voice[0]
    if lang not in pipelines:
        pipelines[lang] = KPipeline(lang_code=lang, repo_id="hexgrad/Kokoro-82M", model=model)
    parts = []
    for r in pipelines[lang](text, voice=voice, speed=speed):
        if r.audio is not None:
            parts += [r.audio.cpu().numpy().astype(np.float32), GAP]
    if not parts:
        raise ValueError("nothing to say")
    pcm = (np.clip(np.concatenate(parts), -1, 1) * 32767).astype("<i2").tobytes()
    return subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-f", "s16le", "-ar", str(RATE), "-ac", "1", "-i", "-",
         "-codec:a", "libmp3lame", "-b:a", "64k", "-f", "mp3", "-"],
        input=pcm, capture_output=True, check=True).stdout


async def tts(request: web.Request) -> web.Response:
    try:
        body = await request.json()
        text = str(body.get("text", "")).strip()
        voice = str(body.get("voice") or VOICE)
        speed = float(body.get("speed") or SPEED)
    except (ValueError, TypeError):
        return web.json_response({"error": "expected JSON {text, voice?, speed?}"}, status=400)
    if not text:
        return web.json_response({"error": "no text"}, status=400)
    async with lock:
        try:
            mp3 = await asyncio.to_thread(synth, text, voice, speed)
        except Exception as e:   # bad voice name, ffmpeg failure, ...
            return web.json_response({"error": f"{type(e).__name__}: {e}"}, status=400)
    return web.Response(body=mp3, content_type="audio/mpeg")


async def health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "voice": VOICE, "speed": SPEED})


app = web.Application(client_max_size=1024 * 1024)
app.add_routes([web.post("/tts", tts), web.get("/health", health)])

if __name__ == "__main__":
    synth("Ready.", VOICE, SPEED)   # warm up: loads the voice and compiles kernels before the first request
    web.run_app(app, host="127.0.0.1", port=PORT, access_log=None, print=None)
