#!/usr/bin/env python3
"""
Linux UI Element Scanner for Auto-Use.

Reads the on-screen UI through AT-SPI2 (the desktop accessibility bus) via
PyGObject, and captures the screen through the XDG desktop portal, so it works
on both Wayland (GNOME) and X11.

The agent drives one scanner per platform behind a shared interface, which
this class implements:
    - UIElementScanner(config, frontend_callback=None)
    - scanner.scan_elements()
    - scanner.get_scan_data()        → (element_tree_text, annotated_image_base64, uac_detected)
    - scanner.get_elements_mapping() → dict
    - scanner.application_name       → str
    - scanner.print_summary()
    - scanner.save_to_file()

Standalone usage — the full scan, or one pipeline step at a time:
    python3 element.py              full scan (5s countdown → screenshot → annotate)
    python3 element.py topmost      step 1: topmost-application detection only
    python3 element.py screenshot   step 2: screen capture only (saves element_screenshot.png)
    python3 element.py walk         step 3: element walk only (prints what was found)

Requires: python3-gi (Atspi 2.0, Gdk 3.0) and Pillow — see
linux_requirements.txt. There is no OCR pass: AT-SPI already exposes text
that would otherwise have to be recognised from pixels.
"""

import os
import io
import re
import sys
import time
import base64
import secrets
import threading
import subprocess
from collections import namedtuple, Counter
from urllib.parse import urlparse, unquote

from PIL import Image, ImageDraw, ImageFont


def _add_system_gi_to_path():
    """Put the system PyGObject on sys.path. Returns True if one was found.

    gi cannot come from pip: it binds to the AT-SPI and GTK typelibs, which
    are introspection data under /usr/lib/<triplet>/girepository-1.0/ rather
    than Python, so the pip half alone still cannot import Atspi. That makes
    gi a system package, and a venv built without --system-site-packages
    cannot see it. Locating it here keeps that off the user's plate — nobody
    should have to hand-symlink a package to launch the app.

    Only a directory whose compiled _gi extension carries THIS interpreter's
    ABI tag is accepted, so a 3.12 build can never be loaded into 3.14. The
    path is APPENDED, so anything already installed in the venv still wins.
    """
    import glob
    tag = f"{sys.version_info.major}{sys.version_info.minor}"
    ver = f"{sys.version_info.major}.{sys.version_info.minor}"
    for base in (f"/usr/lib/python3/dist-packages",          # Debian, Ubuntu
                 f"/usr/lib/python{ver}/site-packages",      # Arch, Fedora
                 f"/usr/lib64/python{ver}/site-packages"):   # Fedora (64-bit)
        if base not in sys.path and glob.glob(
                os.path.join(base, "gi", f"_gi.cpython-{tag}-*.so")):
            sys.path.append(base)
            return True
    return False


try:
    import gi
except ImportError:
    if not _add_system_gi_to_path():
        raise
    import gi

gi.require_version('Atspi', '2.0')
gi.require_version('Gdk', '3.0')
from gi.repository import Atspi, Gdk, Gio, GLib


# ========== CONFIGURATION ==========
# Toggle switches — what a scan produces besides the element tree.
SCREENSHOT = True    # Set to False to only generate element tree without screenshot
DEBUG = True         # Set to True to save files to debug folders, False for direct LLM only
FRONTEND = True      # Set to True when running from app.py to send images to frontend

# Define Rect namedtuple matching Windows format (left, top, right, bottom)
Rect = namedtuple('Rect', ['left', 'top', 'right', 'bottom'])

# One colour for every element, so the boxes read as a single overlay.
BOX_COLOR = (255, 0, 255)     # Bright magenta for all boxes
NUMBER_COLOR = (255, 0, 255)  # Same magenta for numbers
# Outline width in DELIVERED pixels (boxes are drawn after the downscale, so
# this is literal). A dense tree nests boxes only a few pixels apart, and at
# 2px adjacent borders merge into magenta slabs that bury the UI underneath —
# the box is meant to delimit an element, not fill it.
BOX_WIDTH = 1

# Final geometry/encoding of the annotated screenshot.
# The image is encoded ONCE, here; those exact bytes are the LLM payload and
# what DEBUG writes to disk. Callers must NOT re-encode.
LLM_IMAGE_MAX_EDGE = 2300
LLM_IMAGE_MAX_PIXELS = 3_300_000
LLM_IMAGE_FORMAT = "PNG"          # lossless — annotations are thin, saturated detail
LLM_IMAGE_COMPRESS_LEVEL = 1      # PNG is lossless at every level; 1 encodes fastest
LLM_IMAGE_MEDIA_TYPE = "image/png"

# Index-label styling, in delivered pixels (labels are drawn AFTER the downscale).
LABEL_FONT_SIZE = 13
LABEL_STROKE = 2
LABEL_STROKE_COLOR = (0, 0, 0)

# The plain screenshot mirrored to the frontend is a human-facing preview.
FRONTEND_IMAGE_MAX_DIMENSION = 1920
FRONTEND_IMAGE_QUALITY = 100

MAX_DEPTH = 30
MAX_NODES = 20_000        # hard cap on AT-SPI nodes visited per scan (runaway guard)
MAX_CHILDREN = 500        # per-node child cap

# Electron/Chromium apps publish their AT-SPI tree only when a screen reader is
# "present", which org.a11y.Status.ScreenReaderEnabled announces. The tree then
# populates lazily, so after flipping the flag we poll until it stops growing.
A11Y_TREE_READY_TIMEOUT = 6.0
A11Y_TREE_READY_INTERVAL = 0.4
MENU_STRIP_BOTTOM = 40    # shell items above this y are "menu bar" (top bar)

# Runtime Electron/Chromium tree activation (see electron_nudge): when the
# active window's tree is empty, pulse the screen-reader flag until the app
# starts building its tree, then revert. Universal — no launch flags or
# per-app settings required. Set False to disable the pulse; then sparse
# Electron apps need --force-renderer-accessibility (or, for VS Code family,
# the "editor.accessibilitySupport": "on" setting).
ELECTRON_NUDGE = True
NUDGE_TIMEOUT = 5.0

# Stop the walk at a node that is not SHOWING, skipping its whole subtree —
# ~70% of a real Electron tree, and the single biggest cost in a scan. See
# walk() for why this cannot lose an element. Escape hatch for a toolkit that
# reports SHOWING wrongly; leave it on.
#
# NOTE: Atspi.Accessible.set_cache_mask was tried here and removed. It makes
# a REPEAT read of the same node's role/state/name free, but a walk touches
# each node exactly once, so there is no repeat to serve — measured 6.28s
# without it vs 6.28s with it on a 2900-node tree. Caching across scans would
# help even less: SHOWING is what the prune below depends on, and a stale
# SHOWING would silently amputate the tree.
PRUNE_HIDDEN = True


# ========== ELEMENT CONFIG (AT-SPI role names) ==========
# Keyed by Atspi.Accessible.get_role_name() strings. `is_enabled_flag` roles
# are skipped when they report not-ENABLED; `fallback` is the label search
# order ("_text" reads the Text interface contents).
ELEMENT_CONFIG = {
    "push button":      {"track": True, "is_enabled_flag": True,  "fallback": ["name", "description", "_text"]},
    "button":           {"track": True, "is_enabled_flag": True,  "fallback": ["name", "description", "_text"]},
    "toggle button":    {"track": True, "is_enabled_flag": True,  "fallback": ["name", "description", "_text"]},
    "check box":        {"track": True, "is_enabled_flag": True,  "fallback": ["name", "description", "_text"]},
    "radio button":     {"track": True, "is_enabled_flag": True,  "fallback": ["name", "description", "_text"]},
    "menu item":        {"track": True, "is_enabled_flag": True,  "fallback": ["name", "description", "_text"]},
    "check menu item":  {"track": True, "is_enabled_flag": True,  "fallback": ["name", "description"]},
    "radio menu item":  {"track": True, "is_enabled_flag": True,  "fallback": ["name", "description"]},
    "menu":             {"track": True, "is_enabled_flag": True,  "fallback": ["name"]},
    "page tab":         {"track": True, "is_enabled_flag": True,  "fallback": ["name", "description", "_text"]},
    "link":             {"track": True, "is_enabled_flag": True,  "fallback": ["name", "description", "_text"]},
    "entry":            {"track": True, "is_enabled_flag": True,  "fallback": ["name", "description", "_text"]},
    "password text":    {"track": True, "is_enabled_flag": True,  "fallback": ["name", "description"]},
    "text":             {"track": True, "is_enabled_flag": True,  "fallback": ["name", "description"]},
    "combo box":        {"track": True, "is_enabled_flag": True,  "fallback": ["name", "description", "_text"]},
    "spin button":      {"track": True, "is_enabled_flag": True,  "fallback": ["name", "description"]},
    "slider":           {"track": True, "is_enabled_flag": True,  "fallback": ["name", "description"]},
    "list item":        {"track": True, "is_enabled_flag": True,  "fallback": ["name", "_text", "description"]},
    "tree item":        {"track": True, "is_enabled_flag": True,  "fallback": ["name", "_text", "description"]},
    "table cell":       {"track": True, "is_enabled_flag": True,  "fallback": ["name", "_text", "description"]},
    "icon":             {"track": True, "is_enabled_flag": True,  "fallback": ["name", "description"]},
    # ATK's "object that fills up space" — pure layout, and in GTK3 every
    # GtkBox reports it (measured: 67 of accerciser's 567 nodes, none named).
    # It is tracked anyway for one reason: GNOME's Desktop Icons extension
    # builds each desktop tile from a GtkBox, so the tiles ARE fillers, and
    # without this the desktop reaches the tree only as its 38x19 text labels
    # and never as the 128x111 target you actually click.
    #
    # "name" alone, like "menu", the other pure container here. A filler has
    # no description (measured: none of the 67, nor the desktop tiles), and
    # "_text" would be worse than useless — a container implements no Text
    # interface, so it costs a failed D-Bus round-trip to return "" on
    # essentially every filler in existence. Unnamed ones are real layout and
    # build_label drops them.
    #
    # is_enabled_flag False, matching the convention the other non-interactive
    # roles use: ENABLED on a layout box is toolkit noise, not signal.
    "filler":           {"track": True, "is_enabled_flag": False, "fallback": ["name"]},
    "image":            {"track": True, "is_enabled_flag": True,  "fallback": ["name", "description"]},
    "label":            {"track": True, "is_enabled_flag": False, "fallback": ["name", "_text"]},
    "static":           {"track": True, "is_enabled_flag": False, "fallback": ["name", "_text"]},
    "heading":          {"track": True, "is_enabled_flag": False, "fallback": ["name", "_text"]},
}

# AT-SPI role name → the CamelCase type printed in the tree text. The agent
# reads the same vocabulary on every platform, so the toolkit-specific role
# names are normalised into it here rather than leaked through.
TYPE_MAP = {
    "push button": "Button", "button": "Button", "toggle button": "ToggleButton",
    "check box": "CheckBox", "radio button": "RadioButton",
    "menu item": "MenuItem", "check menu item": "CheckMenuItem",
    "radio menu item": "RadioMenuItem", "menu": "Menu", "page tab": "Tab",
    "link": "Link", "entry": "TextField", "password text": "PasswordField",
    "text": "TextArea", "combo box": "ComboBox", "spin button": "SpinButton",
    "slider": "Slider", "list item": "ListItem", "tree item": "TreeItem",
    "table cell": "Cell", "icon": "Icon", "image": "Image",
    "label": "StaticText", "static": "StaticText", "heading": "Heading",
    # "Group", not "Filler": this map exists to speak the vocabulary the agent
    # already understands, and "Filler" is an ATK implementation detail that
    # means nothing to it. A filler groups things, so it is a Group.
    "filler": "Group",
}

def clean_type(role_str):
    return TYPE_MAP.get(role_str, role_str.title().replace(" ", ""))

# Roles that clip their children's visible area to their own viewport.
CLIP_ROLES = frozenset({"scroll pane", "viewport"})

# Value-bearing single-line controls whose current text is captured, so the
# agent can read a URL bar or a filled-in field. "text" (multi-line) is
# deliberately excluded so editors don't dump whole documents into the tree.
VALUE_ROLES = frozenset({"entry", "combo box", "spin button"})

GENERIC_LABELS = frozenset({
    "", "group", "application", "image", "icon", "text", "button", "cell",
    "row", "tab", "label", "panel", "frame", "radio button", "check box",
    "menu item", "list item",
})

# Roles collected from the GNOME Shell chrome walk (top bar, dash/dock).
SHELL_TRACK_ROLES = frozenset({
    "label", "push button", "button", "toggle button", "menu item",
    "check box", "icon",
})

_IS_WAYLAND = (os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland"
               or bool(os.environ.get("WAYLAND_DISPLAY")))


# ========== AT-SPI HELPERS ==========

_STATE = Atspi.StateType

def acc_states(acc):
    try:
        return acc.get_state_set()
    except Exception:
        return None


def acc_extents(acc, offset=(0, 0)):
    """Return {x, y, width, height} in (offset-corrected) screen coords, or None."""
    try:
        ext = acc.get_extents(Atspi.CoordType.SCREEN)
    except Exception:
        return None
    if ext.width <= 0 or ext.height <= 0:
        return None
    return {"x": ext.x + offset[0], "y": ext.y + offset[1],
            "width": ext.width, "height": ext.height}


def acc_text(acc, limit=120):
    """Read the Text interface contents (single line, capped), or "".

    Calls the Atspi.Text interface methods UNBOUND: acc.get_text() resolves to
    the deprecated Accessible.get_text (an interface getter that takes no
    offsets), so bound calls with offsets fail."""
    try:
        n = Atspi.Text.get_character_count(acc)
        if n and n > 0:
            s = Atspi.Text.get_text(acc, 0, min(n, limit))
            return (s or "").replace("\n", " ").strip()
    except Exception:
        pass
    return ""


def _clean_label(s):
    """Collapse whitespace and drop Unicode private-use icon glyphs (Chromium
    exposes icon fonts as PUA codepoints, which look like empty labels)."""
    s = "".join(ch for ch in s if not ('\ue000' <= ch <= '\uf8ff')
                and not ('\U000f0000' <= ch <= '\U0010fffd'))
    return " ".join(s.split())


def iter_children(acc):
    try:
        count = min(acc.get_child_count(), MAX_CHILDREN)
    except Exception:
        return
    for i in range(count):
        try:
            child = acc.get_child_at_index(i)
        except Exception:
            continue
        if child is not None:
            yield child


def build_label(acc, cfg, name=None, description=None):
    """Try each fallback source, return first non-empty, non-generic string."""
    for attr in cfg.get("fallback", []):
        if attr == "name":
            val = name if name is not None else _safe_name(acc)
        elif attr == "description":
            val = description if description is not None else _safe_desc(acc)
        elif attr == "_text":
            val = acc_text(acc)
        else:
            val = ""
        if val:
            label = _clean_label(str(val))
            if not label or label.lower() in GENERIC_LABELS:
                continue
            return label[:50] if len(label) > 50 else label
    return ""


def _safe_name(acc):
    try:
        return acc.get_name() or ""
    except Exception:
        return ""


def _safe_desc(acc):
    try:
        return acc.get_description() or ""
    except Exception:
        return ""


# ========== GEOMETRY ==========

def _rect_intersect(a, b):
    """Return intersection rect of a and b, or None if no overlap."""
    x1 = max(a["x"], b["x"])
    y1 = max(a["y"], b["y"])
    x2 = min(a["x"] + a["width"], b["x"] + b["width"])
    y2 = min(a["y"] + a["height"], b["y"] + b["height"])
    if x2 <= x1 or y2 <= y1:
        return None
    return {"x": x1, "y": y1, "width": x2 - x1, "height": y2 - y1}


def _visibility(frame, clip, screen):
    """Return (visibility_str, visible_rect_or_None) for frame within the
    innermost scroll clip and the screen bounds — "full", "partial N%", or
    "hidden". AT-SPI's SHOWING state already excludes most scrolled-out
    elements; this catches the partially-clipped remainder."""
    visible = dict(frame)
    if clip is not None:
        visible = _rect_intersect(visible, clip)
        if visible is None:
            return "hidden", None
    visible = _rect_intersect(visible, screen)
    if visible is None:
        return "hidden", None
    total = frame["width"] * frame["height"]
    if total <= 0:
        return "hidden", None
    pct = (visible["width"] * visible["height"]) / total * 100.0
    if pct >= 99.0:
        return "full", None
    if pct > 0:
        return f"partial {int(pct)}%", visible
    return "hidden", None


def _contains(a, b):
    """Return True if element a spatially contains element b."""
    return (a["x"] <= b["x"] and a["y"] <= b["y"]
            and a["x"] + a["width"] >= b["x"] + b["width"]
            and a["y"] + a["height"] >= b["y"] + b["height"])


def get_screen():
    """Bounding box of all monitors in logical coords: {x, y, width, height}."""
    try:
        display = Gdk.Display.get_default()
        if display and display.get_n_monitors() > 0:
            x0 = y0 = 10 ** 9
            x1 = y1 = -10 ** 9
            for i in range(display.get_n_monitors()):
                g = display.get_monitor(i).get_geometry()
                x0, y0 = min(x0, g.x), min(y0, g.y)
                x1, y1 = max(x1, g.x + g.width), max(y1, g.y + g.height)
            return {"x": x0, "y": y0, "width": x1 - x0, "height": y1 - y0}
    except Exception:
        pass
    return {"x": 0, "y": 0, "width": 1920, "height": 1080}


# ========== DESKTOP / ACTIVE WINDOW ==========

# Desktop Icons publishes one toplevel per monitor, and the title depends on
# which implementation is installed: the original extension used a readable
# "Desktop Icons" prefix, while DING / desktop-icons-ng encodes the monitor
# origin instead — e.g. "@!0,0;BDHF". Matching only the readable form left the
# whole desktop invisible to the scanner on a stock Ubuntu GNOME session.
_DING_WINDOW_RE = re.compile(r"^@!-?\d+,-?\d+;")


def is_desktop_window(name):
    """True if `name` is a Desktop Icons toplevel, either naming scheme."""
    name = name or ""
    return name.startswith("Desktop Icons") or bool(_DING_WINDOW_RE.match(name))


def get_desktop_apps():
    desktop = Atspi.get_desktop(0)
    for i in range(desktop.get_child_count()):
        try:
            app = desktop.get_child_at_index(i)
        except Exception:
            continue
        if app is not None:
            yield app


def find_app(name):
    for app in get_desktop_apps():
        if _safe_name(app) == name:
            return app
    return None


def find_active_window():
    """Return (app, window) whose frame reports the ACTIVE state. When focus
    sits on shell UI and no client window is ACTIVE, fall back to the largest
    SHOWING toplevel, so a scan still describes something useful."""
    best = (None, None)
    best_area = 0
    for app in get_desktop_apps():
        if _safe_name(app) == "gnome-shell":
            continue
        for win in iter_children(app):
            states = acc_states(win)
            if states is None:
                continue
            if states.contains(_STATE.ACTIVE):
                return app, win
            # The desktop is excluded from the largest-window fallback on
            # purpose: it is a fullscreen toplevel, so it would win that
            # contest on every session where no app happens to be ACTIVE.
            if states.contains(_STATE.SHOWING) \
                    and not is_desktop_window(_safe_name(win)):
                ext = acc_extents(win)
                if ext and ext["width"] * ext["height"] > best_area:
                    best_area = ext["width"] * ext["height"]
                    best = (app, win)
    return best


# ========== WAYLAND WINDOW-OFFSET CORRECTION ==========
# On Wayland, AT-SPI reports toplevel windows at (0, 0) — clients can't know
# their own global position. GNOME Shell's own accessibility tree, however,
# exposes a "Wayland window" actor per client window with REAL screen
# geometry. Matching the window's size against those actors recovers the true
# origin. Actors can include the CSD shadow margin, so an equal-margin match
# (actor centred on the client area) is accepted too.

def _collect_shell_window_actors():
    actors = []
    shell = find_app("gnome-shell")
    if shell is None:
        return actors

    def _walk(acc, depth):
        if depth > 6:
            return
        # Role first: a window clone is always a "panel", so asking the role
        # (one D-Bus read) rules out most of the tree without also paying for
        # the name. Reading the name of every node on the way down doubled the
        # cost of this walk to find a handful of clones.
        try:
            role_str = acc.get_role_name() or ""
        except Exception:
            return
        if role_str == "panel" and _safe_name(acc) == "Wayland window":
            ext = acc_extents(acc)
            if ext:
                actors.append(ext)
            return  # window clones have no useful children
        for child in iter_children(acc):
            _walk(child, depth + 1)

    try:
        _walk(shell, 0)
    except Exception:
        pass
    return actors


def resolve_window_offset(win_frame, actors, allow_topmost=False):
    """Return ((dx, dy), how) mapping a Wayland window's local coords to screen
    coords, or (None, None) when the origin cannot be resolved. `how` is
    "exact", "margin", "topmost" or "x11", for callers that want to report how
    much to trust the answer.

    Three strategies, most trustworthy first:

    "exact"/"margin" match the window against an actor BY SIZE. That is
    ambiguous when two windows share exact dimensions (two half-screen tiles),
    since both resolve to the same actor — Wayland gives clients nothing to
    disambiguate with. The actors carry no identity either: measured, they
    expose an empty name beyond "Wayland window", empty description, no
    attributes, no relations, and a single generic child. Size is all there is.

    "topmost" exists because size matching fails outright in a case that is not
    rare: AT-SPI can report a STALE window size. Cursor was observed reporting
    1280x800 while its actor said 1853x926, so nothing matched and the caller
    fell back to a (0, 0) offset — which silently makes every coordinate
    window-relative and every click miss. The window's ORIGIN was correct and
    stable at (330, 40) throughout. Actors are listed in Clutter paint order,
    so the last is the topmost window, and the window this is asked to resolve
    is the ACTIVE one — which is the topmost window. Measured over repeated
    samples, the topmost actor agreed with size matching on every scan where
    size matching succeeded (10/10), and supplied the correct origin on the
    scans where it did not. Only enable it for the active window; a guess is
    right there and would be arbitrary for a background one.
    """
    if not _IS_WAYLAND:
        return (0, 0), "x11"
    if win_frame is None:
        return None, None
    w, h = win_frame["width"], win_frame["height"]
    # Reversed: shell actors are listed bottom-to-top, and the window being
    # resolved is usually the active one, i.e. the topmost match.
    for a in reversed(actors):  # exact size match
        if a["width"] == w and a["height"] == h:
            return (a["x"] - win_frame["x"], a["y"] - win_frame["y"]), "exact"
    for a in reversed(actors):  # actor carries a CSD/shadow margin — centre it
        mw, mh = a["width"] - w, a["height"] - h
        if 0 <= mw <= 160 and 0 <= mh <= 160:
            return (a["x"] + mw // 2 - win_frame["x"],
                    a["y"] + mh // 2 - win_frame["y"]), "margin"
    if allow_topmost and actors:
        a = actors[-1]
        return (a["x"] - win_frame["x"], a["y"] - win_frame["y"]), "topmost"
    return None, None


def window_offset(win_frame, actors):
    """Return (dx, dy) by size matching alone, or None. Background windows use
    this: a wrong origin there is worse than none, so they never guess."""
    return resolve_window_offset(win_frame, actors)[0]


# ========== ELECTRON / CHROMIUM TREE ACTIVATION ==========

def enable_screen_reader_flag():
    """Set org.a11y.Status.IsEnabled so toolkits publish their AT-SPI trees.

    Deliberately does NOT set ScreenReaderEnabled here: on stock GNOME that
    property gsettings-syncs to screen-reader-enabled, which LAUNCHES the
    Orca screen reader audibly. Electron/Chromium apps need that flag, so
    electron_nudge() pulses it (with Orca suppressed) only when the active
    window's tree is actually empty."""
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        bus.call_sync(
            "org.a11y.Bus", "/org/a11y/bus",
            "org.freedesktop.DBus.Properties", "Set",
            GLib.Variant("(ssv)", ("org.a11y.Status", "IsEnabled",
                                   GLib.Variant("b", True))),
            None, Gio.DBusCallFlags.NONE, 2000, None)
        return True
    except Exception as e:
        print(f"  Could not enable accessibility flag: {e}")
        return False


def _set_screen_reader_flag(value):
    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    bus.call_sync(
        "org.a11y.Bus", "/org/a11y/bus",
        "org.freedesktop.DBus.Properties", "Set",
        GLib.Variant("(ssv)", ("org.a11y.Status", "ScreenReaderEnabled",
                               GLib.Variant("b", value))),
        None, Gio.DBusCallFlags.NONE, 2000, None)


def electron_nudge(window):
    """Runtime tree activation for Electron/Chromium apps. No launch flags or
    per-app settings needed.

    Chromium builds its AT-SPI tree when org.a11y.Status.ScreenReaderEnabled
    turns on, and KEEPS it for the process lifetime after the flag drops. So
    when the active window's tree is empty (an app frame with nothing in it),
    the flag is pulsed just long enough for the tree to appear, then
    reverted. On stock GNOME that flag would launch the Orca screen reader
    audibly (it syncs to the screen-reader-enabled gsetting), so Orca is
    suppressed for the pulse's duration — measured: GNOME retries ~5 times,
    each killed within ~100ms, well under Orca's ~1s speech startup."""
    if not ELECTRON_NUDGE:
        return
    # >4 nodes means a real tree already (a bare Electron frame is 2-3
    # nodes; even a minimal GTK dialog exceeds this) — nothing to nudge.
    if _quick_node_count(window, cap=6) > 4:
        return

    print("  Empty accessibility tree — pulsing screen-reader flag "
          "(Orca suppressed)...")
    stop = threading.Event()

    def _suppress():
        while not stop.is_set():
            subprocess.run(["pkill", "-x", "orca"], capture_output=True)
            time.sleep(0.08)

    suppressor = threading.Thread(target=_suppress, daemon=True)
    suppressor.start()
    try:
        _set_screen_reader_flag(True)
        deadline = time.time() + NUDGE_TIMEOUT
        while time.time() < deadline:
            if _quick_node_count(window, cap=40) > 4:
                break  # tree building has begun; wait_for_tree_ready takes over
            time.sleep(0.3)
    except Exception as e:
        print(f"  Nudge failed: {e}")
    finally:
        try:
            _set_screen_reader_flag(False)
        except Exception:
            pass
        subprocess.run(["gsettings", "set",
                        "org.gnome.desktop.a11y.applications",
                        "screen-reader-enabled", "false"],
                       capture_output=True)
        # GNOME may take a beat to notice the revert — keep suppressing.
        time.sleep(1.0)
        stop.set()


def _quick_node_count(acc, cap=300):
    """Cheaply estimate subtree size, stopping at `cap` nodes.

    The depth guard is MAX_DEPTH, not something tighter, because `cap` is what
    bounds the cost — the guard only has to stop a cyclic tree. A tighter one
    silently answers a different question: this is a depth-first stack, so an
    8-deep guard walked one long chain and reported 17 nodes for a 2900-node
    Electron window, which left every caller believing the tree was empty.
    """
    count = 0
    stack = [(acc, 0)]
    while stack and count < cap:
        node, depth = stack.pop()
        count += 1
        if depth >= MAX_DEPTH:
            continue
        for child in iter_children(node):
            stack.append((child, depth + 1))
    return count


def wait_for_tree_ready(window):
    """Poll until the window's AT-SPI subtree stops growing (Chromium builds
    it lazily after the screen-reader flag flips).

    Small trees are legitimate (a plain GTK dialog is ~7 nodes), so "stable"
    means four consecutive equal counts (~1.2s) — enough for a freshly
    flagged Electron tree to start growing, without stalling the full
    timeout on every small static window."""
    deadline = time.time() + A11Y_TREE_READY_TIMEOUT
    prev2 = prev = last = -1
    while time.time() < deadline:
        count = _quick_node_count(window)
        if count >= 300 or count == last == prev == prev2:
            return
        if last >= 0 and count > last:
            print(f"  Waiting for accessibility tree... ({count} nodes)")
        prev2, prev, last = prev, last, count
        time.sleep(A11Y_TREE_READY_INTERVAL)


# ========== TREE WALK ==========

_seen_roles = set()


def walk(acc, results, depth, screen, offset=(0, 0), clip=None, budget=None,
         source=""):
    """Recursively walk the AT-SPI tree, collecting ELEMENT_CONFIG matches.

    `offset` is the Wayland window-origin correction applied to every extent.
    `clip` is the innermost scroll-pane viewport rect (already corrected).
    `budget` is a single-element list holding the remaining node budget.
    """
    if depth > MAX_DEPTH:
        return
    if budget is not None:
        if budget[0] <= 0:
            return
        budget[0] -= 1

    try:
        role_str = acc.get_role_name() or ""
    except Exception:
        return
    _seen_roles.add(role_str)
    states = acc_states(acc)
    showing = states is not None and states.contains(_STATE.SHOWING)

    # A node that is not being rendered prunes its whole subtree. AT-SPI
    # propagates SHOWING downwards, so nothing under an unrendered ancestor is
    # rendered either — probed across a 2900-node Electron tree, the number of
    # SHOWING nodes sitting under a non-SHOWING ancestor was zero. Since a
    # non-SHOWING node is never tracked anyway (see the track test below),
    # descending only bought a D-Bus round-trip per node on the way to
    # discarding it: 70% of that tree. Closed menus are the extreme case, as
    # they publish every phantom item they contain.
    #
    # depth 0 is exempt so a caller handing us a window that has not been
    # marked SHOWING yet gets the old behaviour rather than silently nothing —
    # except for a closed menu, which was already pruned here before.
    if not showing and (role_str == "menu"
                        or (PRUNE_HIDDEN and depth > 0)):
        return

    my_frame = None
    cfg = ELEMENT_CONFIG.get(role_str)
    if cfg or role_str in CLIP_ROLES:
        my_frame = acc_extents(acc, offset)

    child_clip = clip
    if role_str in CLIP_ROLES and my_frame:
        child_clip = (_rect_intersect(clip, my_frame) or clip) if clip else my_frame

    if cfg and cfg.get("track") and my_frame and showing \
            and states.contains(_STATE.VISIBLE) \
            and my_frame["width"] >= 3 and my_frame["height"] >= 3:
        skip = False
        if cfg.get("is_enabled_flag") and not states.contains(_STATE.ENABLED):
            skip = True
        # Multi-line "text" is only tracked when editable — otherwise Chromium
        # spams read-only text runs, and GTK TextViews are covered by "_text".
        if role_str == "text" and not states.contains(_STATE.EDITABLE):
            skip = True

        if not skip:
            label = build_label(acc, cfg)
            if label:
                vis_str, vis_rect = _visibility(my_frame, child_clip, screen)
                if vis_str != "hidden":
                    value = None
                    if role_str in VALUE_ROLES:
                        v = acc_text(acc, 200)
                        if v and v.lower() != label.lower():
                            value = v[:200]
                    results.append({
                        "type": role_str,
                        "label": label,
                        "value": value,
                        "x": my_frame["x"], "y": my_frame["y"],
                        "width": my_frame["width"], "height": my_frame["height"],
                        "depth": depth,
                        "visibility": vis_str,
                        "visible_rect_raw": vis_rect,
                        "acc_element": acc,
                        "source": source,
                    })

    for child in iter_children(acc):
        walk(child, results, depth + 1, screen, offset, child_clip, budget,
             source)


# ========== GNOME SHELL CHROME (top bar / dock) ==========

def scan_shell_chrome(screen):
    """Collect labelled items from GNOME Shell's own UI. Items in the top
    strip become menu-bar entries — the top bar is where GNOME puts what other
    desktops put in a menu bar; the rest (dash/dock icons, desktop widgets) are
    regular elements."""
    menu_items = []
    elements = []
    shell = find_app("gnome-shell")
    if shell is None:
        return menu_items, elements

    def _walk(acc, depth):
        if depth > 12:
            return
        try:
            role_str = acc.get_role_name() or ""
        except Exception:
            return
        # The name is only ever needed to label a tracked node or to spot a
        # "Wayland window" clone, and clones are always panels — so an
        # anonymous container costs one D-Bus read here instead of two.
        tracked = role_str in SHELL_TRACK_ROLES
        name = _safe_name(acc) if (tracked or role_str == "panel") else ""
        if name == "Wayland window":
            return  # client window clones — walked separately with offsets
        if tracked:
            states = acc_states(acc)
            if states is not None and states.contains(_STATE.SHOWING):
                ext = acc_extents(acc)
                in_strip = ext is not None and \
                    ext["y"] + ext["height"] <= screen["y"] + MENU_STRIP_BOTTOM
                # Labels outside the top bar are dock/dash hover-tooltips —
                # SHOWING even when not displayed. The icons themselves are
                # separate nodes, so drop the phantom labels.
                if role_str == "label" and not in_strip:
                    ext = None
                if ext and name and name.lower() not in GENERIC_LABELS:
                    item = {
                        "type": role_str,
                        "label": name[:50],
                        "value": None,
                        "x": ext["x"], "y": ext["y"],
                        "width": ext["width"], "height": ext["height"],
                        "depth": depth,
                        "visibility": "full",
                        "visible_rect_raw": None,
                        "acc_element": acc,
                        "source": "shell",
                    }
                    if in_strip:
                        menu_items.append(item)
                    else:
                        elements.append(item)
        for child in iter_children(acc):
            _walk(child, depth + 1)

    try:
        _walk(shell, 0)
    except Exception:
        pass
    menu_items.sort(key=lambda m: m["x"])
    return menu_items, elements


def _rects_match(a, b, tol=8):
    """True if two rects describe the same window, within a few pixels."""
    return (abs(a["x"] - b["x"]) <= tol and abs(a["y"] - b["y"]) <= tol
            and abs(a["width"] - b["width"]) <= tol
            and abs(a["height"] - b["height"]) <= tol)


def _desktop_occluders(actors, desk_frame):
    """The window rects that paint over a desktop window.

    That is every visible toplevel except the desktop's own clone. Exactly ONE
    match is dropped, not all of them: a genuinely fullscreen app has the same
    geometry as the desktop, and removing every match would let it hide inside
    the exclusion and occlude nothing.
    """
    out, skipped = [], False
    for a in actors:
        if not skipped and desk_frame and _rects_match(a, desk_frame):
            skipped = True
            continue
        out.append(a)
    return out


def scan_desktop_icons(screen, results, actors):
    """Walk the Desktop Icons (DING) windows — one toplevel per monitor.

    The desktop is the BOTTOM surface: every ordinary window paints over it.
    AT-SPI still reports its icons as SHOWING when a maximised browser covers
    them, so without an occlusion test the agent is handed a numbered box for
    a folder it cannot click and the click lands in the browser instead.
    gnome-shell publishes the real geometry of every visible toplevel as the
    same actors used for the origin correction, so that is the occluder set.

    On X11 `actors` is empty (the origin correction is not needed there), so
    no occlusion is applied and every desktop icon is reported as before.
    """
    for app in get_desktop_apps():
        for win in iter_children(app):
            if not is_desktop_window(_safe_name(win)):
                continue
            wf = acc_extents(win)
            off = window_offset(wf, actors) if wf else None
            # Fullscreen desktop windows normally match an actor exactly;
            # the primary really does sit at the origin, so (0,0) is a
            # sound fallback there.
            off = off or (0, 0)
            desk_frame = acc_extents(win, off) if wf else None
            occluders = _desktop_occluders(actors, desk_frame)

            # Whole-monitor test first. A maximised window covers the desktop
            # completely, which is the common case, so this usually skips the
            # DING traversal outright rather than walking it to throw it away.
            if desk_frame and occluders and _visible_fraction_after_occluders(
                    desk_frame, occluders) < 0.01:
                continue

            mark = len(results)
            walk(win, results, 0, screen, off, budget=[2000], source="desktop")
            if not occluders:
                continue
            kept = []
            for e in results[mark:]:
                rect = {"x": e["x"], "y": e["y"],
                        "width": e["width"], "height": e["height"]}
                hits = [o for o in occluders if _rect_intersect(rect, o)]
                if not hits:
                    kept.append(e)
                    continue
                frac = _visible_fraction_after_occluders(rect, hits)
                if frac < 0.01:
                    continue  # behind a window — not clickable
                if frac < 0.99:
                    e["visibility"] = f"partial {int(frac * 100)}%"
                kept.append(e)
            results[mark:] = kept


def _visible_fraction_after_occluders(rect, occluders, samples=12):
    """Uncovered-area fraction of rect (0.0..1.0), by sampling a 12x12 grid of
    points and asking how many land inside an occluder."""
    if rect["width"] <= 0 or rect["height"] <= 0:
        return 0.0
    if not occluders:
        return 1.0
    step_x = rect["width"] / samples
    step_y = rect["height"] / samples
    covered = 0
    for i in range(samples):
        px = rect["x"] + (i + 0.5) * step_x
        for j in range(samples):
            py = rect["y"] + (j + 0.5) * step_y
            for occ in occluders:
                if (occ["x"] <= px <= occ["x"] + occ["width"]
                        and occ["y"] <= py <= occ["y"] + occ["height"]):
                    covered += 1
                    break
    return (samples * samples - covered) / (samples * samples)


def _apply_sibling_occlusion(results, win_frames):
    """Recompute visibility of elements covered by later-walked windows of
    the same app. AT-SPI exposes
    no global z-order, but within one app the sibling windows walked after
    the active one are its dialogs/popovers, which paint on top. Elements
    from other sources (shell, desktop) carry no _win_seq and pass through."""
    if len(win_frames) < 2:
        for e in results:
            e.pop("_win_seq", None)
        return results
    out = []
    for e in results:
        seq = e.pop("_win_seq", None)
        occluders = win_frames[seq + 1:] if seq is not None else []
        if not occluders:
            out.append(e)
            continue
        rect = {"x": e["x"], "y": e["y"],
                "width": e["width"], "height": e["height"]}
        occluders = [o for o in occluders if _rect_intersect(rect, o)]
        if not occluders:
            out.append(e)
            continue
        frac = _visible_fraction_after_occluders(rect, occluders)
        vr = e.get("visible_rect_raw")
        walk_frac = (vr["width"] * vr["height"]) / max(
            1, rect["width"] * rect["height"]) if vr else 1.0
        final = walk_frac * frac
        if final < 0.01:
            continue  # fully behind a dialog — not clickable
        if final < 0.99:
            e["visibility"] = f"partial {int(final * 100)}%"
        out.append(e)
    return out


# ========== SCAN PHASES ==========

def prepare_scan(screen):
    """Phase 1: everything that can change on-screen pixels or tree contents
    (screen-reader flag flip, lazy-tree wait) runs BEFORE the screenshot."""
    enable_screen_reader_flag()
    app, window = find_active_window()
    ctx = {"app": app, "window": window, "offset": (0, 0), "actors": [],
           "offset_how": None}
    if window is None:
        return ctx

    electron_nudge(window)
    wait_for_tree_ready(window)
    ctx["actors"] = _collect_shell_window_actors() if _IS_WAYLAND else []
    win_frame = acc_extents(window)
    if win_frame:
        off, how = resolve_window_offset(win_frame, ctx["actors"])
        if off is None:
            # The window may be mid restore/move animation, leaving the shell
            # actor's size out of step with the frame — resettle and retry.
            time.sleep(0.7)
            win_frame = acc_extents(window) or win_frame
            ctx["actors"] = _collect_shell_window_actors()
            off, how = resolve_window_offset(win_frame, ctx["actors"])
        if off is None:
            # Still nothing after resettling, so this is not an animation: the
            # window is reporting a size no actor has. Fall back to the
            # topmost actor, which IS this window — see resolve_window_offset.
            off, how = resolve_window_offset(
                win_frame, ctx["actors"], allow_topmost=True)
        if off is None:
            # No actors at all (no gnome-shell). A window-relative scan of the
            # active window still beats no scan.
            print("  Window origin unresolved — coordinates may be window-relative.")
            off, how = (0, 0), "none"
        ctx["offset"] = off
        ctx["offset_how"] = how
    return ctx


def collect_elements(screen, ctx):
    """Phase 2: read-only element gathering from every source.
    Returns (app_info, menu_items, elements)."""
    results = []
    app_info = None

    app, window = ctx["app"], ctx["window"]
    offset = ctx["offset"]

    # The shell walk is the longest single pole in a scan and it talks to
    # gnome-shell, a DIFFERENT process from the client window — so unlike two
    # walks of one app's tree, where the target's accessibility server
    # serialises every request and a thread buys nothing, these genuinely
    # overlap. Each AT-SPI call releases the GIL while it waits on D-Bus.
    # Measured 5.55s sequential vs 3.49s overlapped, with byte-identical
    # output across repeated runs.
    shell_out = {}

    def _shell_worker():
        try:
            shell_out["result"] = scan_shell_chrome(screen)
        except Exception:
            shell_out["result"] = ([], [])

    shell_thread = threading.Thread(target=_shell_worker)
    shell_thread.start()

    if window is not None:
        name = _safe_name(app) or _safe_name(window) or "Desktop"
        win_frame = acc_extents(window, offset) or dict(screen)
        app_info = {"name": name, "frame": win_frame}
        budget = [MAX_NODES]
        win_frames = []          # walk order ≈ stacking order (dialogs above)
        mark = len(results)
        walk(window, results, 0, screen, offset, clip=None, budget=budget)
        for e in results[mark:]:
            e["_win_seq"] = 0
        win_frames.append(win_frame)

        # Other on-screen windows of the same app (dialogs, popups) — Wayland
        # gives no position for them, so only walk when a shell actor matches.
        for win in iter_children(app):
            if win is window:
                continue
            states = acc_states(win)
            if states is None or not states.contains(_STATE.SHOWING):
                continue
            wf = acc_extents(win)
            if not wf:
                continue
            off = window_offset(wf, ctx["actors"])
            if off is None:
                continue  # unresolved origin — wrong coords are worse than none
            mark = len(results)
            walk(win, results, 0, screen, off, clip=None, budget=budget)
            for e in results[mark:]:
                e["_win_seq"] = len(win_frames)
            win_frames.append(acc_extents(win, off) or wf)

        results = _apply_sibling_occlusion(results, win_frames)

    # GNOME Shell chrome: the top bar and the dash/dock.
    shell_thread.join()
    menu_items, shell_elements = shell_out.get("result", ([], []))
    results.extend(shell_elements)

    # Desktop icons. Deliberately AFTER the join, not overlapped: this one
    # enumerates the AT-SPI desktop root, which is what the shell walk is
    # already traversing, and the two serialise on it badly.
    # Measured alone 0.2s; run alongside the shell walk it took 8.6s and
    # dragged the shell walk from 3.3s to 10.8s with it. Only the client
    # window walk — a genuinely separate process — overlaps safely.
    scan_desktop_icons(screen, results, ctx["actors"])

    # Deduplicate
    seen = set()
    unique = []
    for e in results:
        key = (e["label"], e["type"], round(e["x"]), round(e["y"]))
        if key not in seen:
            seen.add(key)
            unique.append(e)

    return app_info, menu_items, unique


def extract_all(screen):
    """Gather elements from all visible sources (standalone helper)."""
    return collect_elements(screen, prepare_scan(screen))


# ========== SCREENSHOT ==========

def _screenshot_portal():
    """Wayland-safe capture via the org.freedesktop.portal.Screenshot D-Bus
    API. Returns a PIL image, or None."""
    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    token = "elemscan" + secrets.token_hex(4)
    sender = bus.get_unique_name()[1:].replace(".", "_")
    req_path = f"/org/freedesktop/portal/desktop/request/{sender}/{token}"
    loop = GLib.MainLoop()
    result = {}

    def on_response(conn, sender_name, path, iface, signal, params):
        code, data = params.unpack()
        result["code"] = code
        result["uri"] = data.get("uri")
        loop.quit()

    sub = bus.signal_subscribe(
        "org.freedesktop.portal.Desktop",
        "org.freedesktop.portal.Request", "Response", req_path,
        None, Gio.DBusSignalFlags.NONE, on_response)
    try:
        bus.call_sync(
            "org.freedesktop.portal.Desktop",
            "/org/freedesktop/portal/desktop",
            "org.freedesktop.portal.Screenshot", "Screenshot",
            GLib.Variant("(sa{sv})", ("", {
                "handle_token": GLib.Variant("s", token),
                "interactive": GLib.Variant("b", False),
            })),
            GLib.VariantType("(o)"), Gio.DBusCallFlags.NONE, -1, None)
        GLib.timeout_add_seconds(15, loop.quit)
        loop.run()
    finally:
        bus.signal_unsubscribe(sub)

    if result.get("code") != 0 or not result.get("uri"):
        return None
    path = unquote(urlparse(result["uri"]).path)
    try:
        img = Image.open(path)
        img.load()
        return img
    finally:
        # The portal writes into ~/Pictures — don't litter it.
        try:
            os.remove(path)
        except OSError:
            pass


def take_screenshot(screen):
    """Capture the screen. Returns (PIL Image, scale) — scale maps logical
    (AT-SPI) coords to captured pixels (HiDPI factor)."""
    img = None
    try:
        img = _screenshot_portal()
    except Exception as e:
        print(f"  Portal screenshot failed: {e}")
    if img is None:
        try:
            from PIL import ImageGrab   # X11 fallback
            img = ImageGrab.grab()
        except Exception as e:
            print(f"Screenshot error: {e}")
            return None, 1.0
    scale = img.width / screen["width"] if screen["width"] else 1.0
    return img, scale


# ========== XML ESCAPE ==========

def _xml_escape(text):
    if not text:
        return ""
    return (text
            .replace('&', '&amp;')
            .replace('<', '&lt;')
            .replace('>', '&gt;')
            .replace('"', '&quot;'))


# ========== ANNOTATION FONT (cached) ==========

_ANNOTATE_FONT = None  # cached (font, stroke_width)
_label_tiles = {}      # label -> pre-rendered RGBA tile


def _build_annotate_font():
    font = None
    for font_path in (
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
            "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf"):
        try:
            font = ImageFont.truetype(font_path, LABEL_FONT_SIZE)
            break
        except Exception:
            continue
    # PIL strokes render through FreeType, which load_default() lacks.
    stroke_w = LABEL_STROKE if font is not None else 0
    if font is None:
        font = ImageFont.load_default()
    return font, stroke_w


def _load_annotate_font():
    global _ANNOTATE_FONT
    if _ANNOTATE_FONT is None:
        _ANNOTATE_FONT = _build_annotate_font()
    return _ANNOTATE_FONT


def _label_tile(label, font, stroke_w):
    """Cached pre-rendered RGBA tile for an index label — glyph rasterisation
    is the most expensive part of annotation and the label set repeats."""
    tile = _label_tiles.get(label)
    if tile is None:
        pad = 2 * stroke_w + 4
        tile = Image.new("RGBA",
                         (len(label) * LABEL_FONT_SIZE + pad,
                          2 * LABEL_FONT_SIZE + pad),
                         (0, 0, 0, 0))
        ImageDraw.Draw(tile).text(
            (0, 0), label, fill=NUMBER_COLOR, font=font,
            stroke_width=stroke_w, stroke_fill=LABEL_STROKE_COLOR)
        _label_tiles[label] = tile
    return tile


# ========== SCANNER CLASS ==========

class UIElementScanner:
    """The scanner the Auto-Use agent drives on Linux.

    Implements the interface the agent and controller expect from every
    platform's scanner, so neither needs to know which one it is holding."""

    def __init__(self, config, frontend_callback=None):
        self.config = config
        self.frontend_callback = frontend_callback

        # State populated by scan_elements()
        self.element_tree = []          # Hierarchical tree structure (top layer)
        self.menu_bar_tree = []         # Top-bar items as tree nodes
        self.element_index = 0          # Global index counter
        self.application_name = "Desktop"
        self.elements_to_draw = []      # List for screenshot bounding boxes
        self.elements_mapping = {}      # Mapping of index → element info for controller
        self.app_rect = None            # Application window rectangle
        self.top_layer_info = None      # {"name": ..., "type": "app"}
        self.second_layer_info = None   # Not used on Linux (no overlay layers)
        self.second_layer_tree = []     # Empty on Linux
        self.found_elements = {}        # Dictionary to store elements by type
        self._debug_iteration = 0       # Debug iteration counter

        self._screenshot = None         # Captured PIL screenshot (pixels)
        self._scale = 1.0               # logical-coords → capture-pixels factor
        self._annotated_image_base64 = None
        self._plain_screenshot = None
        self._annotate_prep = None      # (shrunk RGB image, shrink factor)
        self._annotate_thread = None

    def scan_elements(self):
        """Scan the active window, shell chrome and desktop for configured
        element types, then annotate the screenshot."""
        # Clear previous scan state
        self.element_tree = []
        self.menu_bar_tree = []
        self.second_layer_tree = []
        self.element_index = 0
        self.application_name = "Desktop"
        self.elements_to_draw = []
        self.elements_mapping = {}
        self.app_rect = None
        self.top_layer_info = None
        self.second_layer_info = None
        self.found_elements = {}
        self._screenshot = None
        self._annotated_image_base64 = None
        self._plain_screenshot = None
        self._annotate_prep = None
        self._annotate_thread = None

        screen = get_screen()
        _seen_roles.clear()
        t_start = time.time()

        # Phase 1: anything that changes pixels or tree contents.
        ctx = prepare_scan(screen)
        t_prepare = time.time()

        # Capture the screen ONCE, then let the resize worker overlap the walk
        # (PIL's resize releases the GIL).
        if SCREENSHOT:
            self._screenshot, self._scale = take_screenshot(screen)
            if self._screenshot is not None:
                self._annotate_thread = threading.Thread(
                    target=self._prepare_annotate_base)
                self._annotate_thread.start()
        t_capture = time.time()

        try:
            app_info, menu_items, elements = collect_elements(screen, ctx)
            elements.sort(key=lambda e: (e["y"], e["x"]))

            if app_info:
                self.application_name = app_info["name"]
                self.top_layer_info = {"name": app_info["name"], "type": "app"}
                f = app_info.get("frame")
                if f:
                    self.app_rect = Rect(
                        int(f["x"]), int(f["y"]),
                        int(f["x"] + f["width"]), int(f["y"] + f["height"]))
            else:
                self.top_layer_info = {"name": "Desktop", "type": "app"}

            # ----- Build menu bar tree nodes -----
            for m in menu_items:
                self.element_index += 1
                ctype = clean_type(m["type"])
                rect = Rect(int(m["x"]), int(m["y"]),
                            int(m["x"] + m["width"]), int(m["y"] + m["height"]))
                node = {
                    "element": None,
                    "name": m["label"],
                    "aria_role": "",
                    "type": ctype,
                    "active": True,
                    "index": self.element_index,
                    "value": None,
                    "actions": None,
                    "visibility": "full",
                    "clipped_by": None,
                    "rect": rect,
                    "visible_rect": rect,
                    "children": [],
                    "browser_top_layer": None,
                    "browser_second_layer": None,
                    "source": "",
                }
                self.menu_bar_tree.append(node)
                self.elements_mapping[str(self.element_index)] = {
                    'element': None,
                    'rect': rect,
                    'visible_rect': rect,
                    'name': m["label"],
                    'aria_role': '',
                    'type': ctype,
                    'value': None,
                    'visibility': 'full',
                    'clipped_by': None,
                    'acc_element': m.get("acc_element"),
                }
                if SCREENSHOT:
                    self.elements_to_draw.append({
                        "rect": rect,
                        "index": self.element_index,
                        "depth": 0,
                        "visibility": "full",
                        "source": "",
                    })

            # ----- Build element tree using spatial containment -----
            self.element_tree = self._build_hierarchical_tree(elements)
        finally:
            if self._annotate_thread is not None:
                self._annotate_thread.join()
                self._annotate_thread = None
        t_collect = time.time()

        # ----- Annotated screenshot -----
        self._debug_iteration += 1
        if SCREENSHOT and self.elements_to_draw:
            self._capture_and_annotate(screen)
        t_end = time.time()

        if DEBUG:
            self.save_to_file()
            print(f"  [scan timing] prepare={t_prepare - t_start:.2f}s "
                  f"capture={t_capture - t_prepare:.2f}s "
                  f"collect+build={t_collect - t_capture:.2f}s "
                  f"annotate={t_end - t_collect:.2f}s "
                  f"total={time.time() - t_start:.2f}s")

    def _build_hierarchical_tree(self, flat_elements):
        """Convert flat element list into a hierarchical tree using spatial
        containment, assign indices, and populate elements_mapping."""
        if not flat_elements:
            return []

        # Sort by area descending — larger containers first
        by_area = sorted(flat_elements,
                         key=lambda e: e["width"] * e["height"], reverse=True)

        # Nearest containing parent: scan backwards, stop at first hit (the
        # list is area-descending, so the first backward hit is the nearest
        # containing ancestor).
        n = len(by_area)
        parent = [None] * n
        depth = [0] * n
        for i in range(n):
            e = by_area[i]
            for j in range(i - 1, -1, -1):
                if _contains(by_area[j], e):
                    parent[i] = j
                    depth[i] = depth[j] + 1
                    break

        nodes = []
        for i, e in enumerate(by_area):
            rect = Rect(int(e["x"]), int(e["y"]),
                        int(e["x"] + e["width"]), int(e["y"] + e["height"]))
            vr = e.get("visible_rect_raw")
            visible_rect = Rect(
                int(vr["x"]), int(vr["y"]),
                int(vr["x"] + vr["width"]), int(vr["y"] + vr["height"])
            ) if vr else rect

            nodes.append({
                "element": None,
                "name": e["label"],
                "aria_role": "",
                "type": clean_type(e["type"]),
                "active": True,
                "index": None,
                "value": e.get("value"),
                "actions": None,
                "visibility": e.get("visibility", "full"),
                "clipped_by": None,
                "rect": rect,
                "visible_rect": visible_rect,
                "children": [],
                "browser_top_layer": None,
                "browser_second_layer": None,
                "source": e.get("source", ""),
                "_parent_idx": parent[i],
                "_depth": depth[i],
                "_raw_element": e,
            })

        for i, node in enumerate(nodes):
            p = node["_parent_idx"]
            if p is not None:
                nodes[p]["children"].append(node)
        roots = [nd for nd in nodes if nd["_parent_idx"] is None]

        # Sort children by position (top-to-bottom, left-to-right)
        def _sort_children(node_list):
            node_list.sort(key=lambda nd: (nd["rect"].top, nd["rect"].left))
            for nd in node_list:
                if nd["children"]:
                    _sort_children(nd["children"])

        _sort_children(roots)

        # Assign sequential indices depth-first
        def _assign_indices(node_list):
            for nd in node_list:
                self.element_index += 1
                nd["index"] = self.element_index
                e = nd.pop("_raw_element")

                self.elements_mapping[str(self.element_index)] = {
                    'element': None,
                    'rect': nd["rect"],
                    'visible_rect': nd["visible_rect"],
                    'name': nd["name"],
                    'aria_role': '',
                    'type': nd["type"],
                    # Deliberate divergence: the other platforms' scanners
                    # hardcode None here and surface the value only in the
                    # tree text. The controller can use it directly.
                    'value': nd["value"],
                    'visibility': nd["visibility"],
                    'clipped_by': None,
                    'acc_element': e.get("acc_element"),
                }
                if SCREENSHOT:
                    self.elements_to_draw.append({
                        "rect": nd["rect"],
                        "index": self.element_index,
                        "depth": nd.get("_depth", 0),
                        "visibility": nd["visibility"],
                        "source": nd.get("source", ""),
                    })
                self.found_elements.setdefault(nd["type"], []).append(nd)
                if nd["children"]:
                    _assign_indices(nd["children"])

        _assign_indices(roots)

        def _cleanup(node_list):
            for nd in node_list:
                nd.pop("_parent_idx", None)
                nd.pop("_depth", None)
                nd.pop("_raw_element", None)
                if nd["children"]:
                    _cleanup(nd["children"])

        _cleanup(roots)
        return roots

    # ========== ANNOTATION ==========

    def _prepare_annotate_base(self):
        """Worker-thread half of annotation: LLM-payload downscale + RGB
        conversion. Downscale FIRST, annotate later — drawing before the
        resize would put the labels through the resampler too."""
        try:
            img = self._screenshot
            src_w, src_h = img.size
            shrink = min(1.0,
                         LLM_IMAGE_MAX_EDGE / max(src_w, src_h),
                         (LLM_IMAGE_MAX_PIXELS / (src_w * src_h)) ** 0.5)
            if shrink < 1.0:
                img = img.resize(
                    (max(1, int(src_w * shrink)), max(1, int(src_h * shrink))),
                    Image.Resampling.LANCZOS)
            else:
                img = img.copy()
            if img.mode in ('RGBA', 'LA', 'P'):
                rgb = Image.new('RGB', img.size, (255, 255, 255))
                rgb.paste(img, mask=img.split()[-1] if img.mode == 'RGBA' else None)
                img = rgb
            elif img.mode != 'RGB':
                img = img.convert('RGB')
            self._annotate_prep = (img, shrink)
        except Exception:
            self._annotate_prep = None

    def _capture_and_annotate(self, screen):
        """Annotate the captured screenshot and store it as base64."""
        if self._screenshot is None or self._annotate_prep is None:
            self._annotated_image_base64 = None
            return
        screenshot, shrink = self._annotate_prep
        self._plain_screenshot = self._screenshot.copy()
        draw = ImageDraw.Draw(screenshot)
        # Logical coords -> delivered pixels: HiDPI capture scale × downscale.
        draw_scale = self._scale * shrink
        ox, oy = screen["x"], screen["y"]
        font, stroke_w = _load_annotate_font()

        for item in self.elements_to_draw:
            rect = item["rect"]
            box = (
                int((rect.left - ox) * draw_scale),
                int((rect.top - oy) * draw_scale),
                int((rect.right - ox) * draw_scale),
                int((rect.bottom - oy) * draw_scale),
            )
            draw.rectangle(box, outline=BOX_COLOR, width=BOX_WIDTH)
            label = f"[{item['index']}]"
            tile = _label_tile(label, font, stroke_w)
            screenshot.paste(tile, (box[0] + 4, box[1] + 3), tile)

        self._encode_annotation(screenshot)

    def _encode_annotation(self, screenshot):
        """Single lossless encode — these exact bytes are the LLM payload."""
        buffered = io.BytesIO()
        screenshot.save(buffered, format=LLM_IMAGE_FORMAT,
                        compress_level=LLM_IMAGE_COMPRESS_LEVEL)
        annotated_image_bytes = buffered.getvalue()
        self._annotated_image_base64 = base64.b64encode(
            annotated_image_bytes).decode('utf-8')

        if DEBUG:
            debug_dir = f"debug/iteration_{self._debug_iteration}"
            os.makedirs(debug_dir, exist_ok=True)
            with open(f"{debug_dir}/annotated_screenshot.png", "wb") as f:
                f.write(annotated_image_bytes)

        if FRONTEND and self.frontend_callback:
            if DEBUG:
                self.frontend_callback(self._annotated_image_base64)
            else:
                # Plain screenshot for production frontend (human preview).
                plain = self._plain_screenshot
                w, h = plain.size
                md = FRONTEND_IMAGE_MAX_DIMENSION
                if w > md or h > md:
                    if w > h:
                        plain = plain.resize((md, int(h * md / w)),
                                             Image.Resampling.LANCZOS)
                    else:
                        plain = plain.resize((int(w * md / h), md),
                                             Image.Resampling.LANCZOS)
                if plain.mode in ('RGBA', 'LA', 'P'):
                    rgb = Image.new('RGB', plain.size, (255, 255, 255))
                    rgb.paste(plain, mask=plain.split()[-1]
                              if plain.mode == 'RGBA' else None)
                    plain = rgb
                elif plain.mode != 'RGB':
                    plain = plain.convert('RGB')
                buf = io.BytesIO()
                plain.save(buf, format="JPEG", quality=FRONTEND_IMAGE_QUALITY)
                self.frontend_callback(
                    base64.b64encode(buf.getvalue()).decode('utf-8'))

    # ========== OUTPUT ==========

    def get_scan_data(self):
        """Get scan data for use by AgentService.

        Returns:
            tuple: (element_tree_text, annotated_image_base64, uac_detected)
                   uac_detected is always False on Linux (no UAC).
        """
        element_tree_text = ""
        if self.menu_bar_tree:
            element_tree_text += "<menu_bar>\n"
            element_tree_text += self._get_tree_text_recursive(self.menu_bar_tree, 1)
            element_tree_text += "</menu_bar>\n\n"

        element_tree_text += "<top_layer>\n"
        if self.top_layer_info:
            layer_name = _xml_escape(self.top_layer_info["name"])
            element_tree_text += (
                f'  <application name="{layer_name}" '
                f'type="{self.top_layer_info["type"]}" />\n')
        else:
            element_tree_text += '  <application name="Desktop" type="app" />\n'
        element_tree_text += self._get_tree_text_recursive(self.element_tree, 1)
        element_tree_text += "</top_layer>\n"

        return element_tree_text, self._annotated_image_base64, False

    def _get_tree_text_recursive(self, tree_list, depth):
        """Generate tree text recursively — matches the Windows format."""
        result = ""
        indent = "  " * depth
        for item in tree_list:
            name = _xml_escape(item['name'])
            visibility = item.get('visibility', 'full')
            clipped_by = item.get('clipped_by', None)
            clipped_by_attr = ""
            if clipped_by and visibility != "full":
                clipped_by_attr = f', clipped_by="{_xml_escape(clipped_by)}"'

            if item.get("value"):
                value = _xml_escape(item["value"])
                result += (f'{indent}[{item["index"]}]<element name="{name}", '
                           f'valuePattern.value="{value}", type="{item["type"]}", '
                           f'active="{item["active"]}", visibility="{visibility}"'
                           f'{clipped_by_attr} />\n')
            else:
                result += (f'{indent}[{item["index"]}]<element name="{name}", '
                           f'type="{item["type"]}", active="{item["active"]}", '
                           f'visibility="{visibility}"{clipped_by_attr} />\n')
            if item.get("children"):
                result += self._get_tree_text_recursive(item["children"], depth + 1)
        return result

    def get_elements_mapping(self):
        """Get the elements mapping for controller.

        Returns:
            dict: mapping index (str) → element info dict
        """
        return self.elements_mapping

    def print_summary(self):
        """Print summary of found elements — silent (matches Windows behavior)."""
        pass

    def save_to_file(self):
        """Save element tree to file when DEBUG is True."""
        if not DEBUG:
            return
        debug_dir = f"debug/iteration_{self._debug_iteration}"
        os.makedirs(debug_dir, exist_ok=True)
        with open(f"{debug_dir}/tree.txt", "w", encoding="utf-8") as f:
            text, _, _ = self.get_scan_data()
            f.write(text)


# ========== MAIN PROGRAM (full scan + step-by-step debug commands) ==========

def _countdown(seconds, verb):
    for i in range(seconds, 0, -1):
        print(f"  {verb} in {i}... (focus the window you want)")
        time.sleep(1)
    print()


def cmd_topmost():
    """Step 1: topmost-application detection, in isolation."""
    _countdown(3, "Detecting")
    print("All AT-SPI applications and their windows:")
    for app in get_desktop_apps():
        name = _safe_name(app) or "?"
        lines = []
        for win in iter_children(app):
            states = acc_states(win)
            if states is None:
                continue
            flags = "".join((
                "A" if states.contains(_STATE.ACTIVE) else "-",
                "S" if states.contains(_STATE.SHOWING) else "-",
            ))
            ext = acc_extents(win)
            geo = (f"{ext['width']}x{ext['height']}@({ext['x']},{ext['y']})"
                   if ext else "no-extents")
            lines.append(f"    [{flags}] {win.get_role_name()} "
                         f"{(_safe_name(win) or '')[:45]!r} {geo}")
        print(f"  {name}" + ("" if lines else "  (no windows)"))
        for ln in lines:
            print(ln)
    print("  (flags: A=active, S=showing)\n")

    app, win = find_active_window()
    if win is None:
        print("RESULT: no active or visible window -> would scan Desktop only")
        return

    states = acc_states(win)
    via = ("ACTIVE state" if states and states.contains(_STATE.ACTIVE)
           else "fallback: largest visible window")
    ext = acc_extents(win)
    print(f"RESULT: topmost app = {_safe_name(app)!r}")
    print(f"        window      = {_safe_name(win)!r}  (found via {via})")
    print(f"        raw extents = {ext}")
    if _IS_WAYLAND and ext:
        actors = _collect_shell_window_actors()
        off = window_offset(ext, actors)
        print(f"        shell actors seen = {len(actors)}")
        if off is None:
            print("        origin correction = UNRESOLVED (no actor matched)")
        else:
            print(f"        origin correction = +{off}  "
                  f"-> true position ({ext['x'] + off[0]}, {ext['y'] + off[1]})")


def cmd_screenshot():
    """Step 2: screen capture, in isolation."""
    screen = get_screen()
    print(f"screen (logical coords): {screen}")
    t0 = time.time()
    img, scale = take_screenshot(screen)
    if img is None:
        print("RESULT: FAILED — no capture method worked")
        return
    img.save("element_screenshot.png")
    print(f"RESULT: captured {img.width}x{img.height} pixels "
          f"in {time.time() - t0:.2f}s")
    print(f"        scale (logical->pixels) = {scale:.3f}")
    print("        saved -> element_screenshot.png  (open it to verify)")


def cmd_walk():
    """Step 3: element walk of the topmost window, no drawing."""
    _countdown(3, "Scanning")
    screen = get_screen()
    ctx = prepare_scan(screen)
    if ctx["window"] is None:
        print("No topmost window found — scanning shell/desktop only.")
    else:
        print(f"Walking window of app (offset correction {ctx['offset']})...")

    t0 = time.time()
    app_info, menu_items, elements = collect_elements(screen, ctx)
    print(f"\nRESULT: {len(elements)} elements + {len(menu_items)} menu-bar "
          f"items in {time.time() - t0:.2f}s")
    print(f"        application = "
          f"{app_info['name'] if app_info else 'Desktop'!r}")
    counts = Counter(clean_type(e["type"]) for e in elements)
    print("        by type:", ", ".join(
        f"{t}={n}" for t, n in counts.most_common()))

    print("\nMenu bar items:")
    for m in menu_items:
        print(f"  [{clean_type(m['type'])}] {m['label']!r} "
              f"at ({m['x']},{m['y']}) {m['width']}x{m['height']}")
    print("\nFirst 30 elements (corrected screen coords):")
    for e in sorted(elements, key=lambda e: (e["y"], e["x"]))[:30]:
        print(f"  [{clean_type(e['type'])}] {e['label'][:35]!r} "
              f"at ({int(e['x'])},{int(e['y'])}) "
              f"{int(e['width'])}x{int(e['height'])} {e['visibility']}"
              + (f"  value={e['value']!r}" if e.get("value") else ""))


def cmd_scan():
    """Full pipeline: detect → screenshot → walk → annotate."""
    print("Linux UI Element Scanner (AT-SPI2)")
    print(f"DEBUG = {DEBUG}")
    print(f"SCREENSHOT = {SCREENSHOT}")
    print(f"Session: {'Wayland' if _IS_WAYLAND else 'X11'}")

    scanner = UIElementScanner(ELEMENT_CONFIG)
    _countdown(5, "Scanning")
    print("Scanning now!\n")
    scanner.scan_elements()

    element_tree_text, annotated_image_base64, _ = scanner.get_scan_data()
    mapping = scanner.get_elements_mapping()

    print(f"Application: {scanner.application_name}")
    print(f"Elements found: {len(mapping)}")
    print(f"Image captured: {annotated_image_base64 is not None}")

    if DEBUG:
        print("\nElement tree text:")
        print(element_tree_text)
        print("Scan complete. Check debug/ for files.")
    else:
        print("Scan complete. Data ready for LLM.")


def main():
    commands = {"scan": cmd_scan, "topmost": cmd_topmost,
                "screenshot": cmd_screenshot, "walk": cmd_walk}
    cmd = sys.argv[1].lstrip("-").lower() if len(sys.argv) > 1 else "scan"
    fn = commands.get(cmd)
    if fn is None:
        print(f"Unknown command {cmd!r}. "
              f"Usage: python3 element.py [{'|'.join(commands)}]")
        sys.exit(1)
    fn()


if __name__ == "__main__":
    main()
