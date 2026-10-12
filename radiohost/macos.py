"""macOS-only helpers. GTK4 has no signal for "the app was brought to the front" (Cmd+Tab),
so this asks AppKit directly through the Objective-C runtime (no PyObjC needed)."""

from __future__ import annotations

import ctypes

_objc = ctypes.cdll.LoadLibrary("/usr/lib/libobjc.A.dylib")
_objc.objc_getClass.restype = ctypes.c_void_p
_objc.objc_getClass.argtypes = [ctypes.c_char_p]
_objc.sel_registerName.restype = ctypes.c_void_p
_objc.sel_registerName.argtypes = [ctypes.c_char_p]
_send = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)(
    ("objc_msgSend", _objc))


_send_arg = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)(
    ("objc_msgSend", _objc))


def _nsapp() -> int | None:
    return _send(_objc.objc_getClass(b"NSApplication"), _objc.sel_registerName(b"sharedApplication"))


def app_is_active() -> bool:
    """True while this app is the frontmost one."""
    nsapp = _nsapp()
    return bool(nsapp) and bool(_send(nsapp, _objc.sel_registerName(b"isActive")))


def hide_app() -> None:
    """Hide the whole app, like Cmd+H. Hiding the window itself would make GTK ask macOS to
    quit (it terminates after the last window closes); a hidden app keeps running, and
    Cmd+Tab or the Dock shows it again."""
    nsapp = _nsapp()
    if nsapp:
        _send_arg(nsapp, _objc.sel_registerName(b"hide:"), None)
