---
name: pymux
description: Run background jobs with pymux instead of foreground commands, then wait on them, read their output, and query them with SQL. Use when work outlasts a comfortable foreground command or must survive the turn, when polling for completion, or when finding a previous job by tag or session.
---

# pymux jobs

`pymux run` starts a command as a server-owned job: pipes instead of a
terminal, so an agent submits it, goes away, and asks for the exit code
and the output later. The answer is the id: `run 'sleep 30'` says `1`,
and `wait-job 1` holds until it ends.

```sh
pymux run --tag deploy -- ./build.sh
pymux wait-job --tag deploy
pymux show-job --tag deploy
```

A job keeps the tail of both streams past a cap and counts what fell
off, the exit code, and the times, until the table forgets it past
fifty finished. Running jobs are never forgotten.

## Run

Reach for `run` when the work outlasts a comfortable foreground
command or must survive the turn; a ten-second check runs directly.

```sh
pymux run --tag deploy --tag branch=main -- ./build.sh
pymux run -w -- ./quick-check.sh
```

A bare run inherits the caller's working directory and the caller's
full environment, so it behaves like the same command in the calling
shell. Explicit `-d` wins over the inherited directory, and explicit
`--env KEY=VALUE` wins over inherited variables; a bare `--env KEY`
sets the empty string. Every job also carries `PYMUX_JOB` with its
id. With no caller on the wire -- a key binding, an old client -- the
job runs where the server stands, the way it always has.

`-w` waits here and answers with the output instead of the id, for the
agent that submits one command and reads the result. Everything else
(`-d`, `--tag`, `--session`, `--env`) is in `run --help`. Two rules
matter:

- Tag every job you will wait on later: the tag resolves the job after
  its id has scrolled past.
- A tag is `key` or `key=value`. A bare key matches any value; a
  repeated key keeps its last value; `key` and `key=` are two different
  tags.

## Find it again: tags, sessions, SQL

`wait-job`, `show-job`, `kill-job`, and `view-job` take an id or
`--tag`, never both and never neither. A lookup prefers the caller's
session -- its jobs sort first -- and anything else is fallback rather
than a miss, so an agent finds its own job and still reaches another's.
Only an explicit `--session ID` scopes the search to that session
alone. `list-jobs` needs no arguments: the caller's session sorts
first, oldest first within.

```sh
pymux wait-job --tag deploy
pymux show-job --tag deploy
pymux kill-job --tag deploy
pymux view-job --tag deploy
pymux list-jobs
```

The session travels by itself. The CLI resolves it fresh on every
invocation -- `OPENCODE_SESSION_ID`, `OCAHUB_SESSION`, `OC_SESSION`,
then the harness variables -- and the server stamps it onto the job,
so lookups and listings prefer it with no flag to remember. `run
--session ID` stamps that id instead, for acting as another session.
`PYMUX_AGENTIC_ID` set in the calling environment passes straight
through and wins over every harness variable: that is the override
when the resolution should say something else.

`pymux sql` asks the job database directly: free-form read-only
queries, or saved ones under `--save` / `--run` / `--list` /
`--delete`, with `--param KEY=VALUE`, `-n` to cap rows, `--timeout`
to bound the run, and `--json` for machine-readable output. Four
seeds ship with every boot: `running`, `failed`, `recent`, and
`by_tag`.

```sh
pymux sql --json --run by_tag --param tag=deploy
pymux sql "SELECT id, command FROM jobs WHERE status = 'done'"
```

## Wait bounded

`wait-job` holds until the job ends and reports the way `run -w`
does; a nonzero exit fails the command line, so a waiting script
reads `$?` the way a shell does. Bound every wait the harness allows
rather than polling asleep in a loop; `show-job` reads a job that
still runs when one peek is enough, and `view-job` tails one live in
a read-only pane (`-k` replaces a running program, `-t` names the
pane). At a keyboard `choose-job` (prefix `J`) is the picker: the jobs
newest first in a centered box, Enter showing one in the session's
overlay, `o` opening one as a pane in a new window, `t` running one's
command again interactively, `/` searching. None of the three touches
the recorded job.
