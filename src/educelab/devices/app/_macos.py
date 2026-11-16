"""What `el-devices preview` needs from macOS that OpenCV's window lacks.

Called through the Objective-C runtime with ctypes, so nothing extra is
installed. Everything here is best effort: on failure, or on another
platform, the functions do nothing.
"""

import ctypes
import ctypes.util
import functools
import logging
import platform
import sys
from typing import Any

logger = logging.getLogger(__name__)

# Kept alive for as long as AppKit may call it.
_callbacks: list[Any] = []


class _Rect(ctypes.Structure):
    _fields_ = [(name, ctypes.c_double) for name in ("x", "y", "width", "height")]


def _runtime() -> Any:
    objc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc") or "")
    # The classes exist only once AppKit is loaded.
    ctypes.cdll.LoadLibrary(ctypes.util.find_library("AppKit") or "")
    objc.objc_getClass.restype = ctypes.c_void_p
    objc.objc_getClass.argtypes = [ctypes.c_char_p]
    objc.sel_registerName.restype = ctypes.c_void_p
    objc.sel_registerName.argtypes = [ctypes.c_char_p]
    objc.class_getInstanceMethod.restype = ctypes.c_void_p
    objc.class_getInstanceMethod.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    objc.method_setImplementation.restype = ctypes.c_void_p
    objc.method_setImplementation.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    return objc


def _send(
    objc: Any,
    result: Any,
    target: Any,
    selector: bytes,
    *args: Any,
    types: tuple[Any, ...] = (),
    function: str = "objc_msgSend",
) -> Any:
    prototype = ctypes.CFUNCTYPE(result, ctypes.c_void_p, ctypes.c_void_p, *types)
    return prototype((function, objc))(target, objc.sel_registerName(selector), *args)


def _guarded(function: Any) -> Any:
    @functools.wraps(function)
    def call() -> Any:
        if sys.platform != "darwin":
            return None
        try:
            return function(_runtime())
        except (OSError, AttributeError, ValueError) as exc:
            logger.debug("%s failed: %s", function.__name__, exc)
            return None

    return call


@_guarded
def accept_first_clicks(objc: Any) -> None:
    """Deliver a press on a window that isn't the key window.

    macOS withholds it otherwise, with the drag that follows, and a window
    opened from a terminal often never becomes key: its title bar stays grey
    and every drag needs a click first.
    """
    accepts = ctypes.CFUNCTYPE(
        ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p
    )(lambda view, selector, event: True)
    _callbacks.append(accepts)
    method = objc.class_getInstanceMethod(
        objc.objc_getClass(b"NSView"), objc.sel_registerName(b"acceptsFirstMouse:")
    )
    objc.method_setImplementation(method, ctypes.cast(accepts, ctypes.c_void_p))


@_guarded
def activate(objc: Any) -> None:
    """Bring the application to the front, as far as macOS allows."""
    pointer = ctypes.c_void_p
    application = _send(
        objc, pointer, objc.objc_getClass(b"NSApplication"), b"sharedApplication"
    )
    _send(
        objc,
        None,
        application,
        b"activateIgnoringOtherApps:",
        True,
        types=(ctypes.c_bool,),
    )


@_guarded
def visible_screen_size(objc: Any) -> tuple[int, int] | None:
    """The main screen's size in points, without the menu bar and the Dock."""
    screen = _send(
        objc, ctypes.c_void_p, objc.objc_getClass(b"NSScreen"), b"mainScreen"
    )
    if not screen:
        return None
    # Intel returns a struct this large through a separate entry point.
    function = (
        "objc_msgSend_stret" if platform.machine() == "x86_64" else "objc_msgSend"
    )
    frame = _send(objc, _Rect, screen, b"visibleFrame", function=function)
    return int(frame.width), int(frame.height)
