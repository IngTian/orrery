"""A proposal's diff lines and the chat bar under them.

Pure rendering: no reads and no widgets. Textual Content in theme tokens, every data-derived
string a plain segment, never parsed as markup.
"""

from textual.content import Content

from orrery.proposals import DiffLine

# Muted, in a Content span. Not $text-muted: that is "auto 60%", and Textual never blends a
# span's auto colour with the background, so it comes out pure white or black.
MUTED = "$foreground-muted"


def render_diff(lines: list[DiffLine]) -> Content:
    """The header line in bold, then `−` lines muted and struck, `+` lines in the accent with
    their struck phrases struck, and the rest muted. Theme tokens only, the text as plain
    segments."""
    out = []
    for i, line in enumerate(lines):
        if i == 0 and line.sign == " ":
            out.append(Content.styled(line.text, "bold"))
        elif line.sign == "-":
            out.append(Content.assemble(("− ", MUTED), (line.text, f"{MUTED} strike")))
        elif line.sign == "+":
            row = Content.styled(f"+ {line.text}", "$accent")
            for start, end in line.struck:
                row = row.stylize("strike", start + 2, end + 2)  # past the sign and its space
            out.append(row)
        else:
            out.append(Content.styled(f"  {line.text}", MUTED))
    return Content("\n").join(out)


def chat_bar(text: Content, width: int) -> Content:
    """text on the chat bar, the panel's background running across width on every line."""
    rows = text.wrap(width) if width > 0 else [text]
    out = Content("\n").join(row.pad_right(max(0, width - row.cell_length)) for row in rows)
    return out.stylize_before("$foreground on $panel")
