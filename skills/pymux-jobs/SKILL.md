---
name: pymux-jobs
description: Run background jobs with pymux instead of foreground commands, then wait on them and read their output. Use when work outlasts a comfortable foreground command or must survive the turn, when waiting on completion, or when finding a previous job by tag or session.
---

# pymux jobs

`pymux run` starts a command as a server-owned job and answers with
its id. The job runs without a pane -- pipes instead of a terminal,
unless `run --pty` gives it one -- so submit it, go away, and ask for
the exit code and the output later.

```sh
pymux run --tag deploy -- ./build.sh   # answers 1
pymux wait-job --tag deploy            # holds, answers with the output
pymux show-job --tag deploy            # one peek, no waiting
```

The default loop is start tagged, then wait, then read. One wait per
tool call: `wait-job` holds until the job ends, and the tool-call
limit bounds it -- a job that outlasts the call is still there for
the next `wait-job`, or for a `show-job` peek meanwhile.

```sh
pymux run --tag deploy -- ./build.sh
pymux wait-job --tag deploy; echo rc=$?
pymux show-job --tag deploy
```

`wait-job` reports the way `run -w` does: the output on stdout, and
a nonzero exit fails the command line, so a waiting script reads `$?`
the way a shell does. Read it with `; echo` after the command, never
through a pipe (`| tail` reports tail's code).

Two rules matter, and both bite after the id has scrolled past:

- Tag every job you will wait on later: the tag resolves the job
  when its id is gone from view. An id and tags together refuse, and
  so does neither.
- A tag is `key` or `key=value`. A bare key matches any value; a
  repeated key keeps its last value; `key` and `key=` are two
  different tags.

```sh
pymux run --tag deploy --tag branch=main -- ./build.sh
pymux run -w -- ./quick-check.sh       # waits here, answers output not id
```

A bare run inherits the caller's working directory and the caller's
full environment, so it behaves like the same command in the calling
shell. Explicit `-d` wins over the inherited directory, and explicit
`--env KEY=VALUE` wins over inherited variables; a bare `--env KEY`
sets the empty string. Every job also carries `PYMUX_JOB` with its
id. `run --pty` runs the command on a terminal of its own instead of
pipes, for the program that needs one to behave; waiting and reading
stay the same, with the whole terminal output in the stdout tail.

A job keeps the tail of both streams past a cap and counts what fell
off, the exit code, and the times. A job that does nothing is
forgotten past `job-ttl` seconds, an hour unless set: a running one
is ended first, a waited one never goes, zero keeps everything.

## Never

No `sleep` before re-reading: `sleep 60; pymux show-job` is a wait
with no exit code and no early return. `wait-job` holds until the job
ends and reports; that is what it is for.

No `&`, `nohup`, or `setsid`: a backgrounded shell command is a job
pymux cannot see, wait on, or read back. `run` is the backgrounding.

No `pkill` / `pgrep` for job control: name the job, don't hunt the
process. `kill-job` ends one with SIGTERM; a job that already ended
says so instead of failing.

No re-running to check: `run` again starts a second job, it does not
look at the first. `show-job` reads a job that still runs when one
peek is enough.

## Find it again: tags, sessions, SQL

`wait-job`, `show-job`, and `kill-job` take an id or `--tag`. A
lookup prefers the caller's session -- its jobs sort first -- and
anything else is fallback rather than a miss, so you find your own
job and still reach another's. Only an explicit `--session ID`
scopes the search to that session alone. `list-jobs` needs no
arguments: the caller's session sorts first, oldest first within.

```sh
pymux wait-job --tag deploy
pymux kill-job --tag deploy
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
