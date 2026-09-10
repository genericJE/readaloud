#!/usr/bin/env bash
# Build docs/demo.mp4: the terminal recording with the spoken audio muxed in.
#
#   ./scripts/build-demo-video.sh [output.mp4]
#
# vhs records headlessly and captures no audio, and macOS cannot capture system
# output audio without a loopback device, so the sound cannot be recorded
# alongside the picture. The same text is rendered twice instead: once as video
# by really running readaloud under vhs, and once as audio straight from the
# engine. scripts/align_demo_video.py then measures where the speech belongs by
# finding the highlight in the picture; see its docstring for the three traps
# that produced badly out-of-sync videos before.
#
# The demo text is deliberately ONE chunk: live playback runs chunks together
# while a rendered file has to guess the gaps, so anything longer would drift.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${1:-$REPO/docs/demo.mp4}"
WORK="$(mktemp -d)"; WORK="$(cd "$WORK" && pwd -P)"
trap 'rm -rf "$WORK"' EXIT

command -v mdcat >/dev/null || { echo "needs mdcat" >&2; exit 1; }
command -v vhs   >/dev/null || { echo "needs vhs" >&2; exit 1; }

cat > "$WORK/spot.md" <<'DOC'
A terminal reader that speaks your documents aloud and highlights each word as you
hear it. The timing comes from the model itself, so the highlight tracks the voice
instead of drifting. Scroll ahead with `less` keys while it keeps talking, or click
any word to jump playback straight there.
DOC
mdcat --ansi "$WORK/spot.md" > "$WORK/spot.ansi"

echo "==> rendering the audio and its word timings"
# Both come from ONE synth call, so the timings cannot disagree with the audio.
uv run --directory "$REPO" python - "$WORK" <<'PY'
import json, sys, wave
import numpy as np
from readaloud.cli import build_document
from readaloud.speech import Engine, SAMPLE_RATE
work = sys.argv[1]
doc = build_document(open(f"{work}/spot.ansi").read(),
                     max_sentences=4, max_chars=380, no_color=False)
chunks = [c for c in doc.chunks if c.speakable]
if len(chunks) != 1:
    sys.exit(f"demo text must be one chunk, got {len(chunks)}")
engine = Engine(); engine.load()
spoken = engine.synth(chunks[0])
audio = np.clip(np.asarray(spoken.audio, dtype=np.float32), -1.0, 1.0)
with wave.open(f"{work}/spot.wav", "wb") as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(SAMPLE_RATE)
    w.writeframes((audio * 32767).astype("<i2").tobytes())
json.dump({"starts": [t.start for t in spoken.timings], "duration": spoken.duration},
          open(f"{work}/timings.json", "w"))
print(f"    {len(spoken.timings)} words, {spoken.duration:.2f}s")
PY

echo "==> recording the video (this really speaks out loud)"
sed "s|^Type \"cd .*|Type \"cd $WORK \&\& clear\"|" "$REPO/docs/demo-video.tape" > "$WORK/demo.tape"
# vhs cannot parse an absolute path in `Output`, and we cd into $WORK anyway.
( cd "$WORK" && sed -i '' "s|^Output .*|Output silent.mp4|" demo.tape && vhs demo.tape >/dev/null )

echo "==> aligning"
read -r DELAY END < <(uv run --directory "$REPO" python "$REPO/scripts/align_demo_video.py" \
                        "$WORK/silent.mp4" "$WORK/timings.json")

echo "==> muxing (audio at ${DELAY}s, trimmed at ${END}s)"
MS=$(python3 -c "print(int(float('$DELAY')*1000))")
ffmpeg -v error -y -i "$WORK/silent.mp4" -i "$WORK/spot.wav" \
  -filter_complex "[1:a]adelay=${MS}|${MS}[a]" -map 0:v -map "[a]" -t "$END" \
  -c:v libx264 -pix_fmt yuv420p -crf 24 -preset slow -movflags +faststart \
  -c:a aac -b:a 128k -ac 2 "$OUT"

echo "==> wrote $OUT"
