# A lean xonsh in a pane

The idea: run xonsh inside the pymux server instead of in a process of
its own, so that a pane costs less. Python 3.14 has subinterpreters
(PEP 734), which look like the way to do it.

**Subinterpreters do not work for this.** A fork of the server does,
and it is better on both counts. The numbers and the reasons follow.

## What a pane costs

Private memory is what a pane really adds: pages that it shares with
the server are counted once, in the server. Interactive xonsh, 24x80,
measured to the first command it runs.

| how a pane runs xonsh | private memory | to a first command |
| --- | --- | --- |
| a process of its own (today) | 43.6 MB | 0.60 s |
| a subinterpreter | 26.8 MB | 0.25 s |
| a fork of a server that imported xonsh | **27.7 MB** | **0.30 s** |

The fork reaches the memory of a subinterpreter, because both share
what the parent already holds, and it starts in half the time of a
process. The fork itself takes 0.001 s; the rest is xonsh building its
prompt.

The saving needs the *interactive* stack imported in the parent, not
only `xonsh.main`: `xonsh.shells.ptk_shell`, `xonsh.pyghooks`,
`pygments.lexers` and a lexer built once. Without those the fork saves
only 12%, because xonsh dirties those pages after the fork either way.

## Why a subinterpreter cannot run a shell

Three restrictions of CPython, each of which xonsh needs:

1. **No signal handlers.** `signal.signal` raises "signal only works in
   main thread of the main interpreter". `XSH.load()` installs handlers
   and fails there.
2. **No daemon threads.** Running a command raises "daemon threads are
   disabled in this interpreter". xonsh reads the output of a process
   on a thread.
3. **No `os.fork`.** It raises "fork not supported for isolated
   subinterpreters". `subprocess` still works, because it uses
   `posix_spawn` or `fork_exec` rather than `os.fork`.

What does work: importing xonsh, prompt_toolkit and pyte; opening a pty
and pointing `sys.stdout` at the slave; `subprocess.run`; threads that
are not daemons.

Two more reasons to stay away even if xonsh changed. A subinterpreter
shares the process, so a crash or an `os._exit` in one pane takes the
whole server and every other pane with it. And there is no way to raise
`KeyboardInterrupt` in one interpreter from another, so Ctrl-C in a
pane has nothing to act on.

## Why a fork of the server works

ptterm already runs an **arbitrary callable** in the child of
`pty.fork`: `PosixPtyWithStdinFallback.from_command` builds a closure
that execs, and the class takes any callable. A child that does not
exec keeps everything the parent imported.

Everything a shell needs keeps working, because the child is a real
process: its own signal handlers, its own `os.fork`, real file
descriptors, and a crash that costs one pane.

This was measured, not guessed: xonsh starts interactively in such a
fork, draws its prompt, and runs a command.

## What it would take

1. An optional module, `pymux/inprocess.py`, that imports xonsh lazily.
   A pymux without xonsh installed must not notice, so the import goes
   inside the function and a failure turns the feature off.
2. An option, along the lines of `set-option in-process-shell on`, and
   a command that opens such a pane.
3. **File descriptor hygiene.** The child of a fork keeps every
   descriptor of the server: the listening socket and the pty master of
   every other pane. `exec` closes them today through the
   close-on-exec flag; a child that does not exec has to close them
   itself, by walking `/proc/self/fd`. This is the one part that is a
   correctness and a safety matter, not an optimisation.
4. The child also inherits the asyncio loop and the prompt_toolkit
   state of the server. xonsh builds its own loop, so the inherited one
   has to be dropped first.

## Reproducing the numbers

The scripts that produced this table are throwaway; the shapes are:

- `concurrent.interpreters.create()`, then `interp.exec(...)` for each
  restriction above.
- `pty.fork()`, then `xonsh.main.main(["--no-rc", "-i"])` in the child
  against `os.execv` of the `xonsh` binary in the child.
- `Private_Dirty` out of `/proc/<pid>/smaps_rollup`, read after the
  shell answered its first command.
