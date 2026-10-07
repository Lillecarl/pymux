---
name: pymux
description: Drive pymux terminals interactively instead of through the shell. Use when attaching to a session, arranging panes and windows, or watching a job's output live in a pane.
---

# pymux

pymux is a terminal multiplexer: sessions holding windows holding
panes, each pane a program on a pty. Work that outlasts a foreground
command goes through jobs instead -- that is the `pymux-jobs` skill,
and this one does not repeat it.

## Watch a job live

`view-job` tails a job in a read-only pane: the tail replayed, then
everything new as it arrives, with `[job N exited X]` at the end.
Keys fall on the floor, and closing the pane never touches the job.
`-k` replaces a program that still runs, `-t` names the pane to
replace.

```sh
pymux view-job --tag deploy
```

At a keyboard `choose-job` (prefix `J`) is the picker: the jobs
newest first in a centered box, Enter showing one in the session's
overlay, `o` opening one as a pane in a new window, `t` running one's
command again interactively, `/` searching. None of the three touches
the recorded job.
