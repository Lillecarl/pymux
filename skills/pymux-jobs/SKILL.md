---
name: pymux-jobs
description: Run background jobs with pymux instead of foreground commands, then follow their output. Use when work outlasts a comfortable foreground command or must survive the turn, or when checking on a previous job by tag.
---

# pymux jobs

`pymux run` starts a command as a server-owned job and answers with
its id. The job runs without a pane -- pipes instead of a terminal,
unless `run --pty` gives it one -- so submit it, go away, and follow
it later.

```sh
pymux run --tag deploy -- ./build.sh
```

`pymux tail` replays the last lines and follows the rest live, and
the command line reads the exit code when it ends. One call does the
waiting and the reading. Always pass `--timeout`: under an agent
session a `tail` without one refuses rather than waiting without
bound, and short ones cost nothing, since tailing again picks up
where the last one stopped.

```sh
pymux run --tag deploy -- ./build.sh
pymux tail --tag deploy --timeout 90; echo rc=$?
pymux tail --tag deploy --timeout 90 --lines 0; echo rc=$?
```

Or skip the second turn: `run --timeout` streams a fresh job the
same way, and past the timeout the job keeps running under its tag.

```sh
pymux run --tag deploy --timeout 90 -- ./build.sh; echo rc=$?
```

The codes: 0 done, and anything else the job's own exit. 124 still
running -- tail again. 4 no job answers to that tag, so check the
spelling. Read the code with `; echo` after the command, never
through a pipe (`| tail` reports tail's code).

Two rules matter, and both bite after the id has scrolled past:

- Tag every job you will tail later: the tag resolves the job when
  its id is gone from view. An id and tags together refuse, and so
  does neither.
- A tag is `key` or `key=value`. A bare key matches any value; a
  repeated key keeps its last value; `key` and `key=` are two
  different tags.

```sh
pymux run --tag deploy --tag branch=main -- ./build.sh
```

A bare run inherits the caller's working directory and the caller's
full environment, so it behaves like the same command in the calling
shell. Explicit `-d` wins over the inherited directory, and explicit
`--env KEY=VALUE` wins over inherited variables; a bare `--env KEY`
sets the empty string. Every job also carries `PYMUX_JOB` with its
id. `run --pty` runs the command on a terminal of its own instead of
pipes, for the program that needs one to behave; tailing stays the
same, with the whole terminal output in the stdout tail.

A job keeps the tail of both streams past a cap and counts what fell
off, the exit code, and the times. A job that does nothing is
forgotten past `job-ttl` seconds, an hour unless set: a running one
is ended first, a tailed one never goes, zero keeps everything. A
lookup prefers the caller's session -- its jobs sort first -- and
anything else is fallback rather than a miss; only an explicit
`--session ID` scopes the search to that session alone, and `run
--session ID` stamps that id instead. The session travels by itself
with no flag to remember.

## Never

No `sleep` before re-reading: `sleep 60; pymux tail` is a wait with
no exit code and no early return. `tail --timeout 90` holds,
reports, and stops in time; that is what it is for.

No `&`, `nohup`, or `setsid`: a backgrounded shell command is a job
pymux cannot see, follow, or read back. `run` is the backgrounding.

No `pkill` / `pgrep` for job control: name the job, don't hunt the
process. `kill-job --tag deploy` ends one early with SIGTERM.

No re-running to check: `run` again starts a second job, it does not
look at the first. `tail --timeout 0` replays without waiting when
one peek is enough.
