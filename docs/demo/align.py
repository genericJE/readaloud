"""Mux a scene's audio tap into its silent VHS recording.

    align.py <scene.silent.mp4> <tap.wav> <out.mp4>

The tap is real time, the recording is not quite: vhs cannot always capture
at the rate the file is encoded at, so the picture runs a few percent fast
(measured 0.92 to 0.96 video seconds per real second, worse with more on
screen) and a highlight anchored at the first word pulls ahead of the voice
by seconds. So the speed is measured too: the sound's onsets after a pause
are matched to the highlight's moves in the picture, the scale that fits
them best is read off, and the video is retimed to the sound before the
two are muxed. The offset is then the first word lighting against the
first sound. The video is cut a beat after the tap's last sound.
"""
import json, subprocess, sys, wave
import numpy as np

video, tap, out = sys.argv[1:4]
TEXT_BOTTOM = 470          # the status bar sits below
STILL = 10                 # frames without change = the document is drawn
CHANGED = 300              # pixels: a word's highlight, not noise
TAIL = 1.2                 # seconds after the last sound

info = json.loads(subprocess.run(
    ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
     "stream=width,height,r_frame_rate", "-of", "json", video],
    capture_output=True, check=True).stdout)["streams"][0]
w, h = info["width"], info["height"]
num, den = info["r_frame_rate"].split("/")
fps = int(num) / int(den)
raw = subprocess.run(["ffmpeg", "-v", "error", "-i", video, "-pix_fmt", "gray",
                      "-f", "rawvideo", "-"], capture_output=True, check=True).stdout
n = len(raw) // (w * h)
frames = np.frombuffer(raw[: n * w * h], dtype=np.uint8).reshape(n, h, w)
band = frames[:, :TEXT_BOTTOM, :]
bar = frames[:, TEXT_BOTTOM:, :]
def changes(a):
    return np.array([0] + [int((np.abs(a[i].astype(np.int16) - a[i - 1]) > 40).sum())
                           for i in range(1, len(a))])
text_ch, bar_ch = changes(band), changes(bar)
big = int(np.argmax(bar_ch))                        # the TUI appears, status bar and all
# the first word lights as its sound starts (Kokoro's own word timing), which
# is the first change in the text band once the document sits still
word = big + 1
while word < n and text_ch[word] < CHANGED:
    word += 1
assert word < n and word - big >= STILL, f"no word lit after the draw at {big / fps:.2f}s"
moves = np.array([i / fps for i in range(word, n) if text_ch[i] >= CHANGED])

side = json.load(open(tap + ".json"))
with wave.open(tap) as wv:
    sr = wv.getframerate()
    pcm = np.frombuffer(wv.readframes(wv.getnframes()), dtype="<i2")
loud = np.flatnonzero(np.abs(pcm) > 300) / sr
t_last_sound = loud[-1]
onsets = np.array([loud[0]] + [loud[i] for i in range(1, len(loud)) if loud[i] - loud[i - 1] > 0.12])

# the picture's speed: video seconds per real second, from the scale that
# lands the most onsets on a highlight move, anchored at the first word
def fit(scale):
    d = moves[0] - scale * onsets[0]
    err = [m - (d + scale * o) for o in onsets
           for m in [moves[np.argmin(np.abs(moves - (d + scale * o)))]]
           if abs(m - (d + scale * o)) < 0.08]
    return (len(err), -float(np.std(err)) if err else 0.0), d
scale, (score, d) = max(((sc, fit(sc)) for sc in np.arange(0.85, 1.05, 0.0005)), key=lambda x: x[1][0])
assert score[0] >= min(6, len(onsets) - 1), f"only {score[0]} of {len(onsets)} onsets match a highlight move"

t_video = moves[0] / scale                          # the first word, in real seconds
delay = t_video - loud[0]
delay_by_play = (play_at := max((i for i in range(big + 1, word) if bar_ch[i] >= CHANGED), default=word)) / fps / scale - side["first_play"]
length = n / fps / scale
end = delay + t_last_sound + TAIL
print(f"draw at {big / fps:.2f}s; the picture runs at {scale:.4f} of real time "
      f"({score[0]}/{len(onsets)} onsets on a highlight move, {-score[1] * 1000:.0f} ms apart), retimed; first word lit at "
      f"{t_video:.2f}s, first sound {loud[0]:.2f}s into the tap, so the audio is delayed "
      f"{delay:+.3f}s ({delay_by_play:+.3f}s by PLAY at {play_at / fps / scale:.2f}s against play "
      f"starting {side['first_play']:.2f}s into the tap); last sound {t_last_sound:.2f}s, "
      f"cut at {end:.2f}s of {length:.2f}s", file=sys.stderr)
assert abs(delay - delay_by_play) < 0.15, "the two offsets disagree; look at the frames"
if end > length:
    print(f"the recording stops {length - delay - t_last_sound:.2f}s after the last sound; "
          f"give the tape a longer Sleep for a {TAIL}s tail", file=sys.stderr)
    end = length
afilter = f"adelay={int(delay * 1000)}|{int(delay * 1000)}" if delay >= 0 else f"atrim=start={-delay},asetpts=PTS-STARTPTS"
subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", video, "-i", tap,
                "-filter_complex", f"[0:v]setpts=PTS/{scale:.5f}[v];"
                                   f"[1:a]{afilter},aformat=sample_rates=48000:channel_layouts=stereo[a]",
                "-map", "[v]", "-map", "[a]", "-t", f"{end:.3f}",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20", "-r", f"{fps:g}",
                "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", out], check=True)
