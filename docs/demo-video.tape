# Recording for the README/social demo video (docs/demo.mp4).
#
#   vhs docs/demo-video.tape
#
# This produces a SILENT video: vhs records headlessly and captures no audio.
# docs/demo.mp4 has the speech muxed in, which was done by rendering the same
# text separately with `readaloud --save` and lining the two up by finding the
# frame where the highlight first appears.  Two things to know if you redo it:
# the mp4 comes out at 25fps whatever `Set Framerate` says, and the demo text is
# deliberately ONE chunk because a rendered file has to guess the inter-chunk
# gaps that live playback does not have.

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
