"""The EPUB reader's voice-server API (API.md) with Reverb-TTS.

The reader and the voice studio speak the same API as epub-reader-tts-server's Breeze server: a multipart
POST /v1/audio/speech that streams 16-bit PCM back, and GET /health. This serves it with Reverb-TTS
(software-mansion/Reverb-TTS, through its Python package), in this process, on the GPU:

  GET  /health               {"status": "ok", "sample_rate": 24000, "languages": [...], "instructions": false}
                             once the model is loaded; 503 {"status": "loading"} until then
  POST /v1/audio/speech      text, ref_audio + ref_text (the clip and its exact transcript: Reverb needs both),
                             seed, language (the book's, e.g. "en-GB"), speaker_id (a preset voice, instead of a
                             clip), cfg_scale (accepted; Reverb has no such setting) -> s16le PCM
  GET  /v1/builtin-voices    Reverb's preset voices (for the studio)
  GET  /v1/builtin-voices/{id}/preview   a short sample in a preset voice (made the first time it is asked for)

A clip is made into a voice once (its codec codes: keyed by the clip's SHA-256, its transcript and language) and
spoken with afterwards, so it isn't encoded again for every paragraph. Requests run one at a time on the GPU, in
order; one whose caller has gone while it waited is skipped. `instruction` (Breeze's voice design) has no Reverb
equivalent: with a clip it is ignored, without one it is refused (400).

Environment:
  REVERB_VARIANT     the weights: fp16 (default), fp32, int8 or int4 (all run at the same precision on the GPU)
  REVERB_DEVICE      cuda, mps or cpu (default: the first available)
  REVERB_STEPS       the sampler's main passes (default 8, Reverb's tuned value)
  DEFAULT_LANGUAGE   when a request names none (default en)
  HF_HOME            where the weights are kept (the compose file mounts ./models there)
  REVERB_LOAD        0: don't load the model (the tests)
  REVERB_HOST, REVERB_PORT   default 0.0.0.0, 7860
"""

from __future__ import annotations

import asyncio
import collections
import hashlib
import io
import logging
import math
import os
import re
import subprocess
import threading
import wave
from contextlib import asynccontextmanager

import numpy as np
import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, Response, StreamingResponse

VARIANT = os.environ.get("REVERB_VARIANT", "fp16")
DEVICE = os.environ.get("REVERB_DEVICE") or None
STEPS = int(os.environ.get("REVERB_STEPS", "8"))
DEFAULT_LANGUAGE = os.environ.get("DEFAULT_LANGUAGE", "en")
LANGUAGES = ["de", "en", "es", "fr", "it", "pl", "pt"]  # Reverb's seven, as language subtags
CHUNK = 32768  # bytes of PCM per streamed piece
VOICE_CACHE = 64  # clip voices kept in memory
PREVIEW_TEXT = {
    "en": "Hello! This is how I sound when I read a book aloud.",
    "de": "Hallo! So klinge ich, wenn ich ein Buch vorlese.",
    "fr": "Bonjour ! Voici comment je sonne quand je lis un livre à voix haute.",
    "es": "¡Hola! Así sueno cuando leo un libro en voz alta.",
    "it": "Ciao! Ecco come suono quando leggo un libro ad alta voce.",
    "pl": "Dzień dobry! Tak brzmię, gdy czytam książkę na głos.",
    "pt": "Olá! É assim que eu soo quando leio um livro em voz alta.",
}

log = logging.getLogger("reverb")


def load_model():
    """The model, its tokenizer and codec (downloaded the first time, into HF_HOME), and its preset voices: fetched
    once beside them and read from there, so listing them doesn't ask the Hub on every request, nor need it at all
    once downloaded."""
    from pathlib import Path

    from huggingface_hub import snapshot_download
    from reverb_tts import TTS, SamplerConfig
    from reverb_tts.tts import REPOSITORY, REVISION
    tts = TTS.from_pretrained(variant=VARIANT, device=DEVICE, sampler=SamplerConfig(steps=STEPS))
    tts.presets = Path(snapshot_download(REPOSITORY, revision=REVISION, allow_patterns=["voices/*.json"])) / "voices"
    return tts


@asynccontextmanager
async def lifespan(app: FastAPI):
    st = app.state
    st.voices = collections.OrderedDict()  # (clip SHA-256, transcript, language) -> Voice
    st.previews = {}                       # preset name -> WAV bytes
    st.gpu = asyncio.Lock()                # one generation at a time
    st.error = ""
    if getattr(st, "tts", None) is None and os.environ.get("REVERB_LOAD", "1") != "0":   # (tests put a stand-in there first)
        st.tts = None

        def load():
            try:
                st.tts = load_model()
                log.info("Reverb loaded: %s on %s", VARIANT, st.tts.device)
            except Exception as exc:  # shown by /health and in the log
                st.error = f"{type(exc).__name__}: {exc}"
                log.exception("Reverb couldn't be loaded")
        threading.Thread(target=load, daemon=True).start()
    yield


app = FastAPI(lifespan=lifespan)


def reverb_language(given: str | None) -> str:
    """Reverb's code for a book language such as "en-GB", "pt-BR" or "fr" (its primary subtag)."""
    if not given or not given.strip():
        return DEFAULT_LANGUAGE
    code = re.split(r"[-_]", given.strip().lower())[0]
    if code not in LANGUAGES:
        raise HTTPException(400, f"Language '{given}' isn't one Reverb speaks (it speaks {', '.join(LANGUAGES)}).")
    return code


def ready(request: Request):
    tts = request.app.state.tts
    if tts is None:
        raise HTTPException(503, f"Reverb couldn't be loaded: {request.app.state.error}" if request.app.state.error
                            else "Reverb is still loading.")
    return tts


@app.get("/health")
async def health(request: Request):
    st = request.app.state
    if st.tts is None:
        return JSONResponse({"status": "error", "detail": st.error} if st.error else {"status": "loading"}, status_code=503)
    return {"status": "ok", "sample_rate": st.tts.sample_rate, "languages": LANGUAGES, "instructions": False,
            "engine": "reverb"}


def decode_clip(clip: bytes, rate: int) -> np.ndarray:
    """Any recording ffmpeg reads -> mono float32 samples at rate."""
    try:
        out = subprocess.run(["ffmpeg", "-v", "error", "-i", "pipe:0", "-ac", "1", "-ar", str(rate), "-f", "f32le", "pipe:1"],
                             input=clip, capture_output=True, timeout=120, check=True).stdout
    except subprocess.CalledProcessError as exc:
        raise HTTPException(400, f"The reference clip couldn't be read: {exc.stderr.decode(errors='replace').strip()[:300]}") from None
    samples = np.frombuffer(out, dtype="<f4")
    if not samples.size:
        raise HTTPException(400, "The reference clip has no audio.")
    return samples


async def clip_voice(request: Request, tts, clip: bytes, transcript: str, language: str):
    """The voice of a clip (made the first time it is seen with this transcript and language)."""
    st = request.app.state
    key = (hashlib.sha256(clip).hexdigest(), transcript, language)
    if key in st.voices:
        st.voices.move_to_end(key)
        return st.voices[key]
    samples = await asyncio.to_thread(decode_clip, clip, tts.sample_rate)
    async with st.gpu:
        if key not in st.voices:
            try:
                st.voices[key] = await asyncio.to_thread(tts.voice, samples, transcript, language, sample_rate=tts.sample_rate)
            except ValueError as exc:
                raise HTTPException(400, f"Reverb couldn't use the reference clip: {exc}") from None
            while len(st.voices) > VOICE_CACHE:
                st.voices.popitem(last=False)
    return st.voices[key]


def preset(tts, name: str):
    try:
        return tts.preset_voice(name)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


def to_s16(audio: np.ndarray) -> bytes:
    a = np.nan_to_num(np.asarray(audio, dtype=np.float32), nan=0.0, posinf=1.0, neginf=-1.0)
    return np.rint(np.clip(a, -1.0, 1.0) * 32767).astype("<i2").tobytes()


@app.post("/v1/audio/speech")
async def speech(request: Request, text: str = Form(...), instruction: str | None = Form(None),
                 cfg_scale: float = Form(1.0), ref_audio: UploadFile | None = File(None),
                 ref_text: str = Form(""), seed: int = Form(42), language: str | None = Form(None),
                 speaker_id: str | None = Form(None)):
    if not math.isfinite(cfg_scale) or cfg_scale <= 0:
        raise HTTPException(400, "cfg_scale must be greater than 0.")
    if not text.strip():
        raise HTTPException(400, "text is empty.")
    clip = await ref_audio.read() if ref_audio is not None and ref_audio.filename else None
    if clip is not None and not clip:
        raise HTTPException(400, "Reference audio is empty.")
    if clip is not None and not ref_text.strip():
        raise HTTPException(400, "Reverb needs the reference clip's exact transcript (ref_text).")
    if clip is None and not speaker_id:
        raise HTTPException(400, "This voice server (Reverb-TTS) speaks in the voice of a clip or a preset voice: "
                                 + ("it can't make a voice from an instruction." if instruction and instruction.strip()
                                    else "give the voice a reference clip (ref_audio, ref_text) or a speaker_id."))
    lang = reverb_language(language)
    tts = ready(request)
    st = request.app.state
    voice = await clip_voice(request, tts, clip, ref_text.strip(), lang) if clip is not None else preset(tts, speaker_id)

    async with st.gpu:
        # (A caller that went away while this waited its turn, as the reader does when it jumps: not made.)
        if await request.is_disconnected():
            return Response(status_code=499)
        try:
            audio = await asyncio.to_thread(tts.generate, text, voice, lang, seed=seed % 2**31)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
    pcm = to_s16(audio)

    async def body():
        for i in range(0, len(pcm), CHUNK):
            yield pcm[i:i + CHUNK]

    return StreamingResponse(body(), media_type="audio/pcm", headers={
        "X-Sample-Rate": str(tts.sample_rate), "X-Sample-Format": "s16le", "Cache-Control": "no-store"})


PRESET_NAME = re.compile(r"^(?P<lang>[a-z]{2})_(?P<gender>male|female)_(?P<style>[a-z]+)(?:_(?P<n>\d+))?$")


def describe(name: str) -> dict:
    m = PRESET_NAME.match(name)
    if not m:
        return {"id": name, "name": name, "language": "", "style": "", "gender": "", "preview": True}
    title = m["style"].capitalize() + (f" {m['n']}" if m["n"] else "")
    return {"id": name, "name": title, "language": m["lang"], "style": m["style"], "gender": m["gender"], "preview": True}


@app.get("/v1/builtin-voices")
async def builtin_voices(request: Request):
    """Reverb's preset voices: [{"id", "name", "language", "style", "gender"}], English first, then by language."""
    tts = ready(request)
    try:
        names = await asyncio.to_thread(tts.preset_voices)
    except Exception as exc:
        raise HTTPException(502, f"The preset voices couldn't be listed: {exc}") from None
    out = [describe(n) for n in names]
    return {"voices": sorted(out, key=lambda v: (v["language"] != "en", v["language"], v["name"]))}


def wav_bytes(audio: np.ndarray, rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes(to_s16(audio))
    return buf.getvalue()


@app.get("/v1/builtin-voices/{voice_id}/preview")
async def builtin_preview(request: Request, voice_id: str):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", voice_id):
        raise HTTPException(404, "Not Found")
    tts = ready(request)
    st = request.app.state
    if voice_id not in st.previews:
        try:
            voice = await asyncio.to_thread(tts.preset_voice, voice_id)
        except ValueError:
            raise HTTPException(404, "Not Found") from None
        lang = voice.language if voice.language in PREVIEW_TEXT else "en"
        async with st.gpu:
            audio = await asyncio.to_thread(tts.generate, PREVIEW_TEXT[lang], voice, lang, seed=42)
        st.previews[voice_id] = wav_bytes(audio, tts.sample_rate)
    return Response(st.previews[voice_id], media_type="audio/wav", headers={"Cache-Control": "no-cache"})


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    uvicorn.run(app, host=os.environ.get("REVERB_HOST", "0.0.0.0"), port=int(os.environ.get("REVERB_PORT", "7860")))
