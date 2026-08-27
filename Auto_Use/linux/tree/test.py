#!/usr/bin/env python3
# Copyright 2026 Ashish Yadav — Auto-Use

"""
Standalone test runner for element.py scanner (Linux via AT-SPI2).

File location : Auto_Use/linux/tree/test.py

Run from the project root:

    python3 -m Auto_Use.linux.tree.test

Or run the file directly from anywhere:

    python3 /path/to/auto-use/Auto_Use/linux/tree/test.py
    python3 .../test.py 0      # no countdown — scan whatever is focused now
    python3 .../test.py -v     # full detail: counts, geometry, role backlog

This is the development loop for the Linux port. One run does three things:

    preflight   is the accessibility stack in a state where a scan can work?
    scan        one full UIElementScanner.scan_elements() pass, timed
    audit       does the result look right, and if not, WHY?

A run answers one question — which window did the scanner decide it was
looking at, and how long did that take — and says nothing else while things
work. Everything the preflight and audit learn is reported BY EXCEPTION, so
any output beyond those two lines means something needs attention.

That matters because the Linux port fails quietly. An unresolved Wayland
window origin, an Electron app that never published its tree, a role nobody
added to ELEMENT_CONFIG yet — none of those raise, they just produce a tree
that reads fine and points at the wrong pixels. The audit names the cause so
element.py does not have to be bisected by hand.

For driving ONE stage in isolation, element.py runs itself:

    python3 element.py topmost      topmost-window detection only
    python3 element.py screenshot   screen capture only
    python3 element.py walk         element walk only, no drawing

Requires: python3-gi (Atspi 2.0, Gdk 3.0) and Pillow — see
linux_requirements.txt for why gi is best taken from the system packages.
"""

import io
import os
import sys
import time
import contextlib
from collections import Counter

# Ensure project root is on path when run directly. abspath, not the raw
# dirname: the docstring promises this file runs from anywhere, and a relative
# __file__ would resolve the three levels up against the caller's cwd instead.
sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..', '..')))

try:
    import Auto_Use.linux.tree.element as element
    from Auto_Use.linux.tree.element import UIElementScanner, ELEMENT_CONFIG
except (ImportError, ValueError) as exc:
    # The two ways this import fails are both environment, not code, and a
    # bare traceback makes them look like element.py is broken. gi comes from
    # the system (pip builds it from source and wants the GObject dev
    # headers), so a venv made without --system-site-packages cannot see it;
    # and gi.require_version raises ValueError — not ImportError — when the
    # typelibs are missing. Diagnose both instead of re-raising.
    # element.py already finds a system PyGObject a venv cannot see, so
    # reaching here means the system packages are genuinely absent (or the
    # typelibs are — gi.require_version raises ValueError, not ImportError).
    # Both need one apt install; neither is anything the venv can fix.
    if not (getattr(exc, "name", None) == "gi"
            or (isinstance(exc, ValueError) and "amespace" in str(exc))):
        raise

    print(f"Cannot import the scanner: {exc}\n")
    print("PyGObject and its typelibs are system packages — the typelibs are")
    print("introspection data, not Python, so pip cannot supply them:")
    print("  sudo apt install python3-gi gir1.2-atspi-2.0 gir1.2-gtk-3.0")
    sys.exit(1)

# Force debug flags on — DEBUG is what writes debug/iteration_N/tree.txt and
# the annotated PNG, which are the two artifacts this runner exists to produce.
element.DEBUG = True
element.SCREENSHOT = True

DEFAULT_COUNTDOWN = 5

# Set by -v. Everything this runner learns beyond "which app, how long" is
# reported BY EXCEPTION: a clean scan says nothing, a broken one explains
# itself. -v prints the full picture regardless.
VERBOSE = False


def detail(line):
    """Print only under -v. Context that is noise while things are working."""
    if VERBOSE:
        print(line)


# ========== PREFLIGHT ==========

def preflight():
    """Check the accessibility stack. Returns False when a scan cannot
    meaningfully run.

    Each check below has already cost the port a debugging session: an a11y
    bus with nothing published on it, a Wayland session with no gnome-shell
    actors to resolve window origins against, a PIL fallback font that cannot
    stroke the index labels. Nothing here prints on a healthy machine.
    """
    ok = True

    session = "Wayland" if element._IS_WAYLAND else "X11"
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "?")
    detail(f"Session     : {session}  ({desktop})")

    # An AT-SPI desktop with almost nothing on it means the bus is alive but
    # toolkits are not publishing — usually org.a11y.Status.IsEnabled was
    # never set, which is exactly what element.enable_screen_reader_flag()
    # fixes at scan time. Two is the floor: gnome-shell plus one real client.
    try:
        apps = [element._safe_name(a) or "?" for a in element.get_desktop_apps()]
    except Exception as e:
        print(f"AT-SPI      : UNREACHABLE — {e}")
        print("              Install gir1.2-atspi-2.0 and check that the "
              "a11y bus is running.")
        return False

    detail(f"AT-SPI      : {len(apps)} applications on the bus")
    if len(apps) < 2:
        print(f"AT-SPI      : only {len(apps)} application(s) on the bus — "
              f"accessibility is probably off:")
        print("              gsettings set org.gnome.desktop.interface "
              "toolkit-accessibility true")
        ok = False

    # Wayland clients cannot know their own global position, so element.py
    # recovers it by size-matching gnome-shell's "Wayland window" actors. No
    # gnome-shell on the bus means that match can never succeed and every
    # coordinate stays window-relative.
    if element._IS_WAYLAND:
        actors = element._collect_shell_window_actors()
        detail(f"Shell actors: {len(actors)}  (Wayland window-origin sources)")
        if not actors:
            print("Shell actors: none — gnome-shell publishes no window "
                  "actors, so coordinates will be")
            print("              window-relative and clicks will miss.")
            ok = False

    # load_default() hands back a bitmap font with no FreeType face, and PIL
    # renders strokes through FreeType — so the index labels lose their dark
    # rim and magenta digits become unreadable on light UI.
    _, stroke_w = element._load_annotate_font()
    detail(f"Label font  : {'TrueType' if stroke_w else 'PIL bitmap fallback'}")
    if not stroke_w:
        print("Label font  : PIL bitmap fallback — labels will be unstroked "
              "and hard to read.")
        print("              Install fonts-dejavu-core.")

    return ok


# ========== AUDIT ==========

def _iter_nodes(nodes):
    """Depth-first walk of the hierarchical element tree."""
    for node in nodes:
        yield node
        yield from _iter_nodes(node.get("children") or [])


def audit(scanner, screen):
    """Check the finished scan for the failures that do not raise.

    Returns a list of human-readable problems; an empty list means the scan
    looks structurally sound (it does NOT mean the labels are good).
    """
    problems = []
    nodes = list(_iter_nodes(scanner.element_tree)) + scanner.menu_bar_tree
    mapping = scanner.get_elements_mapping()

    # --- Wayland window origin -------------------------------------------
    # The highest-value check in the file. AT-SPI reports client windows at
    # (0, 0) on Wayland; when the shell-actor size match fails, prepare_scan()
    # falls back to a (0, 0) offset and carries on. The tree still renders
    # perfectly — every rect is just silently window-relative. Re-running the
    # probe here is cheap and tells the dev which of the two worlds they are
    # looking at. Note the match is size-only, so two windows with identical
    # dimensions are genuinely ambiguous — Wayland gives clients nothing else
    # to disambiguate with.
    if element._IS_WAYLAND:
        _, win = element.find_active_window()
        frame = element.acc_extents(win) if win else None
        if frame:
            # Ask exactly what prepare_scan asks, topmost fallback included, or
            # this cries wolf on every scan the fallback quietly rescued.
            offset, how = element.resolve_window_offset(
                frame, element._collect_shell_window_actors(),
                allow_topmost=True)
            if offset is None:
                problems.append(
                    "Wayland window origin unresolved — gnome-shell published "
                    "no window actors at all, so every coordinate is "
                    "window-relative and every click will miss.")
            elif how == "topmost":
                # Worth surfacing even though it is almost certainly right:
                # it means AT-SPI reported a size no actor has, which is the
                # signature of a stale frame.
                detail(f"Win origin  : +{offset[0]},+{offset[1]} "
                       f"(inferred from the topmost actor — AT-SPI reported a "
                       f"size no actor matched)")
            else:
                detail(f"Win origin  : +{offset[0]},+{offset[1]} ({how})")

    # --- Structural consistency ------------------------------------------
    # elements_mapping is what the controller clicks through, so it must hold
    # exactly one entry per node the tree numbered. A mismatch means an index
    # was reused or a node never made it into the mapping.
    if len(mapping) != len(nodes):
        problems.append(
            f"elements_mapping has {len(mapping)} entries but the tree holds "
            f"{len(nodes)} nodes — an index was dropped or reused.")

    degenerate = [n for n in nodes
                  if n["rect"].right <= n["rect"].left
                  or n["rect"].bottom <= n["rect"].top]
    if degenerate:
        problems.append(
            f"{len(degenerate)} element(s) have a zero or inverted rect — "
            f"e.g. {degenerate[0]['name']!r} ({degenerate[0]['type']}).")

    # walk() already drops anything the screen does not intersect, but the
    # shell-chrome and desktop-icon passes bypass that filter, so an actor
    # reported on a monitor that is not in `screen` still lands here.
    off_screen = [n for n in nodes
                  if n["rect"].right <= screen["x"]
                  or n["rect"].left >= screen["x"] + screen["width"]
                  or n["rect"].bottom <= screen["y"]
                  or n["rect"].top >= screen["y"] + screen["height"]]
    if off_screen:
        problems.append(
            f"{len(off_screen)} element(s) lie entirely outside the screen "
            f"bounds {screen['width']}x{screen['height']} — a bad offset "
            f"correction, or a monitor get_screen() did not account for.")

    # --- Coverage ---------------------------------------------------------
    # A real desktop window yields dozens of elements. A handful means the app
    # never published its tree — the Electron/Chromium case electron_nudge()
    # is there to fix, which fails silently when the pulse is disabled or the
    # app ignores it.
    if len(mapping) < 10:
        problems.append(
            f"only {len(mapping)} element(s) found — the app likely never "
            f"published its AT-SPI tree. If it is Electron/Chromium, check "
            f"ELECTRON_NUDGE, or launch it with "
            f"--force-renderer-accessibility.")

    # Only a capture failure is worth reporting here. With nothing to draw
    # element.py skips annotation entirely, and the empty-tree problem above
    # already covers that case.
    if element.SCREENSHOT and scanner.elements_to_draw \
            and not scanner._annotated_image_base64:
        problems.append(
            "no annotated screenshot was produced — the XDG portal capture "
            "was denied or timed out (see take_screenshot).")

    return problems


def report_untracked_roles():
    """AT-SPI roles the walk met but ELEMENT_CONFIG does not track — the
    porting to-do list, since _seen_roles is everything walk() laid eyes on.
    Under -v only: it is a backlog, not a result."""
    untracked = sorted(r for r in element._seen_roles
                       if r and r not in ELEMENT_CONFIG)
    if untracked:
        detail(f"\nRoles seen but NOT tracked ({len(untracked)}) — candidates "
               f"for ELEMENT_CONFIG:")
        detail("  " + ", ".join(untracked))


def parse_args(argv):
    """(countdown_seconds, verbose) from a bare number and/or -v, in any order."""
    seconds, verbose = DEFAULT_COUNTDOWN, False
    for arg in argv:
        if arg in ("-v", "--verbose"):
            verbose = True
        else:
            try:
                seconds = max(0, int(arg))
            except ValueError:
                print(f"Usage: {os.path.basename(__file__)} "
                      f"[countdown_seconds] [-v]")
                sys.exit(1)
    return seconds, verbose


# ========== MAIN ==========

def main():
    global VERBOSE
    seconds, VERBOSE = parse_args(sys.argv[1:])

    if not preflight():
        print("Preflight failed — fix the above, then re-run.")
        sys.exit(1)

    # Countdown — switch to the window you want to scan. Pass 0 to skip it and
    # scan this terminal, which is the fastest loop when the thing being
    # debugged is the walk itself rather than a specific app.
    for i in range(seconds, 0, -1):
        print(f"  Scanning in {i}... (focus the window you want)")
        time.sleep(1)

    screen = element.get_screen()
    t0 = time.time()

    scanner = UIElementScanner(ELEMENT_CONFIG)
    # element.py narrates itself as it works — tree-ready polling, the Electron
    # nudge, portal failures, its own timing breakdown. That is context for a
    # scan that went wrong, not for one that worked, so hold it and replay it
    # below only if the audit has something to say.
    chatter = io.StringIO()
    with contextlib.redirect_stdout(sys.stdout if VERBOSE else chatter):
        scanner.scan_elements()
    scan_time = time.time() - t0

    tree_text, image_b64, _ = scanner.get_scan_data()
    mapping = scanner.get_elements_mapping()

    # The whole point of a run: which window did the scanner decide it was
    # looking at, and how long did deciding take.
    print(f"\nApplication : {scanner.application_name}")
    print(f"Scan time   : {scan_time:.2f}s")

    detail(f"Elements    : {len(mapping)} "
           f"({len(scanner.menu_bar_tree)} in the top bar)")
    detail(f"Screen      : {screen['width']}x{screen['height']} logical")
    if scanner._screenshot is not None:
        detail(f"Capture     : {scanner._screenshot.width}x"
               f"{scanner._screenshot.height} px  (scale {scanner._scale:.2f})")
    detail(f"Image       : {'yes' if image_b64 else 'no'}")
    detail(f"Tree text   : {len(tree_text)} chars")

    problems = audit(scanner, screen)

    counts = Counter(n["type"] for n in _iter_nodes(scanner.element_tree))
    if counts:
        detail("\nBy type     : " + ", ".join(
            f"{t}={n}" for t, n in counts.most_common()))

    report_untracked_roles()

    # Silence is the pass signal; a problem is worth interrupting for — and
    # brings the scanner's own narration with it, which is usually where the
    # cause shows up.
    if problems:
        held = chatter.getvalue().strip()
        if held:
            print("\nScanner output:")
            print(held)
        print(f"\n{len(problems)} problem(s) found:")
        for p in problems:
            print(f"  - {p}")

    # element.py writes its debug artifacts to a RELATIVE "debug/iteration_N",
    # so they land next to wherever this was launched from — print the
    # absolute path rather than let the dev hunt for it.
    detail(f"\nDebug saved to: "
           f"{os.path.abspath(f'debug/iteration_{scanner._debug_iteration}')}/")


if __name__ == "__main__":
    main()
