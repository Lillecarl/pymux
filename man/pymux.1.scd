pymux(1)

# NAME

pymux - pure Python terminal multiplexer, with server-owned background jobs

# SYNOPSIS

*pymux* [_OPTION_]... [_COMMAND_ [_ARGS_]...]

*pymux run* [_OPTION_]... _COMMAND_...

*pymux wait-job*|*show-job*|*kill-job*|*view-job* [_OPTION_]... [_ID_]

*pymux list-jobs*

*pymux sql* [_OPTION_]... [_QUERY_]

# DESCRIPTION

*pymux* is a terminal multiplexer in the shape of *tmux*(1): a server
holds sessions, windows and panes, and clients attach to them or send
them commands. Any word that is not a mode (*standalone*, *integrated*,
*start-server*, *attach*, *list-sessions*, *ls*, *find*, *diagnose*,
*web*) is a command for a running server, answered on stdout; *--help*
lists the rest.

*run* starts a command as a server-owned job: pipes instead of a
terminal, so the caller goes away and asks for the exit code and the
output later. The answer is the id, and *wait-job* holds until it ends.
Jobs keep the tail of both streams past a cap and count what fell off,
with the exit code and the times, until the table forgets them past
fifty finished. Running jobs are never forgotten, and neither is one
somebody still waits for.

# JOB CONTROL

*run* [_OPTION_]... _COMMAND_...++
*wait-job* [_ID_|--tag _KEY_[=_VALUE_]...] [_--session ID_]++
*show-job* [_ID_|--tag _KEY_[=_VALUE_]...] [_--session ID_]++
*kill-job* [_ID_|--tag _KEY_[=_VALUE_]...] [_--session ID_]++
*view-job* [_-k_] [_-t _PANE_] [_ID_|--tag _KEY_[=_VALUE_]...] [_--session ID_]++
*list-jobs*

Start a tagged job, then wait on the tag: the tag resolves the job
after its id has scrolled past. A tag is *key* or *key=value*; a bare
key matches any value, a repeated key keeps its last value, and *key*
and *key=* are two different tags. An id and tags together refuse, and
so does neither.

	pymux run --tag deploy -- ./build.sh++
	pymux wait-job --tag deploy; echo $?

*-w* waits in *run* itself and answers with the output instead of the
id. *show-job* reads a job that still runs; *-e* reads stderr.
*view-job* tails one live in a read-only pane: *-k* replaces a program
that still runs, *-t* names the pane to replace.

A lookup prefers the caller's session -- its jobs sort first -- and
anything else is fallback rather than a miss. Only *--session ID*
scopes the search to that session alone. *list-jobs* needs no
arguments: the caller's session sorts first, oldest first within.

A bare *run* inherits the caller's working directory and the caller's
full environment, so the job behaves like the same command in the
calling shell. *-d* _DIRECTORY_ wins over the inherited directory, and
*--env* _KEY_=_VALUE_ wins over inherited variables; a bare *--env*
_KEY_ sets the empty string. Every job also carries *PYMUX_JOB* with
its id. *run --session ID* stamps that session instead of the caller's.

# QUERYING

*sql* [_QUERY_] [_--save NAME_] [_--run NAME_] [_--list_] [_--delete NAME_] [_--param KEY_=_VALUE_]... [_-n _ROWS_] [_--timeout _SECONDS_] [_--json_]

*sql* asks the job database directly. A free-form query reads only:
*SELECT*, *WITH*, *EXPLAIN*, *PRAGMA* and *VALUES*, and anything else
is refused before it runs. *-n* caps the rows answered, default 100
and 0 for all; *--timeout* bounds the run in seconds, default 10 and 0
for unbounded; *--json* answers machine-readable rows.

Four saved queries ship with every boot: *running*, *failed*,
*recent*, and *by_tag*. *--save* keeps a query under a name,
*--delete* forgets one, and a name an agent saves replaces a seed for
the rest of the boot.

	pymux sql --json --run by_tag --param tag=deploy++
	pymux sql "SELECT id, command FROM jobs WHERE status = 'done'"

# ENVIRONMENT

*OPENCODE_SESSION_ID*, *OCAHUB_SESSION*, *OC_SESSION*,
*CODEX_THREAD_ID*, *TRAE_AI_SHELL_ID*, *CURSOR_TRACE_ID*

	The agent session the calling command comes from, first set wins.
	The client resolves it fresh on every invocation and the server
	stamps it onto the job, so lookups and listings prefer it with no
	flag to remember.

*PYMUX_AGENTIC_ID*

	The override: a value here passes straight through and wins over
	every harness variable above.

*PYMUX*

	The socket to reach, when *-S* names none. A number after a comma
	names a pane: commands that take one read it from there.

# FILES

*~/.pymux.conf*, *$XDG_CONFIG_HOME/pymux/pymux.conf*

	The configuration file, the first one found. *pymux -f* names one
	instead.

*share/skills/pymux/pymux/SKILL.md*

	The agent skill under the *pymux* package: the job workflow above,
	written for the caller rather than the reference.
