# API

Every HTTP endpoint the services in `docker-compose.yml` offer: what each request takes, what comes back, and the
errors. The reader-facing API is the same as [epub-reader-tts-server](https://github.com/jskenney/epub-reader-tts-server)'s
(Breeze TTS 2), so the reader, the proxy configuration and the voices folder work with either; the differences are
listed under [Differences from the Breeze server](#differences-from-the-breeze-server).

| Service | Port | Site path (through the proxy) | Endpoints | Used by |
|---|---|---|---|---|
| `reverb` | 7860 | `/tts/` | [`GET /health`](#get-health), [`POST /v1/audio/speech`](#post-v1audiospeech), [`GET /v1/builtin-voices`](#get-v1builtin-voices) | the reader, the studio, `make-voice.sh` |
| `studio` | 7861 | `/tts-voices/` → `/voices/`, `/tts-studio/` → `/` | [`GET /voices/voices.json`](#get-voicesvoicesjson), [`GET /voices/<clip>`](#get-voicesclip), [`GET /health`](#get-health-1), the studio UI | the reader (voices), you (UI) |
| `asr` | 7862 (not published) | none | [`GET /health`](#get-health-2), [`POST /transcribe`](#post-transcribe) | the studio only |

Paths below are as the service sees them. Through the proxy, prefix the site path: the reader asks for
`https://<site>/tts/v1/audio/speech`, which the proxy sends to `http://tts-host:7860/v1/audio/speech`. The services
themselves have no authentication; the proxy's password (see `nginx.conf.example`, `Caddyfile.example`) is what keeps
others off your GPU. The `reverb` service is a FastAPI app, so it also serves generated docs at `/docs` and `/openapi.json`.

Times are in seconds. Audio is 24 kHz, 16-bit mono (48,000 bytes per second).

---

## Voice server API (`reverb`, port 7860)

The `reverb` service (`reverb/app.py`) answers the reader's API with Reverb-TTS (software-mansion/Reverb-TTS 1.0.0,
its Python package) on the GPU, in the same process. Requests are made one at a time, in the order they come; there is
no 409 (a Breeze server answers 409 while busy): a request waits its turn, and one whose caller has gone by then is
skipped. Reverb is fast (a paragraph takes a fraction of its length to make on a recent GPU), so the wait is short.

### `GET /health`

| Status | Body | When |
|---|---|---|
| 200 | `{"status": "ok", "sample_rate": 24000, "languages": ["de", "en", "es", "fr", "it", "pl", "pt"], "instructions": false, "engine": "reverb"}` | The model is loaded. |
| 503 | `{"status": "loading"}` | The model is still loading (the first start also downloads it, about 1.4 GB). |
| 503 | `{"status": "error", "detail": "…"}` | The model couldn't be loaded (the log says why). |

| Field | Meaning |
|---|---|
| `status` | `"ok"`: the reader shows **Server voice** only on a 200 with this. |
| `sample_rate` | The rate of the PCM speech returns. |
| `languages` | The seven languages Reverb speaks, as language subtags. The reader offers Server voice for a book whose language is one of these, and sends the book's language with each request. |
| `instructions` | `false`: voices that have only an `instruction` (Breeze's voice design) can't be spoken here; the reader leaves them out. |
| `engine` | `"reverb"`, for information. |

### `POST /v1/audio/speech`

Speak one text.

**Request:** `multipart/form-data` (or `application/x-www-form-urlencoded` without a clip). Fields:

| Field | Type | Required | Default | Meaning |
|---|---|---|---|---|
| `text` | string | yes | | What to say. Numbers, dates, money, units and abbreviations are spelled out first (Reverb's normalizer, Verbalis, in the text's language). |
| `ref_audio` | file | with `ref_text`, or `speaker_id` | none | A clip of the voice to speak in (any format ffmpeg reads; 3–10 s of clean speech from one speaker is best; over 15 s makes worse takes). |
| `ref_text` | string | with `ref_audio` | `""` | The clip's exact transcript. **Reverb needs it**: a clip without one is refused (400). |
| `language` | string | no | `en` | The book's language, as a BCP 47 tag (`en`, `en-GB`, `pt-BR`, `de-AT`): its primary subtag is used. A language Reverb doesn't speak is a 400. |
| `speaker_id` | string | with no clip | none | One of Reverb's [preset voices](#get-v1builtin-voices), instead of a clip. (An extension: the studio and `make-voice.sh` use it to make voices from them.) |
| `seed` | integer | no | `42` | The random seed. The same text, voice, language and seed give the same audio on the same hardware. |
| `cfg_scale` | number | no | `1.0` | Accepted (the reader sends it) and not used: Reverb has no speaker guidance. Must be finite and greater than 0. |
| `instruction` | string | no | none | Breeze's voice description. With a clip (or `speaker_id`) it is ignored; alone, it is refused (400): Reverb makes voices from clips and their words. |

A clip is made into a voice (its audio codec codes) the first time it is seen with that transcript and language, and
spoken with afterwards; the server keeps the 64 most recent, by the clip's SHA-256. A text over about 30 s of speech
is spoken in pieces of about 15 s, cut at sentence ends.

**Response: 200**, the audio:

| Header | Value |
|---|---|
| `Content-Type` | `audio/pcm` |
| `X-Sample-Rate` | `24000` |
| `X-Sample-Format` | `s16le` |
| `Cache-Control` | `no-store` |

The body is raw PCM, with no WAV header: mono, signed 16-bit little-endian samples at `X-Sample-Rate`. Reverb makes a
passage whole (it isn't streamed while generated), and the body is then sent in pieces. To save it as WAV:

```bash
ffmpeg -f s16le -ar 24000 -ac 1 -i speech.pcm speech.wav
```

**Errors:** JSON, FastAPI's form, as the Breeze server's:

| Status | Body | Cause |
|---|---|---|
| 400 | `{"detail": "cfg_scale must be greater than 0."}` | `cfg_scale` is 0, negative, infinite or NaN. |
| 400 | `{"detail": "text is empty."}` | Only spaces. |
| 400 | `{"detail": "Reference audio is empty."}` | `ref_audio` has no bytes. |
| 400 | `{"detail": "Reverb needs the reference clip's exact transcript (ref_text)."}` | A clip without `ref_text`. |
| 400 | `{"detail": "The reference clip couldn't be read: …"}` | ffmpeg couldn't decode the clip. |
| 400 | `{"detail": "This voice server (Reverb-TTS) speaks in the voice of a clip or a preset voice: …"}` | Neither a clip nor `speaker_id` (with or without an `instruction`). |
| 400 | `{"detail": "Language 'xx' isn't one Reverb speaks (it speaks de, en, es, fr, it, pl, pt)."}` | An unknown `language`. |
| 400 | `{"detail": "unknown preset voice 'x', …"}` | An unknown `speaker_id`. |
| 422 | `{"detail": [{"type": "missing", "loc": ["body", "text"], …}]}` | A required field is missing, or a field isn't the right type. |
| 503 | `{"detail": "Reverb is still loading."}` | The model isn't loaded yet (or couldn't be: the detail says why). The reader then treats the server as gone until `/health` answers again. |

#### Example

```bash
curl -sS -X POST http://localhost:7860/v1/audio/speech \
  -F "text=It was a bright cold day in April." \
  -F "ref_audio=@tts-voices/narrator.wav" -F "ref_text=The exact words spoken in narrator.wav." \
  -F "language=en-GB" -F "seed=42" -o speech.pcm
```

### `GET /v1/builtin-voices`

Reverb's 16 preset voices (an extension, for the studio's **Pick a built-in voice** and `make-voice.sh list`):

```json
{"voices": [
  {"id": "en_female_narrator", "name": "Narrator", "language": "en", "style": "narrator", "gender": "female", "preview": true},
  {"id": "pl_male_standard", "name": "Standard", "language": "pl", "style": "standard", "gender": "male", "preview": true}
]}
```

English first, then by language and name. `id` goes in [`speaker_id`](#post-v1audiospeech).
`GET /v1/builtin-voices/<id>/preview` returns a short sample in the voice, in its language (WAV, made the first time
it is asked for). 503 while the model is loading.

The reader doesn't use preset voices directly: the studio saves a sample in one as an ordinary clip voice in
`voices.json`.

---

## Voices (`studio`, port 7861)

The studio serves the voices folder (`./tts-voices` on the host, `VOICES_DIR` in the container) read-only at
`/voices/`; the site proxies `/tts-voices/` to it. The studio, `make-voice.sh` and you (by hand) write it. Responses
from `/voices/` carry `Cache-Control: no-cache`, so a new or changed voice shows up as soon as the reader asks again.

### `GET /voices/voices.json`

The voices the reader offers, in the order it lists them.

```json
{
  "voices": [
    {
      "id": "narrator",
      "name": "Warm narrator",
      "ref_audio": "narrator.wav",
      "ref_text": "The old house stood at the end of the lane, its windows catching the last of the evening light.",
      "seed": 42,
      "cfg_scale": 1.0,
      "description": "A warm, calm middle-aged woman with a clear, steady storytelling voice."
    },
    {
      "id": "me",
      "name": "My voice",
      "ref_audio": "me.wav",
      "ref_text": "The exact words spoken in the clip.",
      "seed": 42,
      "cfg_scale": 1.0,
      "source": "chapter-03.m4b 12:04.5–12:16.0"
    }
  ]
}
```

| Field | Type | Required | Meaning |
|---|---|---|---|
| `voices` | array | yes | The voices, in the order the reader lists them (the studio's **Voices** tab reorders them). |
| `id` | string | yes | Unique; `a-z`, `0-9`, `-`, `_` (the studio and `make-voice.sh` insist). The reader remembers the chosen voice by it. |
| `name` | string | no | Shown in the reader's voice list; `id` if missing. |
| `ref_audio` | string | with `ref_text` | The reference clip's file name, relative to the voices folder. Sent as [`ref_audio`](#post-v1audiospeech) with every request. |
| `ref_text` | string | with `ref_audio` | The clip's exact transcript, sent as `ref_text`. Reverb needs it. |
| `instruction` | string | no | Breeze's voice description. Reverb can't make a voice from one: with a clip it is ignored, and a voice with only an instruction fails (400) and is hidden by readers that know this server can't use instructions ([`/health`](#get-health)). |
| `seed` | integer | no | Sent as `seed`; 42 if missing. |
| `cfg_scale` | number | no | Sent as `cfg_scale`; not used by Reverb. |
| `description` | string | no | What the voice was made from, such as the preset voice (kept for you; not sent). |
| `source` | string | no | The recording and times a clip was cut from (kept for you; not sent). |

Other fields are kept and ignored. How the reader reads the list:

- A voice is offered if it has `ref_audio` with `ref_text` (or an `instruction`, on a server that can use one); others
  are left out.
- `ref_audio` must stay inside the voices folder: a name that resolves elsewhere (`../x.wav`, another site, an
  encoded slash) isn't fetched. The voice then falls back to its `instruction` if it has one, and is left out
  otherwise.
- The reader caches the speech it makes per voice, keyed by the server address, `ref_audio` and its version (below),
  `ref_text`, `instruction`, `cfg_scale`, `seed` and the book's language. Changing any of those makes new speech;
  changing `name`, the
  order, or other fields doesn't.

| Status | When |
|---|---|
| 200 | `application/json`, as above. |
| 404 | No voices yet (no `voices.json`). The reader says so and points to the studio. |

### `GET /voices/<clip>`

A reference clip named in `voices.json`, for example `GET /voices/narrator.wav`. The clips the studio and
`make-voice.sh` write are WAV, mono, 24 kHz, 16-bit (clips from the other servers' studios work as well). Served as static files: `HEAD` works, and responses carry `ETag`
and `Last-Modified`. The reader fetches each clip once per session and sends it with every request in that voice;
it asks with `HEAD` for the clip's version (its `ETag`, or date and size), so replacing a clip under the same name
makes its cached speech be made again. 404 if the file isn't there.

### `GET /health`

`{"status": "ok", "voices": 3}`: the studio is up, and `voices` is how many entries `voices.json` has (0 without
one). Used by the container's health check. 500 if `voices.json` exists but isn't valid JSON.

### The studio UI (`/`)

The Gradio app (served at `/tts-studio/` through the proxy). Gradio's own endpoints under it (`/gradio_api/…`, the
upload and queue routes) belong to the UI and change with Gradio's version: they aren't an API to build on. Use
[`/v1/audio/speech`](#post-v1audiospeech) and the voices folder instead.

---

## Speech-to-text (`asr`, port 7862)

English Whisper (`ASR_MODEL`, default `openai/whisper-large-v3-turbo`) with word timings. Only the studio calls it,
over the compose network at `http://asr:7862`; the port isn't published on the host. From the host:
`docker compose exec asr curl -s localhost:7862/health`.

The model is loaded on the first transcription (downloaded the first time, about 1.6 GB) and unloaded after
`ASR_IDLE_UNLOAD` seconds without one (600; 0 keeps it). One transcription runs at a time; others wait.

### `GET /health`

```json
{"status": "ok", "model": "openai/whisper-large-v3-turbo", "device": "cuda", "loaded": false}
```

| Field | Meaning |
|---|---|
| `status` | Always `"ok"` while the service runs. |
| `model` | The Whisper model it uses. |
| `device` | `"cuda"`, or `"cpu"` without a GPU (much slower). |
| `loaded` | Whether the model is in memory now. |

### `POST /transcribe`

Transcribe a recording; progress streams back as it goes.

**Request:** `multipart/form-data` with one field:

| Field | Type | Required | Meaning |
|---|---|---|---|
| `file` | file | yes | Any audio ffmpeg can read (MP3, M4A, M4B, WAV, FLAC, OGG, …), any length (up to 30 minutes of ffmpeg time to decode it). The file name's extension is used as a hint. Video is ignored. |

**Response: 200**, `application/x-ndjson`: one JSON object per line, streamed. Lines come in this order:

1. Zero or more **progress** lines while it works:

   ```json
   {"progress": 0.0, "status": "Reading the audio…"}
   {"progress": 0.0, "status": "Waiting for another transcription to finish…"}
   {"progress": 0.0, "status": "Loading openai/whisper-large-v3-turbo (the first time downloads it)…"}
   {"progress": 0.4213, "status": "Transcribed 12.6 of 30.0 minutes, about 2 min left"}
   ```

   `progress` goes from 0 to 1 (how far through the audio, by time); `status` is a sentence to show.

2. Then exactly one of:

   - a **result** line, the last line:

     ```json
     {"result": {
       "duration": 1800.04,
       "model": "openai/whisper-large-v3-turbo",
       "words": [
         {"start": 0.52, "end": 0.81, "text": "It"},
         {"start": 0.81, "end": 0.98, "text": "was"},
         {"start": 0.98, "end": 1.44, "text": "bright."}
       ],
       "segments": [
         {"start": 0.52, "end": 1.44, "first": 0, "last": 2, "text": "It was bright."}
       ]
     }}
     ```

   - or an **error** line, the last line: `{"error": "The recording is empty."}`, or
     `{"error": "<ExceptionType>: <message>"}` (ffmpeg couldn't read the file, the GPU ran out of memory, …).

Errors come as an `error` line in a 200 response, since the status has been sent before the work starts; check every
line for `error`. Only a request without `file` gets a non-200 answer (422, FastAPI's validation error).

| Result field | Type | Meaning |
|---|---|---|
| `duration` | number | The recording's length, in seconds. |
| `model` | string | The model that transcribed it. |
| `words` | array | Every word, in order. |
| `words[].start`, `words[].end` | number | When the word begins and ends, in seconds from the start (3 decimals). `end` ≥ `start`; a missing end is filled in (the next word's start, at most 0.6 s on). |
| `words[].text` | string | The word with its punctuation attached (`"bright."`), no surrounding spaces. |
| `segments` | array | The words grouped into sentences, in order. |
| `segments[].start`, `segments[].end` | number | The sentence's first word's start and last word's end. |
| `segments[].first`, `segments[].last` | integer | Indexes into `words` of the sentence's first and last word (inclusive). |
| `segments[].text` | string | The sentence's words joined by spaces. |

How it gets there, which explains the shape:

- The audio is decoded to 16 kHz mono and cut into pieces of 18–28 s at the quietest 50 ms near each cut, so a piece
  never splits a word; pieces that are nearly silent are skipped (Whisper invents words for silence), so stretches of
  silence or music have no words.
- Pieces are transcribed `ASR_BATCH` at a time, always as English. A piece Whisper wrote without punctuation is
  transcribed again with a punctuated prompt, and its punctuation and capitals are copied onto the words where the two
  agree.
- If the model can't give word timings, it falls back to sentence timings and spreads each sentence's words evenly
  over it.
- Sentences end at `.`, `!`, `?` or `…` (not after `Mr.`, `Dr.`, `St.` and the like), or failing that at a pause:
  over 1 s, over 0.5 s once a sentence is 8 s long, or the longest gap among its last words once it is 15 s long.

```bash
docker compose exec asr sh -c 'curl -sS -N -F "file=@/tmp/chapter.m4b" localhost:7862/transcribe'
```

---


---

## How the reader uses the API

The reader ([jskenney/epub-reader](https://github.com/jskenney/epub-reader)) takes the two addresses from its
`TTS_SERVER` setting (default `{"api": "/tts/", "voices": "/tts-voices/"}` on the site root). In order:

1. **Is there a server, and what does it speak?** `GET /tts/health`, with a 5 s time limit, when the read-aloud bar
   opens and before playing, at most every 15 s. Only a 200 JSON answer with `"status": "ok"` counts. With a
   `languages` list (this server), Server voice is offered for a book in one of those languages; without one (a
   Breeze server), for English and Chinese books.
2. **Which voices?** `GET /tts-voices/voices.json`, then each voice's clip from `/tts-voices/<ref_audio>` (once per
   session, plus a `HEAD` for its version). With `"instructions": false`, voices that have only an `instruction` are
   left out.
3. **Speech.** One `POST /tts/v1/audio/speech` per paragraph, as multipart: `text` (parentheses and brackets turned
   into dashes), the voice's `ref_audio` and `ref_text`, or its `instruction`, `cfg_scale` and `seed` (defaults 1
   and 42), and, to a server that listed its languages, `language` (the book's, as its metadata gives it, such as
   `en-GB`). A paragraph longer than 600 characters is split at sentence ends. Requests go one at a time, with the
   next two paragraphs asked for ahead.
4. **Reading the answer.** The stream is read as it arrives, at `X-Sample-Rate`. The reader gives up on a request that
   hasn't started answering after 60 s, or whose stream stops for 180 s before any audio or 60 s after some; an
   answer with less than 0.12 s of audio per word counts as cut off.
5. **Failures.** 502, 503, 504, an HTML answer, or no connection: the server counts as gone until `/health` answers
   again. Another error status: that paragraph fails with the `detail` shown, and a paragraph that keeps failing is
   asked for one sentence at a time.

The reader caches the speech per voice, keyed by the server address, the voice's clip and settings, and the
language it sent.

## Differences from the other servers

| | Breeze (epub-reader-tts-server) | ZONOS3 (epub-reader-tts-server-zonos3) | Reverb (this repository) |
|---|---|---|---|
| Languages | English, Chinese | 43 | 7: English, German, French, Spanish, Italian, Polish, Portuguese |
| Voices | a clip with its transcript, or a description (`instruction`) | a clip (transcript not used), or a built-in voice | a clip **with its transcript**, or a preset voice; no descriptions |
| `cfg_scale` | guidance | speaker guidance | not used |
| Audio | 24 kHz | 44.1 kHz | 24 kHz |
| Busy | 409, the caller retries | queued; several at once | queued, one at a time (fast) |
| Streaming | yes | yes | no: a passage is made whole, then sent |
| Text | read as written | numbers, dates and money normalized | numbers, dates, money and units normalized (Verbalis) |
| Extra routes | `/v1/model`, `/v1/audio/speech/batch` | `/v1/builtin-voices` | `/v1/builtin-voices` |
| Licence | weights non-commercial | code MIT, weights Apache-2.0 | code Apache-2.0, **weights CC BY-NC 4.0 (non-commercial)** |

## Proxy requirements

What the proxy must let through for the API to work (both examples do):

| Path | Needs |
|---|---|
| `/tts/` | No response buffering; request bodies of at least 20 MB (the reference clip comes with every request); a read timeout of a few minutes. |
| `/tts-voices/` | Nothing special: small static files. |
| `/tts-studio/` | WebSocket upgrade, no buffering (progress streams back), request bodies up to the largest recording you upload (1 GB in `nginx.conf.example`; Caddy has no limit), and HTTPS for microphone recording. |
