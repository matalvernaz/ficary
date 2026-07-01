"""Reader text-control theming: font size + light/dark/high-contrast palettes.

Applied only to the chapter text control (a low-vision affordance). Palettes
carry a highlight colour pair for the Phase 2 live-TTS follow highlight, so it
stays legible in every theme. Persisted via the ``reader_*`` prefs keys.
"""
from __future__ import annotations

THEMES = ("light", "dark", "high_contrast")
DEFAULT_THEME = "light"
DEFAULT_FONT_PT = 14
MIN_FONT_PT = 8
MAX_FONT_PT = 48

# fg / bg text colours, plus the live-TTS highlight fg/bg, as hex strings.
_PALETTES = {
    "light":         {"fg": "#101010", "bg": "#FFFFFF", "hl_fg": "#000000", "hl_bg": "#FFE24D"},
    "dark":          {"fg": "#DCDCDC", "bg": "#101014", "hl_fg": "#000000", "hl_bg": "#F0C000"},
    "high_contrast": {"fg": "#FFFFFF", "bg": "#000000", "hl_fg": "#000000", "hl_bg": "#FFFF00"},
}


def palette(name: str) -> dict:
    return _PALETTES.get(name, _PALETTES[DEFAULT_THEME])


def next_theme(name: str) -> str:
    """The next theme in the cycle, for a 'toggle theme' shortcut."""
    try:
        return THEMES[(THEMES.index(name) + 1) % len(THEMES)]
    except ValueError:
        return DEFAULT_THEME


def clamp_font_pt(pt) -> int:
    try:
        pt = int(pt)
    except (TypeError, ValueError):
        return DEFAULT_FONT_PT
    return max(MIN_FONT_PT, min(MAX_FONT_PT, pt))


def apply_to_textctrl(ctrl, theme_name: str, font_pt) -> None:
    """Apply a palette + font size to a wx.TextCtrl. Imports wx lazily so
    this module stays importable in headless tests."""
    import wx

    pal = palette(theme_name)
    fg = wx.Colour(pal["fg"])
    bg = wx.Colour(pal["bg"])
    ctrl.SetBackgroundColour(bg)
    ctrl.SetForegroundColour(fg)
    font = ctrl.GetFont()
    font.SetPointSize(clamp_font_pt(font_pt))
    ctrl.SetFont(font)
    # Reapply a base style across all existing text so a TE_RICH2 control
    # doesn't keep stale colours from the previous theme.
    attr = wx.TextAttr(fg, bg)
    attr.SetFont(font)
    ctrl.SetStyle(0, ctrl.GetLastPosition(), attr)
    ctrl.Refresh()
