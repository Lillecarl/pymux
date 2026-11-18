"""
tmux's command grammar, ported from `cmd-parse.y` of tmux 3.6.

One reader for every place a command arrives as text: a configuration
file, the command prompt, a hook, the command line of a client. Each
used to split with `shlex`, which knows no `#` comment after a command,
no `\\` at the end of a line, no `{ }` block, no `%if` and no `$VAR`.
Lillecarl/pyterm#1029.

The lexer works on bytes, as tmux's does: an octal escape makes a byte,
not a character, and `\\u` makes the UTF-8 of its code point.

**A file is parsed whole, then run.** `%if` and `NAME=value` act while
it is read, so a `%if` cannot see an option a line above it sets. That
is tmux's order, and a configuration written for tmux expects it.
"""

from __future__ import annotations

import os
import pwd
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum, auto

__all__ = [
    "Block",
    "Command",
    "ParseError",
    "from_arguments",
    "join",
    "parse",
    "print_arguments",
    "print_commands",
    "quote",
    "template_replace",
    "to_arguments",
    "words_of",
]

EOF = -1
_BACKSLASH = ord("\\")
_NEWLINE = ord("\n")
_MAX_ENVIRON = 16384


class ParseError(Exception):
    "A command text tmux would refuse, and the line it is on."

    def __init__(self, line: int, message: str) -> None:
        super().__init__(message)
        self.line = line
        self.message = message


@dataclass
class Block:
    "A `{ … }` argument: commands, which a command such as bind-key runs later."

    commands: list[Command]

    def __str__(self) -> str:
        return print_commands(self.commands)


@dataclass
class Command:
    line: int
    words: list[str | Block] = field(default_factory=list)

    def strings(self) -> list[str]:
        "The words, with each block written back as command text."
        return [str(word) for word in self.words]


class _Kind(Enum):
    END = auto()
    NEWLINE = auto()
    SEMICOLON = auto()
    OPEN = auto()
    CLOSE = auto()
    TOKEN = auto()
    EQUALS = auto()
    FORMAT = auto()
    HIDDEN = auto()
    IF = auto()
    ELIF = auto()
    ELSE = auto()
    ENDIF = auto()


_KEYWORDS = {
    b"%hidden": _Kind.HIDDEN,
    b"%if": _Kind.IF,
    b"%elif": _Kind.ELIF,
    b"%else": _Kind.ELSE,
    b"%endif": _Kind.ENDIF,
}


@dataclass
class _Token:
    kind: _Kind
    line: int
    text: str = ""


def _decode(data: bytes | bytearray) -> str:
    return bytes(data).decode("utf-8", "surrogateescape")


def _is_var(ch: int, first: bool) -> bool:
    if ch == ord("="):
        return False
    if first and ord("0") <= ch <= ord("9"):
        return False
    return 0 <= ch < 128 and (chr(ch).isalnum() or ch == ord("_"))


class _Lexer:
    "`yylex` and its helpers, one function each, in tmux's names."

    def __init__(self, text: str, environment: Mapping[str, str], line: int) -> None:
        self.data = text.encode("utf-8", "surrogateescape")
        self.offset = 0
        self.pushed: list[int] = []
        self.escapes = 0
        self.line = line
        self.eol = False
        self.eof = False
        self.condition = False
        self.environment = environment

    def error(self, message: str) -> ParseError:
        return ParseError(self.line, message)

    def getc1(self) -> int:
        if self.pushed:
            return self.pushed.pop()
        if self.offset == len(self.data):
            return EOF
        ch = self.data[self.offset]
        self.offset += 1
        return ch

    def ungetc(self, ch: int) -> None:
        if ch != EOF:
            self.pushed.append(ch)

    def getc(self) -> int:
        "One character, with `\\` and a newline taken out as a continued line."
        if self.escapes:
            self.escapes -= 1
            return _BACKSLASH
        while True:
            ch = self.getc1()
            if ch == _BACKSLASH:
                self.escapes += 1
                continue
            if ch == _NEWLINE and self.escapes % 2 == 1:
                self.line += 1
                self.escapes -= 1
                continue
            if self.escapes:
                self.ungetc(ch)
                self.escapes -= 1
                return _BACKSLASH
            return ch

    def get_word(self, ch: int) -> bytes:
        buf = bytearray()
        while True:
            buf.append(ch)
            ch = self.getc()
            if ch == EOF or ch in b" \t\n":
                break
        self.ungetc(ch)
        return bytes(buf)

    def next(self) -> _Token:
        if self.eol:
            self.line += 1
        self.eol = False

        condition = self.condition
        self.condition = False

        while True:
            ch = self.getc()
            if ch == EOF:
                # Every text ends in a newline, so the parser has one rule.
                if self.eof:
                    return _Token(_Kind.END, self.line)
                self.eof = True
                return _Token(_Kind.NEWLINE, self.line)
            if ch in b" \t":
                continue
            if ch == ord("\r"):
                ch = self.getc()
                if ch != _NEWLINE:
                    self.ungetc(ch)
                    ch = ord("\r")
            if ch == _NEWLINE:
                self.eol = True
                return _Token(_Kind.NEWLINE, self.line)
            if ch == ord(";"):
                return _Token(_Kind.SEMICOLON, self.line)
            if ch == ord("{"):
                return _Token(_Kind.OPEN, self.line)
            if ch == ord("}"):
                return _Token(_Kind.CLOSE, self.line)

            if ch == ord("#"):
                # `#{` after `%if` opens a format; any other `#` a comment.
                following = self.getc()
                if condition and following == ord("{"):
                    return _Token(_Kind.FORMAT, self.line, self.format())
                while following not in (_NEWLINE, EOF):
                    following = self.getc()
                if following == _NEWLINE:
                    line = self.line
                    self.line += 1
                    return _Token(_Kind.NEWLINE, line)
                continue

            if ch == ord("%"):
                # A word of only `%` and digits is a word: `%1` names a pane.
                word = self.get_word(ch)
                if not word.strip(b"%0123456789"):
                    return _Token(_Kind.TOKEN, self.line, _decode(word))
                self.condition = True
                kind = _KEYWORDS.get(word)
                if kind is None:
                    raise self.error("unknown condition: %s" % (_decode(word),))
                return _Token(kind, self.line)

            line = self.line
            word = self.token(ch)
            kind = _Kind.TOKEN
            name, equals, _value = word.partition(b"=")
            if equals and name and _is_var(name[0], True) and all(_is_var(c, False) for c in name[1:]):
                kind = _Kind.EQUALS
            return _Token(kind, line, _decode(word))

    def format(self) -> str:
        buf = bytearray(b"#{")
        brackets = 1
        while True:
            ch = self.getc()
            if ch in (EOF, _NEWLINE):
                raise self.error("unterminated format")
            if ch == ord("#"):
                ch = self.getc()
                if ch in (EOF, _NEWLINE):
                    raise self.error("unterminated format")
                if ch == ord("{"):
                    brackets += 1
                buf.append(ord("#"))
            elif ch == ord("}"):
                brackets -= 1
                if brackets == 0:
                    buf.append(ch)
                    return _decode(buf)
            buf.append(ch)

    def escape(self, buf: bytearray) -> None:
        ch = self.getc()
        if ord("4") <= ch <= ord("7"):
            raise self.error("invalid octal escape")
        if ord("0") <= ch <= ord("3"):
            second = self.getc()
            if ord("0") <= second <= ord("7"):
                third = self.getc()
                if ord("0") <= third <= ord("7"):
                    buf.append(64 * (ch - 48) + 8 * (second - 48) + (third - 48))
                    return
            raise self.error("invalid octal escape")
        if ch == EOF:
            raise self.error("syntax error")
        if ch in (ord("u"), ord("U")):
            size = 4 if ch == ord("u") else 8
            digits = bytearray()
            for _ in range(size):
                one = self.getc()
                if one in (EOF, _NEWLINE):
                    raise self.error("syntax error")
                if one not in b"0123456789abcdefABCDEF":
                    raise self.error("invalid \\%c argument" % (ch,))
                digits.append(one)
            try:
                buf.extend(chr(int(digits, 16)).encode("utf-8"))
            except ValueError, UnicodeEncodeError:
                raise self.error("invalid \\%c argument" % (ch,)) from None
            return
        buf.append(_ESCAPES.get(ch, ch))

    def variable(self, buf: bytearray) -> None:
        ch = self.getc()
        if ch == EOF:
            raise self.error("syntax error")
        brackets = ch == ord("{")
        name = bytearray()
        if not brackets:
            if not _is_var(ch, True):
                buf.append(ord("$"))
                self.ungetc(ch)
                return
            name.append(ch)
        while True:
            ch = self.getc()
            if brackets and ch == ord("}"):
                break
            if ch == EOF or not _is_var(ch, False):
                if not brackets:
                    self.ungetc(ch)
                    break
                raise self.error("invalid environment variable")
            if len(name) == 1022:
                raise self.error("environment variable is too long")
            name.append(ch)
        value = self.environment.get(_decode(name))
        if value is not None:
            buf.extend(value.encode("utf-8", "surrogateescape"))

    def tilde(self, buf: bytearray) -> None:
        name = bytearray()
        while True:
            ch = self.getc()
            if ch == EOF or ch in b"/ \t\n\"'":
                self.ungetc(ch)
                break
            if len(name) == 1022:
                raise self.error("user name is too long")
            name.append(ch)
        home = None
        if not name:
            home = self.environment.get("HOME") or None
            if home is None:
                home = _home_of(None)
        else:
            home = _home_of(_decode(name))
        if home is None:
            raise self.error("syntax error")
        buf.extend(home.encode("utf-8", "surrogateescape"))

    def token(self, ch: int) -> bytes:
        none, double, single, start = 0, 1, 2, 3
        state, last = none, start
        buf = bytearray()
        opened = self.line
        while True:
            if ch == EOF:
                break
            if state == none and ch == ord("\r"):
                ch = self.getc()
                if ch != _NEWLINE:
                    self.ungetc(ch)
                    ch = ord("\r")
            if ch == _NEWLINE:
                if state == none:
                    break
                self.line += 1
            if state == none and ch in b" \t;}":
                break

            if ch == _NEWLINE and state != none:
                # Inside quotes a newline stays, and the indent and a
                # comment line after it go.
                buf.append(_NEWLINE)
                ch = self.getc()
                while ch != EOF and ch in b" \t":
                    ch = self.getc()
                if ch != ord("#"):
                    continue
                ch = self.getc()
                if ch != EOF and ch in b",#{}:":
                    self.ungetc(ch)
                    ch = ord("#")
                else:
                    while ch not in (_NEWLINE, EOF):
                        ch = self.getc()
                continue

            if ch == _BACKSLASH and state != single:
                self.escape(buf)
                last = state
                ch = self.getc()
                continue
            if ch == ord("~") and last != state and state != single:
                self.tilde(buf)
                last = state
                ch = self.getc()
                continue
            if ch == ord("$") and state != single:
                self.variable(buf)
                last = state
                ch = self.getc()
                continue

            if ch == ord("'") and state in (none, single):
                state = single if state == none else none
                ch = self.getc()
                continue
            if ch == ord('"') and state in (none, double):
                state = double if state == none else none
                ch = self.getc()
                continue

            buf.append(ch)
            last = state
            ch = self.getc()
        self.ungetc(ch)
        if state != none:
            # tmux takes the rest of the file into the word instead.
            raise ParseError(opened, "unclosed quote")
        return bytes(buf)


_ESCAPES = {
    ord("a"): 0x07,
    ord("b"): 0x08,
    ord("e"): 0x1B,
    ord("f"): 0x0C,
    ord("s"): 0x20,
    ord("v"): 0x0B,
    ord("r"): 0x0D,
    ord("n"): 0x0A,
    ord("t"): 0x09,
}


def _home_of(user: str | None) -> str | None:
    try:
        entry = pwd.getpwuid(os.getuid()) if user is None else pwd.getpwnam(user)
    except KeyError:
        return None
    return entry.pw_dir


class _Parser:
    """
    The rules of `cmd-parse.y`, by recursive descent.

    `scopes` is tmux's scope stack: one flag per open `%if`, the
    innermost last. A command is kept when the innermost is true; an
    assignment runs when all are.
    """

    def __init__(
        self,
        lexer: _Lexer,
        condition: Callable[[str], bool] | None,
        assign: Callable[[str, str, bool], None] | None,
    ) -> None:
        self.lexer = lexer
        self.condition = condition
        self.assign = assign
        self.scopes: list[bool] = []
        self.ahead: _Token | None = None

    def peek(self) -> _Token:
        if self.ahead is None:
            self.ahead = self.lexer.next()
        return self.ahead

    def take(self) -> _Token:
        token = self.peek()
        self.ahead = None
        return token

    def expect(self, kind: _Kind) -> _Token:
        token = self.take()
        if token.kind is not kind:
            raise ParseError(token.line, "syntax error")
        return token

    def keeps(self) -> bool:
        return not self.scopes or self.scopes[-1]

    def lines(self) -> list[Command]:
        commands = self.statements({_Kind.END})
        self.expect(_Kind.END)
        return commands

    def statements(self, stop: set[_Kind]) -> list[Command]:
        "`statement '\\n'`, until a token in `stop`."
        commands: list[Command] = []
        while self.peek().kind not in stop:
            if self.peek().kind is _Kind.END:
                raise ParseError(self.peek().line, "syntax error")
            commands += self.statement()
            self.expect(_Kind.NEWLINE)
        return commands

    def statement(self) -> list[Command]:
        kind = self.peek().kind
        if kind in (_Kind.NEWLINE, _Kind.CLOSE):
            return []
        if kind is _Kind.HIDDEN:
            self.take()
            self.assignment(self.expect(_Kind.EQUALS), hidden=True)
            return []
        if kind is _Kind.IF:
            flag = self.if_open()
            if self.peek().kind is _Kind.NEWLINE:
                chosen = self.condition_lines(flag)
                return chosen if self.keeps() else []
            commands = self.commands(opened=flag)
        elif kind in (_Kind.TOKEN, _Kind.EQUALS):
            commands = self.commands()
        else:
            raise ParseError(self.peek().line, "syntax error")
        return commands if self.keeps() else []

    def if_open(self) -> bool:
        self.expect(_Kind.IF)
        flag = self.expanded()
        self.scopes.append(flag)
        return flag

    def expanded(self) -> bool:
        token = self.take()
        if token.kind not in (_Kind.FORMAT, _Kind.TOKEN):
            raise ParseError(token.line, "syntax error")
        if self.condition is None:
            raise ParseError(token.line, "%if needs a server to expand its format")
        return self.condition(token.text)

    def condition_lines(self, flag: bool) -> list[Command]:
        "The `%if` that spans lines: `condition` and `elif`."
        stop = {_Kind.ELIF, _Kind.ELSE, _Kind.ENDIF}
        self.expect(_Kind.NEWLINE)
        body = self.statements(stop)
        chosen: list[Command] = body if flag else []
        taken = flag
        while self.peek().kind is _Kind.ELIF:
            self.take()
            branch = self.expanded()
            self.scopes[-1] = branch
            self.expect(_Kind.NEWLINE)
            body = self.statements(stop)
            if branch and not taken:
                chosen, taken = body, True
        if self.peek().kind is _Kind.ELSE:
            self.take()
            self.scopes[-1] = not self.scopes[-1]
            self.expect(_Kind.NEWLINE)
            body = self.statements({_Kind.ENDIF})
            if not taken:
                chosen = body
        self.expect(_Kind.ENDIF)
        self.scopes.pop()
        return chosen

    def condition_inline(self, flag: bool) -> list[Command]:
        "The `%if` inside one line: `condition1` and `elif1`."
        body = self.commands()
        chosen: list[Command] = body if flag else []
        taken = flag
        while self.peek().kind is _Kind.ELIF:
            self.take()
            branch = self.expanded()
            self.scopes[-1] = branch
            body = self.commands()
            if branch and not taken:
                chosen, taken = body, True
        if self.peek().kind is _Kind.ELSE:
            self.take()
            self.scopes[-1] = not self.scopes[-1]
            body = self.commands()
            if not taken:
                chosen = body
        self.expect(_Kind.ENDIF)
        self.scopes.pop()
        return chosen

    def commands(self, opened: bool | None = None) -> list[Command]:
        """
        Commands separated by `;`.

        tmux drops what came before when a later command is empty or
        out of scope (`commands ';' command`); kept, so that a file
        means here what it means there.
        """
        commands = self.condition_inline(opened) if opened is not None else self.first_command()
        while self.peek().kind is _Kind.SEMICOLON:
            self.take()
            kind = self.peek().kind
            if kind is _Kind.IF:
                commands += self.condition_inline(self.if_open())
            elif kind in (_Kind.TOKEN, _Kind.EQUALS):
                command = self.command()
                if command.words and self.keeps():
                    commands.append(command)
                else:
                    commands = list[Command]()
        return commands

    def first_command(self) -> list[Command]:
        if self.peek().kind is _Kind.IF:
            return self.condition_inline(self.if_open())
        command = self.command()
        return [command] if command.words and self.keeps() else []

    def command(self) -> Command:
        if self.peek().kind is _Kind.EQUALS:
            self.assignment(self.take(), hidden=False)
            if self.peek().kind is not _Kind.TOKEN:
                return Command(self.peek().line)
        first = self.expect(_Kind.TOKEN)
        command = Command(first.line, [first.text])
        while True:
            kind = self.peek().kind
            if kind in (_Kind.TOKEN, _Kind.EQUALS):
                command.words.append(self.take().text)
            elif kind is _Kind.OPEN:
                self.take()
                command.words.append(Block(self.block()))
            else:
                return command

    def block(self) -> list[Command]:
        "`argument_statements`: statements whose last ends at `}`."
        commands: list[Command] = []
        while True:
            commands += self.statement()
            token = self.take()
            if token.kind is _Kind.CLOSE:
                return commands
            if token.kind is not _Kind.NEWLINE:
                raise ParseError(token.line, "syntax error")

    def assignment(self, token: _Token, hidden: bool) -> None:
        if len(token.text) > _MAX_ENVIRON:
            raise ParseError(token.line, "environment variable is too long")
        if all(self.scopes) and self.assign is not None:
            name, _, value = token.text.partition("=")
            self.assign(name, value, hidden)


def parse(
    text: str,
    *,
    environment: Mapping[str, str] | None = None,
    condition: Callable[[str], bool] | None = None,
    assign: Callable[[str, str, bool], None] | None = None,
    line: int = 1,
) -> list[Command]:
    """
    The commands of `text`, in order.

    `environment` answers `$NAME` and a bare `~`. `condition` judges the
    expanded format of a `%if`; without one a `%if` is an error.
    `assign` takes each `NAME=value`, and `hidden` for `%hidden`.
    An assignment changes `environment` too, so a later `$NAME` sees it.
    """
    variables = dict(os.environ if environment is None else environment)

    def assigned(name: str, value: str, hidden: bool) -> None:
        variables[name] = value
        if assign is not None:
            assign(name, value, hidden)

    return _Parser(_Lexer(text, variables, line), condition, assigned).lines()


def words_of(text: str) -> list[str]:
    "The words of the first command of `text`, as a client reads its own command line."
    commands = parse(text)
    return commands[0].strings() if commands else []


def from_arguments(words: Sequence[str]) -> list[list[str]]:
    """
    Commands from words that are already split, as tmux reads its own
    command line and the arguments of bind-key: a word that ends in `;`
    ends a command, and one that ends in `\\;` ends in a plain `;`.
    """
    commands: list[list[str]] = [[]]
    for word in words:
        end = False
        if word.endswith(";"):
            word = word[:-1]
            if word.endswith("\\"):
                word = word[:-1] + ";"
            else:
                end = True
        if not end or word:
            commands[-1].append(word)
        if end:
            commands.append([])
    return [command for command in commands if command]


def template_replace(template: str, answer: str, index: int = 1) -> str:
    """
    `template` with `answer` in place of its first `%%` and each `%1`,
    tmux's `cmd_template_replace`.

    `%%` is meant to sit in single quotes, so a `'` in the answer is
    escaped for them; `%%%` is meant for double quotes. Unquoted, an
    answer is command text as typed.
    """
    out: list[str] = []
    replaced = False
    i = 0
    while i < len(template):
        ch = template[i]
        i += 1
        if ch != "%":
            out.append(ch)
            continue
        following = template[i : i + 1]
        if following in "123456789" and following and int(following) == index:
            i += 1
            quoting = ""
        elif following == "%" and not replaced:
            replaced = True
            i += 1
            quoting = "'"
        else:
            out.append(ch)
            continue
        if template[i : i + 1] == "%":
            quoting = '"'
            i += 1
        for one in answer:
            if quoting == "'" and one == "'":
                out.append("'\\''")
                continue
            if quoting == '"' and one in '"\\$;~':
                out.append("\\")
            out.append(one)
    return "".join(out)


def to_arguments(commands: Sequence[Sequence[str]]) -> list[str]:
    "Words that `from_arguments` reads back as `commands`."
    words: list[str] = []
    for index, command in enumerate(commands):
        if index:
            words.append(";")
        words += [word[:-1] + "\\;" if word.endswith(";") else word for word in command]
    return words


def print_arguments(words: Sequence[str]) -> str:
    "Words split as `from_arguments` splits them, as tmux's list-keys prints a binding."
    return " \\; ".join(
        " ".join(quote(word, first=index == 0) for index, word in enumerate(command))
        for command in from_arguments(words)
    )


_SPECIAL = frozenset(" \t\n\r\"'\\$#;{}~")


def quote(word: str, *, first: bool = False) -> str:
    """
    `word` as command text that reads back as that one word.

    `first` marks the word that names the command: there `NAME=value`
    would read as an assignment.
    """
    if word == "":
        return "''"
    special = any(ch in _SPECIAL or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in word)
    special = special or (word[0] == "%" and bool(word.strip("%0123456789")))
    if first and "=" in word:
        special = True
    if not special:
        return word
    if len(word) == 1 and word not in " \t\n\r" and 0x20 < ord(word) < 0x7F:
        return "\\" + word
    out = ['"']
    for ch in word:
        if ch in '"\\$~':
            out.append("\\" + ch)
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif ch == "\r":
            out.append("\\r")
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append("\\%03o" % ord(ch))
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def join(words: Sequence[str]) -> str:
    "Command text for words split as tmux's command line splits them (`from_arguments`)."
    return " ; ".join(
        " ".join(quote(word, first=index == 0) for index, word in enumerate(command))
        for command in from_arguments(words)
    )


def print_commands(commands: Sequence[Command]) -> str:
    "Commands as text that parses back to them."
    return " ; ".join(
        " ".join(
            "{ %s }" % (word,) if isinstance(word, Block) else quote(word, first=index == 0)
            for index, word in enumerate(command.words)
        )
        for command in commands
    )
