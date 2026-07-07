"""Presentation helpers shared by the Tk UI: per-platform fonts + tooltip.

No App state, no networking — safe to import anywhere. Split out of ui.py to
keep that file within the size budget.
"""
import sys
import tkinter as tk
from tkinter import font as tkfont
from typing import Optional


def ui_family() -> str:
    if sys.platform.startswith('win'):
        return 'Segoe UI'
    if sys.platform.startswith('darwin'):
        return 'Helvetica'
    return 'DejaVu Sans'


def mono_family() -> str:
    if sys.platform.startswith('win'):
        return 'Consolas'
    if sys.platform.startswith('darwin'):
        return 'Menlo'
    return 'DejaVu Sans Mono'


def font(size: int, weight: Optional[str] = None, mono: bool = False) -> tuple:
    # Tk parses tuple-form font=('TkDefaultFont', N) as family='TkDefaultFont',
    # not as the named font. On Windows that family lookup fails and Tk falls
    # back to Times Roman 10pt. Use an explicit per-platform family instead.
    family = mono_family() if mono else ui_family()
    if weight:
        return (family, size, weight)
    return (family, size)


def apply_named_font_defaults() -> None:
    # Belt-and-suspenders: retarget Tk's named fonts too so any widget that
    # resolves them (menus, message boxes, ttk theme defaults) picks up the
    # same family. Must be called after tk.Tk() — fonts don't exist before.
    ui = ui_family()
    mono = mono_family()
    for name in (
        'TkDefaultFont', 'TkTextFont', 'TkMenuFont', 'TkHeadingFont',
        'TkCaptionFont', 'TkSmallCaptionFont', 'TkIconFont', 'TkTooltipFont',
    ):
        try:
            tkfont.nametofont(name).configure(family=ui)
        except tk.TclError:
            pass
    try:
        tkfont.nametofont('TkFixedFont').configure(family=mono)
    except tk.TclError:
        pass


class Tooltip:
    """Tiny stdlib-only tooltip. Shows a borderless Toplevel on hover after a
    short delay; hides it on leave or click. Wraps long text."""

    DELAY_MS = 450
    BG = '#2D2D2D'
    FG = '#FFFFFF'

    def __init__(self, widget: tk.Widget, text: str, wraplength: int = 280):
        self.widget = widget
        self.text = text
        self.wraplength = wraplength
        self._after_id: Optional[str] = None
        self._tip: Optional[tk.Toplevel] = None
        widget.bind('<Enter>', self._schedule, add='+')
        widget.bind('<Leave>', self._cancel, add='+')
        widget.bind('<ButtonPress>', self._cancel, add='+')

    def _schedule(self, _evt=None) -> None:
        self._cancel()
        self._after_id = self.widget.after(self.DELAY_MS, self._show)

    def _cancel(self, _evt=None) -> None:
        if self._after_id is not None:
            self.widget.after_cancel(self._after_id)
            self._after_id = None
        if self._tip is not None:
            try:
                self._tip.destroy()
            except tk.TclError:
                pass
            self._tip = None

    def _show(self) -> None:
        if self._tip is not None:
            return
        x = self.widget.winfo_rootx() + 12
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        tip = tk.Toplevel(self.widget)
        tip.overrideredirect(True)
        # macOS-only: keep the tooltip on top of the Resolve window.
        try:
            tip.attributes('-topmost', True)
        except tk.TclError:
            pass
        tk.Label(
            tip, text=self.text,
            bg=self.BG, fg=self.FG,
            font=font(11),
            wraplength=self.wraplength,
            justify='left', padx=10, pady=6,
            bd=0, highlightthickness=0,
        ).pack()
        tip.geometry(f'+{x}+{y}')
        self._tip = tip


def attach_tooltip(widget: tk.Widget, text: str) -> None:
    """Convenience: ttk widgets like ttk.Combobox/ttk.Button accept the same
    bindings as plain Tk widgets. Cursor hint goes hand-in-hand."""
    Tooltip(widget, text)
    try:
        widget.configure(cursor='hand2')
    except tk.TclError:
        pass
