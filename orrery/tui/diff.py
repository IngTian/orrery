"""A proposal's diff lines and the chat bar under them, and para, the hanging-indent wrap the
panels share.

Pure rendering: no reads and no widgets. Textual Content in theme tokens, every data-derived
string a plain segment, never parsed as markup.
"""

from textual.content import Content

from orrery.proposals import DiffLine

# Muted, in a Content span. Not $text-muted: that is "auto 60%", and Textual never blends a
# span's auto colour with the background, so it comes out pure white or black.
MUTED = "$foreground-muted"
INDENT = Content("  ")


def para(first: Content, rest: Content, text: Content, width: int) -> list[Content]:
    """text wrapped to what first leaves of width, after first on its first line and rest
    (as wide) on the others: nothing of it is cut. A line the wrap starts has its leading
    spaces trimmed (a folded word that fills its line leaves the next one's); one after a
    newline keeps its own."""
    w, lines = max(1, width - first.cell_length), []
    for p in text.split("\n", allow_blank=True):
        for i, ln in enumerate(p.wrap(w)):
            lines.append(ln[len(ln.plain) - len(ln.plain.lstrip(" ")) :] if i else ln)
    return [Content.assemble(rest if i else first, line) for i, line in enumerate(lines)]


def render_diff(lines: list[DiffLine], width: int) -> Content:
    """The header line in bold, then `−` lines muted and struck, `+` lines in the accent with
    their struck phrases struck, and the rest muted, each wrapped to width under its text, two
    columns in. Theme tokens only, the text as plain segments."""
    out = []
    for i, line in enumerate(lines):
        if i == 0 and line.sign == " ":
            out.append(Content.styled(line.text, "bold"))
        elif line.sign == "-":
            old = Content.styled(line.text, f"{MUTED} strike")
            out += para(Content.styled("− ", MUTED), INDENT, old, width)
        elif line.sign == "+":
            new = Content(line.text)
            for start, end in line.struck:
                new = new.stylize("strike", start, end)
            out += [r.stylize_before("$accent") for r in para(Content("+ "), INDENT, new, width)]
        else:
            said = para(INDENT, INDENT, Content(line.text), width)
            out += [r.stylize_before(MUTED) for r in said]
    return Content("\n").join(out)


def chat_bar(text: Content, width: int) -> Content:
    """text on the chat bar, the panel's background running across width on every line."""
    rows = text.wrap(width) if width > 0 else [text]
    out = Content("\n").join(row.pad_right(max(0, width - row.cell_length)) for row in rows)
    return out.stylize_before("$foreground on $panel")
