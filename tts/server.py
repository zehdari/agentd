"""Local speech for agents: POST /tts {"text", "voice"?, "speed"?, "fast"?} -> MP3, and POST /stt
(any audio a browser records) -> {"text"}.

Speaks with Higgs TTS 3 through voiceclone's TTS server (:8793) when that is running the higgs
engine, cloning a stock voice from ~/voiceclone/data/stock/<voice>/; otherwise, or with "fast", with
Kokoro-82M here. Listens with Whisper large-v3-turbo (faster-whisper).
Runs as the agentd-tts user service on 127.0.0.1:8781; `agentd-say` and the dashboard's voice mode use it.
"""
import asyncio
import os
import subprocess

import aiohttp
import numpy as np
import torch
from aiohttp import web
from faster_whisper import WhisperModel
from kokoro import KModel, KPipeline

PORT = int(os.environ.get("AGENTD_TTS_PORT", "8781"))
VOICE = os.environ.get("AGENTD_VOICE", "clarke")                 # a Higgs stock voice, or a Kokoro voice name
FALLBACK = os.environ.get("AGENTD_FALLBACK_VOICE", "bm_george")   # Kokoro, when Higgs can't speak
SPEED = float(os.environ.get("AGENTD_VOICE_SPEED", "0.85"))       # Kokoro only
HIGGS = os.environ.get("AGENTD_HIGGS_URL", "http://127.0.0.1:8793")
RATE = 24000
GAP = np.zeros(int(RATE * 0.35), dtype=np.float32)   # pause between Kokoro's sentence chunks

DEVICE = "cuda" if torch.cuda.is_available() else "cpu"   # CPU while the GPU is passed to the Windows VM
model = KModel(repo_id="hexgrad/Kokoro-82M").to(DEVICE).eval()
pipelines: dict[str, KPipeline] = {}   # one per language; the voice name's first letter picks it
lock = asyncio.Lock()
WHISPER = os.environ.get("AGENTD_WHISPER", "mobiuslabsgmbh/faster-whisper-large-v3-turbo")
ears = WhisperModel(WHISPER, device=DEVICE, compute_type="float16" if DEVICE == "cuda" else "int8")
ears_lock = asyncio.Lock()   # its own, so listening never waits behind a long Higgs reply


def mp3(src: bytes, *fmt: str) -> bytes:
    return subprocess.run(
        ["ffmpeg", "-loglevel", "error", *fmt, "-i", "-", "-ac", "1", "-codec:a", "libmp3lame", "-b:a", "64k", "-f", "mp3", "-"],
        input=src, capture_output=True, check=True).stdout


def kokoro_voice(voice: str) -> bool:
    return len(voice) > 3 and voice[1] in "fm" and voice[2] == "_"   # am_michael, bf_emma, ...


async def higgs(text: str, voice: str) -> bytes | None:
    """WAV from voiceclone's Higgs server, or None if it isn't up with the higgs engine."""
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=600)) as http:
            async with http.get(f"{HIGGS}/health") as r:
                h = await r.json()
            if h.get("engine") != "higgs" or not h.get("ready"):
                return None
            async with http.post(f"{HIGGS}/tts", json={"text": text, "voice": voice}) as r:
                return await r.read() if r.status == 200 else None
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        return None


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
    return mp3(pcm, "-f", "s16le", "-ar", str(RATE), "-ac", "1")


async def tts(request: web.Request) -> web.Response:
    try:
        body = await request.json()
        text = str(body.get("text", "")).strip()
        voice = str(body.get("voice") or (FALLBACK if body.get("fast") else VOICE))
        speed = float(body.get("speed") or SPEED)
    except (ValueError, TypeError):
        return web.json_response({"error": "expected JSON {text, voice?, speed?}"}, status=400)
    if not text:
        return web.json_response({"error": "no text"}, status=400)
    async with lock:
        try:
            wav = None if kokoro_voice(voice) else await higgs(text, voice)
            if wav:
                out = await asyncio.to_thread(mp3, wav)
            else:
                out = await asyncio.to_thread(synth, text, voice if kokoro_voice(voice) else FALLBACK, speed)
        except Exception as e:   # bad voice name, ffmpeg failure, ...
            return web.json_response({"error": f"{type(e).__name__}: {e}"}, status=400)
    return web.Response(body=out, content_type="audio/mpeg",
                        headers={"X-Engine": "higgs" if wav else "kokoro"})


def transcribe(audio: bytes) -> str:
    pcm = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", "-", "-ac", "1", "-ar", "16000", "-f", "f32le", "-"],
                         input=audio, capture_output=True, check=True).stdout
    segments, _ = ears.transcribe(np.frombuffer(pcm, np.float32), language="en", beam_size=1, vad_filter=True)
    return " ".join(s.text.strip() for s in segments).strip()


async def stt(request: web.Request) -> web.Response:
    audio = await request.read()
    if not audio:
        return web.json_response({"error": "no audio"}, status=400)
    async with ears_lock:
        try:
            text = await asyncio.to_thread(transcribe, audio)
        except subprocess.CalledProcessError as e:
            return web.json_response({"error": f"couldn't decode the audio: {e.stderr.decode(errors='replace')[-200:]}"}, status=400)
    return web.json_response({"text": text})


async def health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "voice": VOICE, "fallback": FALLBACK, "speed": SPEED})


app = web.Application(client_max_size=32 * 1024 * 1024)
app.add_routes([web.post("/tts", tts), web.post("/stt", stt), web.get("/health", health)])

if __name__ == "__main__":
    synth("Ready.", FALLBACK, SPEED)   # warm up: loads the voice and compiles kernels before the first request
    web.run_app(app, host="127.0.0.1", port=PORT, access_log=None, print=None)
