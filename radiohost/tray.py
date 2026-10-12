"""System tray icon via StatusNotifierItem + com.canonical.dbusmenu (GTK4 has no tray API).

Works with any SNI host (Quickshell, Waybar, KDE, GNOME with the AppIndicator extension).
Uses theme icon names, not pixmaps.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Callable

from gi.repository import Gio, GLib

log = logging.getLogger(__name__)

ITEM_PATH = "/StatusNotifierItem"
MENU_PATH = "/MenuBar"
ICON_DIR = Path(__file__).resolve().parent.parent / "data" / "icons"
ICONS = {"off": "ai-radio-host-off", "on": "ai-radio-host-on", "live": "ai-radio-host-live"}

SNI_XML = """
<node>
  <interface name="org.kde.StatusNotifierItem">
    <property name="Category" type="s" access="read"/>
    <property name="Id" type="s" access="read"/>
    <property name="Title" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="IconName" type="s" access="read"/>
    <property name="IconThemePath" type="s" access="read"/>
    <property name="AttentionIconName" type="s" access="read"/>
    <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
    <property name="ItemIsMenu" type="b" access="read"/>
    <property name="Menu" type="o" access="read"/>
    <method name="Activate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
    <method name="SecondaryActivate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
    <method name="ContextMenu"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
    <method name="Scroll"><arg name="delta" type="i" direction="in"/><arg name="orientation" type="s" direction="in"/></method>
    <signal name="NewIcon"/>
    <signal name="NewAttentionIcon"/>
    <signal name="NewTitle"/>
    <signal name="NewToolTip"/>
    <signal name="NewStatus"><arg name="status" type="s"/></signal>
  </interface>
</node>
"""

MENU_XML = """
<node>
  <interface name="com.canonical.dbusmenu">
    <property name="Version" type="u" access="read"/>
    <property name="TextDirection" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="IconThemePath" type="as" access="read"/>
    <method name="GetLayout">
      <arg name="parentId" type="i" direction="in"/>
      <arg name="recursionDepth" type="i" direction="in"/>
      <arg name="propertyNames" type="as" direction="in"/>
      <arg name="revision" type="u" direction="out"/>
      <arg name="layout" type="(ia{sv}av)" direction="out"/>
    </method>
    <method name="GetGroupProperties">
      <arg name="ids" type="ai" direction="in"/>
      <arg name="propertyNames" type="as" direction="in"/>
      <arg name="properties" type="a(ia{sv})" direction="out"/>
    </method>
    <method name="GetProperty">
      <arg name="id" type="i" direction="in"/>
      <arg name="name" type="s" direction="in"/>
      <arg name="value" type="v" direction="out"/>
    </method>
    <method name="Event">
      <arg name="id" type="i" direction="in"/>
      <arg name="eventId" type="s" direction="in"/>
      <arg name="data" type="v" direction="in"/>
      <arg name="timestamp" type="u" direction="in"/>
    </method>
    <method name="EventGroup">
      <arg name="events" type="a(isvu)" direction="in"/>
      <arg name="idErrors" type="ai" direction="out"/>
    </method>
    <method name="AboutToShow">
      <arg name="id" type="i" direction="in"/>
      <arg name="needUpdate" type="b" direction="out"/>
    </method>
    <method name="AboutToShowGroup">
      <arg name="ids" type="ai" direction="in"/>
      <arg name="updatesNeeded" type="ai" direction="out"/>
      <arg name="idErrors" type="ai" direction="out"/>
    </method>
    <signal name="ItemsPropertiesUpdated">
      <arg name="updatedProps" type="a(ia{sv})"/>
      <arg name="removedProps" type="a(ias)"/>
    </signal>
    <signal name="LayoutUpdated">
      <arg name="revision" type="u"/>
      <arg name="parent" type="i"/>
    </signal>
  </interface>
</node>
"""

MENU_SHOW, MENU_ONAIR, MENU_BREAK, MENU_SEP, MENU_QUIT = 1, 2, 5, 3, 4
MENU_ORDER = (MENU_SHOW, MENU_ONAIR, MENU_BREAK, MENU_SEP, MENU_QUIT)


class TrayIcon:
    def __init__(self, on_activate: Callable[[], None], on_toggle_air: Callable[[], None],
                 on_break: Callable[[], None], on_quit: Callable[[], None]):
        self.on_activate, self.on_toggle_air, self.on_quit = on_activate, on_toggle_air, on_quit
        self.on_break = on_break
        self.state = "off"
        self.tooltip = "Off air"
        self.revision = 1
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION)
        self.name = f"org.kde.StatusNotifierItem-{os.getpid()}-1"

        sni = Gio.DBusNodeInfo.new_for_xml(SNI_XML).interfaces[0]
        menu = Gio.DBusNodeInfo.new_for_xml(MENU_XML).interfaces[0]
        self._regs = [
            self.bus.register_object(ITEM_PATH, sni, self._sni_call, self._sni_prop, None),
            self.bus.register_object(MENU_PATH, menu, self._menu_call, self._menu_prop, None),
        ]
        self._own = Gio.bus_own_name_on_connection(
            self.bus, self.name, Gio.BusNameOwnerFlags.NONE, None, None)
        # (Re-)register whenever a tray host appears, e.g. after the shell restarts.
        self._watch = Gio.bus_watch_name_on_connection(
            self.bus, "org.kde.StatusNotifierWatcher", Gio.BusNameWatcherFlags.NONE,
            self._register, lambda *a: log.debug("tray host gone"))

    def _register(self, *_args) -> None:
        self.bus.call("org.kde.StatusNotifierWatcher", "/StatusNotifierWatcher",
                      "org.kde.StatusNotifierWatcher", "RegisterStatusNotifierItem",
                      GLib.Variant("(s)", (self.name,)), None, Gio.DBusCallFlags.NONE,
                      -1, None, self._registered)

    def _registered(self, bus, result) -> None:
        try:
            bus.call_finish(result)
            log.debug("tray icon registered")
        except GLib.Error as e:
            log.warning("Could not add tray icon: %s", e.message)

    def close(self) -> None:
        Gio.bus_unwatch_name(self._watch)
        Gio.bus_unown_name(self._own)
        for reg in self._regs:
            self.bus.unregister_object(reg)

    # ---------- state ----------

    def update(self, state: str, tooltip: str) -> None:
        """state: off | on | live"""
        changed_state = state != self.state
        changed_tip = tooltip != self.tooltip
        self.state, self.tooltip = state, tooltip
        if changed_state:
            self._emit("NewIcon")
            self._emit("NewStatus", GLib.Variant("(s)", (self._status(),)))
            self.revision += 1
            self.bus.emit_signal(None, MENU_PATH, "com.canonical.dbusmenu", "ItemsPropertiesUpdated",
                                 GLib.Variant("(a(ia{sv})a(ias))",
                                              ([(i, self._item_props(i)) for i in (MENU_ONAIR, MENU_BREAK)],
                                               [])))
        if changed_tip:
            self._emit("NewToolTip")

    def _status(self) -> str:
        return "NeedsAttention" if self.state == "live" else "Active"

    def _emit(self, signal: str, params: GLib.Variant | None = None) -> None:
        try:
            self.bus.emit_signal(None, ITEM_PATH, "org.kde.StatusNotifierItem", signal, params)
        except GLib.Error as e:
            log.debug("tray signal %s failed: %s", signal, e.message)

    # ---------- StatusNotifierItem ----------

    def _sni_prop(self, _bus, _sender, _path, _iface, prop):
        icon = ICONS[self.state]
        values = {
            "Category": GLib.Variant("s", "ApplicationStatus"),
            "Id": GLib.Variant("s", "ai-radio-host"),
            "Title": GLib.Variant("s", "AI Radio Host"),
            "Status": GLib.Variant("s", self._status()),
            "IconName": GLib.Variant("s", icon),
            "AttentionIconName": GLib.Variant("s", ICONS["live"]),
            "IconThemePath": GLib.Variant("s", str(ICON_DIR)),
            "ToolTip": GLib.Variant("(sa(iiay)ss)", (icon, [], "AI Radio Host", self.tooltip)),
            "ItemIsMenu": GLib.Variant("b", False),
            "Menu": GLib.Variant("o", MENU_PATH),
        }
        return values.get(prop)

    def _sni_call(self, _bus, _sender, _path, _iface, method, _params, invocation):
        if method in ("Activate", "SecondaryActivate"):
            GLib.idle_add(lambda: self.on_activate() and False)
        invocation.return_value(None)

    # ---------- dbusmenu ----------

    def _item_props(self, item: int) -> dict:
        if item == MENU_SHOW:
            return {"label": GLib.Variant("s", "Show window")}
        if item == MENU_ONAIR:
            return {"label": GLib.Variant("s", "On air"),
                    "toggle-type": GLib.Variant("s", "checkmark"),
                    "toggle-state": GLib.Variant("i", 0 if self.state == "off" else 1)}
        if item == MENU_BREAK:
            return {"label": GLib.Variant("s", "Break after this song"),
                    "enabled": GLib.Variant("b", self.state != "off")}
        if item == MENU_SEP:
            return {"type": GLib.Variant("s", "separator")}
        if item == MENU_QUIT:
            return {"label": GLib.Variant("s", "Quit")}
        return {"children-display": GLib.Variant("s", "submenu")}

    def _layout(self) -> tuple:
        children = [GLib.Variant("(ia{sv}av)", (i, self._item_props(i), []))
                    for i in MENU_ORDER]
        return (0, self._item_props(0), children)

    def _menu_prop(self, _bus, _sender, _path, _iface, prop):
        return {
            "Version": GLib.Variant("u", 3),
            "TextDirection": GLib.Variant("s", "ltr"),
            "Status": GLib.Variant("s", "normal"),
            "IconThemePath": GLib.Variant("as", [str(ICON_DIR)]),
        }.get(prop)

    def _menu_call(self, _bus, _sender, _path, _iface, method, params, invocation):
        if method == "GetLayout":
            invocation.return_value(GLib.Variant("(u(ia{sv}av))", (self.revision, self._layout())))
        elif method == "GetGroupProperties":
            ids = params.unpack()[0] or list(MENU_ORDER)
            invocation.return_value(GLib.Variant(
                "(a(ia{sv}))", ([(i, self._item_props(i)) for i in ids],)))
        elif method == "GetProperty":
            item, name = params.unpack()
            value = self._item_props(item).get(name, GLib.Variant("s", ""))
            invocation.return_value(GLib.Variant("(v)", (value,)))
        elif method == "Event":
            item, event = params.unpack()[:2]
            if event == "clicked":
                self._clicked(item)
            invocation.return_value(None)
        elif method == "EventGroup":
            for item, event, _data, _ts in params.unpack()[0]:
                if event == "clicked":
                    self._clicked(item)
            invocation.return_value(GLib.Variant("(ai)", ([],)))
        elif method == "AboutToShow":
            invocation.return_value(GLib.Variant("(b)", (False,)))
        elif method == "AboutToShowGroup":
            invocation.return_value(GLib.Variant("(aiai)", ([], [])))
        else:
            invocation.return_value(None)

    def _clicked(self, item: int) -> None:
        action = {MENU_SHOW: self.on_activate, MENU_ONAIR: self.on_toggle_air,
                  MENU_BREAK: self.on_break, MENU_QUIT: self.on_quit}.get(item)
        if action:
            GLib.idle_add(lambda: action() and False)
