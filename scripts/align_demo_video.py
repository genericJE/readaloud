"""Work out where the speech belongs in the silent screen recording.

    align_demo_video.py <silent.mp4> <word_starts.json> -> "<delay> <end>"

vhs records headlessly and captures no audio, so the sound is rendered
separately and muxed. This finds the offset by locating the reverse-video
highlight in the picture and lining it up with the model's own word timings.

Three things here are not optional, each having produced a badly out-of-sync
video when got wrong:

* Read the frame rate from the FILE. `Set Framerate 30` in the tape does not
  survive the MP4 encode, which comes out at 25. Assuming 30 scales every
  timestamp by 1.2 and put the audio 1.2s early.
* Look only at the text band. The status bar is a full-width light bar and
  swamps any "longest bright run" test.
* The current chunk has a background wash. Inside the text band it never
  exceeds ~9px of contiguous brightness, while a highlighted word is 25px+,
  so the run-length floor is what separates them.

The offset is then found by cross-correlation rather than by pairing the Nth
transition with the Nth word: one-character words like "A" are ~10px wide and
fall under the detection floor, so the two sequences are not the same length
and index pairing invents drift that is not there.
"""

from __future__ import annotations

import json
import subprocess
import sys

import numpy as np

TEXT_TOP, TEXT_BOTTOM = 35, 130   # the body rows, excluding the status bar
BRIGHT = 200                      # a reverse-video block; the wash sits below this
MIN_RUN = 25                      # px: wider than a glyph, narrower than a bar
TOLERANCE = 0.06                  # s: a transition counts as matching a word start


def probe(path: str) -> tuple[int, int, float]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height,r_frame_rate", "-of", "json", path],
        capture_output=True, check=True).stdout
    s = json.loads(out)["streams"][0]
    num, den = s["r_frame_rate"].split("/")
    return int(s["width"]), int(s["height"]), int(num) / int(den)


def highlight_transitions(path: str) -> tuple[np.ndarray, float]:
    w, h, fps = probe(path)
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", path, "-pix_fmt", "gray", "-f", "rawvideo", "-"],
        capture_output=True, check=True).stdout
    n = len(raw) // (w * h)
    frames = np.frombuffer(raw[:n * w * h], dtype=np.uint8).reshape(n, h, w)
    band = frames[:, TEXT_TOP:TEXT_BOTTOM, :]

    def block(frame):
        # Scan EVERY row: the highlight is usually not on the first row that
        # happens to contain bright pixels, and bailing early silently loses
        # most of the transitions.
        for row_i, row in enumerate(frame > BRIGHT):
            if not row.any():
                continue
            edges = np.flatnonzero(np.diff(np.concatenate(([0], row.view(np.int8), [0]))))
            for start, end in zip(edges[::2], edges[1::2]):
                if end - start >= MIN_RUN:
                    return (row_i // 20, int(start))
        return None

    seen, previous = [], None
    for i in range(n):
        current = block(band[i])
        if current is not None and current != previous:
            seen.append(i / fps)
        previous = current
    return np.array(seen), n / fps


def main() -> int:
    video, timings_json = sys.argv[1], sys.argv[2]
    starts = np.array(json.loads(open(timings_json).read())["starts"])
    duration = json.loads(open(timings_json).read())["duration"]
    transitions, video_len = highlight_transitions(video)
    if transitions.size == 0:
        print("no highlight found in the recording", file=sys.stderr)
        return 1

    best_delay, best_hits = 0.0, -1
    for delay in np.arange(0.0, max(1.0, video_len - duration + 1.0), 0.004):
        hits = sum(1 for t in transitions
                   if np.min(np.abs(starts + delay - t)) <= TOLERANCE)
        if hits > best_hits:
            best_delay, best_hits = float(delay), hits

    residuals = []
    for t in transitions:
        err = t - (starts + best_delay)
        j = int(np.argmin(np.abs(err)))
        if abs(err[j]) <= TOLERANCE:
            residuals.append(err[j])
    r = np.array(residuals)
    print(f"matched {len(r)}/{len(transitions)} transitions, "
          f"mean {r.mean() * 1000:+.0f} ms, sd {r.std() * 1000:.0f} ms", file=sys.stderr)
    if abs(r.mean()) > 0.04:
        print("alignment looks wrong; refusing to mux", file=sys.stderr)
        return 1
    print(f"{best_delay:.3f} {best_delay + duration + 0.9:.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
