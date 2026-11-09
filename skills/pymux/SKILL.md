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

## Go out from the person's machine

A site blocks the server's address, or answers for the wrong place:
ask the person's machine for a SOCKS proxy. It holds the SSH link,
whether it attaches as a client or mirrors the server from a pymux of
its own (`connect ssh://...`).

```sh
pymux forward-port -R 127.0.0.1:0      # SOCKS on this machine, connecting from theirs
pymux sql "SELECT listening, error FROM forwards WHERE direction = 'remote' AND dest = 'socks'"
curl --socks5-hostname 127.0.0.1:PORT https://ipinfo.io/json
```

- The person confirms it once on their screen; until then no row.
  Poll the table; an `error` says why it failed.
- A standing row is reused: ask again only when there is none.
- It stays until `pymux unforward-port -R 127.0.0.1:PORT`.
- `--socks5-hostname`, not `--socks5`: names resolve at the far side.
- A browser: pymux-mcp `browser.open` with `via: "client"` does all of this.
