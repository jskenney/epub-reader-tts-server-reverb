"""Tests of the reader's API (API.md) with a stand-in for Reverb: no model, no GPU.

    pytest reverb/test_app.py     (with fastapi, httpx, numpy, python-multipart, uvicorn; and ffmpeg for the clips)
"""

import io
import shutil
import struct
import wave

import numpy as np
import pytest
from fastapi.testclient import TestClient

import os

os.environ["REVERB_LOAD"] = "0"  # (no model: the stand-in below)
import app as server  # noqa: E402

RATE = 24000


class FakeVoice:
    def __init__(self, name, language, transcript=""):
        self.name, self.language, self.text = name, language, transcript


class FakeTTS:
    """Reverb's TTS as the server uses it: voice(), generate(), preset_voices(), preset_voice(), sample_rate."""

    sample_rate = RATE
    device = "cpu"

    def __init__(self):
        self.generated, self.voices_made = [], 0

    def preset_voices(self):
        return ["de_male_standard", "en_female_narrator", "en_male_narrator_1", "en_male_narrator_2"]

    def preset_voice(self, name):
        if name not in self.preset_voices():
            raise ValueError(f"unknown preset voice {name!r}")
        return FakeVoice(name, name[:2])

    def voice(self, samples, text, language, sample_rate=None):
        assert sample_rate == RATE and samples.dtype == np.float32 and samples.size
        self.voices_made += 1
        return FakeVoice(f"clip{self.voices_made}", language, text)

    def generate(self, text, voice, language, seed=None):
        if "FAILME" in text:
            raise ValueError("text too long")
        self.generated.append({"text": text, "voice": voice.name, "language": language, "seed": seed})
        n = int(RATE * 0.3 * len(text.split()))
        return (0.25 * np.sin(np.arange(n) * 0.05)).astype(np.float32)


@pytest.fixture
def client():
    server.app.state.tts = FakeTTS()
    with TestClient(server.app) as c:
        yield c
    server.app.state.tts = None


def clip_wav(seconds=2.0, rate=16000):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes(b"".join(struct.pack("<h", int(8000 * np.sin(i * 0.03))) for i in range(int(seconds * rate))))
    return buf.getvalue()


needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")


def test_health_lists_reverbs_languages_and_no_instructions(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "sample_rate": RATE, "languages": ["de", "en", "es", "fr", "it", "pl", "pt"],
                        "instructions": False, "engine": "reverb"}


def test_health_says_loading_until_the_model_is_there():
    server.app.state.tts = None
    try:
        with TestClient(server.app) as c:
            server.app.state.tts = None  # (the loader thread may still be working)
            server.app.state.error = ""
            r = c.get("/health")
            assert r.status_code == 503 and r.json()["status"] == "loading"
            r = c.post("/v1/audio/speech", data={"text": "Hi.", "speaker_id": "en_female_narrator"})
            assert r.status_code == 503
    finally:
        server.app.state.tts = None


def test_a_preset_voice_speaks_as_16_bit_pcm(client):
    r = client.post("/v1/audio/speech", data={"text": "It was a bright cold day.", "speaker_id": "en_female_narrator",
                                              "language": "en-GB", "seed": "7"})
    assert r.status_code == 200
    assert r.headers["content-type"] == "audio/pcm"
    assert r.headers["x-sample-rate"] == str(RATE) and r.headers["x-sample-format"] == "s16le"
    samples = np.frombuffer(r.content, dtype="<i2")
    assert samples.size == int(RATE * 0.3 * 6) and np.abs(samples).max() > 7000
    g = server.app.state.tts.generated[-1]
    assert g == {"text": "It was a bright cold day.", "voice": "en_female_narrator", "language": "en", "seed": 7}


@needs_ffmpeg
def test_a_clip_with_its_transcript_is_made_into_a_voice_once(client):
    clip = clip_wav()
    for text in ("First paragraph.", "Second paragraph."):
        r = client.post("/v1/audio/speech", data={"text": text, "ref_text": "The words in the clip.", "language": "fr-CA"},
                        files={"ref_audio": ("me.wav", clip, "audio/wav")})
        assert r.status_code == 200, r.text
    tts = server.app.state.tts
    assert tts.voices_made == 1
    assert [g["language"] for g in tts.generated] == ["fr", "fr"]
    # Another transcript (or language) for the same clip is another voice.
    client.post("/v1/audio/speech", data={"text": "Third.", "ref_text": "Other words.", "language": "fr"},
                files={"ref_audio": ("me.wav", clip, "audio/wav")})
    assert tts.voices_made == 2


def test_a_clip_without_its_transcript_is_refused(client):
    r = client.post("/v1/audio/speech", data={"text": "Hello."}, files={"ref_audio": ("me.wav", clip_wav(), "audio/wav")})
    assert r.status_code == 400 and "transcript" in r.json()["detail"]


@needs_ffmpeg
def test_a_clip_that_isnt_audio_is_refused(client):
    r = client.post("/v1/audio/speech", data={"text": "Hello.", "ref_text": "Words."},
                    files={"ref_audio": ("me.wav", b"not audio at all", "audio/wav")})
    assert r.status_code == 400 and "couldn't be read" in r.json()["detail"]


def test_an_instruction_alone_is_refused_and_ignored_with_a_voice(client):
    r = client.post("/v1/audio/speech", data={"text": "Hello.", "instruction": "A warm voice."})
    assert r.status_code == 400 and "instruction" in r.json()["detail"]
    r = client.post("/v1/audio/speech", data={"text": "Hello.", "instruction": "A warm voice.", "speaker_id": "en_male_narrator_1"})
    assert r.status_code == 200


@pytest.mark.parametrize("lang,code", [("en", "en"), ("en-US", "en"), ("pt-BR", "pt"), ("de_AT", "de"), ("PL", "pl"), ("", "en")])
def test_book_languages_are_read_by_their_primary_subtag(client, lang, code):
    r = client.post("/v1/audio/speech", data={"text": "Hi.", "speaker_id": "de_male_standard", "language": lang})
    assert r.status_code == 200
    assert server.app.state.tts.generated[-1]["language"] == code


def test_a_language_reverb_doesnt_speak_is_refused(client):
    r = client.post("/v1/audio/speech", data={"text": "你好。", "speaker_id": "en_female_narrator", "language": "zh-Hans"})
    assert r.status_code == 400 and "isn't one Reverb speaks" in r.json()["detail"]


@pytest.mark.parametrize("bad", ["0", "-1", "nan", "inf"])
def test_cfg_scale_must_be_positive(client, bad):
    r = client.post("/v1/audio/speech", data={"text": "Hi.", "speaker_id": "en_female_narrator", "cfg_scale": bad})
    assert r.status_code == 400


def test_empty_text_unknown_preset_and_model_refusals(client):
    assert client.post("/v1/audio/speech", data={"text": "  ", "speaker_id": "en_female_narrator"}).status_code == 400
    r = client.post("/v1/audio/speech", data={"text": "Hi.", "speaker_id": "xx_nobody"})
    assert r.status_code == 400 and "unknown preset voice" in r.json()["detail"]
    r = client.post("/v1/audio/speech", data={"text": "FAILME now.", "speaker_id": "en_female_narrator"})
    assert r.status_code == 400
    assert client.post("/v1/audio/speech", data={"speaker_id": "en_female_narrator"}).status_code == 422


def test_builtin_voices_are_described_english_first(client):
    r = client.get("/v1/builtin-voices")
    assert r.status_code == 200
    voices = r.json()["voices"]
    assert [v["id"] for v in voices] == ["en_female_narrator", "en_male_narrator_1", "en_male_narrator_2", "de_male_standard"]
    assert voices[1] == {"id": "en_male_narrator_1", "name": "Narrator 1", "language": "en", "style": "narrator",
                         "gender": "male", "preview": True}


def test_a_preset_voices_preview_is_a_wav_made_once(client):
    r = client.get("/v1/builtin-voices/de_male_standard/preview")
    assert r.status_code == 200 and r.headers["content-type"] == "audio/wav" and r.content[:4] == b"RIFF"
    client.get("/v1/builtin-voices/de_male_standard/preview")
    tts = server.app.state.tts
    assert len(tts.generated) == 1 and tts.generated[0]["language"] == "de"
    assert client.get("/v1/builtin-voices/nobody/preview").status_code == 404
    assert client.get("/v1/builtin-voices/..%2Fx/preview").status_code == 404
