"""Run readaloud and write every block its audio stream plays to a WAV, silence
included, so the file is a real-time timeline from the stream's start:
READALOUD_TAP=out.wav python tap.py <readaloud args>"""
import json, os, sys, wave
import numpy as np
from readaloud import player

blocks = []
first_play = []   # the block at which the first track started being consumed
orig_cb = player.Player._callback
def cb(self, out, frames, time_info, status):
    orig_cb(self, out, frames, time_info, status)
    if not first_play:
        tr = self._track
        if tr is not None and tr.pos > 0:
            first_play.append(len(blocks))
    blocks.append(np.array(out[:, 0], dtype=np.float32, copy=True))
player.Player._callback = cb

orig_close = player.Player.close
def close(self):
    orig_close(self)
    if blocks:
        audio = np.concatenate(blocks)
        pcm = np.clip(audio * 32767.0, -32768, 32767).astype("<i2")
        with wave.open(os.environ["READALOUD_TAP"], "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(self._sr)
            w.writeframes(pcm.tobytes())
        with open(os.environ["READALOUD_TAP"] + ".json", "w") as f:
            json.dump({"first_play": first_play[0] * self._blocksize / self._sr if first_play else None,
                       "blocksize": self._blocksize, "samplerate": self._sr}, f)
        blocks.clear()
player.Player.close = close

from readaloud.cli import main
sys.exit(main(sys.argv[1:]))
