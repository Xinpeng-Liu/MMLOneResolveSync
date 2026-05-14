"""Tkinter UI loaded by MMLOneSync.py inside Resolve.

Threading model: HTTP and media download run on a worker thread; the UI thread
only updates labels via root.after(...). No mutable state shared without queues.
"""
import os
import queue
import sys
import threading
import tkinter as tk
import urllib.request
import webbrowser
from datetime import datetime
from pathlib import Path
from tkinter import font as tkfont, messagebox, ttk
from typing import Any, Callable, Dict, List, Optional

from . import api, config, diff, resolve_apply, storage


def _ui_family() -> str:
    if sys.platform.startswith('win'):
        return 'Segoe UI'
    if sys.platform.startswith('darwin'):
        return 'Helvetica'
    return 'DejaVu Sans'


def _mono_family() -> str:
    if sys.platform.startswith('win'):
        return 'Consolas'
    if sys.platform.startswith('darwin'):
        return 'Menlo'
    return 'DejaVu Sans Mono'


def _font(size: int, weight: Optional[str] = None, mono: bool = False) -> tuple:
    # Tk parses tuple-form font=('TkDefaultFont', N) as family='TkDefaultFont',
    # not as the named font. On Windows that family lookup fails and Tk falls
    # back to Times Roman 10pt. Use an explicit per-platform family instead.
    family = _mono_family() if mono else _ui_family()
    if weight:
        return (family, size, weight)
    return (family, size)


def _apply_named_font_defaults() -> None:
    # Belt-and-suspenders: retarget Tk's named fonts too so any widget that
    # resolves them (menus, message boxes, ttk theme defaults) picks up the
    # same family. Must be called after tk.Tk() — fonts don't exist before.
    ui = _ui_family()
    mono = _mono_family()
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


def _now_str() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


class _Tooltip:
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
            font=_font(11),
            wraplength=self.wraplength,
            justify='left', padx=10, pady=6,
            bd=0, highlightthickness=0,
        ).pack()
        tip.geometry(f'+{x}+{y}')
        self._tip = tip


def _attach_tooltip(widget: tk.Widget, text: str) -> None:
    """Convenience: ttk widgets like ttk.Combobox/ttk.Button accept the same
    bindings as plain Tk widgets. Cursor hint goes hand-in-hand."""
    _Tooltip(widget, text)
    try:
        widget.configure(cursor='hand2')
    except tk.TclError:
        pass


class App:
    def __init__(self, root: tk.Tk, resolve_obj: Any):
        self.root = root
        self.resolve = resolve_obj
        self.root.title('MML ONE Sync')
        self.root.geometry('480x460')

        self.data_root = config.user_data_dir()
        self.data_root.mkdir(parents=True, exist_ok=True)
        self.api_base = config.api_base_url()

        self._device: Optional[Dict[str, Any]] = storage.load_device(self.data_root)
        self._projects: List[Dict[str, Any]] = []
        self._selected_project: Optional[Dict[str, Any]] = None
        self._selected_episode: Optional[Dict[str, Any]] = None
        self._active_target_updated_at: int = 0
        self._auto_target: bool = False  # True when current selection came from web hint

        self._events: 'queue.Queue[Callable[[], None]]' = queue.Queue()
        self._build_ui()
        self._tick()
        self._refresh_after_pair()
        # Poll the web app for the "active target" hint so the user never has
        # to pick a project/episode manually after they click "Send to Resolve".
        self._schedule_active_target_poll()

    # ---- ui ----

    def _build_ui(self) -> None:
        # The window now hosts two screens: a welcome / pair landing, and a
        # main control screen for paired devices. _refresh_after_pair
        # decides which one to mount.
        self._container = ttk.Frame(self.root)
        self._container.pack(fill='both', expand=True)

        # State vars used by the main screen — declared here so handlers can
        # reference them safely even between rebuilds.
        self.status_var = tk.StringVar(value='Not paired')
        self.proj_var = tk.StringVar()
        self.ep_var = tk.StringVar()
        self.hint_kicker_var = tk.StringVar(value='')
        self.hint_var = tk.StringVar(value='')
        self.last_sync_var = tk.StringVar(value='—')
        self._selector_open = False

    def _clear_container(self) -> None:
        for child in list(self._container.winfo_children()):
            child.destroy()

    # ---- welcome / pair landing ----

    def _show_welcome(self) -> None:
        self._clear_container()
        # Centered column: glyph + title + 1-line description + Pair button +
        # link out to mml.one. Native ttk widgets, no theming.
        outer = ttk.Frame(self._container, padding=(32, 40, 32, 24))
        outer.pack(fill='both', expand=True)

        # Push content to vertical center using a pair of expanding spacers.
        ttk.Frame(outer).pack(fill='both', expand=True)

        glyph = ttk.Label(outer, text='🎬', font=_font(56))
        glyph.pack(pady=(0, 12))

        ttk.Label(
            outer, text='MML ONE Sync',
            font=_font(22, 'bold'),
        ).pack()

        ttk.Label(
            outer, text='Connect MML ONE with DaVinci Resolve',
            font=_font(13), foreground='#444',
        ).pack(pady=(6, 18))

        ttk.Label(
            outer,
            text=(
                'Generate a 6-digit code in MML ONE → Settings → '
                'DaVinci Resolve sync, then click below to enter it.'
            ),
            font=_font(11),
            foreground='#666',
            wraplength=380, justify='center',
        ).pack(pady=(0, 24))

        pair_btn = ttk.Button(
            outer, text='Pair Resolve with MML ONE',
            command=self._on_pair, width=28,
        )
        pair_btn.pack()
        _attach_tooltip(pair_btn, 'Open the 6-digit code entry to pair this Mac with your account.')

        ttk.Frame(outer).pack(fill='both', expand=True)  # bottom spacer

        # Footer: link to MML ONE web app.
        footer = ttk.Frame(outer)
        footer.pack(fill='x', pady=(16, 0))
        ttk.Label(
            footer, text="Don't have an account? ",
            font=_font(10), foreground='#666',
        ).pack(side='left', expand=True, anchor='e')

        link = ttk.Label(
            footer, text='Open MML ONE',
            font=_font(10, 'underline'), foreground='#0a5fc4',
            cursor='hand2',
        )
        link.pack(side='left', expand=True, anchor='w')
        link.bind('<Button-1>', lambda _e: webbrowser.open(self._mml_one_web_url()))
        _attach_tooltip(link, 'Open the MML ONE web app in your browser.')

    def _mml_one_web_url(self) -> str:
        # Best-effort: api_base ends in convex.site, the public web app is the
        # mirror domain. Fall back to a hard-coded URL.
        return 'https://mml.one'

    # ---- main screen (paired) ----

    def _show_main(self) -> None:
        self._clear_container()
        frm = ttk.Frame(self._container, padding=16)
        frm.pack(fill='both', expand=True)

        # ---- header row: status + sign-out ----
        header = ttk.Frame(frm)
        header.pack(fill='x')

        self.status_label = ttk.Label(
            header, textvariable=self.status_var,
            font=_font(12, 'bold'),
        )
        self.status_label.pack(side='left')

        # Always 'Sign out' on the main screen (welcome screen has its own
        # 'Pair' CTA so we never need a 'Pair…' button up here).
        self.pair_btn = ttk.Button(
            header, text='Sign out', command=self._on_disconnect, width=10,
        )
        self.pair_btn.pack(side='right')
        _attach_tooltip(
            self.pair_btn,
            'Forget the device token saved on this Mac. You can pair again '
            'with a new 6-digit code at any time.',
        )

        # ---- active target hint — promoted to primary signal ----
        # Two lines: a small label + a bigger value, easier to scan than one
        # wrapping line.
        # ---- active target hint — primary signal when MML ONE pushes ----
        self.hint_kicker = ttk.Label(
            frm, textvariable=self.hint_kicker_var,
            foreground='#0a7', font=_font(11),
        )
        self.hint_kicker.pack(anchor='w', pady=(14, 0))
        self.hint_label = ttk.Label(
            frm, textvariable=self.hint_var,
            foreground='#0a7', font=_font(14, 'bold'),
        )
        self.hint_label.pack(anchor='w', pady=(2, 0))

        # ---- selectors (collapsible: hidden until "Choose manually" clicked) ----
        self.proj_var = tk.StringVar()
        self.ep_var = tk.StringVar()

        # Reset disclosure state on every (re)mount of the main screen.
        self._selector_open = False
        self.toggle_btn = ttk.Button(
            frm, text='▸ Choose project / episode manually',
            command=self._toggle_selectors, style='Toolbutton',
        )
        self.toggle_btn.pack(anchor='w', pady=(14, 0))

        self._selectors_frame = ttk.Frame(frm)
        # Not packed yet — _toggle_selectors handles visibility.

        proj_row = ttk.Frame(self._selectors_frame)
        proj_row.pack(fill='x', pady=(8, 4))
        ttk.Label(proj_row, text='Project', width=8).pack(side='left')
        self.proj_combo = ttk.Combobox(
            proj_row, textvariable=self.proj_var, state='readonly',
        )
        self.proj_combo.pack(side='left', fill='x', expand=True, padx=(8, 0))
        self.proj_combo.bind('<<ComboboxSelected>>', self._on_project_change)

        ep_row = ttk.Frame(self._selectors_frame)
        ep_row.pack(fill='x', pady=(0, 4))
        ttk.Label(ep_row, text='Episode', width=8).pack(side='left')
        self.ep_combo = ttk.Combobox(
            ep_row, textvariable=self.ep_var, state='readonly',
        )
        self.ep_combo.pack(side='left', fill='x', expand=True, padx=(8, 0))

        # ---- primary action row ----
        actions = ttk.Frame(frm)
        actions.pack(fill='x', pady=(16, 0))

        # Preview Sync is the day-to-day action — full width on its own row.
        self.sync_btn = ttk.Button(
            actions, text='Preview Sync',
            command=self._on_preview_sync, state='disabled',
        )
        self.sync_btn.pack(fill='x')

        # Secondary row: Force Sync + Import Media — equally sized.
        secondary = ttk.Frame(frm)
        secondary.pack(fill='x', pady=(8, 0))
        self.force_btn = ttk.Button(
            secondary, text='Force Sync',
            command=self._on_force_sync, state='disabled',
        )
        self.force_btn.pack(side='left', expand=True, fill='x')
        self.media_btn = ttk.Button(
            secondary, text='Import Media Only',
            command=self._on_import_media, state='disabled',
        )
        self.media_btn.pack(side='left', expand=True, fill='x', padx=(8, 0))

        # ---- last-synced status (compact) ----
        self.last_sync_var = tk.StringVar(value='—')
        self.last_sync_label = ttk.Label(
            frm, textvariable=self.last_sync_var, foreground='#666',
        )
        self.last_sync_label.pack(anchor='w', pady=(8, 0))

        # ---- divider + activity log (height-bounded) ----
        ttk.Separator(frm, orient='horizontal').pack(fill='x', pady=(14, 10))

        log_header = ttk.Frame(frm)
        log_header.pack(fill='x')
        ttk.Label(log_header, text='Activity', font=_font(11, 'bold')).pack(side='left')
        clear_btn = ttk.Button(log_header, text='Clear', style='Toolbutton',
                               command=self._clear_log)
        clear_btn.pack(side='right')
        _attach_tooltip(clear_btn, 'Clear the activity log on this Mac. Does not affect Resolve or MML ONE.')

        self.log = tk.Text(frm, height=8, wrap='word', state='disabled',
                           bg='#f7f7f7', borderwidth=1, relief='solid', padx=8, pady=6,
                           font=_font(10, mono=True))
        self.log.pack(fill='both', expand=True, pady=(6, 0))

        # ---- tooltips + hover cursor on every interactive control ----
        _attach_tooltip(
            self.pair_btn,
            'Pair this Mac with your MML ONE account using a 6-digit code, '
            'or disconnect the saved device token.',
        )
        _attach_tooltip(
            self.toggle_btn,
            'Show / hide the Project and Episode dropdowns. Normally you '
            'don\'t need them — clicking "Send to Resolve" in MML ONE '
            'auto-selects the right episode here.',
        )
        _attach_tooltip(
            self.proj_combo,
            'Pick a project manually. Overrides the most recent "Send to '
            'Resolve" selection until MML ONE pushes a new one.',
        )
        _attach_tooltip(
            self.ep_combo,
            'Pick an episode within the selected project.',
        )
        _attach_tooltip(
            self.sync_btn,
            'Apply only the changes since the last sync (added / modified / '
            'removed clips). Asks for confirmation before writing.',
        )
        _attach_tooltip(
            self.force_btn,
            'Wipe the existing MML ONE timeline + imported media in Resolve '
            'and re-import everything from scratch. Use when things look '
            'out of sync or after manual edits in Resolve.',
        )
        _attach_tooltip(
            self.media_btn,
            'Download the timeline\'s media files into Resolve\'s Media Pool '
            'without touching any timeline. Use when you want to assemble '
            'your own cut in Resolve from MML ONE assets.',
        )

    def _toggle_selectors(self) -> None:
        if self._selector_open:
            self._selectors_frame.pack_forget()
            self.toggle_btn.configure(text='▸ Choose project / episode manually')
        else:
            self._selectors_frame.pack(after=self.toggle_btn, fill='x', pady=(4, 0))
            self.toggle_btn.configure(text='▾ Choose project / episode manually')
        self._selector_open = not self._selector_open

    def _clear_log(self) -> None:
        self.log.configure(state='normal')
        self.log.delete('1.0', 'end')
        self.log.configure(state='disabled')

    def _log(self, message: str) -> None:
        # Guard for the welcome / unpaired screen where the log widget
        # doesn't exist yet (or has been destroyed by a screen swap).
        log = getattr(self, 'log', None)
        if log is None:
            return
        try:
            if not log.winfo_exists():
                return
        except tk.TclError:
            return
        log.configure(state='normal')
        log.insert('end', f'[{_now_str()}] {message}\n')
        log.see('end')
        log.configure(state='disabled')

    def _tick(self) -> None:
        try:
            while True:
                fn = self._events.get_nowait()
                fn()
        except queue.Empty:
            pass
        self.root.after(100, self._tick)

    def _post(self, fn: Callable[[], None]) -> None:
        self._events.put(fn)

    # ---- pair ----

    def _on_pair(self) -> None:
        win = tk.Toplevel(self.root)
        win.title('Pair Resolve with MML ONE')
        ttk.Label(
            win,
            text=(
                'Open MML ONE → Settings → Resolve sync → Pair new device. '
                'Enter the 6-digit code:'
            ),
        ).pack(padx=12, pady=12)
        code_var = tk.StringVar()
        entry = ttk.Entry(
            win, textvariable=code_var, width=12, justify='center',
            font=_font(18, mono=True),
        )
        entry.pack(pady=4)
        entry.focus_set()
        msg = tk.StringVar()
        ttk.Label(win, textvariable=msg, foreground='red').pack(pady=2)

        def submit():
            code = code_var.get().strip()
            if not code.isdigit() or len(code) != 6:
                msg.set('Enter 6 digits.')
                return
            msg.set('Pairing…')

            def worker():
                try:
                    out = api.claim_pairing_code(
                        self.api_base, code=code,
                        device_name=f'Resolve on {self._platform()}',
                        platform=self._platform(),
                    )
                    storage.save_device(
                        self.data_root, token=out['token'],
                        user_id=out.get('userId', ''),
                        display_name=out.get('displayName', ''),
                        api_base=self.api_base,
                    )
                    self._post(lambda: (win.destroy(), self._refresh_after_pair()))
                except api.ApiError as e:
                    err_msg = str(e)
                    self._post(lambda err_msg=err_msg: msg.set(err_msg))

            threading.Thread(target=worker, daemon=True).start()

        ttk.Button(win, text='Pair', command=submit).pack(pady=(0, 12))

    def _platform(self) -> str:
        import sys
        if sys.platform.startswith('darwin'):
            return 'macos'
        if sys.platform.startswith('win'):
            return 'windows'
        return 'linux'

    def _refresh_after_pair(self) -> None:
        self._device = storage.load_device(self.data_root)
        if not self._device:
            self._show_welcome()
            return
        # Prefer the human-readable displayName the backend resolved at claim
        # time. Fall back to "Connected" rather than dumping the raw Clerk
        # subject — that string is not useful to the user.
        display = (self._device.get('displayName') or '').strip()
        if display and not display.startswith('user_'):
            self.status_var.set(f'Connected · {display}')
        else:
            self.status_var.set('Connected')
        self._show_main()
        self._fetch_projects()

    def _on_disconnect(self) -> None:
        if not messagebox.askokcancel(
            'Sign out of MML ONE Sync',
            'Forget the saved device token on this Mac? '
            'You can pair again later with a fresh 6-digit code.',
        ):
            return
        storage.clear_device(self.data_root)
        self._device = None
        self._projects = []
        self.proj_var.set('')
        self.ep_var.set('')
        self.hint_var.set('')
        self.hint_kicker_var.set('')
        self.status_var.set('Not paired')
        self.last_sync_var.set('—')
        self._show_welcome()

    def _fetch_projects(self) -> None:
        def worker():
            try:
                out = api.get_projects(self.api_base, token=self._device['token'])
                self._post(lambda: self._populate_projects(out['projects']))
            except api.AuthError:
                self._post(self._on_unauth)
            except api.ApiError as e:
                err_msg = str(e)
                self._post(
                    lambda err_msg=err_msg: self._log(f'Failed to load projects: {err_msg}')
                )

        threading.Thread(target=worker, daemon=True).start()

    def _populate_projects(self, projects: List[Dict[str, Any]]) -> None:
        self._projects = projects
        self.proj_combo['values'] = [p['name'] for p in projects]
        if projects:
            self.proj_combo.current(0)
            self._on_project_change()

    def _on_project_change(self, _evt=None) -> None:
        # Manual selection overrides the auto-target hint until the user
        # pushes a new target from the web app.
        self._auto_target = False
        self.hint_var.set('')
        self.hint_kicker_var.set('')
        idx = self.proj_combo.current()
        if idx < 0 or idx >= len(self._projects):
            return
        self._selected_project = self._projects[idx]
        episodes = self._selected_project.get('episodes', [])
        self.ep_combo['values'] = [e['name'] for e in episodes]
        if episodes:
            self.ep_combo.current(0)
            self._selected_episode = episodes[0]
            self.sync_btn.configure(state='normal')
            self.force_btn.configure(state='normal')
            self.media_btn.configure(state='normal')
        else:
            self._selected_episode = None
            self.sync_btn.configure(state='disabled')
            self.force_btn.configure(state='disabled')
            self.media_btn.configure(state='disabled')

    # ---- active-target polling ----

    def _schedule_active_target_poll(self) -> None:
        # Re-arms every 3 seconds. We use root.after instead of a busy loop
        # so the call always fires on the Tk main thread.
        self.root.after(3000, self._poll_active_target)

    def _poll_active_target(self) -> None:
        try:
            if self._device is None:
                return  # not paired yet
            token = self._device['token']

            def worker():
                try:
                    out = api.get_active_target(self.api_base, token=token)
                except api.AuthError:
                    return self._post(self._on_unauth)
                except api.ApiError:
                    return  # transient — try again next tick
                target = out.get('target')
                if not target:
                    return
                if int(target.get('updatedAt', 0)) <= self._active_target_updated_at:
                    return  # nothing newer than what we already applied
                self._post(lambda t=target: self._apply_active_target(t))

            threading.Thread(target=worker, daemon=True).start()
        finally:
            self._schedule_active_target_poll()

    def _apply_active_target(self, target: Dict[str, Any]) -> None:
        """Web app pushed a new target — auto-select the matching project +
        episode in the dropdowns and surface a hint label so the user knows
        what's queued. Only marks the timestamp as 'applied' after every
        step succeeds, otherwise an early-return path would suppress every
        future poll."""
        # Guard: a poll callback can fire after the user clicked Sign out,
        # leaving the main-screen widgets destroyed. Bail in that case.
        if self._device is None or not hasattr(self, 'proj_combo'):
            return
        try:
            if not self.proj_combo.winfo_exists():
                return
        except tk.TclError:
            return
        proj_id = target.get('projectId')
        ep_id = target.get('episodeId')

        # Make sure the projects list is populated. If we just paired and
        # the list hasn't loaded yet, this poll-cycle is a no-op; the next
        # one will succeed.
        if not self._projects:
            self._fetch_projects()
            return

        proj_idx = next(
            (i for i, p in enumerate(self._projects) if p.get('projectId') == proj_id),
            None,
        )
        if proj_idx is None:
            # Project list might be stale — refresh once and retry on next tick.
            self._fetch_projects()
            return

        self.proj_combo.current(proj_idx)
        self._selected_project = self._projects[proj_idx]
        episodes = self._selected_project.get('episodes', [])
        self.ep_combo['values'] = [e['name'] for e in episodes]
        ep_idx = next(
            (i for i, e in enumerate(episodes) if e.get('episodeId') == ep_id),
            None,
        )
        if ep_idx is None:
            return
        self.ep_combo.current(ep_idx)
        self._selected_episode = episodes[ep_idx]
        self.sync_btn.configure(state='normal')
        self.force_btn.configure(state='normal')
        self.media_btn.configure(state='normal')

        self._auto_target = True
        self.hint_kicker_var.set('⤴ Ready to import from MML ONE')
        self.hint_var.set(
            f'{target.get("projectName", "")} · {target.get("episodeName", "")}'
        )
        self._log(
            f'MML ONE → Resolve: now targeting "{target.get("projectName", "")}'
            f' / {target.get("episodeName", "")}". Click Preview Sync or Force Sync.'
        )
        # Now that we've fully applied the target, remember its timestamp so
        # the next identical poll doesn't trigger a redundant log entry.
        self._active_target_updated_at = int(target.get('updatedAt', 0))

    def _on_unauth(self) -> None:
        storage.clear_device(self.data_root)
        self._device = None
        self.status_var.set('Token revoked. Pair again.')
        self.pair_btn.configure(state='normal')
        self.sync_btn.configure(state='disabled')
        self.force_btn.configure(state='disabled')
        self.media_btn.configure(state='disabled')

    # ---- force sync ----

    def _on_force_sync(self) -> None:
        if not self._device or not self._selected_project or not self._selected_episode:
            return
        ep_idx = self.ep_combo.current()
        episodes = self._selected_project.get('episodes', [])
        if ep_idx < 0 or ep_idx >= len(episodes):
            return
        self._selected_episode = episodes[ep_idx]
        proj_name = self._selected_project.get('name', '')
        ep_name = self._selected_episode.get('name', '')
        timeline_name = f'MML ONE - {proj_name} - {ep_name}'.strip()

        ok = messagebox.askokcancel(
            'Force Sync',
            f'This will delete the Resolve timeline "{timeline_name}" and any '
            f'media imported by MML ONE for this episode, then re-import '
            f'everything from the latest MML ONE state.\n\n'
            f'Any color grade, Fusion comp, or per-clip retime you did in '
            f'Resolve on this timeline will be lost.\n\nContinue?',
            icon=messagebox.WARNING,
        )
        if not ok:
            return

        token = self._device['token']
        proj_id = self._selected_project['projectId']
        ep_id = self._selected_episode['episodeId']
        self._log(f'Force sync: resetting "{timeline_name}"…')
        self.sync_btn.configure(state='disabled')
        self.force_btn.configure(state='disabled')

        def worker():
            # 1. Wipe Resolve-side: target timeline + previously-imported media.
            try:
                stats = resolve_apply.reset_resolve_state(
                    resolve=self.resolve, root=self.data_root,
                    project_id=proj_id, episode_id=ep_id,
                    timeline_name=timeline_name,
                )
                self._post(lambda s=stats: self._log(
                    f'Cleared Resolve: {s["timelinesDeleted"]} timeline(s), '
                    f'{s["mediaItemsDeleted"]} media item(s)'
                ))
            except Exception as e:
                err_msg = str(e)
                self._post(lambda err_msg=err_msg: self._log(
                    f'Reset failed (continuing anyway): {err_msg}'
                ))

            # 2. Wipe local: snapshot + mapping + media cache.
            storage.reset_episode_state(self.data_root, proj_id, ep_id)
            self._post(lambda: self._log('Cleared local snapshot and media cache'))

            # 3. Run a normal sync — diff will see everything as added.
            try:
                next_state = api.get_timeline_state(
                    self.api_base, token=token, project_id=proj_id, episode_id=ep_id,
                )
            except api.AuthError:
                return self._post(self._on_unauth)
            except api.ApiError as e:
                err_msg = str(e)
                return self._post(lambda err_msg=err_msg: (
                    self._log(f'Fetch failed: {err_msg}'),
                    self.sync_btn.configure(state='normal'),
                    self.force_btn.configure(state='normal'),
                ))

            prev_state = self._empty_state(next_state)
            d = diff.diff_sync_state(prev_state, next_state)
            self._post(lambda: self._show_preview(prev_state, next_state, d))

        threading.Thread(target=worker, daemon=True).start()

    # ---- sync ----

    def _on_preview_sync(self) -> None:
        if not self._device or not self._selected_project or not self._selected_episode:
            return
        ep_idx = self.ep_combo.current()
        episodes = self._selected_project.get('episodes', [])
        if ep_idx < 0 or ep_idx >= len(episodes):
            return
        self._selected_episode = episodes[ep_idx]
        token = self._device['token']
        proj_id = self._selected_project['projectId']
        ep_id = self._selected_episode['episodeId']

        self._log('Fetching latest timeline…')
        self.sync_btn.configure(state='disabled')

        def worker():
            try:
                next_state = api.get_timeline_state(
                    self.api_base, token=token, project_id=proj_id, episode_id=ep_id,
                )
            except api.AuthError:
                return self._post(self._on_unauth)
            except api.ApiError as e:
                err_msg = str(e)
                return self._post(lambda err_msg=err_msg: (
                    self._log(f'Fetch failed: {err_msg}'),
                    self.sync_btn.configure(state='normal'),
                ))

            prev_state = (
                storage.load_snapshot(self.data_root, proj_id, ep_id)
                or self._empty_state(next_state)
            )

            # Reconcile manual deletions in Resolve (user removed media or
            # cleared the timeline by hand). Drops the affected clips from
            # the snapshot so the diff treats them as added.
            timeline_name = (
                f'MML ONE - {next_state.get("projectName", "")} - '
                f'{next_state.get("episodeName", "")}'.strip()
            )
            try:
                prev_state = resolve_apply.reconcile_drift(
                    resolve=self.resolve, root=self.data_root,
                    project_id=proj_id, episode_id=ep_id,
                    timeline_name=timeline_name, snapshot=prev_state,
                )
            except Exception as e:
                err_msg = str(e)
                self._post(lambda err_msg=err_msg: self._log(
                    f'Reconcile skipped: {err_msg}'
                ))

            d = diff.diff_sync_state(prev_state, next_state)
            self._post(lambda: self._show_preview(prev_state, next_state, d))

        threading.Thread(target=worker, daemon=True).start()

    def _empty_state(self, like: Dict[str, Any]) -> Dict[str, Any]:
        return {
            'schemaVersion': like.get('schemaVersion', 1),
            'projectId': like.get('projectId', ''),
            'episodeId': like.get('episodeId', ''),
            'projectName': like.get('projectName', ''),
            'episodeName': like.get('episodeName', ''),
            'fps': like.get('fps', 30),
            'canvasWidth': like.get('canvasWidth', 1920),
            'canvasHeight': like.get('canvasHeight', 1080),
            'videoTracks': like.get('videoTracks', []),
            'clips': [], 'imageOverlays': [], 'audioClips': [],
            'generatedAt': 0,
        }

    def _show_preview(self, prev: Dict[str, Any], nxt: Dict[str, Any], d: Dict[str, Any]) -> None:
        win = tk.Toplevel(self.root)
        win.title('Preview Sync')

        # Drift warning: if everything looks "unchanged" but the local mapping
        # has no clip → timeline-item links, the user almost certainly deleted
        # the Resolve-side state by hand. Tell them to use Force Sync.
        no_diff = (not d['added'] and not d['modified'] and not d['removed'])
        proj_id = nxt.get('projectId', '')
        ep_id = nxt.get('episodeId', '')
        try:
            mapping = storage.load_mapping(self.data_root, proj_id, ep_id)
            no_mapped_items = not mapping.get('clipIdToTimelineItemId')
        except Exception:
            no_mapped_items = False
        if no_diff and d['unchanged'] > 0 and no_mapped_items:
            ttk.Label(
                win,
                foreground='red',
                text=(
                    'Heads up: this episode looks fully synced, but no clips are '
                    'tracked in this Resolve project. If you deleted them by hand, '
                    'click Cancel and use Force Sync to re-import everything.'
                ),
                wraplength=420, justify='left',
            ).pack(padx=12, pady=(12, 0))

        msg = (
            f'Add: {len(d["added"])} clip(s)\n'
            f'Modify: {len(d["modified"])} clip(s) (re-applied; '
            'per-clip Resolve grade/effects on these will be lost)\n'
            f'Remove: {len(d["removed"])} clip(s) (will be marked red and kept)\n'
            f'Unchanged: {d["unchanged"]}\n\n'
            'Apply now?'
        )
        ttk.Label(win, text=msg, justify='left').pack(padx=12, pady=12)
        btns = ttk.Frame(win); btns.pack(pady=(0, 12))

        def apply_now():
            win.destroy()
            self._apply(prev, nxt, d)

        ttk.Button(btns, text='Apply', command=apply_now).pack(side='left', padx=4)

        def cancel():
            win.destroy()
            self.sync_btn.configure(state='normal')
            self.force_btn.configure(state='normal')

        ttk.Button(btns, text='Cancel', command=cancel).pack(side='left')

    def _apply(self, prev: Dict[str, Any], nxt: Dict[str, Any], d: Dict[str, Any]) -> None:
        timeline_name = (
            f'MML ONE - {nxt.get("projectName", "")} - {nxt.get("episodeName", "")}'.strip()
        )

        def downloader(url: str, dest: Path) -> Path:
            """Atomic + size-bounded download. Writes to <dest>.tmp, verifies size,
            then os.replace to <dest>. A network drop or oversized response leaves
            no partial file at the cache path Resolve will read from."""
            tmp = dest.with_suffix(dest.suffix + '.tmp')
            try:
                with urllib.request.urlopen(url, timeout=config.HTTP_READ_TIMEOUT) as r:
                    declared = int(r.headers.get('Content-Length') or 0)
                    if declared and declared > config.MEDIA_MAX_BYTES:
                        raise IOError(
                            f'media too large: {declared} > {config.MEDIA_MAX_BYTES}'
                        )
                    written = 0
                    with open(tmp, 'wb') as f:
                        while True:
                            chunk = r.read(1 << 20)
                            if not chunk:
                                break
                            written += len(chunk)
                            if written > config.MEDIA_MAX_BYTES:
                                raise IOError(
                                    f'media exceeded {config.MEDIA_MAX_BYTES} bytes mid-stream'
                                )
                            f.write(chunk)
                    if declared and written != declared:
                        raise IOError(f'short download: {written}/{declared}')
                os.replace(tmp, dest)
            except Exception:
                try:
                    tmp.unlink()
                except OSError:
                    pass
                raise
            return dest

        def worker():
            try:
                result = resolve_apply.apply_diff(
                    resolve=self.resolve, root=self.data_root,
                    project_id=nxt['projectId'], episode_id=nxt['episodeId'],
                    timeline_name=timeline_name,
                    current_state=nxt, diff=d,
                    downloader=downloader,
                )
                if result.skipped == 0:
                    storage.save_snapshot(
                        self.data_root, nxt['projectId'], nxt['episodeId'], nxt,
                    )
                else:
                    self._post(lambda c=result.skipped: self._log(
                        f'{c} item(s) skipped — snapshot NOT advanced; next sync will retry'
                    ))
                api.heartbeat(self.api_base, token=self._device['token'])
                self._post(lambda: self._after_apply(result))
            except resolve_apply.FrameRateMismatch as e:
                err_msg = str(e)
                self._post(lambda err_msg=err_msg: (
                    messagebox.showerror('Frame rate mismatch', err_msg),
                    self.sync_btn.configure(state='normal'),
                ))
            except resolve_apply.TimelineNotFound as e:
                err_msg = str(e)
                self._post(lambda err_msg=err_msg: (
                    messagebox.showerror('No project open', err_msg),
                    self.sync_btn.configure(state='normal'),
                ))
            except Exception as e:
                err_msg = str(e)
                self._post(lambda err_msg=err_msg: (
                    self._log(f'Apply failed: {err_msg}'),
                    self.sync_btn.configure(state='normal'),
                ))

        threading.Thread(target=worker, daemon=True).start()

    # ---- import-media-only ----

    def _on_import_media(self) -> None:
        if not self._device or not self._selected_project or not self._selected_episode:
            return
        ep_idx = self.ep_combo.current()
        episodes = self._selected_project.get('episodes', [])
        if ep_idx < 0 or ep_idx >= len(episodes):
            return
        self._selected_episode = episodes[ep_idx]
        token = self._device['token']
        proj_id = self._selected_project['projectId']
        ep_id = self._selected_episode['episodeId']

        self._log('Import Media: fetching latest timeline state…')
        self.sync_btn.configure(state='disabled')
        self.force_btn.configure(state='disabled')
        self.media_btn.configure(state='disabled')

        def downloader(url: str, dest: Path) -> Path:
            tmp = dest.with_suffix(dest.suffix + '.tmp')
            try:
                with urllib.request.urlopen(url, timeout=config.HTTP_READ_TIMEOUT) as r:
                    declared = int(r.headers.get('Content-Length') or 0)
                    if declared and declared > config.MEDIA_MAX_BYTES:
                        raise IOError(f'media too large: {declared} > {config.MEDIA_MAX_BYTES}')
                    written = 0
                    with open(tmp, 'wb') as f:
                        while True:
                            chunk = r.read(1 << 20)
                            if not chunk:
                                break
                            written += len(chunk)
                            if written > config.MEDIA_MAX_BYTES:
                                raise IOError(
                                    f'media exceeded {config.MEDIA_MAX_BYTES} bytes mid-stream'
                                )
                            f.write(chunk)
                    if declared and written != declared:
                        raise IOError(f'short download: {written}/{declared}')
                os.replace(tmp, dest)
            except Exception:
                try:
                    tmp.unlink()
                except OSError:
                    pass
                raise
            return dest

        def worker():
            try:
                next_state = api.get_timeline_state(
                    self.api_base, token=token, project_id=proj_id, episode_id=ep_id,
                )
            except api.AuthError:
                return self._post(self._on_unauth)
            except api.ApiError as e:
                err_msg = str(e)
                return self._post(lambda err_msg=err_msg: (
                    self._log(f'Fetch failed: {err_msg}'),
                    self.sync_btn.configure(state='normal'),
                    self.force_btn.configure(state='normal'),
                    self.media_btn.configure(state='normal'),
                ))
            try:
                result = resolve_apply.import_media_only(
                    resolve=self.resolve, root=self.data_root,
                    project_id=proj_id, episode_id=ep_id,
                    current_state=next_state, downloader=downloader,
                )
                api.heartbeat(self.api_base, token=token)
                self._post(lambda r=result: self._after_import_media(r))
            except Exception as e:
                err_msg = str(e)
                self._post(lambda err_msg=err_msg: (
                    self._log(f'Import Media failed: {err_msg}'),
                    self.sync_btn.configure(state='normal'),
                    self.force_btn.configure(state='normal'),
                    self.media_btn.configure(state='normal'),
                ))

        threading.Thread(target=worker, daemon=True).start()

    def _after_import_media(self, result: resolve_apply.ApplyResult) -> None:
        self._log(
            f'Imported media: +{result.added} into Media Pool '
            f'(skipped {result.skipped}, timeline NOT modified)'
        )
        for w in result.warnings:
            self._log(f'Warning: {w}')
        self.sync_btn.configure(state='normal')
        self.force_btn.configure(state='normal')
        self.media_btn.configure(state='normal')

    def _after_apply(self, result: resolve_apply.ApplyResult) -> None:
        self._log(
            f'Applied: +{result.added} ~{result.modified} -{result.removed} '
            f'(skipped {result.skipped})'
        )
        for w in result.warnings:
            self._log(f'Warning: {w}')
        self.last_sync_var.set(f'Last synced {_now_str()}')
        self.sync_btn.configure(state='normal')
        self.force_btn.configure(state='normal')


def main(resolve_obj: Any) -> None:
    root = tk.Tk()
    _apply_named_font_defaults()
    App(root, resolve_obj)
    root.mainloop()
