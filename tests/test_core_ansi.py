"""readaloud.ansi, now in Rust, against the Python it replaced
(tests/reference/ansi.py): escape soup for the parser, random parameter
bodies for SGR, every colour for the quantiser, and random Markdown lines in
random styles for strip_markdown."""

from __future__ import annotations

import random
from dataclasses import astuple

from readaloud import ansi
from reference import ansi as reference

ESC = "\x1b"
SOUP = [
    "a", "word", " ", "\t", "\n", "\r", "\r\n", "é", "日本", "✓", "\x00",
    "\x07", "\x7f", "\x85", "\x9b", " ", ESC, ESC + "[", ESC + "[0m",
    ESC + "[m", ESC + "[1m", ESC + "[1;3;4m", ESC + "[22m", ESC + "[;1m",
    ESC + "[31m", ESC + "[97m", ESC + "[38;5;196m", ESC + "[48;5;300m",
    ESC + "[38;2;10;200;30m", ESC + "[38;2;1m", ESC + "[38:2::255:0:0m",
    ESC + "[38:5:21m", ESC + "[58:5:1m", ESC + "[38;7;1m", ESC + "[9m",
    ESC + "[?25l", ESC + "[?1m", ESC + "[2J", ESC + "[1 q", ESC + "]8;;http://x" + ESC + "\\",
    ESC + "]8;id=1;https://y\x07", ESC + "]8;;" + ESC + "\\", ESC + "]8;; \x07",
    ESC + "]11;?\x07", ESC + "]8;;open", ESC + "P1$r" + ESC + "\\", ESC + "_x",
    ESC + "(B", ESC + "(", ESC + "7", ESC + "c", ESC + "[999999999999999999999m",
    ESC + "[38;5;99999999999999999999999m",
]


def runs(lines) -> list[list[tuple[str, tuple]]]:
    return [[(r.text, astuple(r.style)) for r in line] for line in lines]


def test_escape_soup_parses_as_before():
    rng = random.Random(8)
    for _ in range(20000):
        data = "".join(rng.choice(SOUP) for _ in range(rng.randint(0, 20)))
        assert runs(ansi.parse(data)) == runs(reference.parse(data)), data
        assert ansi.has_ansi(data) == reference.has_ansi(data), data


def test_every_character_after_an_escape():
    for cp in range(0x3000):
        data = "x" + ESC + chr(cp) + "y" + ESC + "[" + chr(cp) + "z"
        assert runs(ansi.parse(data)) == runs(reference.parse(data)), hex(cp)
        assert ansi.has_ansi(data) == reference.has_ansi(data), hex(cp)


def test_sgr_bodies_apply_as_before():
    rng = random.Random(9)
    fields = ["", "0", "1", "2", "3", "4", "7", "21", "22", "23", "24", "27",
              "30", "37", "39", "40", "49", "90", "107", "38", "48", "5",
              "2", "255", "256", "1000", "38:5:9", "48:2::1:2:3", "38:2:4:5:6",
              "38:2:1", "58:2::1:2:3", "38:", ":", "x"]
    start = reference.Style(fg=3, href="h")
    for _ in range(20000):
        body = ";".join(rng.choice(fields) for _ in range(rng.randint(0, 7)))
        want = astuple(reference.apply_sgr(start, body))
        assert astuple(ansi.apply_sgr(ansi.Style(fg=3, href="h"), body)) == want, body


def test_every_colour_quantises_as_before():
    values = list(range(-3, 260, 1))
    rng = random.Random(10)
    for _ in range(60000):
        r, g, b = (rng.choice(values) for _ in range(3))
        assert ansi.rgb_to_x256(r, g, b) == reference.rgb_to_x256(r, g, b), (
            r, g, b)
    for v in range(256):                      # the whole grey diagonal
        assert ansi.rgb_to_x256(v, v, v) == reference.rgb_to_x256(v, v, v)


MD = [
    "a", "word", "snake_case", "a*b", " ", "  ", "\t", "#", "##", "#######",
    "*", "**", "***", "_", "__", "~~", "`", "``", "```", "~~~", "\\", "\\*",
    "\\_", "[", "]", "(", ")", "!", "<", ">", "-", "+", "=", "===", "|", ":",
    "'", '"', "x", "1", "é", "https://ex.org/a_b*c", "www.x.y", "mailto:a@b",
    "data:x", "<https://a.b>", "<ftp:x y>", "[t](u)", "[t](<u>)", '[t](u "T")',
    "[t](u 'T')", "![alt](i.png)", "[t][1]", "[1]: https://x", "> ", "- ",
    "* ", "| --- |", ":--", "---", "* * *", "\x00",
]


STYLES = [{}, {"bold": True}, {"fg": 2, "href": "h"}]


def random_doc(rng: random.Random) -> list[list[tuple[str, int]]]:
    """Random lines of Markdown, as runs of (text, index into STYLES)."""
    doc = []
    for _ in range(rng.randint(0, 6)):
        text = "".join(rng.choice(MD) for _ in range(rng.randint(0, 12)))
        line, i = [], 0
        while i < len(text) or (not line and rng.random() < 0.1):
            n = rng.randint(0, 6)
            line.append((text[i:i + n], rng.randrange(len(STYLES))))
            i += n
        doc.append(line)
    return doc


def build(kind, doc):
    return [[kind.Run(text, kind.Style(**STYLES[k])) for text, k in line]
            for line in doc]


def test_markdown_is_stripped_as_before():
    rng = random.Random(11)
    for _ in range(8000):
        doc = random_doc(rng)
        assert runs(ansi.strip_markdown(build(ansi, doc))) == runs(
            reference.strip_markdown(build(reference, doc))), doc
