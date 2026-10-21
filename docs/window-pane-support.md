# Window panes: what each terminal supports

A window pane shows a GUI program through the Wayland compositor of
`pysmithay`. pymux finds out per client which features that client's
terminal has. This page records which terminals were tried, and what
each one answered. Lillecarl/pymux#651.

## The features, and how pymux detects them

| Feature | Detection |
| --- | --- |
| Window pane pictures (#620) | the kitty graphics query; without it, a notice |
| Shared memory frames (#634) | a one-pixel `t=s` probe |
| VP9 (#639), RGBA (#649), one edit per frame (#644) | follow from the graphics protocol |
| Pacing by the terminal's answer (#643) | kitty's reply to the last command |
| Pixel mouse, mode 1016 (#648) | DECRQM |
| Pointer shapes, OSC 22 (#650) | none: pymux always sends them |

## What was measured

`checks.pymux-window-pane-pictures` opens a window pane in each terminal
that the picture harness drives, and writes `support.txt` from each
terminal's server log. On 2026-10-21 it wrote:

| Terminal | kitty graphics | shared memory | pixel mouse |
| --- | --- | --- | --- |
| xterm | no | no | yes |
| foot | no | no | yes |
| kitty | yes | yes | yes |

Run it again to update this table:

    nix build --file . checks.pymux-window-pane-pictures.run

By hand, every feature in the first table was tried in kitty: 0.49.2 on
macOS, and 0.48.2 on Linux under cage.

## Not tried

Ghostty and WezTerm say they speak the kitty graphics protocol, and
Ghostty says it has mode 1016. Neither is a seat of the picture
harness, so nothing here has tested them. A difference there shows only
when somebody opens that terminal.
