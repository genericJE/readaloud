#!/usr/bin/env bash
# Build docs/demo.mp4: two terminal recordings, with the spoken audio muxed in.
#
#   docs/demo/build.sh
#
# needs vhs, ffmpeg, mdcat and the model weights already downloaded.
#
# vhs records headlessly and captures no audio, and macOS cannot capture what
# the speakers play without a loopback device.  So each scene runs readaloud
# through tap.py, which writes every block the audio stream plays, silence
# included, to <file>.tap.wav: a real-time timeline of the sound.  align.py
# then finds where it belongs in the picture (the first word lights as its
# sound starts) and muxes the two.  Things that cost time to learn:
#
# * the mp4 comes out at 25 fps whatever `Set Framerate` says, and vhs does
#   not capture every frame, so the picture runs 3 to 8 percent faster than
#   real time (worse with more on screen) and a highlight that merely starts
#   in sync pulls ahead of the voice by seconds: align.py measures the speed
#   against the sound's onsets and retimes the picture;
# * the tap's timeline is only real time once playback starts: while the model
#   loads, the audio callback is starved and blocks go missing, so nothing
#   before the first sound can be used for the offset;
# * notes.md has a `##` heading because mdcat draws a `#` one as bright white
#   on bright blue, which the Catppuccin theme turns into grey on blue;
# * the recordings are made one at a time: two readings at once would both
#   fight for the audio device and the CPU.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
command -v vhs >/dev/null || { echo "needs vhs (brew install vhs)" >&2; exit 1; }
command -v mdcat >/dev/null || { echo "needs mdcat (brew install mdcat)" >&2; exit 1; }

rm -rf conf && uv run --project ../.. readaloud --config conf --write-config >/dev/null
rm -f ./*.tap.wav ./*.tap.wav.json ./*.silent.mp4 notes.mp4 app.mp4
vhs notes.tape
vhs app.tape
uv run --project ../.. python align.py notes.silent.mp4 notes.md.tap.wav notes.mp4
uv run --project ../.. python align.py app.silent.mp4 app.py.tap.wav app.mp4
printf "file 'notes.mp4'\nfile 'app.mp4'\n" > scenes.txt
ffmpeg -v error -y -f concat -safe 0 -i scenes.txt -c copy ../demo.mp4
rm -rf conf ./*.tap.wav ./*.tap.wav.json ./*.silent.mp4 notes.mp4 app.mp4 scenes.txt
echo "wrote docs/demo.mp4"
