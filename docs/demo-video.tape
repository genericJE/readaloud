# Recording for the README/social demo video (docs/demo.mp4).
#
#   vhs docs/demo-video.tape          -> silent video
#   scripts/build-demo-video.sh       -> the same, with the speech muxed in
#
# vhs records headlessly and captures no audio, so the sound is rendered
# separately with `readaloud --save` and muxed afterwards.  That only stays in
# sync because the demo text is ONE chunk: --save inserts 0.20s between chunks
# while live playback has almost none, so anything longer would drift.

Output docs/demo.mp4

Set Shell "bash"
Set FontSize 16
Set Width 1180
Set Height 200
Set Padding 14
Set Theme "Catppuccin Mocha"
Set TypingSpeed 55ms
Set Framerate 30

Hide
Type "cd /private/tmp/claude-501/-Users-je/877580db-1478-42e7-867f-f1e1052788a5/scratchpad && clear"
Enter
Sleep 1s
Show

Type "mdcat --ansi spot.md | readaloud"
Sleep 500ms
Enter

# A beat of the honest "loading voice..." state, then skip the rest of the load.
Sleep 900ms
Hide
Sleep 3s
Show

# 17.0s of speech, plus the tail of the load and a beat at the end.
Sleep 26s

Hide
Type "q"
Sleep 500ms
