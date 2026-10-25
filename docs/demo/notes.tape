# One scene of docs/demo.mp4, recorded by build.sh; see there.  This really
# runs readaloud, so it really speaks out loud.
Output notes.silent.mp4

Set Shell "bash"
Set FontSize 16
Set Width 1180
Set Height 520
Set Padding 14
Set Theme "Catppuccin Mocha"
Set TypingSpeed 55ms
Set Framerate 30

Hide
Type `readaloud() { READALOUD_TAP=$1.tap.wav uv run --project ../.. python tap.py --config conf --no-media-keys "$@"; } && clear`
Enter
Sleep 1s
Show

Type "readaloud notes.md"
Sleep 500ms
Enter

# the model loads in about two seconds, shown as it is; the rest is the
# reading, cut to length by align.py
Sleep 30s

Hide
Type "q"
Sleep 1s
