#!/usr/bin/env bash
# Build docs/demo.mp4: the terminal recording with the spoken audio muxed in.
#
#   ./scripts/build-demo-video.sh
#
# vhs records headlessly and captures no audio, so the sound cannot simply be
# recorded alongside the picture.  Instead the same text is rendered twice: once
# as video by really running readaloud under vhs, and once as audio with
# `readaloud --save`.  They are then aligned.
#
# Two things make that alignment exact rather than approximate:
#
#   1. The demo text is ONE chunk.  `--save` inserts 0.20s of silence between
#      chunks while live playback has almost none, so anything longer would
#      drift further out of sync with every chunk boundary.
#   2. The offset is measured, not assumed.  The video is scanned for the frame
#      where the reverse-video highlight first appears, and the audio is delayed
#      to land there, minus the silence Kokoro pads before the first word.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${1:-$REPO/docs/demo.mp4}"
WORK="$(mktemp -d)"; WORK="$(cd "$WORK" && pwd -P)"
trap 'rm -rf "$WORK"' EXIT

W=1180; H=200; FPS=30          # must match docs/demo-video.tape

cat > "$WORK/spot.md" <<'DOC'
A terminal reader that speaks your documents aloud and highlights each word as you
hear it. The timing comes from the model itself, so the highlight tracks the voice
instead of drifting. Scroll ahead with `less` keys while it keeps talking, or click
any word to jump playback straight there.
DOC

command -v mdcat >/dev/null || { echo "needs mdcat" >&2; exit 1; }
command -v vhs   >/dev/null || { echo "needs vhs" >&2; exit 1; }
mdcat --ansi "$WORK/spot.md" > "$WORK/spot.ansi"

echo "==> rendering the audio"
readaloud -f "$WORK/spot.ansi" --no-config --save "$WORK/spot.wav" >/dev/null

echo "==> recording the video (this really speaks out loud)"
sed "s|^Type \"cd .*|Type \"cd $WORK \&\& clear\"|" "$REPO/docs/demo-video.tape" \
    > "$WORK/demo.tape"
# vhs cannot parse an absolute path in `Output`, and we cd into $WORK anyway.
( cd "$WORK" && sed -i '' "s|^Output .*|Output silent.mp4|" demo.tape && vhs demo.tape )

echo "==> finding the frame where the highlight starts"
read -r DELAY END <<EOF
$(uv run --directory "$REPO" python - "$WORK/silent.mp4" "$WORK/spot.wav" "$W" "$H" "$FPS" <<'PY'
import subprocess, sys, wave, numpy as np
video, wav, W, H, FPS = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4]), int(sys.argv[5])
raw = subprocess.run(["ffmpeg","-v","error","-i",video,"-pix_fmt","gray","-f","rawvideo","-"],
                     capture_output=True).stdout
n = len(raw)//(W*H)
v = np.frombuffer(raw[:n*W*H], dtype=np.uint8).reshape(n, H, W)[:, :H-34, :]  # minus the status bar

def word_run(f):
    b = f > 200; best = 0
    for row in b:
        if not row.any(): continue
        idx = np.flatnonzero(np.diff(np.concatenate(([0], row.view(np.int8), [0]))))
        r = idx[1::2] - idx[::2]
        if r.size:
            m = r.max()
            if 25 <= m <= 400: best = max(best, m)   # a word block, not a glyph or the status bar
    return best

runs = np.array([word_run(v[i]) for i in range(n)])
starts = [i for i in np.where(runs >= 25)[0] if (runs[i:i+10] >= 25).all()]
if not starts: sys.exit("no highlight found in the recording")
first_highlight = starts[0]/FPS

w = wave.open(wav); a = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32)
env = np.abs(a); lead = np.flatnonzero(env > env.max()*0.05)[0]/w.getframerate()
dur = w.getnframes()/w.getframerate()

delay = first_highlight - lead     # the highlight marks the first WORD, not the file start
print(f"{delay:.3f} {delay + dur + 0.9:.3f}")
PY
)
EOF

echo "==> muxing (audio delayed ${DELAY}s, trimmed at ${END}s)"
MS=$(python3 -c "print(int(float('$DELAY')*1000))")
ffmpeg -v error -y -i "$WORK/silent.mp4" -i "$WORK/spot.wav" \
  -filter_complex "[1:a]adelay=${MS}|${MS}[a]" -map 0:v -map "[a]" -t "$END" \
  -c:v libx264 -pix_fmt yuv420p -crf 24 -preset slow -movflags +faststart \
  -c:a aac -b:a 128k -ac 2 "$OUT"

echo "==> wrote $OUT"
ffprobe -v error -show_entries stream=codec_type,codec_name -of csv=p=0 "$OUT"
