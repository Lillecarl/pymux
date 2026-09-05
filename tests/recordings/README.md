# Recordings

What a real program drew, once, on a real machine. `take_a_picture.py`
replays each one twice — bare and in a pymux pane — and subtracts one
picture from the other.

A fixture written by hand can only hold what the person writing it
thought to write. This holds what a program really did.

## Why this exists

Some programs cannot run in a build sandbox. Claude Code needs a login,
a project and the configuration of the person using it, and what it
draws depends on all three. There is no way to start it in a check.

There is a way to record it. Run it once here, keep every byte it wrote,
and play those bytes back on both sides. The pane sees exactly what the
program made, and the picture says whether pymux changed it.

## Making one

    ptterm-record --into . --lines 24 --columns 80 claude -- claude

`ptterm-record` comes with ptterm, so it runs wherever the program
that has the fault runs. `nix shell --file . ptterm` puts it on the
path, and `python -m ptterm.record` works from a checkout.

The program runs normally: keys go to it and what it draws reaches the
screen. Use it, reproduce whatever is wrong, and quit. Three files land
here, and `<name>.bin` is the one the harness reads.

**The size has to be 24 by 80.** That is what the harness runs, and a
recording made at another size draws its own idea of where the edges
are. The recorder sets the size of the pty it makes, so the window this
is typed in does not matter.

**Record outside pymux.** The point is a picture of the program, and a
recording made inside a pane already has pymux in it.

## What the harness does with it

A file called `claude.bin` becomes a fixture called `recorded-claude`.
Nothing else is needed: the harness finds it.

    nix build --file . checks.pymux-pictures
    PYMUX_PICTURES=recorded-claude nix build --file . checks.pymux-pictures.run

The pictures come out at
`result/<terminal>/recorded-claude/{bare,pymux,difference}.png`.

The cursor is hidden before and after the recording plays. A still
picture cannot hold a cursor that blinks, and the two runs do not reach
the same point of the blink at the same moment.

## Before you commit one

**A recording holds whatever was on the screen.** A path, a file name, a
branch, a piece of the code being worked on, the text of a prompt. Read
`<name>.bin` before it goes anywhere:

    cat -v claude.bin | less

A recording of a program that showed a token, a key or a customer's data
does not belong in a repository. Record the same fault again with
nothing private on the screen.

**Keep it short.** Every byte is replayed on both sides of every
terminal in the harness, and the file is in the repository forever.
Record the smallest session that still shows the fault.

## The other two files

`<name>.reads.json` holds the size of every read, and the size of the
terminal. The harness reads the size from it and refuses a recording
that was made at the wrong one.

`<name>.session.json` holds both directions with a time on each piece:
what the program asked the terminal, and what the terminal answered.
The picture harness does not use it. It is there because a capture
without it cannot be understood later — what a program draws depends on
the answers it got, and those answers came from the terminal it was
recorded on.
