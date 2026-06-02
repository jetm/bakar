# VHS tape for the bakar build UI demo recording.
#   vhs scripts/demo_build_ui.tape   ->   docs/build-ui.gif
# Requires vhs, ttyd, ffmpeg, and the IosevkaTerm Nerd Font.

Output docs/build-ui.gif

Require uv

Set Shell "bash"
Set FontFamily "IosevkaTerm Nerd Font"
Set FontSize 18
Set Width 760
Set Height 245
Set Padding 12

Hide
Type "uv run python scripts/demo_build_ui.py"
Enter
Sleep 1400ms
Show
Sleep 11200ms
