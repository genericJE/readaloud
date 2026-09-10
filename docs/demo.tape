# Recording for the README demo GIF.
#   vhs docs/demo.tape
# Produces docs/demo.gif.
#
# The point of this GIF is ONE thing: the highlight stepping from word to
# word in time with the speech. No scrolling, no key presses, no feature
# tour -- those are what the README text is for. Just the reading.
#
# NOTE: this really runs readaloud, so it really speaks out loud.

Output docs/demo.gif

Set Shell "bash"
Set FontSize 16
Set Width 1180
Set Height 460
Set Padding 14
Set Theme "Catppuccin Mocha"
Set TypingSpeed 55ms
Set Framerate 30
Set PlaybackSpeed 1.0

Hide
Type "cd ~/Documents/JE/repos/readaloud && clear"
Enter
Sleep 1s
Show

Type "mdcat --ansi README.md | readaloud"
Sleep 500ms
Enter

# A moment of the honest "loading voice..." state, then skip the rest of
# the ~4s model load rather than spending GIF frames on a progress bar.
Sleep 900ms
Hide
Sleep 5s
Show

# Everything from here is the highlight tracking the audio, uninterrupted.
Sleep 14s

Hide
Type "q"
Sleep 500ms
