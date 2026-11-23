#!/bin/sh
# Sets up the voice server on an Apple Silicon Mac, without Docker (README, "On a Mac").
#
#   1. Creates the Python environment mac/.venv (Reverb-TTS on PyTorch, which uses the GPU through MPS; the studio;
#      speech-to-text).
#   2. Downloads Reverb's weights (about 1.4 GB, once) into models/.
#
# Needs: macOS 14 or later on Apple Silicon with 16 GB or more, and uv and ffmpeg (brew install uv ffmpeg).
# Safe to run again: it only redoes what is missing or out of date.
set -eu

cd "$(dirname "$0")"
ROOT=$(cd .. && pwd)

if [ "$(uname -s)" != "Darwin" ] || [ "$(uname -m)" != "arm64" ]; then
    echo "mac/setup.sh needs an Apple Silicon Mac (this machine: $(uname -s) $(uname -m))." >&2
    exit 1
fi
for tool in uv ffmpeg curl; do
    command -v "$tool" >/dev/null || { echo "$tool is missing: brew install uv ffmpeg" >&2; exit 1; }
done

echo "== Python environment (mac/.venv): Reverb-TTS, the studio and speech-to-text"
[ -x .venv/bin/python ] || uv venv -q --python 3.12
uv pip install -q --python .venv/bin/python torch==2.9.1 -r "$ROOT/reverb/requirements.txt" \
    -r "$ROOT/studio/requirements.txt" -r "$ROOT/asr/requirements.txt"

echo "== Reverb's weights (models/; about 1.4 GB the first time)"
MODELS=${REVERB_MODELS_DIR:-$ROOT/models}
mkdir -p "$MODELS"
HF_HOME="$MODELS" REVERB_VARIANT="${REVERB_VARIANT:-fp16}" .venv/bin/python -c '
import sys; sys.path.insert(0, sys.argv[1])
import app
tts = app.load_model()
print(f"Reverb loaded on {tts.device}, {len(tts.preset_voices())} preset voices.")' "$ROOT/reverb"

mkdir -p "$ROOT/tts-voices" "$ROOT/tts-sources" "$ROOT/asr-models" logs
echo
echo "Done. Start the server with: mac/start.sh   (or at every login: mac/install-service.sh)"
