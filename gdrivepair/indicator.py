"""Gdrive-Pair tray indicator.

Implements a StatusNotifierItem with a com.canonical.dbusmenu menu directly
over D-Bus (Gio), so it needs no GTK3/libappindicator. On GNOME the icon
appears when the "AppIndicator and KStatusNotifierItem Support" extension is
enabled. It also holds a logout inhibitor while a first sync is running.

Runs as gdrive-pair-indicator.service (systemd user unit).
"""

from __future__ import annotations

import os
import subprocess
import sys

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

from . import APP_ID, APP_NAME, core  # noqa: E402
from .core import State  # noqa: E402

POLL_SECS = 3
ICON_DIR = str(core.HOME / ".local/share/icons")

SNI_PATH = "/StatusNotifierItem"
MENU_PATH = "/MenuBar"
WATCHER = "org.kde.StatusNotifierWatcher"

SNI_XML = """
<node><interface name="org.kde.StatusNotifierItem">
  <property name="Category" type="s" access="read"/>
  <property name="Id" type="s" access="read"/>
  <property name="Title" type="s" access="read"/>
  <property name="Status" type="s" access="read"/>
  <property name="WindowId" type="i" access="read"/>
  <property name="IconName" type="s" access="read"/>
  <property name="IconThemePath" type="s" access="read"/>
  <property name="OverlayIconName" type="s" access="read"/>
  <property name="AttentionIconName" type="s" access="read"/>
  <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
  <property name="ItemIsMenu" type="b" access="read"/>
  <property name="Menu" type="o" access="read"/>
  <method name="ContextMenu"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
  <method name="Activate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
  <method name="SecondaryActivate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
  <method name="Scroll"><arg name="delta" type="i" direction="in"/><arg name="orientation" type="s" direction="in"/></method>
  <signal name="NewTitle"/>
  <signal name="NewIcon"/>
  <signal name="NewAttentionIcon"/>
  <signal name="NewOverlayIcon"/>
  <signal name="NewToolTip"/>
  <signal name="NewStatus"><arg name="status" type="s"/></signal>
</interface></node>
"""

MENU_XML = """
<node><interface name="com.canonical.dbusmenu">
  <property name="Version" type="u" access="read"/>
  <property name="TextDirection" type="s" access="read"/>
  <property name="Status" type="s" access="read"/>
  <property name="IconThemePath" type="as" access="read"/>
  <method name="GetLayout">
    <arg type="i" name="parentId" direction="in"/>
    <arg type="i" name="recursionDepth" direction="in"/>
    <arg type="as" name="propertyNames" direction="in"/>
    <arg type="u" name="revision" direction="out"/>
    <arg type="(ia{sv}av)" name="layout" direction="out"/>
  </method>
  <method name="GetGroupProperties">
    <arg type="ai" name="ids" direction="in"/>
    <arg type="as" name="propertyNames" direction="in"/>
    <arg type="a(ia{sv})" name="properties" direction="out"/>
  </method>
  <method name="GetProperty">
    <arg type="i" name="id" direction="in"/>
    <arg type="s" name="name" direction="in"/>
    <arg type="v" name="value" direction="out"/>
  </method>
  <method name="Event">
    <arg type="i" name="id" direction="in"/>
    <arg type="s" name="eventId" direction="in"/>
    <arg type="v" name="data" direction="in"/>
    <arg type="u" name="timestamp" direction="in"/>
  </method>
  <method name="EventGroup">
    <arg type="a(isvu)" name="events" direction="in"/>
    <arg type="ai" name="idErrors" direction="out"/>
  </method>
  <method name="AboutToShow">
    <arg type="i" name="id" direction="in"/>
    <arg type="b" name="needUpdate" direction="out"/>
  </method>
  <method name="AboutToShowGroup">
    <arg type="ai" name="ids" direction="in"/>
    <arg type="ai" name="updatesNeeded" direction="out"/>
    <arg type="ai" name="idErrors" direction="out"/>
  </method>
  <signal name="ItemsPropertiesUpdated">
    <arg type="a(ia{sv})" name="updatedProps"/>
    <arg type="a(ias)" name="removedProps"/>
  </signal>
  <signal name="LayoutUpdated">
    <arg type="u" name="revision"/>
    <arg type="i" name="parent"/>
  </signal>
  <signal name="ItemActivationRequested">
    <arg type="i" name="id"/>
    <arg type="u" name="timestamp"/>
  </signal>
</interface></node>
"""

# menu item ids
ID_SYNC_ALL, ID_PAUSE_ALL, ID_OPEN = 1, 2, 3
ID_SEP1, ID_SEP2 = 10, 11
ID_PAIR_BASE = 100

ICONS = {
    "ok": "gdrive-pair-ok-symbolic",
    "syncing": "gdrive-pair-syncing-symbolic",
    "paused": "gdrive-pair-paused-symbolic",
    "error": "gdrive-pair-error-symbolic",
}


class Indicator:
    def __init__(self):
        self.loop = GLib.MainLoop()
        self.conn: Gio.DBusConnection | None = None
        self.bus_name = f"org.kde.StatusNotifierItem-{os.getpid()}-1"
        self.icon = ICONS["ok"]
        self.tooltip = APP_NAME
        self.revision = 1
        self.items: list[tuple[int, dict]] = []   # menu children of root: (id, props)
        self.statuses: list[core.PairStatus] = []
        self.inhibit_cookie: int | None = None
        self.inhibit_reason = ""

        self.sni_info = Gio.DBusNodeInfo.new_for_xml(SNI_XML).interfaces[0]
        self.menu_info = Gio.DBusNodeInfo.new_for_xml(MENU_XML).interfaces[0]

    # -- D-Bus setup ----------------------------------------------------------
    def run(self):
        Gio.bus_own_name(Gio.BusType.SESSION, self.bus_name, Gio.BusNameOwnerFlags.NONE,
                         self._on_bus, self._on_name, lambda *_: self.loop.quit())
        self.loop.run()

    def _on_bus(self, conn, _name):
        self.conn = conn
        conn.register_object(SNI_PATH, self.sni_info, self._sni_call, self._sni_prop, None)
        conn.register_object(MENU_PATH, self.menu_info, self._menu_call, self._menu_prop, None)

    def _on_name(self, _conn, _name):
        self.update()
        Gio.bus_watch_name(Gio.BusType.SESSION, WATCHER, Gio.BusNameWatcherFlags.NONE,
                           lambda *_: self._register(), None)
        GLib.timeout_add_seconds(POLL_SECS, self._tick)

    def _register(self):
        self.conn.call(WATCHER, "/StatusNotifierWatcher", WATCHER, "RegisterStatusNotifierItem",
                       GLib.Variant("(s)", (self.bus_name,)), None, Gio.DBusCallFlags.NONE, -1, None,
                       lambda c, r: self._finish(c, r, "register with tray"))

    @staticmethod
    def _finish(conn, result, what):
        try:
            conn.call_finish(result)
        except GLib.Error as e:
            print(f"Could not {what}: {e.message}", file=sys.stderr)

    def _emit(self, path, iface, signal, params=None):
        if self.conn:
            self.conn.emit_signal(None, path, iface, signal, params)

    # -- StatusNotifierItem ---------------------------------------------------
    def _sni_prop(self, _c, _s, _p, _i, prop):
        values = {
            "Category": GLib.Variant("s", "ApplicationStatus"),
            "Id": GLib.Variant("s", "gdrive-pair"),
            "Title": GLib.Variant("s", APP_NAME),
            "Status": GLib.Variant("s", "Active"),
            "WindowId": GLib.Variant("i", 0),
            "IconName": GLib.Variant("s", self.icon),
            "IconThemePath": GLib.Variant("s", ICON_DIR),
            "OverlayIconName": GLib.Variant("s", ""),
            "AttentionIconName": GLib.Variant("s", ""),
            "ToolTip": GLib.Variant("(sa(iiay)ss)", (self.icon, [], APP_NAME, self.tooltip)),
            "ItemIsMenu": GLib.Variant("b", True),
            "Menu": GLib.Variant("o", MENU_PATH),
        }
        return values.get(prop)

    def _sni_call(self, _c, _s, _p, _i, method, _params, invocation):
        if method in ("Activate", "SecondaryActivate"):
            self.open_app()
        invocation.return_value(None)

    # -- dbusmenu ---------------------------------------------------------------
    def _menu_prop(self, _c, _s, _p, _i, prop):
        return {
            "Version": GLib.Variant("u", 3),
            "TextDirection": GLib.Variant("s", "ltr"),
            "Status": GLib.Variant("s", "normal"),
            "IconThemePath": GLib.Variant("as", [ICON_DIR]),
        }.get(prop)

    @staticmethod
    def _props_variant(props: dict) -> dict:
        out = {}
        for k, v in props.items():
            if isinstance(v, bool):
                out[k] = GLib.Variant("b", v)
            elif isinstance(v, int):
                out[k] = GLib.Variant("i", v)
            else:
                out[k] = GLib.Variant("s", v)
        return out

    def _item_props(self, item_id: int) -> dict:
        if item_id == 0:
            return {"children-display": "submenu"}
        return next((p for i, p in self.items if i == item_id), {})

    def _layout(self):
        children = [GLib.Variant("(ia{sv}av)", (i, self._props_variant(p), [])) for i, p in self.items]
        return (0, self._props_variant(self._item_props(0)), children)

    def _menu_call(self, _c, _s, _p, _i, method, params, invocation):
        if method == "GetLayout":
            invocation.return_value(GLib.Variant("(u(ia{sv}av))", (self.revision, self._layout())))
        elif method == "GetGroupProperties":
            ids = params.unpack()[0] or [0] + [i for i, _ in self.items]
            invocation.return_value(GLib.Variant(
                "(a(ia{sv}))", ([(i, self._props_variant(self._item_props(i))) for i in ids],)))
        elif method == "GetProperty":
            item_id, name = params.unpack()
            props = self._props_variant(self._item_props(item_id))
            invocation.return_value(GLib.Variant("(v)", (props.get(name, GLib.Variant("s", "")),)))
        elif method == "Event":
            item_id, event, _data, _ts = params.unpack()
            if event == "clicked":
                GLib.idle_add(self._clicked, item_id)
            invocation.return_value(None)
        elif method == "EventGroup":
            for item_id, event, _data, _ts in params.unpack()[0]:
                if event == "clicked":
                    GLib.idle_add(self._clicked, item_id)
            invocation.return_value(GLib.Variant("(ai)", ([],)))
        elif method == "AboutToShow":
            self.update()
            invocation.return_value(GLib.Variant("(b)", (False,)))
        elif method == "AboutToShowGroup":
            invocation.return_value(GLib.Variant("(aiai)", ([], [])))
        else:
            invocation.return_value(None)

    # -- state ----------------------------------------------------------------
    def _tick(self):
        self.update()
        return True

    def update(self):
        try:
            self.statuses = core.get_statuses()
        except Exception as e:  # noqa: BLE001 - keep the tray alive
            print(f"status failed: {e}", file=sys.stderr)
            return
        sts = self.statuses
        states = {s.state for s in sts}
        ready = [s for s in sts if s.state not in (State.FIRST_SYNC, State.NEEDS_FIRST_SYNC)]

        first_sync_failed = any(s.state == State.NEEDS_FIRST_SYNC and s.last and s.last.result != "ok"
                                for s in sts)
        if states & {State.BLOCKED, State.ERROR} or first_sync_failed:
            icon = ICONS["error"]
        elif states & {State.SYNCING, State.FIRST_SYNC}:
            icon = ICONS["syncing"]
        elif sts and all(s.paused for s in ready) and ready:
            icon = ICONS["paused"]
        else:
            icon = ICONS["ok"]

        if not sts:
            tooltip = "No synced folders"
        else:
            tooltip = "\n".join(f"{s.pair.name}: {s.text}" for s in sts)

        items: list[tuple[int, dict]] = []
        if not sts:
            items.append((ID_PAIR_BASE, {"label": "No synced folders", "enabled": False}))
        for n, s in enumerate(sts):
            text = s.text if len(s.text) <= 60 else s.text[:57] + "…"
            items.append((ID_PAIR_BASE + n, {"label": f"{s.pair.name} · {text}", "enabled": False}))
        items.append((ID_SEP1, {"type": "separator"}))
        can_sync = [s for s in ready if not s.paused and not s.syncing]
        items.append((ID_SYNC_ALL, {"label": "Sync All Now", "enabled": bool(can_sync)}))
        all_paused = bool(ready) and all(s.paused for s in ready)
        items.append((ID_PAUSE_ALL, {"label": "Resume All" if all_paused else "Pause All",
                                     "enabled": bool(ready)}))
        items.append((ID_SEP2, {"type": "separator"}))
        items.append((ID_OPEN, {"label": f"Open {APP_NAME}"}))

        if icon != self.icon:
            self.icon = icon
            self._emit(SNI_PATH, "org.kde.StatusNotifierItem", "NewIcon")
        if tooltip != self.tooltip:
            self.tooltip = tooltip
            self._emit(SNI_PATH, "org.kde.StatusNotifierItem", "NewToolTip")
        if items != self.items:
            self.items = items
            self.revision += 1
            self._emit(MENU_PATH, "com.canonical.dbusmenu", "LayoutUpdated",
                       GLib.Variant("(ui)", (self.revision, 0)))

        first = [s.pair.name for s in sts if s.state == State.FIRST_SYNC]
        self._set_inhibit(f"First sync of {', '.join(first)} in progress. Logging out will stop it."
                          if first else "")

    # -- logout inhibitor -----------------------------------------------------
    def _set_inhibit(self, reason: str):
        if reason == self.inhibit_reason:
            return
        if self.inhibit_cookie is not None:
            try:
                self.conn.call_sync("org.gnome.SessionManager", "/org/gnome/SessionManager",
                                    "org.gnome.SessionManager", "Uninhibit",
                                    GLib.Variant("(u)", (self.inhibit_cookie,)), None,
                                    Gio.DBusCallFlags.NONE, 2000, None)
            except GLib.Error:
                pass
            self.inhibit_cookie = None
        self.inhibit_reason = reason
        if reason:
            try:
                r = self.conn.call_sync("org.gnome.SessionManager", "/org/gnome/SessionManager",
                                        "org.gnome.SessionManager", "Inhibit",
                                        GLib.Variant("(susu)", (APP_ID, 0, reason, 1)),  # 1 = logout
                                        GLib.VariantType("(u)"), Gio.DBusCallFlags.NONE, 2000, None)
                self.inhibit_cookie = r.unpack()[0]
            except GLib.Error as e:
                print(f"Could not inhibit logout: {e.message}", file=sys.stderr)

    # -- actions --------------------------------------------------------------
    def _clicked(self, item_id: int):
        ready = [s for s in self.statuses if s.state not in (State.FIRST_SYNC, State.NEEDS_FIRST_SYNC)]
        try:
            if item_id == ID_OPEN or item_id >= ID_PAIR_BASE:
                self.open_app()
            elif item_id == ID_SYNC_ALL:
                for s in ready:
                    if not s.paused and not s.syncing:
                        core.sync_now(s.pair)
            elif item_id == ID_PAUSE_ALL:
                if ready and all(s.paused for s in ready):
                    for s in ready:
                        core.resume(s.pair)
                else:
                    for s in ready:
                        if not s.paused:
                            core.pause(s.pair)
        except core.ActionError as e:
            print(e, file=sys.stderr)
        self.update()
        return False

    def open_app(self):
        info = Gio.DesktopAppInfo.new(f"{APP_ID}.desktop")
        try:
            if info and info.launch([], None):
                return
        except GLib.Error:
            pass
        subprocess.Popen([str(core.BIN_DIR / "gdrive-pair")], start_new_session=True)


def main() -> int:
    Indicator().run()
    return 0
