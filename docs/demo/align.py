"""Mux a scene's audio tap into its silent VHS recording.

    align.py <scene.silent.mp4> <tap.wav> <out.mp4>

Both sides are real time, so only the offset is unknown: the tap's sidecar
says when playback started (stream seconds), and in the video that is the
first change in the text band after the document was drawn and sat still
while the model loaded. The video is cut a beat after the tap's last sound.
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
t_video = word / fps
# cross-check: the status bar's last change before that is LOADING turning
# into PLAY, which is the tap's first_play
play = word - 1
while play > big and bar_ch[play] < CHANGED:
    play -= 1

side = json.load(open(tap + ".json"))
with wave.open(tap) as wv:
    sr = wv.getframerate()
    pcm = np.frombuffer(wv.readframes(wv.getnframes()), dtype="<i2")
loud = np.flatnonzero(np.abs(pcm) > 300)
t_last_sound = loud[-1] / sr
delay = t_video - loud[0] / sr
delay_by_play = play / fps - side["first_play"]
end = delay + t_last_sound + TAIL
if end > n / fps:
    print(f"the recording stops {n / fps - delay - t_last_sound:.2f}s after the last sound; "
          f"give the tape a longer Sleep for a {TAIL}s tail", file=sys.stderr)
    end = n / fps
print(f"draw at {big / fps:.2f}s, first word lit at {t_video:.2f}s, first sound "
      f"{loud[0] / sr:.2f}s into the tap, so the audio is delayed {delay:+.3f}s "
      f"({delay_by_play:+.3f}s by PLAY at {play / fps:.2f}s against play starting "
      f"{side['first_play']:.2f}s into the tap); last sound {t_last_sound:.2f}s, "
      f"cut at {end:.2f}s of {n / fps:.2f}s", file=sys.stderr)
assert abs(delay - delay_by_play) < 0.1, "the two offsets disagree; look at the frames"
afilter = f"adelay={int(delay * 1000)}|{int(delay * 1000)}" if delay >= 0 else f"atrim=start={-delay},asetpts=PTS-STARTPTS"
subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", video, "-i", tap,
                "-filter_complex", f"[1:a]{afilter},aformat=sample_rates=48000:channel_layouts=stereo[a]",
                "-map", "0:v", "-map", "[a]", "-t", f"{end:.3f}",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20", "-r", f"{fps:g}",
                "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", out], check=True)
