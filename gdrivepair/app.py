"""Gdrive-Pair main window (GTK4 + libadwaita)."""

from __future__ import annotations

import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

from . import APP_ID, APP_NAME, GUIDE_URL, VERSION, core  # noqa: E402
from .core import State  # noqa: E402

REFRESH_SECS = 2
SETUP_CHECK_SECS = 30

CSS = b"""
.status-dot { font-size: 18px; min-width: 18px; }
.mono-log { font-family: monospace; font-size: 0.85em; }
"""

DOT_CLASS = {
    State.OK: "success",
    State.SYNCING: "accent",
    State.FIRST_SYNC: "accent",
    State.PAUSED: "dim-label",
    State.BLOCKED: "error",
    State.ERROR: "error",
    State.NEEDS_FIRST_SYNC: "warning",
}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def run_async(func, on_done, *args):
    """Run func(*args) in a thread; call on_done(result, error) on the UI thread."""
    def worker():
        try:
            result, error = func(*args), None
        except Exception as e:  # noqa: BLE001 - shown to the user
            result, error = None, e
        GLib.idle_add(lambda: (on_done(result, error), False)[1])
    threading.Thread(target=worker, daemon=True).start()


def open_uri(uri: str) -> None:
    try:
        Gio.AppInfo.launch_default_for_uri(uri, None)
    except GLib.Error as e:
        print(f"Could not open {uri}: {e.message}", file=sys.stderr)


def open_path(path: Path) -> None:
    open_uri(path.as_uri())


def flat_button(icon: str, tooltip: str, callback) -> Gtk.Button:
    b = Gtk.Button(icon_name=icon, tooltip_text=tooltip, valign=Gtk.Align.CENTER)
    b.add_css_class("flat")
    b.connect("clicked", lambda *_: callback())
    return b


def format_run_time(t: float) -> str:
    d = datetime.fromtimestamp(t)
    today = datetime.now().date()
    if d.date() == today:
        return d.strftime("%H:%M")
    if d.date() == today - timedelta(days=1):
        return d.strftime("Yesterday %H:%M")
    return d.strftime("%-d %b %H:%M")


def scrolled(child: Gtk.Widget) -> Gtk.ScrolledWindow:
    clamp = Adw.Clamp(maximum_size=720, child=child,
                      margin_top=12, margin_bottom=24, margin_start=12, margin_end=12)
    return Gtk.ScrolledWindow(child=clamp, vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)


# ---------------------------------------------------------------------------
# Main list row
# ---------------------------------------------------------------------------

class PairRow(Adw.ActionRow):
    def __init__(self, window: "MainWindow", status: core.PairStatus):
        super().__init__(activatable=True, subtitle_lines=2)
        self.window = window
        self.status = status

        self.dot = Gtk.Label(label="●", valign=Gtk.Align.CENTER)
        self.dot.add_css_class("status-dot")
        self.add_prefix(self.dot)

        self.review_btn = Gtk.Button(label="Review…", valign=Gtk.Align.CENTER)
        self.review_btn.connect("clicked", lambda *_: window.review(self.status.pair))
        self.retry_btn = Gtk.Button(label="Retry", valign=Gtk.Align.CENTER)
        self.retry_btn.connect("clicked", lambda *_: window.retry_first_sync(self.status.pair))
        self.sync_btn = flat_button("view-refresh-symbolic", "Sync now",
                                    lambda: window.do_action(core.sync_now, self.status.pair, "Sync started"))
        self.pause_btn = flat_button("media-playback-pause-symbolic", "Pause", self._toggle_pause)
        self.folder_btn = flat_button("folder-open-symbolic", "Open folder",
                                      lambda: open_path(self.status.pair.local))
        for w in (self.review_btn, self.retry_btn, self.sync_btn, self.pause_btn, self.folder_btn):
            self.add_suffix(w)

        self.connect("activated", lambda *_: window.show_detail(self.status.pair.name))
        self.update(status)

    def _toggle_pause(self):
        p = self.status.pair
        if self.status.paused:
            self.window.do_action(core.resume, p, f"{p.name} resumed")
        else:
            self.window.do_action(core.pause, p, f"{p.name} paused")

    def update(self, status: core.PairStatus):
        self.status = status
        st = status.state
        self.set_title(GLib.markup_escape_text(status.pair.name))
        self.set_subtitle(GLib.markup_escape_text(status.text))
        for cls in set(DOT_CLASS.values()):
            self.dot.remove_css_class(cls)
        self.dot.add_css_class(DOT_CLASS[st])

        ready = st not in (State.FIRST_SYNC, State.NEEDS_FIRST_SYNC)
        self.review_btn.set_visible(st == State.BLOCKED)
        self.retry_btn.set_visible(st == State.NEEDS_FIRST_SYNC)
        self.sync_btn.set_visible(ready)
        self.sync_btn.set_sensitive(not status.paused and not status.syncing)
        self.sync_btn.set_tooltip_text("Paused: resume to sync" if status.paused else "Sync now")
        self.pause_btn.set_visible(ready)
        self.pause_btn.set_icon_name("media-playback-start-symbolic" if status.paused
                                     else "media-playback-pause-symbolic")
        self.pause_btn.set_tooltip_text("Resume" if status.paused else "Pause")
        self.folder_btn.set_visible(status.pair.local.is_dir())


# ---------------------------------------------------------------------------
# Detail page
# ---------------------------------------------------------------------------

class DetailPage(Adw.NavigationPage):
    def __init__(self, window: "MainWindow", status: core.PairStatus):
        super().__init__(title=status.pair.name, tag=f"detail-{status.pair.name}")
        self.window = window
        self.pair = status.pair
        self._history_key = None
        self._log_mtime = None

        self.title_widget = Adw.WindowTitle(title=self.pair.name)
        header = Adw.HeaderBar(title_widget=self.title_widget)
        toolbar = Adw.ToolbarView(content=None)
        toolbar.add_top_bar(header)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)

        info = Adw.PreferencesGroup()
        drive_row = Adw.ActionRow(title="Drive folder", subtitle=self.pair.folder_id or self.pair.remote,
                                  subtitle_selectable=True)
        if self.pair.drive_url:
            drive_row.add_suffix(flat_button("web-browser-symbolic", "Open in browser",
                                             lambda: open_uri(self.pair.drive_url)))
        info.add(drive_row)
        local_row = Adw.ActionRow(title="Local folder", subtitle=core.tilde(self.pair.local),
                                  subtitle_selectable=True)
        local_row.add_suffix(flat_button("folder-open-symbolic", "Open folder",
                                         lambda: open_path(self.pair.local)))
        info.add(local_row)
        info.add(Adw.ActionRow(title="Remote", subtitle=self.pair.remote_name))
        self.notify_row = Adw.SwitchRow(title="Sync notifications",
                                        subtitle="Notify when a sync moves files. Failures always notify.",
                                        active=self.pair.notify_sync)
        self.notify_row.connect("notify::active", self._on_notify_toggled)
        info.add(self.notify_row)
        box.append(info)

        # History / raw log tabs
        self.stack = Adw.ViewStack()
        self.history_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.history_list.add_css_class("boxed-list")
        self.history_list.set_placeholder(Gtk.Label(label="No syncs yet", margin_top=24,
                                                    margin_bottom=24, css_classes=["dim-label"]))
        self.stack.add_titled_with_icon(self.history_list, "history", "History", "document-open-recent-symbolic")

        self.log_view = Gtk.TextView(editable=False, monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR,
                                     top_margin=8, bottom_margin=8, left_margin=8, right_margin=8)
        log_scroll = Gtk.ScrolledWindow(child=self.log_view, min_content_height=380)
        log_scroll.add_css_class("card")
        self.stack.add_titled_with_icon(log_scroll, "log", "Raw Log", "text-x-generic-symbolic")
        self.stack.connect("notify::visible-child-name", lambda *_: self.refresh_log(force=True))

        switcher = Adw.InlineViewSwitcher(stack=self.stack, halign=Gtk.Align.CENTER)
        box.append(switcher)
        box.append(self.stack)

        remove_btn = Gtk.Button(label="Remove Pair…", halign=Gtk.Align.CENTER, margin_top=12)
        remove_btn.add_css_class("destructive-action")
        remove_btn.add_css_class("pill")
        remove_btn.connect("clicked", lambda *_: self._confirm_remove())
        box.append(remove_btn)

        toolbar.set_content(scrolled(box))
        self.set_child(toolbar)
        self.update(status)

    def _on_notify_toggled(self, row, _pspec):
        try:
            core.set_notify_sync(self.pair, row.get_active())
        except OSError as e:
            self.window.toast(f"Could not save setting: {e}")

    def update(self, status: core.PairStatus):
        self.pair = status.pair
        self.title_widget.set_subtitle(status.text)
        self.refresh_history()
        self.refresh_log()

    def refresh_history(self):
        history = core.read_history(self.pair)
        key = (len(history), history[0].time if history else None)
        if key == self._history_key:
            return
        self._history_key = key
        while (child := self.history_list.get_first_child()) is not None:
            self.history_list.remove(child)
        segments = None
        for i, run_ in enumerate(history[:100]):
            icon = {"ok": "✓", "blocked": "⛔"}.get(run_.result, "✗")
            row = Adw.ExpanderRow(title=GLib.markup_escape_text(f"{format_run_time(run_.time)}  {icon}  {run_.summary}"),
                                  title_lines=2)
            label = Gtk.Label(xalign=0, selectable=True, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR,
                              margin_top=8, margin_bottom=8, margin_start=12, margin_end=12)
            label.add_css_class("mono-log")
            row.add_row(label)

            def on_expand(r, _p, idx=i, lbl=label):
                nonlocal segments
                if not r.get_expanded() or lbl.get_label():
                    return
                if segments is None:
                    segments = core.log_segments(self.pair)
                text = core.log_for_run(history, idx, segments)
                lbl.set_label(text[-20000:] if text else "No rclone output for this run "
                              "(it stopped before rclone started, or the log was rotated).")
            row.connect("notify::expanded", on_expand)
            self.history_list.append(row)

    def refresh_log(self, force: bool = False):
        if self.stack.get_visible_child_name() != "log":
            return
        try:
            mtime = self.pair.log_path.stat().st_mtime
        except OSError:
            mtime = None
        if not force and mtime == self._log_mtime:
            return
        self._log_mtime = mtime
        text = core.read_log_tail(self.pair) or "Log is empty."
        buf = self.log_view.get_buffer()
        buf.set_text(text)
        GLib.idle_add(lambda: (self.log_view.scroll_to_iter(buf.get_end_iter(), 0, False, 0, 1), False)[1])

    def _confirm_remove(self):
        name = self.pair.name
        dialog = Adw.AlertDialog(
            heading=f"Remove “{name}”?",
            body=(f"Syncing stops and this pair's settings, history and log are deleted.\n\n"
                  f"Your files in {core.tilde(self.pair.local)} and on Google Drive are not touched."))
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("remove", "Remove")
        dialog.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")

        def on_response(_d, response):
            if response != "remove":
                return
            def done(_r, err):
                if err:
                    self.window.toast(str(err))
                    return
                self.window.nav.pop_to_tag("main")
                self.window.toast(f"Removed {name}. Files were kept.")
                self.window.refresh()
            run_async(core.remove_pair, done, self.pair)
        dialog.connect("response", on_response)
        dialog.present(self.window)


# ---------------------------------------------------------------------------
# Review blocked deletes
# ---------------------------------------------------------------------------

class ReviewDialog(Adw.Dialog):
    MAX_ROWS = 300

    def __init__(self, window: "MainWindow", pair: core.Pair):
        super().__init__(title=f"Review Changes: {pair.name}", content_width=560, content_height=600)
        self.window = window
        self.pair = pair

        self.toolbar = Adw.ToolbarView()
        self.toolbar.add_top_bar(Adw.HeaderBar())
        self.stack = Gtk.Stack()

        loading = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                          valign=Gtk.Align.CENTER, halign=Gtk.Align.CENTER)
        loading.append(Adw.Spinner(width_request=32, height_request=32))
        loading.append(Gtk.Label(label="Checking what the sync would change…", css_classes=["dim-label"]))
        self.stack.add_named(loading, "loading")
        self.toolbar.set_content(self.stack)
        self.set_child(self.toolbar)

        run_async(core.review_changes, self._loaded, pair)

    def _loaded(self, items, err):
        if err:
            self.stack.add_named(Adw.StatusPage(icon_name="dialog-error-symbolic", title="Could not check",
                                                description=GLib.markup_escape_text(str(err))), "error")
            self.stack.set_visible_child_name("error")
            return
        if not items:
            page = Adw.StatusPage(icon_name="object-select-symbolic", title="Nothing to delete now",
                                  description="Things changed since the sync was blocked. A normal sync will do.")
            btn = Gtk.Button(label="Sync Now", halign=Gtk.Align.CENTER, css_classes=["suggested-action", "pill"])
            btn.connect("clicked", lambda *_: (self.window.do_action(core.sync_now, self.pair, "Sync started"),
                                               self.close()))
            page.set_child(btn)
            self.stack.add_named(page, "empty")
            self.stack.set_visible_child_name("empty")
            return

        def pick(kind, side):
            return [i for i in items if i.kind == kind and i.side == side]

        moved_on_drive, moved_here = pick("move", "local"), pick("move", "drive")
        del_on_drive, del_here = pick("delete", "drive"), pick("delete", "local")
        deletes = del_on_drive + del_here

        page = Adw.PreferencesPage()
        if not deletes:
            page.set_description("Nothing is lost: every file was moved or renamed, not deleted. "
                                 "The sync repeats the moves on the other side.")
        elif moved_on_drive or moved_here:
            page.set_description("Some files were only moved. The ones under “Will be deleted” "
                                 "were really removed.")

        if moved_on_drive:
            page.add(self._group(f"Moved on Google Drive ({len(moved_on_drive)})",
                                 "The sync does the same here: it downloads each file at its new place "
                                 "and removes the old copy.", moved_on_drive))
        if moved_here:
            page.add(self._group(f"Moved on this computer ({len(moved_here)})",
                                 "The sync does the same on Google Drive: it uploads each file at its new "
                                 "place and moves the old copy to Drive's Trash.", moved_here))
        if del_on_drive:
            page.add(self._group(f"Will be deleted on Google Drive ({len(del_on_drive)})",
                                 "You deleted these on this computer. On Drive they go to the Trash.",
                                 del_on_drive))
        if del_here:
            page.add(self._group(f"Will be deleted from this computer ({len(del_here)})",
                                 "These were deleted on Google Drive. Local deletion is permanent.",
                                 del_here))
        self.stack.add_named(page, "list")
        self.stack.set_visible_child_name("list")

        bar = Gtk.Box(spacing=12, halign=Gtk.Align.END, margin_top=12, margin_bottom=12,
                      margin_start=12, margin_end=12)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        if deletes:
            allow = Gtk.Button(label="Allow This Once", css_classes=["destructive-action"])
        else:
            allow = Gtk.Button(label="Apply Moves", css_classes=["suggested-action"])
        allow.connect("clicked", lambda *_: self._allow())
        bar.append(cancel)
        bar.append(allow)
        self.toolbar.add_bottom_bar(bar)

    def _group(self, title, description, items):
        g = Adw.PreferencesGroup(title=GLib.markup_escape_text(title), description=description)
        for item in items[:self.MAX_ROWS]:
            if item.kind == "move":
                row = Adw.ActionRow(title=GLib.markup_escape_text(item.new_path),
                                    subtitle=GLib.markup_escape_text(f"from {item.path}"),
                                    title_lines=2, subtitle_lines=2)
                row.add_prefix(Gtk.Image(icon_name="go-next-symbolic"))
            else:
                row = Adw.ActionRow(title=GLib.markup_escape_text(item.path), title_lines=2)
            g.add(row)
        if len(items) > self.MAX_ROWS:
            g.add(Adw.ActionRow(title=f"…and {len(items) - self.MAX_ROWS} more", css_classes=["dim-label"]))
        return g

    def _allow(self):
        self.window.do_action(core.allow_once, self.pair, "Approved sync started")
        self.close()


# ---------------------------------------------------------------------------
# Add pair wizard
# ---------------------------------------------------------------------------

class AddPairDialog(Adw.Dialog):
    def __init__(self, window: "MainWindow"):
        super().__init__(title="Add Pair", content_width=520, content_height=560)
        self.window = window
        self.remote = None
        self.folder_id = None
        self.check: core.FolderCheck | None = None
        self.name = ""

        self.nav = Adw.NavigationView()
        self.set_child(self.nav)
        remotes = core.list_drive_remotes()
        if not remotes:
            page = self._page("Add Pair", Adw.StatusPage(
                icon_name="dialog-warning-symbolic", title="No Google Drive remote",
                description="Create one in a terminal with <tt>rclone config</tt> "
                            "(storage type “drive”, your own client ID), then try again. "
                            f"Step-by-step guide: {GUIDE_URL}"))
            self.nav.add(page)
            return
        self.remotes = remotes
        self.nav.add(self._remote_page())

    # generic page with a header bar and optional Next button
    def _page(self, title, content, next_label=None, on_next=None):
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        toolbar.add_top_bar(header)
        page = Adw.NavigationPage(title=title, child=toolbar)
        page.next_btn = None
        if next_label:
            btn = Gtk.Button(label=next_label, css_classes=["suggested-action"])
            btn.connect("clicked", lambda *_: on_next())
            header.pack_end(btn)
            page.next_btn = btn
        toolbar.set_content(content)
        return page

    def _prefs(self, *groups, description=None):
        page = Adw.PreferencesPage(description=description or "")
        for g in groups:
            page.add(g)
        return page

    # step 1: remote
    def _remote_page(self):
        g = Adw.PreferencesGroup(description="The rclone remote for the Google account that can see the folder.")
        self.remote_row = Adw.ComboRow(title="Google account (remote)", model=Gtk.StringList.new(self.remotes))
        if "gdrive" in self.remotes:
            self.remote_row.set_selected(self.remotes.index("gdrive"))
        g.add(self.remote_row)
        return self._page("Step 1 of 4: Account", self._prefs(g), "Next", self._after_remote)

    def _after_remote(self):
        self.remote = self.remotes[self.remote_row.get_selected()]
        self.nav.push(self._folder_page())

    # step 2: folder link
    def _folder_page(self):
        g = Adw.PreferencesGroup(description="Open the folder in Google Drive in your browser and copy the "
                                             "address bar, or paste just the folder ID.")
        self.link_row = Adw.EntryRow(title="Folder link or ID")
        self.link_row.connect("entry-activated", lambda *_: self._after_folder())
        g.add(self.link_row)
        self.folder_error = Gtk.Label(xalign=0, wrap=True, css_classes=["error"], visible=False, margin_top=6)
        g.add(self.folder_error)
        self.folder_spinner = Adw.Spinner(visible=False, margin_top=12)
        g.add(self.folder_spinner)
        self.folder_page = self._page("Step 2 of 4: Drive Folder", self._prefs(g), "Next", self._after_folder)
        return self.folder_page

    def _folder_fail(self, msg):
        self.folder_spinner.set_visible(False)
        self.folder_page.next_btn.set_sensitive(True)
        self.folder_error.set_label(msg)
        self.folder_error.set_visible(True)

    def _after_folder(self):
        fid = core.parse_folder_id(self.link_row.get_text())
        if not fid:
            self._folder_fail("That doesn't look like a Drive folder link or ID.")
            return
        existing = core.find_pair_for_folder(fid, core.load_pairs())
        if existing:
            self._folder_fail(f"This folder is already synced by “{existing.name}”.")
            return
        self.folder_error.set_visible(False)
        self.folder_spinner.set_visible(True)
        self.folder_page.next_btn.set_sensitive(False)

        def done(check, err):
            if err:
                self._folder_fail(str(err))
                return
            self.folder_spinner.set_visible(False)
            self.folder_page.next_btn.set_sensitive(True)
            self.folder_id, self.check = fid, check
            self.nav.push(self._name_page())
        run_async(core.check_folder, done, self.remote, fid)

    # step 3: name
    def _name_page(self):
        contents = Adw.PreferencesGroup(title="Folder contents")
        entries = self.check.entries if self.check else []
        if not entries:
            contents.add(Adw.ActionRow(title="This folder is empty"))
        for e in entries[:8]:
            icon = "folder-symbolic" if e.endswith("/") else "text-x-generic-symbolic"
            row = Adw.ActionRow(title=GLib.markup_escape_text(e.rstrip("/")))
            row.add_prefix(Gtk.Image(icon_name=icon))
            contents.add(row)
        if len(entries) > 8:
            contents.add(Adw.ActionRow(title=f"…and {len(entries) - 8} more", css_classes=["dim-label"]))

        g = Adw.PreferencesGroup(title="Name",
                                 description="Used for the local folder and in notifications.")
        self.name_row = Adw.EntryRow(title="Pair name")
        self.name_row.connect("changed", lambda *_: self._validate_name())
        self.name_row.connect("entry-activated",
                              lambda *_: self._after_name() if self.name_page.next_btn.get_sensitive() else None)
        g.add(self.name_row)
        self.name_hint = Gtk.Label(xalign=0, wrap=True, margin_top=6, css_classes=["dim-label"])
        g.add(self.name_hint)
        self.name_page = self._page("Step 3 of 4: Name", self._prefs(contents, g), "Next", self._after_name)
        self._validate_name()
        return self.name_page

    def _validate_name(self):
        name = self.name_row.get_text().strip()
        err = core.validate_name(name, core.load_pairs()) if name else "Enter a name."
        if hasattr(self, "name_page"):
            self.name_page.next_btn.set_sensitive(err is None)
        hint = self.name_hint
        for c in ("error", "dim-label"):
            hint.remove_css_class(c)
        if name and err:
            hint.set_label(err)
            hint.add_css_class("error")
        else:
            hint.set_label(f"Local folder: {core.tilde(core.local_path_for(name or '<name>'))}")
            hint.add_css_class("dim-label")

    def _after_name(self):
        self.name = self.name_row.get_text().strip()
        self.nav.push(self._confirm_page())

    # step 4: confirm
    def _confirm_page(self):
        self.confirm_stack = Gtk.Stack()
        loading = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                          valign=Gtk.Align.CENTER, halign=Gtk.Align.CENTER)
        loading.append(Adw.Spinner(width_request=32, height_request=32))
        loading.append(Gtk.Label(label="Measuring the Drive folder…", css_classes=["dim-label"]))
        self.confirm_stack.add_named(loading, "loading")
        self.confirm_page = self._page("Step 4 of 4: Confirm", self.confirm_stack, "Start Sync", self._start)
        self.confirm_page.next_btn.set_sensitive(False)
        run_async(core.folder_size, self._sized, self.remote, self.folder_id)
        return self.confirm_page

    def _sized(self, result, err):
        if err:
            self.confirm_stack.add_named(Adw.StatusPage(icon_name="dialog-error-symbolic",
                                                        title="Could not measure folder",
                                                        description=GLib.markup_escape_text(str(err))), "error")
            self.confirm_stack.set_visible_child_name("error")
            return
        count, size = result
        local = core.local_path_for(self.name)
        disk = core.disk_check(local, size)
        g = Adw.PreferencesGroup(description="The first sync downloads the whole folder. It runs in the "
                                             "background, so you can close Gdrive-Pair meanwhile.")
        for title, value in (("Files", f"{count:,}"), ("Download size", core.human_size(size)),
                             ("Free space", core.human_size(disk.free)),
                             ("Local folder", core.tilde(local)),
                             ("Drive folder", self.folder_id), ("Remote", self.remote)):
            row = Adw.ActionRow(title=title, subtitle=GLib.markup_escape_text(value), subtitle_selectable=True)
            g.add(row)
        page = self._prefs(g)
        if not disk.fits:
            err_group = Adw.PreferencesGroup()
            err_group.add(Gtk.Label(
                label=(f"Not enough free space. The download needs {core.human_size(size)} plus a "
                       f"{core.human_size(disk.margin)} safety margin, but only "
                       f"{core.human_size(disk.free)} is free."),
                wrap=True, xalign=0, css_classes=["error"]))
            page.add(err_group)
        self.confirm_stack.add_named(page, "summary")
        self.confirm_stack.set_visible_child_name("summary")
        self.confirm_page.next_btn.set_sensitive(disk.fits)

    def _start(self):
        self.confirm_page.next_btn.set_sensitive(False)

        def done(pair, err):
            if err:
                self.confirm_page.next_btn.set_sensitive(True)
                self.window.toast(f"Could not start: {err}")
                return
            self.window.toast(f"First sync of {pair.name} started")
            self.window.refresh()
            self.close()
        run_async(core.create_pair, done, self.name, self.remote, self.folder_id)


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application):
        super().__init__(application=app, title=APP_NAME, default_width=600, default_height=640)
        self.rows: dict[str, PairRow] = {}
        self._refreshing = False
        self._problems: list[core.SetupProblem] = []
        self._problems_checked = 0.0

        self.toasts = Adw.ToastOverlay()
        self.nav = Adw.NavigationView()
        self.toasts.set_child(self.nav)
        self.set_content(self.toasts)

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        add_btn = Gtk.Button(icon_name="list-add-symbolic", tooltip_text="Add pair")
        add_btn.connect("clicked", lambda *_: self.add_pair())
        header.pack_start(add_btn)
        menu = Gio.Menu()
        menu.append("Refresh", "app.refresh")
        menu.append("Open Logs Folder", "app.open-logs")
        menu.append(f"About {APP_NAME}", "app.about")
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu, tooltip_text="Main menu"))
        toolbar.add_top_bar(header)

        self.main_stack = Gtk.Stack()
        self.listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, valign=Gtk.Align.START)
        self.listbox.add_css_class("boxed-list")
        self.main_stack.add_named(scrolled(self.listbox), "list")

        empty = Adw.StatusPage(icon_name="folder-remote-symbolic", title="No Synced Folders",
                               description="Add a Google Drive folder to keep it in sync with this computer.")
        empty_btn = Gtk.Button(label="Add Pair", halign=Gtk.Align.CENTER, css_classes=["suggested-action", "pill"])
        empty_btn.connect("clicked", lambda *_: self.add_pair())
        empty.set_child(empty_btn)
        self.main_stack.add_named(empty, "empty")

        self.setup_page = Adw.StatusPage(icon_name="dialog-warning-symbolic", title="Setup Needed")
        self.main_stack.add_named(self.setup_page, "setup")

        toolbar.set_content(self.main_stack)
        self.nav.add(Adw.NavigationPage(title=APP_NAME, tag="main", child=toolbar))

        self.refresh(check_setup=True)
        GLib.timeout_add_seconds(REFRESH_SECS, self._tick)

    # -- refresh ----------------------------------------------------------
    def _tick(self):
        self.refresh()
        return True

    def refresh(self, check_setup: bool = False):
        if self._refreshing:
            return
        self._refreshing = True
        check_setup = check_setup or time.time() - self._problems_checked > SETUP_CHECK_SECS

        def work():
            problems = core.setup_problems() if check_setup else None
            return core.get_statuses(), problems
        run_async(work, self._apply)

    def _apply(self, result, err):
        self._refreshing = False
        if err:
            print(f"refresh failed: {err}", file=sys.stderr)
            return
        statuses, problems = result
        if problems is not None:
            self._problems = problems
            self._problems_checked = time.time()

        blocking = [p for p in self._problems if p.title != "No Google Drive remote found" or not statuses]
        if blocking and not statuses:
            self._show_setup(blocking)
        else:
            self.main_stack.set_visible_child_name("list" if statuses else "empty")

        names = [s.pair.name for s in statuses]
        for name in list(self.rows):
            if name not in names:
                self.listbox.remove(self.rows.pop(name))
        for s in statuses:
            row = self.rows.get(s.pair.name)
            if row is None:
                row = self.rows[s.pair.name] = PairRow(self, s)
                self.listbox.append(row)
            else:
                row.update(s)

        visible = self.nav.get_visible_page()
        if isinstance(visible, DetailPage):
            match = next((s for s in statuses if s.pair.name == visible.pair.name), None)
            if match:
                visible.update(match)

    def _show_setup(self, problems):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        lst = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"])
        for p in problems:
            row = Adw.ActionRow(title=GLib.markup_escape_text(p.title),
                                subtitle=GLib.markup_escape_text(p.detail + (f"\nRun: {p.command}" if p.command else "")),
                                subtitle_selectable=True)
            if p.command:
                cmd = p.command
                row.add_suffix(flat_button("edit-copy-symbolic", "Copy command",
                                           lambda c=cmd: (self.get_clipboard().set(c), self.toast("Copied"))))
            lst.append(row)
        box.append(lst)
        again = Gtk.Button(label="Check Again", halign=Gtk.Align.CENTER, css_classes=["pill"])
        again.connect("clicked", lambda *_: self.refresh(check_setup=True))
        box.append(again)
        self.setup_page.set_description("Gdrive-Pair needs a few things before it can work.")
        self.setup_page.set_child(box)
        self.main_stack.set_visible_child_name("setup")

    # -- actions ------------------------------------------------------------
    def toast(self, text: str):
        self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(text), timeout=4))

    def do_action(self, func, pair, ok_text):
        def done(_r, err):
            self.toast(str(err) if err else ok_text)
            self.refresh()
        run_async(func, done, pair)

    def show_detail(self, name: str):
        row = self.rows.get(name)
        if row:
            self.nav.push(DetailPage(self, row.status))

    def review(self, pair: core.Pair):
        ReviewDialog(self, pair).present(self)

    def retry_first_sync(self, pair: core.Pair):
        self.do_action(core.start_first_sync, pair, f"First sync of {pair.name} started")

    def add_pair(self):
        AddPairDialog(self).present(self)


class GdrivePairApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.window: MainWindow | None = None
        for name, cb in (("refresh", lambda *_: self.window and self.window.refresh(check_setup=True)),
                         ("open-logs", lambda *_: (core.STATE_DIR.mkdir(parents=True, exist_ok=True),
                                                   open_path(core.STATE_DIR))),
                         ("about", lambda *_: self._about())):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", cb)
            self.add_action(action)
        self.set_accels_for_action("app.refresh", ["F5", "<Control>r"])

    def do_startup(self):
        Adw.Application.do_startup(self)
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), provider,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    def do_activate(self):
        if self.window is None:
            self.window = MainWindow(self)
        self.window.present()

    def _about(self):
        Adw.AboutDialog(application_name=APP_NAME, application_icon=APP_ID, version=VERSION,
                        comments="Two-way sync between Google Drive folders and this computer, "
                                 "using rclone bisync and systemd.").present(self.window)


def main() -> int:
    return GdrivePairApp().run(sys.argv)
