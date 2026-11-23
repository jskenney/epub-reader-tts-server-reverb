# EPUB reader TTS server: Reverb-TTS

[Reverb-TTS](https://github.com/software-mansion-labs/Reverb-TTS) on your own GPU, for the **Server voice** in
[jskenney/epub-reader](https://github.com/jskenney/epub-reader), with a voice studio for trying text and creating
voices. It answers the same API as [epub-reader-tts-server](https://github.com/jskenney/epub-reader-tts-server)
(Breeze TTS 2) and [epub-reader-tts-server-zonos3](https://github.com/jskenney/epub-reader-tts-server-zonos3), so the
reader, the proxy configuration and the voices folder work with any of them; run one at a time.

What Reverb brings: voice cloning of high quality from a clip of 3-10 seconds and its transcript (the lowest word error
rate on the Open TTS Leaderboard, by its makers' figures), **7 languages** (English, German, French, Spanish, Italian,
Polish, Portuguese; any voice speaks any of them), 16 preset voices, text normalization (numbers, dates, money, units),
and speed: a 297M-parameter model, many times faster than real time on a recent GPU.

**Licence:** the code here and Reverb's code are Apache-2.0, but **Reverb's weights are
[CC BY-NC 4.0](https://creativecommons.org/licenses/by-nc/4.0/): non-commercial use only, with attribution**
("Reverb-TTS by Software Mansion"). **Its audio codec, the Higgs Audio v2 tokenizer, is under the
[Boson Higgs Audio 2 Community License](https://huggingface.co/bosonai/higgs-audio-v2-tokenizer/blob/main/LICENSE)**,
based on the Meta Llama 3 Community License: a service that uses it must display "Built with Higgs Materials licensed
from Boson AI USA, Inc., Copyright Boson AI USA, Inc., All Rights Reserved and Meta Llama 3 licensed under the Meta
Llama 3 Community License, Copyright Meta Platforms, Inc., All Rights Reserved" on a related page, follow the
[Llama 3 Acceptable Use Policy](https://llama.meta.com/llama3/use-policy), and needs an expanded licence from Boson AI
past 100,000 annual active users. For a public or commercial site, use the ZONOS3 server (Apache-2.0 weights)
instead. As with any voice cloning, you need the rights and consent for any voice you clone.

## What runs

`docker-compose.yml` starts three services on the GPU machine:

| Service | Port | What |
|---|---|---|
| `reverb` | 7860 | Reverb-TTS answering the reader's API (`/health`, `/v1/audio/speech`; [API.md](API.md)). Uses the GPU (about 3 GB). |
| `studio` | 7861 | The voice studio (a Gradio app) and the voices folder at `/voices/`. No GPU. |
| `asr` | (none) | Speech-to-text for the studio: English Whisper large-v3-turbo with word timings. Uses the GPU (about 2 GB) only while transcribing. Only the studio talks to it. |

These are ordinary folders next to `docker-compose.yml`, so they survive rebuilds and are easy to back up:

| Folder | What | Change with |
|---|---|---|
| `./models` | Reverb's weights, codec and preset voices (downloaded on the first start, about 1.4 GB) | `REVERB_MODELS_DIR` |
| `./tts-voices` | Voices: `voices.json` plus one reference clip per voice | `TTS_VOICES_DIR` |
| `./tts-sources` | Long recordings uploaded in the studio, with their transcripts, until you delete them | `TTS_SOURCES_DIR` |
| `./asr-models` | The speech-to-text model (about 1.6 GB, downloaded on the first transcription) | `ASR_MODELS_DIR` |

Your web server proxies three paths on the reader's site to it, exactly as for the other servers:

| Site path | Goes to | Used by |
|---|---|---|
| `/tts/` | `tts-host:7860/` | the reader's speech requests |
| `/tts-voices/` | `tts-host:7861/voices/` | the reader's voice list and clips |
| `/tts-studio/` | `tts-host:7861/` | you, in a browser |

The reader shows **Server voice** when `/tts/health` answers, for books in the languages it lists, and reads its
voices from `/tts-voices/voices.json`.

## Requirements

- Linux with an NVIDIA GPU (4 GB of memory is enough for Reverb; Whisper adds about 2 GB while transcribing), a recent
  driver, and the NVIDIA Container Toolkit for Docker. The image is PyTorch 2.9.1 with CUDA 12.8, which runs on
  Blackwell cards (RTX 50xx, RTX PRO Blackwell) and older ones alike: there is nothing to compile. (For a Mac, see
  [On a Mac](#on-a-mac-apple-silicon).)

## Set up

On the GPU machine, in a clone of this repository:

```bash
docker compose up -d --build        # builds the images; the first start downloads Reverb (about 1.4 GB)
docker compose logs -f reverb       # wait for "Reverb loaded"
curl http://localhost:7860/health
curl http://localhost:7861/health
```

Then add the three proxy paths on your web server: see `nginx.conf.example` or `Caddyfile.example`. Response
buffering must be off for `/tts/` and `/tts-studio/`. Put the site behind a password so nobody else can use your GPU.

If you open the studio directly at `http://<host>:7861/` instead of through the proxy, set `STUDIO_ROOT_PATH=`
(empty) before `docker compose up -d`.

**Switching from another server:** stop it (`docker compose down` in its folder), copy or point `TTS_VOICES_DIR` at
its `tts-voices` folder, and start this one. Voices with a clip and its transcript work as they are; voices made only
from a description (`instruction`) can't be spoken by Reverb, and the reader leaves them out. The reader's saved audio
is kept per server address and voice, so audio made by the other server is reused if the address and voice are the
same; clear it in the reader's settings to hear Reverb instead.

## On a Mac (Apple Silicon)

Docker on a Mac can't use the GPU, so the `mac/` folder runs the same services natively, with the same ports and API.
Reverb runs on PyTorch with the GPU through MPS.

**Not yet run on a Mac.** The server was tested on Linux (on the CPU); the Mac scripts follow the other repositories'.

```bash
brew install uv ffmpeg
mac/setup.sh                 # creates the Python environment and downloads Reverb (~1.4 GB)
mac/start.sh                 # Reverb on 7860, the studio on 7861 (speech-to-text on 127.0.0.1)
curl http://localhost:7860/health
```

`mac/start.sh` runs in the foreground (Ctrl-C stops it) and logs to `mac/logs/`. `mac/install-service.sh` starts it
at login and restarts it if it stops (`--uninstall` removes that). Settings go in `mac/settings.env`, one
`NAME=value` per line: those below, plus `TTS_BIND` (default `0.0.0.0`; `127.0.0.1` if the web server runs on the
same Mac).

## The voice studio

Open `https://<your site>/tts-studio/`.

- **Try text:** paste any text, choose a saved or preset voice, give the language (`en`, `en-GB`, `fr`, `de`, `pl`,
  …), and generate. With "Read it like the reader" on, it sends one request per paragraph as the reader does, and it
  reports seconds of audio, generation time and the real-time factor.
- **Pick a built-in voice:** choose one of Reverb's 16 preset voices (5 English, 2 each German, French, Spanish,
  Italian and Portuguese, 1 Polish), hear a sample, generate a sample of your text in it, try other seeds, and **Save
  as voice**. The sample and its text become the voice's reference clip and transcript.
- **Record or upload:** record with the microphone or upload a file, trim it, type exactly what is said, test it, and
  save it. Use 3-10 seconds of clean speech. Recording needs HTTPS (or localhost); uploading works anywhere.
- **From a long recording:** upload an audiobook chapter or a podcast, transcribe it (English), click sentences to
  choose a clip (its transcript comes with it), test and save it.
- **Voices:** preview, rename, reorder (the order the reader lists them) and delete voices, and **Hear it speak**
  any text in a voice.

`make-voice.sh` does the same from the command line: `./make-voice.sh list`, `./make-voice.sh builtin <id> <name>
<preset id>`, and `./make-voice.sh clip <id> <name> <recording> <transcript>` (set `TTS_URL`).

## Settings

Everything has a working default. Set the `docker-compose.yml` values in a `.env` file next to it.

| Setting | Default | What |
|---|---|---|
| `REVERB_VARIANT` | `fp16` | Reverb's weights: `fp16` (594 MB), `fp32` (1.19 GB), `int8` (307 MB) or `int4` (191 MB). They all run in bfloat16 on the GPU, at the same speed; fp16 is the closest to the original for little more space. |
| `REVERB_STEPS` | `8` | The sampler's main passes (Reverb's tuned value). |
| `REVERB_MODELS_DIR` | `./models` | Where the weights are kept. |
| `TTS_VOICES_DIR` | `./tts-voices` | The voices: `voices.json` and the reference clips. |
| `TTS_SOURCES_DIR` | `./tts-sources` | Long recordings uploaded in the studio, with their transcripts. |
| `ASR_MODELS_DIR` | `./asr-models` | The downloaded speech-to-text model. |
| `STUDIO_ROOT_PATH` | `/tts-studio` | The path your web server serves the studio at (`STUDIO_ROOT_PATH=` to open it on port 7861). |
| `ASR_MODEL`, `ASR_IDLE_UNLOAD`, `ASR_BATCH` | `openai/whisper-large-v3-turbo`, `600`, `8` | Speech-to-text, as in the other repositories. |

## How fast

On 4 CPU cores (no GPU), Reverb here speaks about as fast as it plays: 4.7 s of speech in 4.2 s. Its makers measured
about 165x real time on an RTX 5090 (batch 32). The studio's **Try text** shows the real-time factor on yours. The
first request in a voice also makes it from its clip (a second or two; once per voice while the server runs).

## Updating Reverb

Reverb is pinned by `reverb-tts` in `reverb/requirements.txt`, whose release names the weights' revision (the package
asks for that revision of the Hub repository). To update, change the version (and the other pins with it, as that
release needs), `docker compose up -d --build`, and check `reverb/test_app.py` still passes (`pytest reverb/test_app.py`,
with `fastapi`, `httpx`, `numpy`, `python-multipart`, `uvicorn` and ffmpeg). The weights of a new revision are
downloaded on the next start.

## Files

| File | What |
|---|---|
| `API.md` | Every HTTP endpoint: the fields each request takes, the responses, headers and errors, and the differences from the other servers |
| `docker-compose.yml` | The three services |
| `reverb/` | Reverb-TTS with the reader's API (`app.py`), its tests (`test_app.py`, with a stand-in for the model), its image |
| `studio/` | The voice studio, and the voices folder for the reader |
| `asr/` | Speech-to-text for the studio |
| `make-voice.sh` | Add voices from the command line |
| `nginx.conf.example`, `Caddyfile.example` | The web server's proxy paths |
| `mac/` | The same services on an Apple Silicon Mac, without Docker |

Reverb-TTS is by Software Mansion AI Labs ([software-mansion-labs/Reverb-TTS](https://github.com/software-mansion-labs/Reverb-TTS));
its weights were initialized from k2-fsa's OmniVoice, and its codec is the Higgs Audio v2 tokenizer.
