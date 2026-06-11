"""Rox-derived visual language for the terminal surfaces.

Extracted from the company site capture (screencapture-rox-*.png): near-black
surfaces with hairline borders, two-tone headlines (white lead, grey tail),
muted status pills (red / amber / indigo), one amber accent, rounded panels,
small dim captions. Serif display type has no terminal equivalent; the
two-tone headline pattern carries the brand instead.
"""

from rich import box
from rich.console import Console
from rich.text import Text
from rich.theme import Theme

# Palette (truecolor; Rich degrades gracefully on 256-color terminals)
INK_RED = "#e5484d"       # Rox status-pill red ("Engaging")
INK_AMBER = "#e8a33d"     # the single amber ruler-tick accent
INK_INDIGO = "#9b9ef0"    # status-pill indigo ("Replying")
INK_GREEN = "#5bb98b"     # pastel green ("Customer-facing" blocks)
GREY_TAIL = "grey58"      # headline tails, captions
GREY_LINE = "grey27"      # hairline panel borders

PANEL_BOX = box.ROUNDED   # Rox radius everywhere; nothing heavy

ROX_THEME = Theme({
    "accent": INK_AMBER,
    "border.subtle": GREY_LINE,
    "caption": f"{GREY_TAIL} italic",
    "head.lead": "bold bright_white",
    "head.tail": GREY_TAIL,
    "stat.label": "grey58",
    "stat.value": "bold bright_white",
    "key": f"bold {INK_AMBER}",
    "ok": INK_GREEN,
})


def make_console() -> Console:
    return Console(theme=ROX_THEME)


def two_tone(lead: str, tail: str) -> Text:
    """The Rox headline pattern: white lead phrase, grey tail."""
    text = Text()
    text.append(lead, style="head.lead")
    if tail:
        text.append(f" {tail}", style="head.tail")
    return text
