"""Python traceback parsing (pure, total: any text in, a result or None out).

Handles what CPython 3.8-3.13 prints: plain and chained tracebacks (the *last*
exception is the one that was raised), ``SyntaxError`` frames without a
function, ``[Previous line repeated N more times]``, ``^^^`` markers, notes,
multi-line messages, a traceback that starts mid-line ("ERROR boom\\nTraceback
..."), and exception groups: the top-level group is the one that was raised,
its sub-exceptions (drawn further right in the tree) are ignored.
"""

import re
from dataclasses import dataclass

MAX_INPUT_CHARS = 64_000
MAX_FRAMES = 200

_HEADER = re.compile(r"(?:Exception Group )?Traceback \(most recent call last\):\s*$")
_FRAME = re.compile(r'^\s*File "(?P<file>[^"]+)", line (?P<line>\d+)(?:, in (?P<func>.+?))?\s*$')
_EXC = re.compile(r"^(?P<type>[A-Za-z_][\w.]*)(?::(?: (?P<msg>.*))?)?$")
_CHAIN = (
    "During handling of the above exception, another exception occurred:",
    "The above exception was the direct cause of the following exception:",
)
_TREE = frozenset(" |+-")


@dataclass(frozen=True)
class Frame:
    file: str
    line: int
    function: str  # "" when CPython didn't print one (SyntaxError)
    source: str | None = None


@dataclass(frozen=True)
class ParsedTraceback:
    frames: tuple[Frame, ...]
    exc_type: str | None
    message: str
    chained: int  # earlier exceptions in the chain (0 = none)


@dataclass(frozen=True)
class _Header:
    index: int
    column: int | None  # where the group tree's "+"/"|" is; None for a plain traceback


def _headers(lines: list[str]) -> list[_Header]:
    found = []
    for i, raw in enumerate(lines):
        match = _HEADER.search(raw)
        if match is None:
            continue
        prefix = raw[: match.start()]
        stripped = prefix.strip()
        if stripped and set(prefix) <= _TREE:
            found.append(_Header(i, len(prefix) - len(prefix.lstrip())))
        else:
            found.append(_Header(i, None))
    return found


def _own(raw: str, column: int | None) -> str | None:
    """The line without the group's tree drawing; None once the tree goes elsewhere."""
    if column is None:
        return raw
    if raw[:column].strip() or len(raw) <= column or raw[column] != "|":
        return None
    return raw[column + 2 :] if raw[column + 1 : column + 2] == " " else raw[column + 1 :]


def parse(text: str) -> ParsedTraceback | None:
    """The raised exception's traceback in ``text``, or None if there isn't one."""
    if "Traceback (most recent call last)" not in text:
        return None
    lines = text[:MAX_INPUT_CHARS].splitlines()
    headers = _headers(lines)
    if not headers:
        return None
    # The outermost tree level is the raised exception (a plain traceback is outermost).
    level = min(-1 if h.column is None else h.column for h in headers)
    ours = [h for h in headers if (-1 if h.column is None else h.column) == level]
    header = ours[-1]
    frames: list[Frame] = []
    exc_line: str | None = None
    message: list[str] = []
    for i in range(header.index + 1, len(lines)):
        line = _own(lines[i], header.column)
        if line is None:
            if exc_line is not None or (header.column is not None and lines[i].strip()):
                break
            continue
        if exc_line is None:
            if frame := _FRAME.match(line):
                if len(frames) < MAX_FRAMES:
                    nxt = _own(lines[i + 1], header.column) if i + 1 < len(lines) else None
                    source = (
                        nxt.strip()
                        if nxt and nxt.startswith("    ") and not _FRAME.match(nxt)
                        else None
                    )
                    frames.append(
                        Frame(frame["file"], int(frame["line"]), (frame["func"] or "").strip(),
                              source or None)
                    )  # fmt: skip
                continue
            stripped = line.strip()
            if not stripped or line.startswith(" ") or stripped.startswith("[Previous line"):
                continue  # source lines, ^^^ markers, repeats
            if _EXC.match(stripped):
                exc_line = stripped
            continue
        stripped = line.strip()
        if not stripped or stripped in _CHAIN:
            break
        message.append(line.rstrip())

    chained = len(ours) - 1
    if exc_line is None:
        return ParsedTraceback(tuple(frames), None, "", chained)
    match = _EXC.match(exc_line)
    if match is None:  # can't happen: exc_line was chosen because it matched
        return ParsedTraceback(tuple(frames), None, "", chained)
    text_message = "\n".join([match["msg"] or "", *message]).strip()
    return ParsedTraceback(tuple(frames), match["type"], text_message, chained)
