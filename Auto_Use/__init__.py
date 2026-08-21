# Copyright 2026 Ashish Yadav — Auto-Use

"""Auto Use package root — and the single source of truth for WHERE the user's
data lives.

User data must NOT sit inside the install folder: the Windows installer
uninstalls with `[UninstallDelete] Type: filesandordirs; Name: "{app}"`, i.e. it
deletes the WHOLE install directory, so anything written next to AutoUse.exe
(chats, API keys) died with the uninstall. Everything the user creates goes in
one folder no installer owns:

    <base>/autouse_data/
        agent_conversation/     chats: index.json, settings.json, chat_*/
        api_key/api_key.txt     provider keys + Telegram token + Vertex config

    base = Path.home()   packaged build   -> C:/Users/<u>/autouse_data
                                          -> /Users/<u>/autouse_data on macOS
    base = <repo root>   `python app.py`  -> <repo>/autouse_data

This lives in the package root rather than in any one sub-package because six
modules across windows, mac and ios need the same answer, and two
definitions of "where is autouse_data" that drift apart would silently put user
data back inside the install folder — the exact bug this exists to fix.

RULES for the code below:
  * stdlib ONLY, and no filesystem side effects at import time. Every
    `import Auto_Use.anything` executes this module first.
  * NEVER print(). AutoUse re-execs itself as --banner-mode, which speaks a
    JSON-per-line protocol on stdout, and sys.stdout can be None at import time
    in the Windows GUI-subsystem binary. Log to `logger` (stderr) instead.
  * Never use the builtin open() on a path under autouse_data: compiled builds
    monkey-patch builtins.open to resolve embedded resources by path suffix
    (frontend/service.py setup_embedded_resources), and a matching write is
    silently swallowed into a StringIO. Use Path.read_bytes / write_bytes /
    os.replace here.
"""

import os
import sys
import logging
import threading
from pathlib import Path

logger = logging.getLogger("autouse.paths")

DATA_DIR_NAME = "autouse_data"

# Absolute-path override; wins over the rules below. The --cli-mode /
# --minion-mode / --banner-mode children are spawned with os.environ.copy(), so
# they inherit it and parent + children can never disagree on the data root.
ENV_DATA_DIR = "AUTOUSE_DATA_DIR"

# Compiled (Nuitka) binary vs dev run — mirrors the detection in app.py.
IS_COMPILED = bool(getattr(sys, "frozen", False)) or ("__compiled__" in globals())

# <repo>/Auto_Use/__init__.py -> <repo>. __file__-based, NEVER cwd-based: the
# cli / minion children are spawned as `python -m ...` with no explicit cwd.
_REPO_ROOT = Path(__file__).resolve().parent.parent


def _home_dir() -> Path:
    """The user's home. Path.home() honours USERPROFILE on Windows."""
    try:
        return Path.home()
    except Exception:
        return Path(os.path.expanduser("~"))


def _base_dir() -> Path:
    """Parent folder that holds autouse_data/ (not created here)."""
    if IS_COMPILED:
        return _home_dir()
    # Dev = the checkout this file lives in. Belt-and-braces: if the compiled
    # probe above ever fails inside a packaged build, a dist folder has no
    # app.py at the repo root — so fall back to home rather than silently
    # writing the user's data back into the install folder.
    if (_REPO_ROOT / "app.py").is_file():
        return _REPO_ROOT
    return _home_dir()


def _ensure(p: Path) -> Path:
    """mkdir -p, best effort. Callers already guard their own reads and writes,
    so a read-only or full disk degrades to 'no data' rather than crashing the
    GUI on startup."""
    try:
        p.mkdir(parents=True, exist_ok=True)
    except Exception:
        logger.exception("mkdir %s", p)
    return p


def data_root() -> Path:
    """<home>/autouse_data (packaged) or <repo>/autouse_data (dev)."""
    # A blank/whitespace override must read as UNSET: Path("") resolves to ".",
    # and callers rmtree/overwrite paths under this root.
    override = (os.environ.get(ENV_DATA_DIR) or "").strip()
    root = Path(override).expanduser() if override else _base_dir() / DATA_DIR_NAME
    return _ensure(root)


BROWSER_PROFILES_DIR = "browser_profiles"
DEFAULT_BROWSER_PROFILE = "default"


def normalize_profile_name(name) -> str:
    """A browser profile name, or ValueError.

    The name becomes a path segment under `data_root()`, which is a tree this
    codebase deletes from — so this REJECTS rather than sanitizes. Quietly
    mapping "../../etc" to "etc" would be worse than an error: it would delete
    or overwrite something the caller never named.
    """
    text = str(name or "").strip().lower()
    if not text:
        return DEFAULT_BROWSER_PROFILE
    if len(text) > 64 or text.startswith("."):
        raise ValueError(f"browser profile name {text!r} must be 1-64 chars and not start with '.'")
    if any(c not in "abcdefghijklmnopqrstuvwxyz0123456789._-" for c in text):
        raise ValueError(
            f"browser profile name {text!r} may only contain letters, digits, '.', '_' and '-'"
        )
    if text in ("_tmp", ".", ".."):
        raise ValueError(f"browser profile name {text!r} is reserved")
    return text


def browser_profile_dir(name=None) -> Path:
    """The Chrome user-data-dir for a named browser profile, created if absent.

    <data_root>/browser_profiles/<name>/chrome

    Chrome owns and litters its user-data-dir, so it is nested one level down:
    that leaves room beside it for our own files, and makes "reset this
    profile" a delete of `chrome/` rather than of the profile's identity.

    This is what makes the agent arrive already logged in. Cookies,
    localStorage and IndexedDB live in here and survive between runs, so a site
    the agent signed into once does not have to be signed into again — which
    matters far more than any fingerprint tuning, because a login wall is where
    a web agent usually stops.
    """
    root = data_root() / BROWSER_PROFILES_DIR / normalize_profile_name(name)
    _ensure(root)
    return _ensure(root / "chrome")


def install_dir() -> Path:
    """Where the app's own code sits — the folder the uninstaller deletes. Used
    only to find data left behind by a pre-autouse_data build."""
    return Path(sys.executable).resolve().parent if IS_COMPILED else _REPO_ROOT


# =============================================================================
# skills — the user-editable .md knowledge files
# =============================================================================
# autouse_data/skills/windows/ and .../mac/, seeded once from the defaults that
# ship in Auto_Use/<pkg>/agent/skills/. Seeding happens ONLY when the folder is
# new, so a skill the user deletes in the UI stays deleted.
# NOTE: the platform key and the package directory used to differ ("mac" vs
# "macOS_use"), so a lookup map sat here. Since the packages were renamed to
# Auto_Use/{mac,windows,ios} the two are the same string — `plat` IS the
# package name, and the map would just be an identity.


def skills_platform(value=None) -> str:
    """'windows' | 'mac'. Accepts sys.platform or a package name."""
    key = str(sys.platform if value is None else value).lower()
    return "mac" if ("darwin" in key or "mac" in key) else "windows"


def _skills_from_dir(src: Path) -> dict:
    out = {}
    if src.is_dir():
        for p in sorted(src.iterdir()):
            if p.is_file() and (p.suffix.lower() == ".md" or p.name == "skills.json"):
                try:
                    out[p.name] = p.read_text(encoding="utf-8")
                except Exception:
                    logger.warning("unreadable default skill %s", p)
    return out


def _skills_from_resources(prefix: str) -> dict:
    """Pull skill files out of the binary's embedded-resource blobs. Keys are
    repo-root-relative with forward slashes, e.g.
    'autouse_data/skills/windows/browser.md'."""
    out = {}
    try:
        import base64
        from _embedded_resources import RESOURCES   # generated by the binary build
    except Exception:
        return out
    for key, blob in RESOURCES.items():
        k = str(key).replace("\\", "/")
        i = k.find(prefix)
        name = k[i + len(prefix):] if i >= 0 else ""
        if name and "/" not in name and (name.endswith(".md") or name == "skills.json"):
            try:
                out[name] = base64.b64decode(blob).decode("utf-8")
            except Exception:
                logger.warning("undecodable embedded skill %s", key)
    return out


def _shipped_skills(plat: str) -> dict:
    """{filename: text} of the default skills to seed for `plat`.

    Source of truth is autouse_data/skills/<plat>/ in the repo — those files are
    tracked in git and packed into the binary, so the exe can populate a machine
    that has no <home>/autouse_data/skills/ yet. In a compiled build they arrive
    as embedded blobs; in dev they're read straight off disk. Falls back to the
    older Auto_Use/<pkg>/agent/skills/ copies if the packed set is missing."""
    return (_skills_from_resources("autouse_data/skills/%s/" % plat)
            or _skills_from_dir(_REPO_ROOT / "autouse_data" / "skills" / plat)
            or _skills_from_resources("%s/agent/skills/" % plat)
            or _skills_from_dir(_REPO_ROOT / "Auto_Use" / plat / "agent" / "skills"))


def vault_file() -> Path:
    """autouse_data/vault/credentials.json — the app's saved credentials.

    Created empty ({}) if absent, so the folder is discoverable and the user has
    an obvious file to fill in. Lives outside the install folder like everything
    else here, so an uninstall can't take the credentials with it."""
    p = _ensure(data_root() / "vault") / "credentials.json"
    if not p.exists():
        try:
            # write_text -> io.open, so the compiled build's builtins.open patch
            # can't swallow the write into a throwaway buffer.
            p.write_text("{}", encoding="utf-8")
        except Exception:
            logger.exception("create %s", p)
    return p


def skills_dir(platform=None) -> Path:
    """autouse_data/skills/<windows|mac>, seeded on first use."""
    plat = skills_platform(platform)
    d = data_root() / "skills" / plat
    if d.is_dir():
        return d                       # already set up — never re-seed
    _ensure(d)
    for name, text in _shipped_skills(plat).items():
        try:
            # write_text -> io.open, so the compiled build's builtins.open
            # patch can't swallow the write into a throwaway buffer.
            (d / name).write_text(text, encoding="utf-8")
        except Exception:
            logger.warning("could not seed skill %s", name)
    return d


# =============================================================================
# api_key.txt — provider keys, the Telegram bot token, and Vertex config
# =============================================================================
# Every consumer must agree on this ONE path, or the Settings panel writes a key
# the agent can't read. Before this moved there were three different answers in
# the tree: frontend/service.py and windows/llm_provider walked up to
# Auto_Use/api_key/ (right), while mac and ios llm_provider stopped one
# level short at <pkg>/api_key/ (a folder that never existed, so Vertex config
# silently read back empty on those platforms). One function, one answer.
_api_key_migrated = False
_api_key_lock = threading.Lock()


def api_key_file() -> Path:
    """autouse_data/api_key/api_key.txt, migrating a pre-autouse_data copy on
    the first call in each process. Idempotent and never raises."""
    path = _ensure(data_root() / "api_key") / "api_key.txt"
    _migrate_legacy_api_key(path)
    return path


def _migrate_legacy_api_key(dest: Path) -> None:
    """Move Auto_Use/api_key/api_key.txt out of the install folder, once.

    Crash-safe by construction: the copy is staged as '<dest>.part' and renamed
    into place (atomic within the folder), and the legacy file is unlinked only
    after the rename succeeds. Skip-if-destination-exists makes a re-run a no-op
    and means the newer file always wins, so a downgrade/upgrade round trip can
    never overwrite live keys with stale ones."""
    global _api_key_migrated
    if _api_key_migrated or dest.exists():
        return
    with _api_key_lock:
        if _api_key_migrated or dest.exists():
            return
        try:
            legacy = install_dir() / "Auto_Use" / "api_key" / "api_key.txt"
            if not legacy.is_file() or legacy.resolve() == dest.resolve():
                return
            # Path.read_bytes/write_bytes go through io.open, which the compiled
            # build's builtins.open patch does NOT replace.
            part = dest.with_name(dest.name + ".part")
            part.write_bytes(legacy.read_bytes())
            os.replace(str(part), str(dest))
            try:
                legacy.unlink()
                legacy.parent.rmdir()      # only succeeds if now empty
            except OSError:
                pass
            logger.info("migrated api_key.txt: %s -> %s", legacy, dest)
        except Exception:
            logger.exception("migrate legacy api_key.txt")
        finally:
            # Set even on failure so we don't re-scan on every call; the next
            # app start retries from scratch.
            _api_key_migrated = True
