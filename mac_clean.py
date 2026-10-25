#!/usr/bin/env python3
"""mac-clean - an interactive TUI cleaner for macOS.

Reclaims disk space from caches and rebuildable artifacts. Dry run by
default: nothing is deleted until you switch to APPLY mode and confirm.

Safety model:
  * every deletion must resolve to a path under $HOME
  * a denylist protects home, Developer, Documents, Desktop, Downloads,
    Library, Pictures, Movies, Music, .ssh, .gnupg, .config, .local,
    .cache, .npm, .gradle, .android, Library/Android, Library/Caches,
    Library/Application Support, Library/Logs and .Trash themselves
  * .git, dotfiles, credentials, source files and browser profiles are
    never targeted
  * dist/, build/, out/ and coverage/ are only deleted when git confirms
    they are ignored and contain no tracked files
  * rm -rf -- is used so an empty variable can never expand to /

Usage:
  ./mac-clean                       # TUI, dry run, safe categories
  ./mac-clean --all                 # TUI, dry run, everything safe
  ./mac-clean --all --apply         # TUI, apply mode (still confirms)
  ./mac-clean --system --apply -y   # TUI, system only, no confirm
  ./mac-clean --no-tui --all        # plain-text dry-run report
  ./mac-clean --no-tui --all --apply --yes
"""

from __future__ import annotations

import argparse
import curses
import locale
import os
import queue
import shutil
import subprocess
import sys
import textwrap
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

APP_NAME = "mac-clean"
VERSION = "1.0.0"

# ---------------------------------------------------------------------------
# environment
# ---------------------------------------------------------------------------

_raw_home = os.environ.get("HOME", "")
HOME_INVALID = (not _raw_home) or _raw_home == "/"
HOME = Path(_raw_home).expanduser().resolve() if not HOME_INVALID else Path("/nonexistent")

ENV = os.environ.copy()
_path = ENV.get("PATH", "")
for _p in ("/opt/homebrew/bin", "/usr/local/bin", str(HOME / ".local/bin"), str(HOME / ".spicetify")):
    if _p not in _path.split(":"):
        _path = _p + ":" + _path
ENV["PATH"] = _path
ENV.setdefault("HOMEBREW_NO_AUTO_UPDATE", "1")
ENV.setdefault("HOMEBREW_NO_ENV_HINTS", "1")

try:
    _encoding = (locale.getpreferredencoding(False) or "").upper()
except Exception:
    _encoding = "UTF-8"
UTF8 = "UTF" in _encoding

ICON_OK = "\u2714" if UTF8 else "+"
ICON_FAIL = "\u2718" if UTF8 else "x"
ICON_SKIP = "\u00b7" if UTF8 else "-"
ICON_DRY = "\u2022" if UTF8 else "o"
SPIN = "\u280b\u2819\u2839\u2838\u283c\u2834\u2826\u2827\u2807\u280f" if UTF8 else "|/-\\"
RULE = "\u2500" if UTF8 else "-"
VLINE = "\u2502" if UTF8 else "|"
TL = "\u250c" if UTF8 else "+"
TR = "\u2510" if UTF8 else "+"
BL = "\u2514" if UTF8 else "+"
BR = "\u2518" if UTF8 else "+"
ARROWS = "\u2191\u2193" if UTF8 else "Up/Down"

CATEGORY_ORDER = [
    "brew",
    "dev",
    "apps",
    "xcode",
    "android",
    "android_deep",
    "system",
    "docker",
]

# key: (title, description, destructive)
CATEGORY_META = {
    "brew": (
        "Homebrew",
        "Cached downloads, old formula versions and unused dependencies.",
        False,
    ),
    "dev": (
        "Dev artifacts",
        "node_modules, build output and package-manager caches under your project roots.",
        False,
    ),
    "apps": (
        "App caches",
        "Spotify, Chrome, Stremio, editor and Electron app caches. All rebuild automatically.",
        False,
    ),
    "xcode": (
        "Xcode",
        "DerivedData, iOS device support, SwiftUI previews and simulator caches.",
        False,
    ),
    "android": (
        "Android",
        "Gradle and Android Studio build caches. Keeps your AVDs and SDK images.",
        False,
    ),
    "android_deep": (
        "Android emulators",
        "AVD profiles and SDK system images. Emulators must be recreated afterwards.",
        True,
    ),
    "system": (
        "System",
        "Logs older than 7 days, crash reports older than 30 days, and Trash.",
        False,
    ),
    "docker": (
        "Docker",
        "Prunes ALL unused images, containers, networks and volumes. Destructive.",
        True,
    ),
}

# Names that are always safe to delete because they are pure build output.
SAFE_NAMES = {
    "node_modules",
    ".next",
    ".turbo",
    ".expo",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "__pycache__",
    "test-results",
    "playwright-report",
    ".parcel-cache",
}

# Names that are only safe when git says they are ignored and untracked.
AMBIG_NAMES = {"dist", "build", "out", "coverage"}

# Directories that are never descended into while looking for build output.
SKIP_DIRS = {
    ".git",
    ".Trash",
    "Library",
    "Documents",
    "Desktop",
    "Downloads",
    "Pictures",
    "Movies",
    "Music",
    ".ssh",
    ".gnupg",
    ".config",
    ".local",
    ".cache",
    ".npm",
    ".gradle",
    ".android",
    ".venv",
    "venv",
    ".tox",
    ".eggs",
    "Pods",
    "DerivedData",
}

_DENY_NAMES = (
    "Developer",
    "Documents",
    "Desktop",
    "Downloads",
    "Library",
    "Pictures",
    "Movies",
    "Music",
    ".ssh",
    ".gnupg",
    ".config",
    ".local",
    ".cache",
    ".npm",
    ".gradle",
    ".android",
    ".Trash",
    "Library/Caches",
    "Library/Application Support",
    "Library/Logs",
    "Library/Android",
)


def _make_deny() -> set[Path]:
    if HOME_INVALID:
        return set()
    out = {HOME}
    for name in _DENY_NAMES:
        out.add(HOME / name)
    return out


DENY = _make_deny()

# ---------------------------------------------------------------------------
# data
# ---------------------------------------------------------------------------


@dataclass
class Target:
    label: str
    kind: str = "path"  # path | command | files | note
    path: Path | None = None
    paths: list[Path] = field(default_factory=list)
    cmd: list[str] | None = None
    measure: Path | None = None
    size_kb: int = 0
    detail: str = ""
    destructive: bool = False


@dataclass
class Category:
    key: str
    title: str
    desc: str
    destructive: bool
    targets: list[Target] = field(default_factory=list)
    available: bool = True
    note: str = ""
    error: str = ""
    scanned: bool = False

    @property
    def total_kb(self) -> int:
        return sum(t.size_kb for t in self.targets if t.kind != "note")


@dataclass
class RunResult:
    cat: str
    label: str
    size_kb: int
    freed_kb: int
    status: str  # ok | fail | skip | dry
    message: str = ""


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def run_capture(cmd: list[str], timeout: int = 60):
    """Run a command without a TTY. Returns CompletedProcess or None."""
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=ENV,
            stdin=subprocess.DEVNULL,
        )
    except Exception:
        return None


def which(name: str) -> str | None:
    return shutil.which(name, path=ENV.get("PATH"))


def human_kb(kb: int) -> str:
    kb = max(0, int(kb or 0))
    if kb == 0:
        return "0 B"
    if kb < 1024:
        return f"{kb} KB"
    mb = kb / 1024.0
    if mb < 1024:
        return f"{mb:.0f} MB" if mb >= 100 else f"{mb:.1f} MB"
    gb = mb / 1024.0
    return f"{gb:.1f} GB" if gb < 100 else f"{gb:.0f} GB"


def du_kb(path: Path | str | None) -> int:
    if path is None:
        return 0
    try:
        p = Path(path)
        if not p.exists() or p.is_symlink():
            return 0
        r = run_capture(["du", "-sk", str(p)], timeout=300)
        if not r:
            return 0
        parts = r.stdout.split()
        if parts and parts[0].isdigit():
            return int(parts[0])
    except Exception:
        pass
    return 0


def file_kb(path: Path) -> int:
    try:
        return max(0, path.stat().st_size // 1024)
    except OSError:
        return 0


def display_path(path: Path) -> str:
    try:
        return "~/" + str(Path(path).relative_to(HOME))
    except Exception:
        return str(path)


def clip(text: str, width: int) -> str:
    text = str(text)
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    if width == 1:
        return "\u2026" if UTF8 else "~"
    return text[: width - 1] + ("\u2026" if UTF8 else "~")


def tail_text(text: str, limit: int = 500) -> str:
    text = " ".join((text or "").split())
    return text[-limit:] if len(text) > limit else text


def status_icon(status: str) -> str:
    return {
        "ok": ICON_OK,
        "fail": ICON_FAIL,
        "skip": ICON_SKIP,
        "dry": ICON_DRY,
    }.get(status, "?")


def safe_path(path: Path | str | None) -> bool:
    """True only if path is a removable path under $HOME."""
    if path is None or HOME_INVALID:
        return False
    try:
        p = Path(path)
        if not p.is_absolute() or p.is_symlink():
            return False
        resolved = p.resolve(strict=False)
    except Exception:
        return False
    try:
        if resolved == HOME or not resolved.is_relative_to(HOME):
            return False
    except Exception:
        return False
    if resolved in DENY:
        return False
    try:
        if os.path.ismount(resolved):
            return False
    except Exception:
        pass
    return True


def git_ignored(path: Path) -> bool:
    """True if path is ignored by git and contains no tracked files."""
    try:
        if (path / ".git").exists():
            return False
        parent = path.parent
        top = run_capture(["git", "-C", str(parent), "rev-parse", "--show-toplevel"], timeout=10)
        if not top or top.returncode != 0:
            return False
        repo = top.stdout.strip()
        if not repo:
            return False
        rel = os.path.relpath(path, repo)
        tracked = run_capture(["git", "-C", repo, "ls-files", "--", rel], timeout=15)
        if tracked and tracked.returncode == 0 and tracked.stdout.strip():
            return False
        ignored = run_capture(["git", "-C", repo, "check-ignore", "-q", "--", rel], timeout=15)
        return bool(ignored and ignored.returncode == 0)
    except Exception:
        return False


def find_build_dirs(root: Path) -> tuple[list[Path], list[Path]]:
    """Find safe and ambiguous build directories under root, without following links."""
    safe: list[Path] = []
    ambiguous: list[Path] = []
    if not root.is_dir() or root.is_symlink():
        return safe, ambiguous

    for dirpath, dirnames, _filenames in os.walk(root, followlinks=False):
        keep: list[str] = []
        for name in dirnames:
            full = Path(dirpath) / name
            try:
                if full.is_symlink():
                    continue
            except OSError:
                continue
            if name in SAFE_NAMES:
                safe.append(full)
                continue
            if name in AMBIG_NAMES:
                ambiguous.append(full)
                continue
            if name in SKIP_DIRS:
                continue
            if name.startswith("."):
                continue
            keep.append(name)
        dirnames[:] = keep
    return safe, ambiguous


def path_target(path: Path, detail: str = "rebuildable artifact", destructive: bool = False) -> Target:
    return Target(
        label=display_path(path),
        kind="path",
        path=path,
        size_kb=du_kb(path),
        detail=detail,
        destructive=destructive,
    )


def make_log_path() -> Path:
    logdir = HOME / "Library" / "Logs"
    logdir.mkdir(parents=True, exist_ok=True)
    base = datetime.now().strftime("mac-clean-%Y%m%d-%H%M%S")
    path = logdir / f"{base}.log"
    i = 1
    while path.exists():
        path = logdir / f"{base}-{i}.log"
        i += 1
    return path


def parse_roots(value: str) -> list[Path]:
    roots: list[Path] = []
    for part in (value or "").split(os.pathsep):
        part = part.strip()
        if part:
            roots.append(Path(part).expanduser())
    return roots or [HOME / "Developer"]


# ---------------------------------------------------------------------------
# scanners
# ---------------------------------------------------------------------------


def scan_brew() -> tuple[list[Target], bool, str]:
    brew = which("brew")
    if not brew:
        return [], False, "Homebrew not installed"

    targets: list[Target] = []
    cache = HOME / "Library" / "Caches" / "Homebrew"
    targets.append(
        Target(
            label="brew cleanup --prune=all -s",
            kind="command",
            cmd=[brew, "cleanup", "--prune=all", "-s"],
            measure=cache,
            size_kb=du_kb(cache),
            detail="Removes cached downloads and old formula versions",
        )
    )
    targets.append(
        Target(
            label="brew autoremove",
            kind="command",
            cmd=[brew, "autoremove"],
            size_kb=0,
            detail="Removes unused dependencies (formulae only)",
        )
    )
    return targets, True, "Formulae and casks stay installed; only cached downloads and unused deps go."


def scan_dev(roots: list[Path]) -> tuple[list[Target], bool, str]:
    targets: list[Target] = []
    notes: list[Target] = []
    commands: list[Target] = []

    for root in roots:
        try:
            resolved = root.resolve()
            if resolved != HOME and not resolved.is_relative_to(HOME):
                notes.append(
                    Target(
                        label=str(root),
                        kind="note",
                        detail="skipped: outside $HOME",
                    )
                )
                continue
        except Exception:
            continue
        if not root.is_dir():
            continue

        safe_dirs, ambiguous = find_build_dirs(root)
        for path in safe_dirs:
            targets.append(path_target(path, "build output, reinstalled on demand"))
        for path in ambiguous:
            if git_ignored(path):
                targets.append(path_target(path, "gitignored build output"))
            else:
                notes.append(
                    Target(
                        label=display_path(path),
                        kind="note",
                        size_kb=du_kb(path),
                        detail="kept: tracked or not gitignored",
                    )
                )

    # Package-manager caches
    npm = which("npm")
    npm_cache = HOME / ".npm" / "_cacache"
    if npm and npm_cache.is_dir():
        commands.append(
            Target(
                label="npm cache",
                kind="command",
                cmd=[npm, "cache", "clean", "--force"],
                measure=npm_cache,
                size_kb=du_kb(npm_cache),
                detail="npm's content-addressable cache",
            )
        )

    pnpm = which("pnpm")
    pnpm_store = HOME / "Library" / "pnpm" / "store"
    if pnpm and pnpm_store.is_dir():
        commands.append(
            Target(
                label="pnpm store",
                kind="command",
                cmd=[pnpm, "store", "prune"],
                measure=pnpm_store,
                size_kb=du_kb(pnpm_store),
                detail="Removes unreferenced packages from the pnpm store",
            )
        )

    uv = which("uv")
    for uv_cache in (HOME / ".cache" / "uv", HOME / "Library" / "Caches" / "uv"):
        if uv and uv_cache.is_dir():
            commands.append(
                Target(
                    label="uv cache",
                    kind="command",
                    cmd=[uv, "cache", "clean"],
                    measure=uv_cache,
                    size_kb=du_kb(uv_cache),
                    detail="Python package cache",
                )
            )
            break

    go = which("go")
    go_cache = HOME / "Library" / "Caches" / "go-build"
    if go and go_cache.is_dir():
        commands.append(
            Target(
                label="Go build cache",
                kind="command",
                cmd=[go, "clean", "-cache"],
                measure=go_cache,
                size_kb=du_kb(go_cache),
                detail="Compiled Go package cache",
            )
        )

    extra_paths = [
        (HOME / "Library" / "Caches" / "node-gyp", "node-gyp headers"),
        (HOME / "Library" / "Caches" / "pnpm", "pnpm metadata cache"),
        (HOME / "Library" / "Caches" / "ms-playwright", "Playwright browsers"),
        (HOME / "Library" / "Caches" / "CocoaPods", "CocoaPods cache"),
        (HOME / ".cache" / "codex-runtimes", "Codex runtimes (re-download on demand)"),
        (HOME / ".cache" / "pkg", "pkg / Vercel cache"),
    ]
    for path, label in extra_paths:
        if path.exists() and not path.is_symlink():
            targets.append(path_target(path, label))

    targets.sort(key=lambda t: t.size_kb, reverse=True)
    notes.sort(key=lambda t: t.size_kb, reverse=True)
    commands.sort(key=lambda t: t.size_kb, reverse=True)

    if not targets and not commands:
        if notes:
            return notes, True, "Nothing removable; tracked or non-ignored build dirs are kept."
        return [], False, "No project roots or package caches found."
    return targets + notes + commands, True, "dist/build/out/coverage are only removed when gitignored and untracked."


EXPLICIT_APP_CACHES = [
    ("Library/Caches/com.spotify.client", "Spotify stream cache"),
    ("Library/Caches/Google", "Google / Chrome cache"),
    ("Library/Caches/GeoServices", "Maps geodata"),
    ("Library/Caches/com.westbridge.stremio5-mac", "Stremio cache"),
    ("Library/Caches/com.apple.helpd", "Help index"),
    ("Library/Caches/CloudKit", "CloudKit cache"),
    ("Library/Caches/com.apple.intelligenceflow.intelligenceflowd", "Apple Intelligence cache"),
    ("Library/Caches/com.apple.Safari", "Safari cache"),
    ("Library/Application Support/Code/Cache", "VS Code Cache"),
    ("Library/Application Support/Code/CachedData", "VS Code CachedData"),
    ("Library/Application Support/Cursor/Cache", "Cursor Cache"),
    ("Library/Application Support/Cursor/CachedData", "Cursor CachedData"),
]

APP_SUPPORT_CACHE_APPS = [
    "discord",
    "Notion",
    "obsidian",
    "Figma",
    "Claude",
    "ChatGPT",
    "Termius",
    "Beekeeper Studio",
]

APP_SUPPORT_CACHE_DIRS = ("Cache", "Code Cache", "GPUCache", "CachedData")


def scan_apps() -> tuple[list[Target], bool, str]:
    targets: list[Target] = []
    seen: set[Path] = set()

    def add(path: Path, label: str) -> None:
        if not path.exists() or path.is_symlink():
            return
        try:
            resolved = path.resolve(strict=False)
        except Exception:
            return
        if resolved in seen:
            return
        seen.add(resolved)
        targets.append(path_target(path, label))

    for rel, label in EXPLICIT_APP_CACHES:
        add(HOME / rel, label)

    for app in APP_SUPPORT_CACHE_APPS:
        base = HOME / "Library" / "Application Support" / app
        if not base.is_dir():
            continue
        for sub in APP_SUPPORT_CACHE_DIRS:
            add(base / sub, f"{app} cache ({sub})")

    targets.sort(key=lambda t: t.size_kb, reverse=True)
    if not targets:
        return [], False, "No known app caches found."
    return targets, True, "Quit apps first for the cleanest result; caches rebuild automatically."


def scan_xcode() -> tuple[list[Target], bool, str]:
    xcode = Path("/Applications/Xcode.app")
    xcrun = which("xcrun")
    if not xcode.exists() and not xcrun:
        return [], False, "Xcode not installed"

    targets: list[Target] = []
    paths = [
        ("Library/Developer/Xcode/DerivedData", "Build products and indexes"),
        ("Library/Developer/Xcode/iOS DeviceSupport", "Re-downloaded when a device connects"),
        ("Library/Developer/Xcode/UserData/Previews", "SwiftUI preview caches"),
        ("Library/Developer/CoreSimulator/Caches", "Simulator caches"),
    ]
    for rel, label in paths:
        path = HOME / rel
        if path.exists() and not path.is_symlink():
            targets.append(path_target(path, label))

    if xcrun:
        targets.append(
            Target(
                label="Delete unavailable simulators",
                kind="command",
                cmd=[xcrun, "simctl", "delete", "unavailable"],
                size_kb=0,
                detail="Removes simulators whose runtime no longer exists",
            )
        )

    archives = HOME / "Library" / "Developer" / "Xcode" / "Archives"
    if archives.exists():
        targets.append(
            Target(
                label="Xcode Archives (kept)",
                kind="note",
                size_kb=du_kb(archives),
                detail="App Store builds are never deleted",
            )
        )

    targets.sort(key=lambda t: t.size_kb, reverse=True)
    return targets, True, "Archives are deliberately kept; device support re-downloads on demand."


def scan_android() -> tuple[list[Target], bool, str]:
    targets: list[Target] = []
    paths = [
        (HOME / ".gradle" / "caches", "Gradle dependency and build caches"),
        (HOME / ".android" / "cache", "Android SDK download cache"),
        (HOME / ".android" / "build-cache", "Android build cache"),
    ]
    for path, label in paths:
        if path.exists() and not path.is_symlink():
            targets.append(path_target(path, label))

    for pattern in ("Library/Caches/Google/AndroidStudio*", "Library/Caches/Google/android-studio*"):
        for path in HOME.glob(pattern):
            if path.is_dir() and not path.is_symlink():
                targets.append(path_target(path, "Android Studio indexes (rebuild on launch)"))

    targets.sort(key=lambda t: t.size_kb, reverse=True)
    if not targets:
        return [], False, "No Gradle or Android Studio caches found."
    return targets, True, "AVDs and SDK system images are kept unless you enable Android emulators."


def scan_android_deep() -> tuple[list[Target], bool, str]:
    targets: list[Target] = []
    paths = [
        (HOME / ".android" / "avd", "AVD profiles and emulator user data"),
        (HOME / "Library" / "Android" / "sdk" / "system-images", "SDK system images"),
    ]
    for path, label in paths:
        if path.exists() and not path.is_symlink():
            targets.append(path_target(path, label, destructive=True))

    targets.sort(key=lambda t: t.size_kb, reverse=True)
    if not targets:
        return [], False, "No AVDs or system images found."
    return targets, True, "Destructive: every emulator must be recreated and images re-downloaded."


def _old_files(root: Path, days: int) -> list[Path]:
    cutoff = time.time() - days * 86400
    out: list[Path] = []
    if not root.is_dir():
        return out
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [
            d for d in dirnames
            if not (Path(dirpath) / d).is_symlink()
        ]
        for name in filenames:
            path = Path(dirpath) / name
            if path.is_symlink():
                continue
            try:
                if path.stat().st_mtime < cutoff:
                    out.append(path)
            except OSError:
                continue
    return out


def scan_system() -> tuple[list[Target], bool, str]:
    targets: list[Target] = []
    logs = HOME / "Library" / "Logs"

    old_logs: list[Path] = []
    if logs.is_dir():
        for dirpath, dirnames, filenames in os.walk(logs, followlinks=False):
            dirnames[:] = [
                d for d in dirnames
                if d != "DiagnosticReports" and not (Path(dirpath) / d).is_symlink()
            ]
            for name in filenames:
                path = Path(dirpath) / name
                if path.is_symlink():
                    continue
                try:
                    if path.stat().st_mtime < time.time() - 7 * 86400:
                        old_logs.append(path)
                except OSError:
                    continue
    if old_logs:
        size = sum(file_kb(p) for p in old_logs)
        targets.append(
            Target(
                label=f"Logs older than 7 days ({len(old_logs)} files)",
                kind="files",
                paths=old_logs,
                size_kb=size,
                detail="~/Library/Logs, excluding crash reports",
            )
        )

    diagnostics = _old_files(logs / "DiagnosticReports", 30)
    if diagnostics:
        size = sum(file_kb(p) for p in diagnostics)
        targets.append(
            Target(
                label=f"Crash reports older than 30 days ({len(diagnostics)} files)",
                kind="files",
                paths=diagnostics,
                size_kb=size,
                detail="~/Library/Logs/DiagnosticReports",
            )
        )

    trash = HOME / ".Trash"
    if trash.is_dir():
        try:
            items = sorted(trash.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            items = []
        for item in items:
            if item.is_symlink():
                targets.append(
                    Target(
                        label=f"Trash: {item.name}",
                        kind="note",
                        detail="skipped: symlink",
                    )
                )
                continue
            targets.append(
                Target(
                    label=f"Trash: {item.name}",
                    kind="path",
                    path=item,
                    size_kb=du_kb(item),
                    detail="Emptied automatically when System runs",
                )
            )

    if not targets:
        return [], True, "Nothing to clean: logs are fresh and Trash is empty."
    return targets, True, "Trash is emptied automatically in APPLY mode; there is no separate prompt."


def scan_docker() -> tuple[list[Target], bool, str]:
    docker = which("docker")
    if not docker:
        return [], False, "Docker CLI not installed"
    info = run_capture([docker, "info"], timeout=3)
    if not info or info.returncode != 0:
        return [], False, "Docker daemon not running (start OrbStack or Docker Desktop)"
    return (
        [
            Target(
                label="docker system prune -af --volumes",
                kind="command",
                cmd=[docker, "system", "prune", "-af", "--volumes"],
                size_kb=0,
                detail="Removes ALL unused images, containers, networks and volumes",
                destructive=True,
            )
        ],
        True,
        "This deletes unused Docker images, containers and volumes. Size is reported after pruning.",
    )


def scan_category(key: str, roots: list[Path]) -> Category:
    title, desc, destructive = CATEGORY_META[key]
    cat = Category(key=key, title=title, desc=desc, destructive=destructive)
    try:
        if key == "brew":
            targets, available, note = scan_brew()
        elif key == "dev":
            targets, available, note = scan_dev(roots)
        elif key == "apps":
            targets, available, note = scan_apps()
        elif key == "xcode":
            targets, available, note = scan_xcode()
        elif key == "android":
            targets, available, note = scan_android()
        elif key == "android_deep":
            targets, available, note = scan_android_deep()
        elif key == "system":
            targets, available, note = scan_system()
        elif key == "docker":
            targets, available, note = scan_docker()
        else:
            targets, available, note = [], False, "Unknown category"
        cat.targets = targets
        cat.available = available
        cat.note = note
    except Exception as exc:
        cat.available = False
        cat.error = str(exc)
        cat.note = f"Scan failed: {exc}"
    cat.scanned = True
    return cat


# ---------------------------------------------------------------------------
# execution
# ---------------------------------------------------------------------------


def execute_target(target: Target, dry: bool, log) -> RunResult:
    base = dict(cat="", label=target.label, size_kb=target.size_kb, freed_kb=0)

    if target.kind == "note":
        return RunResult(**base, status="skip", message=target.detail)

    if target.kind == "path":
        path = target.path
        if path is None or not safe_path(path):
            log(f"REFUSED unsafe path: {path}")
            return RunResult(**base, status="fail", message="refused: unsafe path")
        if path.is_symlink():
            return RunResult(**base, status="skip", message="symlink skipped")
        if not path.exists():
            return RunResult(**base, status="skip", message="already gone")

        size = du_kb(path)
        base["size_kb"] = size
        if dry:
            base["freed_kb"] = size
            return RunResult(**base, status="dry", message=target.detail)

        log(f"rm -rf -- {path}")
        try:
            proc = subprocess.run(
                ["rm", "-rf", "--", str(path)],
                capture_output=True,
                text=True,
                timeout=600,
                env=ENV,
                stdin=subprocess.DEVNULL,
            )
        except Exception as exc:
            return RunResult(**base, status="fail", message=str(exc))

        still_there = path.exists() or path.is_symlink()
        after = du_kb(path) if path.exists() else 0
        freed = max(0, size - after)
        base["freed_kb"] = freed
        if still_there:
            msg = tail_text(proc.stderr) or "could not remove (permissions?)"
            return RunResult(**base, status="fail", message=msg)
        return RunResult(**base, status="ok", message=target.detail)

    if target.kind == "command":
        if not target.cmd:
            return RunResult(**base, status="fail", message="empty command")
        if dry:
            base["freed_kb"] = target.size_kb
            return RunResult(**base, status="dry", message=target.detail)

        before = du_kb(target.measure) if target.measure else 0
        base["size_kb"] = before
        log(f"$ {' '.join(target.cmd)}")
        try:
            proc = subprocess.run(
                target.cmd,
                capture_output=True,
                text=True,
                timeout=1800,
                env=ENV,
                stdin=subprocess.DEVNULL,
            )
        except Exception as exc:
            return RunResult(**base, status="fail", message=str(exc))

        after = du_kb(target.measure) if target.measure else 0
        freed = max(0, before - after)
        base["freed_kb"] = freed
        if proc.returncode != 0:
            msg = tail_text(proc.stderr or proc.stdout) or f"exit {proc.returncode}"
            return RunResult(**base, status="fail", message=msg)
        msg = tail_text(proc.stdout) or "done"
        return RunResult(**base, status="ok", message=msg)

    if target.kind == "files":
        if dry:
            base["freed_kb"] = target.size_kb
            return RunResult(**base, status="dry", message=target.detail)
        freed_kb = 0
        removed = 0
        failed = 0
        for path in target.paths:
            if not safe_path(path):
                failed += 1
                log(f"REFUSED unsafe file: {path}")
                continue
            if path.is_symlink():
                failed += 1
                continue
            if not path.exists():
                continue
            size = file_kb(path)
            try:
                path.unlink()
                removed += 1
                freed_kb += size
            except OSError as exc:
                failed += 1
                log(f"FAILED {path}: {exc}")
        msg = f"{removed} files removed"
        if failed:
            msg += f", {failed} failed"
        base["freed_kb"] = freed_kb
        return RunResult(
            **base,
            status="ok" if failed == 0 else "fail",
            message=msg,
        )

    return RunResult(**base, status="fail", message=f"unknown target kind: {target.kind}")


def run_cleanup(items, dry: bool, log_path: Path | None, on_start=None, on_done=None):
    results: list[RunResult] = []
    logf = None
    if log_path:
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            logf = open(log_path, "a", encoding="utf-8")
        except OSError:
            logf = None

    def log(message: str) -> None:
        if logf:
            try:
                logf.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {message}\n")
                logf.flush()
            except OSError:
                pass

    log(f"=== {APP_NAME} {VERSION} {'APPLY' if not dry else 'DRY RUN'} ===")
    log(f"home: {HOME}")
    try:
        for cat, target in items:
            if on_start:
                on_start(cat, target)
            try:
                result = execute_target(target, dry, log)
            except Exception as exc:
                result = RunResult(
                    cat=cat,
                    label=target.label,
                    size_kb=target.size_kb,
                    freed_kb=0,
                    status="fail",
                    message=str(exc),
                )
            result.cat = cat
            results.append(result)
            if on_done:
                on_done(result)
            log(
                f"{result.status.upper():7} {cat}/{result.label} "
                f"freed={human_kb(result.freed_kb)} {result.message}"
            )
        total = sum(r.freed_kb for r in results)
        log(f"=== total: {human_kb(total)} ===")
    finally:
        if logf:
            logf.close()
    return results, total


# ---------------------------------------------------------------------------
# CLI mode
# ---------------------------------------------------------------------------


def selected_keys(opts) -> list[str]:
    keys: list[str] = []
    if opts.all:
        keys.extend(["brew", "dev", "apps", "xcode", "android", "system"])
    for flag, key in (
        ("brew", "brew"),
        ("dev", "dev"),
        ("apps", "apps"),
        ("xcode", "xcode"),
        ("android", "android"),
        ("system", "system"),
        ("docker", "docker"),
    ):
        if getattr(opts, flag, False):
            keys.append(key)
    if opts.android_deep:
        keys.extend(["android", "android_deep"])
    if not keys:
        keys = ["brew", "dev", "apps"]
    wanted = set(keys)
    return [key for key in CATEGORY_ORDER if key in wanted]


def cli_main(opts) -> int:
    roots = parse_roots(opts.roots)
    keys = selected_keys(opts)
    mode = "APPLY" if opts.apply else "DRY RUN"
    print(f"{APP_NAME} {VERSION} - {mode}")
    print(f"home: {HOME}")
    print(f"scan roots: {', '.join(display_path(r) for r in roots)}")

    cats: list[Category] = []
    for key in keys:
        print(f"scanning {CATEGORY_META[key][0]}...", flush=True)
        cats.append(scan_category(key, roots))

    estimated = 0
    for cat in cats:
        print(f"\n{cat.title}  {human_kb(cat.total_kb)}")
        if cat.error:
            print(f"  ! {cat.error}")
            continue
        if not cat.available:
            print(f"  {cat.note}")
            continue
        if cat.note:
            print(f"  {cat.note}")
        for target in cat.targets:
            if target.kind == "note":
                extra = f"  {human_kb(target.size_kb)}" if target.size_kb else ""
                print(f"  {ICON_SKIP} {target.label}{extra}  ({target.detail})")
            else:
                print(f"  {ICON_DRY} {target.label:<52} {human_kb(target.size_kb):>9}")
        estimated += cat.total_kb

    print(f"\nEstimated reclaim: {human_kb(estimated)}")
    if not opts.apply:
        print("Dry run - nothing was deleted. Re-run with --apply to delete.")
        return 0

    items = [
        (cat.key, target)
        for cat in cats
        if cat.available
        for target in cat.targets
        if target.kind != "note"
    ]
    if not items:
        print("Nothing to delete.")
        return 0

    if not opts.yes:
        if not sys.stdin.isatty():
            print("Refusing to delete without --yes in non-interactive mode.", file=sys.stderr)
            return 2
        answer = input("Type 'yes' to permanently delete: ").strip().lower()
        if answer != "yes":
            print("Cancelled.")
            return 1

    try:
        log_path = make_log_path()
    except OSError as exc:
        print(f"Could not create log file: {exc}", file=sys.stderr)
        return 1
    print(f"\nlog: {log_path}")

    def on_start(cat: str, target: Target) -> None:
        print(f"  -> {cat}/{target.label}", flush=True)

    def on_done(result: RunResult) -> None:
        print(
            f"     {status_icon(result.status)} {human_kb(result.freed_kb):>9}  {result.message}",
            flush=True,
        )

    results, freed = run_cleanup(items, False, log_path, on_start, on_done)
    print(f"\nReclaimed: {human_kb(freed)}")
    failures = [r for r in results if r.status == "fail"]
    if failures:
        print(f"{len(failures)} target(s) failed - see {log_path}")
        return 1
    return 0


# ---------------------------------------------------------------------------
# TUI
# ---------------------------------------------------------------------------


class App:
    def __init__(self, stdscr, opts):
        self.scr = stdscr
        self.opts = opts
        self.roots = parse_roots(opts.roots)
        self.apply_mode = bool(opts.apply)
        self.selected = set(selected_keys(opts))
        self.cats: dict[str, Category] = {}
        for key in CATEGORY_ORDER:
            title, desc, destructive = CATEGORY_META[key]
            self.cats[key] = Category(key=key, title=title, desc=desc, destructive=destructive)
        selected_order = [i for i, key in enumerate(CATEGORY_ORDER) if key in self.selected]
        self.focus = selected_order[0] if selected_order else 0
        self.scanning: set[str] = set()
        self.scanning_all = False
        self.scan_complete = False
        self.q: queue.Queue = queue.Queue()
        self.screen = "main"  # main | confirm | running | results
        self.status = "Starting..."
        self.status_kind = "info"
        self.spinner = 0
        self.results: list[RunResult] = []
        self.run_total = 0
        self.run_done = 0
        self.current = ""
        self.result_scroll = 0
        self.log_path: Path | None = None
        self.last_total = 0
        self.last_apply = False
        self.run_apply = False
        self.show_help = False
        self._init_colors()

    # -- colors ------------------------------------------------------------

    def _init_colors(self) -> None:
        self.A_BOLD = curses.A_BOLD
        self.A_ACCENT = curses.A_BOLD
        self.A_BLUE = curses.A_BOLD
        self.A_GREEN = curses.A_BOLD
        self.A_YELLOW = curses.A_BOLD
        self.A_RED = curses.A_BOLD
        self.A_CYAN = curses.A_BOLD
        self.A_DIM = curses.A_DIM
        self.A_FOCUS = curses.A_REVERSE
        try:
            if not curses.has_colors():
                return
            curses.start_color()
            try:
                curses.use_default_colors()
            except curses.error:
                pass
            if curses.COLORS >= 256:
                defs = [
                    (183, -1),  # mauve
                    (111, -1),  # blue
                    (114, -1),  # green
                    (179, -1),  # yellow
                    (203, -1),  # red
                    (116, -1),  # cyan
                    (245, -1),  # dim
                    (252, 236),  # selected row
                ]
            else:
                defs = [
                    (curses.COLOR_MAGENTA, -1),
                    (curses.COLOR_BLUE, -1),
                    (curses.COLOR_GREEN, -1),
                    (curses.COLOR_YELLOW, -1),
                    (curses.COLOR_RED, -1),
                    (curses.COLOR_CYAN, -1),
                    (curses.COLOR_WHITE, -1),
                    (curses.COLOR_WHITE, curses.COLOR_BLACK),
                ]
            for i, (fg, bg) in enumerate(defs, 1):
                try:
                    curses.init_pair(i, fg, bg)
                except curses.error:
                    pass
            self.A_ACCENT = curses.color_pair(1) | curses.A_BOLD
            self.A_BLUE = curses.color_pair(2) | curses.A_BOLD
            self.A_GREEN = curses.color_pair(3) | curses.A_BOLD
            self.A_YELLOW = curses.color_pair(4) | curses.A_BOLD
            self.A_RED = curses.color_pair(5) | curses.A_BOLD
            self.A_CYAN = curses.color_pair(6) | curses.A_BOLD
            self.A_DIM = curses.color_pair(7) | curses.A_DIM
            self.A_FOCUS = curses.color_pair(8) | curses.A_BOLD
        except curses.error:
            pass

    def _status_attr(self):
        return {
            "ok": self.A_GREEN,
            "warn": self.A_YELLOW,
            "error": self.A_RED,
            "info": self.A_CYAN,
        }.get(self.status_kind, 0)

    def _result_attr(self, status: str):
        return {
            "ok": self.A_GREEN,
            "fail": self.A_RED,
            "dry": self.A_CYAN,
            "skip": self.A_DIM,
        }.get(status, 0)

    # -- main loop ---------------------------------------------------------

    def run(self) -> None:
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        self.scr.keypad(True)
        self.scr.timeout(120)
        self._start_scan()
        while True:
            self._drain()
            self._draw()
            ch = self.scr.getch()
            if ch == -1:
                self.spinner += 1
                continue
            if not self._handle(ch):
                break

    def _start_scan(self) -> None:
        if self.scanning_all:
            return
        self.scanning_all = True
        self.scan_complete = False
        self.scanning = set(CATEGORY_ORDER)
        self.status = "Scanning caches and project folders..."
        self.status_kind = "info"
        threading.Thread(target=self._scan_worker, daemon=True).start()

    def _scan_worker(self) -> None:
        for key in CATEGORY_ORDER:
            self.q.put(("scan_start", key))
            cat = scan_category(key, self.roots)
            self.q.put(("scan_done", key, cat))
        self.q.put(("scan_all_done",))

    def _run_worker(self, items, dry: bool, log_path: Path | None) -> None:
        def on_start(cat: str, target: Target) -> None:
            self.q.put(("target_start", cat, target.label))

        def on_done(result: RunResult) -> None:
            self.q.put(("target_done", result))

        try:
            results, total = run_cleanup(items, dry, log_path, on_start, on_done)
            self.q.put(("run_done", results, total))
        except Exception as exc:
            self.q.put(("run_error", str(exc)))

    def _drain(self) -> None:
        while True:
            try:
                msg = self.q.get_nowait()
            except queue.Empty:
                break
            tag = msg[0]
            if tag == "scan_start":
                self.scanning.add(msg[1])
            elif tag == "scan_done":
                key, cat = msg[1], msg[2]
                self.cats[key] = cat
                self.scanning.discard(key)
            elif tag == "scan_all_done":
                self.scanning_all = False
                self.scan_complete = True
                self.status = f"Scan complete - {human_kb(self._selected_total())} selected"
                self.status_kind = "ok"
            elif tag == "target_start":
                self.current = f"{msg[1]} / {msg[2]}"
            elif tag == "target_done":
                self.results.append(msg[1])
                self.run_done = len(self.results)
            elif tag == "run_done":
                self.results = msg[1]
                self.last_total = msg[2]
                self.last_apply = self.run_apply
                self.screen = "results"
                self.result_scroll = 0
                verb = "Reclaimed" if self.last_apply else "Would reclaim"
                self.status = f"{verb} {human_kb(self.last_total)}"
                self.status_kind = "ok"
            elif tag == "run_error":
                self.screen = "main"
                self.status = f"Cleanup failed: {msg[1]}"
                self.status_kind = "error"

    # -- keys --------------------------------------------------------------

    def _handle(self, ch: int) -> bool:
        if self.show_help:
            self.show_help = False
            return True

        if self.screen == "confirm":
            if ch in (ord("y"), ord("Y")):
                self._launch_run()
            elif ch in (ord("n"), ord("N"), ord("q"), 27):
                self.screen = "main"
                self.status = "Cancelled - nothing deleted"
                self.status_kind = "info"
            return True

        if self.screen == "running":
            if ch == ord("q"):
                self.status = "Cleanup in progress - please wait"
                self.status_kind = "warn"
            return True

        if self.screen == "results":
            if ch in (curses.KEY_UP, ord("k")):
                self.result_scroll = max(0, self.result_scroll - 1)
            elif ch in (curses.KEY_DOWN, ord("j")):
                self.result_scroll += 1
            elif ch == curses.KEY_PPAGE:
                self.result_scroll = max(0, self.result_scroll - 10)
            elif ch == curses.KEY_NPAGE:
                self.result_scroll += 10
            elif ch == ord("g"):
                self.result_scroll = 0
            elif ch == ord("G"):
                self.result_scroll = 10**6
            elif ch in (10, 13, curses.KEY_ENTER, 27):
                self.screen = "main"
            elif ch == ord("s"):
                self._rescan()
            elif ch == ord("q"):
                return False
            return True

        # main screen
        if ch == ord("q"):
            return False
        if ch in (curses.KEY_UP, ord("k")):
            self.focus = (self.focus - 1) % len(CATEGORY_ORDER)
        elif ch in (curses.KEY_DOWN, ord("j")):
            self.focus = (self.focus + 1) % len(CATEGORY_ORDER)
        elif ch == ord("g"):
            self.focus = 0
        elif ch == ord("G"):
            self.focus = len(CATEGORY_ORDER) - 1
        elif ch in (ord(" "), 10, 13, curses.KEY_ENTER):
            self._toggle_focus()
        elif ch == ord("a"):
            self._select_all()
        elif ch == ord("n"):
            self.selected.clear()
            self.status = "Selection cleared"
            self.status_kind = "info"
        elif ch == ord("m"):
            self._toggle_mode()
        elif ch == ord("r"):
            self._request_run()
        elif ch == ord("s"):
            self._rescan()
        elif ch == ord("?"):
            self.show_help = True
        return True

    def _toggle_focus(self) -> None:
        key = CATEGORY_ORDER[self.focus]
        cat = self.cats[key]
        if cat.scanned and not cat.available:
            self.status = cat.note or "Not available"
            self.status_kind = "warn"
            return
        if key in self.selected:
            self.selected.discard(key)
            self.status = f"Deselected {cat.title}"
            self.status_kind = "info"
        else:
            self.selected.add(key)
            if cat.destructive:
                self.status = f"Selected {cat.title} - destructive, read the warning"
                self.status_kind = "warn"
            else:
                self.status = f"Selected {cat.title}"
                self.status_kind = "ok"

    def _select_all(self) -> None:
        for key in CATEGORY_ORDER:
            if self.cats[key].available:
                self.selected.add(key)
        self.status = "Selected all available categories"
        self.status_kind = "ok"

    def _toggle_mode(self) -> None:
        self.apply_mode = not self.apply_mode
        if self.apply_mode:
            self.status = "APPLY mode - r will delete after confirmation"
            self.status_kind = "warn"
        else:
            self.status = "Dry run - nothing will be deleted"
            self.status_kind = "ok"

    def _rescan(self) -> None:
        if self.scanning_all or self.screen == "running":
            self.status = "Please wait - a scan or cleanup is already running"
            self.status_kind = "warn"
            return
        for key in CATEGORY_ORDER:
            title, desc, destructive = CATEGORY_META[key]
            self.cats[key] = Category(key=key, title=title, desc=desc, destructive=destructive)
        self.screen = "main"
        self.results = []
        self.last_total = 0
        self.result_scroll = 0
        self._start_scan()

    def _collect_items(self) -> list[tuple[str, Target]]:
        items: list[tuple[str, Target]] = []
        for key in CATEGORY_ORDER:
            if key not in self.selected:
                continue
            cat = self.cats[key]
            if not cat.available:
                continue
            for target in cat.targets:
                if target.kind != "note":
                    items.append((key, target))
        return items

    def _selected_total(self) -> int:
        return sum(
            self.cats[key].total_kb
            for key in self.selected
            if key in self.cats and self.cats[key].available
        )

    def _request_run(self) -> None:
        if not self.scan_complete:
            self.status = "Still scanning - wait for it to finish"
            self.status_kind = "warn"
            return
        items = self._collect_items()
        if not items:
            self.status = "Nothing selected to clean"
            self.status_kind = "warn"
            return
        if self.apply_mode and not self.opts.yes:
            self.screen = "confirm"
            return
        self._launch_run()

    def _launch_run(self) -> None:
        items = self._collect_items()
        if not items:
            self.screen = "main"
            self.status = "Nothing selected to clean"
            self.status_kind = "warn"
            return
        self.screen = "running"
        self.results = []
        self.run_total = len(items)
        self.run_done = 0
        self.current = "Preparing..."
        self.result_scroll = 0
        self.run_apply = self.apply_mode
        dry = not self.apply_mode
        self.log_path = None
        if not dry:
            try:
                self.log_path = make_log_path()
            except OSError as exc:
                self.status = f"Log unavailable: {exc}"
                self.status_kind = "warn"
        threading.Thread(
            target=self._run_worker,
            args=(items, dry, self.log_path),
            daemon=True,
        ).start()

    # -- drawing -----------------------------------------------------------

    def _put(self, y: int, x: int, text: str, attr=0) -> None:
        try:
            h, w = self.scr.getmaxyx()
            if y < 0 or y >= h or x < 0 or x >= w or not text:
                return
            self.scr.addnstr(y, x, str(text), max(0, w - x), attr)
        except curses.error:
            pass

    def _fill_line(self, y: int, attr=0) -> None:
        try:
            _h, w = self.scr.getmaxyx()
            self._put(y, 0, " " * w, attr)
        except curses.error:
            pass

    def _spin(self) -> str:
        return SPIN[(self.spinner // 2) % len(SPIN)]

    def _left_width(self, w: int) -> int:
        if w < 96:
            return w
        return min(54, int(w * 0.46))

    def _draw(self) -> None:
        self.scr.erase()
        h, w = self.scr.getmaxyx()
        if h < 12 or w < 50:
            self._put(0, 0, "Terminal too small - resize to at least 50x12", self.A_RED)
            self.scr.refresh()
            return
        if self.screen == "results":
            self._draw_results(h, w)
        elif self.screen == "running":
            self._draw_running(h, w)
        else:
            self._draw_main(h, w)
        if self.screen == "confirm":
            self._draw_confirm(h, w)
        if self.show_help:
            self._draw_help(h, w)
        self.scr.refresh()

    def _draw_header(self, w: int) -> None:
        try:
            free_gb = shutil.disk_usage(HOME).free / (1024**3)
            free_text = f"Free {free_gb:.0f} GB "
        except OSError:
            free_text = "Free ? "
        self._put(0, 0, " mac-clean", self.A_ACCENT)
        self._put(0, 10, "  TUI disk cleanup", self.A_DIM)
        self._put(0, max(0, w - len(free_text)), free_text, self.A_GREEN)
        self._put(1, 0, RULE * w, self.A_DIM)

    def _draw_row(self, y: int, left_w: int, key: str, focused: bool) -> None:
        cat = self.cats[key]
        selected = key in self.selected
        if cat.scanned and not cat.available:
            box = "[-]"
        else:
            box = "[x]" if selected else "[ ]"

        if key in self.scanning:
            size_text = self._spin()
            size_attr = self.A_DIM
        elif cat.error:
            size_text = "!"
            size_attr = self.A_RED
        elif cat.scanned and not cat.available:
            size_text = "-"
            size_attr = self.A_DIM
        else:
            size_text = human_kb(cat.total_kb)
            size_attr = self.A_GREEN if selected and cat.total_kb else self.A_DIM

        marker = "\u25b8" if (focused and UTF8) else (">" if focused else " ")
        prefix = f" {marker} {box} "
        size_x = max(0, left_w - 1 - len(size_text))
        title_w = max(1, size_x - len(prefix) - 1)

        if focused:
            self._fill_line(y, self.A_FOCUS)
            marker_attr = self.A_FOCUS
            title_attr = self.A_FOCUS
            size_attr = self.A_FOCUS
        else:
            marker_attr = self.A_DIM
            title_attr = self.A_RED if cat.destructive else 0
            if cat.scanned and not cat.available:
                title_attr = self.A_DIM

        self._put(y, 0, prefix[: len(prefix)], marker_attr)
        self._put(y, len(prefix), clip(cat.title, title_w), title_attr)
        self._put(y, size_x, size_text, size_attr)

    def _draw_rows(self, top: int, left_w: int, bottom: int, focus: int | None) -> None:
        for i, key in enumerate(CATEGORY_ORDER):
            y = top + i
            if y > bottom:
                break
            self._draw_row(y, left_w, key, focus == i)

    def _detail_lines(self, cat: Category, width: int, height: int) -> list[tuple[str, int]]:
        lines: list[tuple[str, int]] = [(cat.title, self.A_ACCENT)]
        for line in textwrap.wrap(cat.desc, max(10, width)) or [""]:
            lines.append((line, self.A_DIM))
        lines.append(("", 0))

        if cat.error:
            lines.append((clip(f"Scan error: {cat.error}", width), self.A_RED))
        elif not cat.scanned:
            lines.append((f"{self._spin()} scanning...", self.A_CYAN))
        elif not cat.available:
            for line in textwrap.wrap(cat.note or "Not available", max(10, width)):
                lines.append((line, self.A_YELLOW))
        else:
            lines.append((f"Total  {human_kb(cat.total_kb)}", self.A_GREEN))
            if cat.destructive:
                lines.append(("! Destructive - read before selecting", self.A_RED))
            if cat.note:
                for line in textwrap.wrap(cat.note, max(10, width)):
                    lines.append((line, self.A_YELLOW))
            if cat.key == "system":
                for line in textwrap.wrap("Trash is emptied automatically; there is no separate prompt.", max(10, width)):
                    lines.append((line, self.A_YELLOW))
            lines.append(("", 0))
            lines.append(("Targets", self.A_BOLD))

            remaining_space = max(0, height - len(lines) - 1)
            shown = 0
            for target in cat.targets:
                if shown >= remaining_space:
                    break
                if target.kind == "note":
                    text = f"{ICON_SKIP} {target.label}"
                    if target.size_kb:
                        text += f"  {human_kb(target.size_kb)}"
                    text += f"  ({target.detail})"
                    lines.append((clip(text, width), self.A_DIM))
                else:
                    size = human_kb(target.size_kb)
                    label_w = max(1, width - len(size) - 4)
                    text = f"{ICON_DRY} {clip(target.label, label_w)}  {size}"
                    lines.append((clip(text, width), self.A_RED if target.destructive else 0))
                shown += 1
            remaining = len(cat.targets) - shown
            if remaining > 0:
                lines.append((f"+{remaining} more", self.A_DIM))
            if not cat.targets:
                lines.append(("Nothing to clean.", self.A_DIM))
        return lines

    def _draw_main(self, h: int, w: int) -> None:
        self._draw_header(w)
        top = 2
        bottom = h - 3
        left_w = self._left_width(w)
        show_details = w >= 96

        self._draw_rows(top, left_w, bottom, self.focus)

        if show_details:
            for y in range(top, bottom + 1):
                self._put(y, left_w, VLINE, self.A_DIM)
            cat = self.cats[CATEGORY_ORDER[self.focus]]
            width = max(10, w - left_w - 3)
            lines = self._detail_lines(cat, width, bottom - top + 1)
            for i, (text, attr) in enumerate(lines[: bottom - top + 1]):
                self._put(top + i, left_w + 2, text, attr)

        # summary under the category list
        y = top + len(CATEGORY_ORDER)
        if y + 3 <= bottom:
            self._put(y, 0, RULE * left_w, self.A_DIM)
            selected_count = sum(
                1 for key in self.selected if self.cats[key].available
            )
            self._put(y + 1, 1, f"Selected  {selected_count}/{len(CATEGORY_ORDER)}", self.A_BOLD)
            self._put(y + 2, 1, f"Total     {human_kb(self._selected_total())}", self.A_GREEN)
            mode = "APPLY" if self.apply_mode else "DRY RUN"
            self._put(
                y + 3,
                1,
                f"Mode      {mode}",
                self.A_RED if self.apply_mode else self.A_YELLOW,
            )

        self._draw_footer(h, w)

    def _draw_running(self, h: int, w: int) -> None:
        self._draw_header(w)
        top = 2
        bottom = h - 3
        left_w = self._left_width(w)
        show_details = w >= 96

        self._draw_rows(top, left_w, bottom, None)

        if show_details:
            for y in range(top, bottom + 1):
                self._put(y, left_w, VLINE, self.A_DIM)
            x = left_w + 2
            width = max(10, w - x - 1)
            y = top
            self._put(y, x, "Running cleanup", self.A_ACCENT)
            y += 1
            pct = int(100 * self.run_done / max(1, self.run_total))
            self._put(y, x, f"{self.run_done}/{self.run_total}  {pct}%", self.A_GREEN)
            y += 1
            bar_w = max(10, min(36, width - 2))
            filled = int(bar_w * pct / 100)
            self._put(y, x, "\u2588" * filled + "\u2591" * (bar_w - filled), self.A_GREEN)
            y += 2
            self._put(y, x, "Current", self.A_BOLD)
            y += 1
            self._put(y, x, clip(self.current, width), self.A_YELLOW)
            y += 2
            self._put(y, x, "Recent", self.A_BOLD)
            y += 1
            for result in self.results[-max(1, bottom - y + 1):]:
                icon = status_icon(result.status)
                line = f"{icon} {clip(result.label, max(8, width - 14))}  {human_kb(result.freed_kb)}"
                self._put(y, x, clip(line, width), self._result_attr(result.status))
                y += 1
                if y > bottom:
                    break
        else:
            y = top
            pct = int(100 * self.run_done / max(1, self.run_total))
            self._put(y, 1, f"Running {self.run_done}/{self.run_total}  {pct}%", self.A_GREEN)
            y += 2
            self._put(y, 1, clip(self.current, w - 2), self.A_YELLOW)
            y += 2
            for result in self.results[-max(1, bottom - y + 1):]:
                self._put(
                    y,
                    1,
                    clip(f"{status_icon(result.status)} {result.label}", w - 2),
                    self._result_attr(result.status),
                )
                y += 1
                if y > bottom:
                    break

        self._draw_footer(h, w)

    def _draw_results(self, h: int, w: int) -> None:
        self._draw_header(w)
        verb = "Reclaimed" if self.last_apply else "Would reclaim"
        self._put(2, 1, f"{verb} {human_kb(self.last_total)}", self.A_GREEN)
        failures = sum(1 for r in self.results if r.status == "fail")
        if failures:
            self._put(2, 4 + len(verb) + len(human_kb(self.last_total)) + 2, f"- {failures} failed", self.A_RED)

        top = 4
        max_rows = max(1, h - top - 3)
        self.result_scroll = max(0, min(self.result_scroll, max(0, len(self.results) - max_rows)))

        y = top
        for result in self.results[self.result_scroll: self.result_scroll + max_rows]:
            icon = status_icon(result.status)
            size_text = human_kb(result.freed_kb) if result.freed_kb else "-"
            size_x = max(1, w - len(size_text) - 2)
            label_w = max(8, size_x - 4)
            text = result.label
            if result.status == "fail" and result.message:
                text += f" - {result.message}"
            self._put(y, 1, icon, self._result_attr(result.status))
            self._put(y, 3, clip(text, label_w), self._result_attr(result.status))
            self._put(y, size_x, size_text, self._result_attr(result.status))
            y += 1
        if not self.results:
            self._put(top, 1, "No targets were selected.", self.A_DIM)

        if self.log_path:
            self._put(h - 2, 1, clip(f"Log: {self.log_path}", w - 2), self.A_DIM)
        else:
            self._put(h - 2, 1, "Dry run - nothing was deleted.", self.A_DIM)
        self._put(h - 1, 1, clip(f"{ARROWS} scroll   Enter back   s rescan   q quit", w - 2), self.A_DIM)

    def _draw_confirm(self, h: int, w: int) -> None:
        items = self._collect_items()
        total = sum(t.size_kb for _key, t in items)
        bw = min(70, w - 4)
        bh = 12
        y = max(0, (h - bh) // 2)
        x = max(0, (w - bw) // 2)

        for yy in range(y, min(h, y + bh)):
            self._put(yy, x, " " * bw, 0)
        self._put(y, x, TL + RULE * (bw - 2) + TR, self.A_ACCENT)
        for yy in range(y + 1, y + bh - 1):
            self._put(yy, x, VLINE, self.A_ACCENT)
            self._put(yy, x + bw - 1, VLINE, self.A_ACCENT)
        self._put(y + bh - 1, x, BL + RULE * (bw - 2) + BR, self.A_ACCENT)
        self._put(y, x + 3, " Apply cleanup? ", self.A_ACCENT)

        lines: list[tuple[str, int]] = [
            (f"{len(items)} targets selected (~{human_kb(total)}).", self.A_BOLD),
            ("This cannot be undone.", self.A_RED),
        ]
        if "system" in self.selected:
            lines.append(("Trash will be emptied automatically.", self.A_YELLOW))
        if "android_deep" in self.selected:
            lines.append(("Android AVDs and system images will be deleted.", self.A_YELLOW))
        if "docker" in self.selected:
            lines.append(("Docker images, containers and volumes will be pruned.", self.A_YELLOW))
        lines.append(("", 0))
        lines.append(("Press y to apply   -   n / Esc to cancel", self.A_BOLD))

        yy = y + 2
        for text, attr in lines:
            if yy >= y + bh - 1:
                break
            self._put(yy, x + 2, clip(text, bw - 4), attr)
            yy += 1

    def _draw_help(self, h: int, w: int) -> None:
        bw = min(76, w - 4)
        bh = min(27, h - 4)
        y = max(0, (h - bh) // 2)
        x = max(0, (w - bw) // 2)

        for yy in range(y, min(h, y + bh)):
            self._put(yy, x, " " * bw, 0)
        self._put(y, x, TL + RULE * (bw - 2) + TR, self.A_ACCENT)
        for yy in range(y + 1, y + bh - 1):
            self._put(yy, x, VLINE, self.A_ACCENT)
            self._put(yy, x + bw - 1, VLINE, self.A_ACCENT)
        self._put(y + bh - 1, x, BL + RULE * (bw - 2) + BR, self.A_ACCENT)
        self._put(y, x + 3, " Help & safety ", self.A_ACCENT)

        lines: list[tuple[str, int]] = [
            ("Keys", self.A_BOLD),
            (f"{ARROWS} or j/k       move selection", 0),
            ("Space / Enter    toggle category", 0),
            ("a / n            select all / none", 0),
            ("m                dry run <-> apply", 0),
            ("r                run selected", 0),
            ("s                rescan", 0),
            ("q                quit", 0),
            ("", 0),
            ("Safety", self.A_BOLD),
            ("Dry run by default. APPLY requires confirmation.", 0),
            ("Only paths under $HOME are ever touched.", 0),
            (".git, dotfiles, credentials and browser profiles are never targeted.", 0),
            ("dist/build/out/coverage are removed only if gitignored and untracked.", 0),
            ("Trash is emptied automatically when System runs.", self.A_YELLOW),
            ("", 0),
            ("Android emulators (--android-deep)", self.A_RED),
            ("Deletes ~/.android/avd and the SDK system-images folder.", 0),
            ("Every emulator must be recreated; images re-download (~3 GB).", 0),
            ("", 0),
            ("Press any key to close", self.A_DIM),
        ]
        yy = y + 2
        for text, attr in lines:
            if yy >= y + bh - 1:
                break
            for wrapped in textwrap.wrap(text, max(10, bw - 4)) or [""]:
                if yy >= y + bh - 1:
                    break
                self._put(yy, x + 2, wrapped, attr)
                yy += 1

    def _draw_footer(self, h: int, w: int) -> None:
        badge = "APPLY" if self.apply_mode else "DRY RUN"
        badge_attr = self.A_RED if self.apply_mode else self.A_YELLOW
        self._put(h - 2, 1, badge, badge_attr)
        status_x = 3 + len(badge)
        self._put(h - 2, status_x, self.status, self._status_attr())

        if w >= 100:
            right = f"{human_kb(self._selected_total())} selected "
            self._put(h - 2, max(0, w - len(right) - 1), right, self.A_GREEN)

        keys = f"{ARROWS} move   space toggle   a all   n none   m dry/apply   r run   s rescan   ? help   q quit"
        if w < 118:
            keys = f"{ARROWS} move   space toggle   m mode   r run   ? help   q quit"
        self._put(h - 1, 1, clip(keys, w - 2), self.A_DIM)


# ---------------------------------------------------------------------------
# argument parsing / entry point
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=APP_NAME,
        description=(
            "Interactive TUI cleaner for macOS caches and rebuildable files. "
            "Dry run by default; --apply is required to delete."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  mac-clean                          TUI, dry run (brew + dev + apps)\n"
            "  mac-clean --all                    TUI, dry run, everything safe\n"
            "  mac-clean --all --apply            TUI, apply mode (still confirms)\n"
            "  mac-clean --system --apply -y      TUI, system only, no confirm\n"
            "  mac-clean --android-deep           include AVDs + system images\n"
            "  mac-clean --no-tui --all           plain-text dry-run report\n"
            "  mac-clean --no-tui --all --apply --yes\n"
            "\n"
            "Android deep mode deletes ~/.android/avd (emulator profiles and\n"
            "user data) and ~/Library/Android/sdk/system-images. Emulators stop\n"
            "working until you recreate them and re-download the images (~3 GB).\n"
            "It is off by default and is a separate, red category in the TUI.\n"
        ),
    )
    group = parser.add_argument_group("categories")
    group.add_argument("--brew", action="store_true", help="Homebrew downloads and unused dependencies")
    group.add_argument("--dev", action="store_true", help="node_modules, build output, package caches")
    group.add_argument("--apps", action="store_true", help="Spotify, Chrome, Stremio, editor caches")
    group.add_argument("--xcode", action="store_true", help="DerivedData, device support, simulator caches")
    group.add_argument("--android", action="store_true", help="Gradle and Android Studio caches")
    group.add_argument("--system", action="store_true", help="old logs, crash reports, Trash (automatic)")
    group.add_argument("--docker", action="store_true", help="prune ALL unused images, containers and volumes")
    group.add_argument(
        "--android-deep",
        action="store_true",
        help="also delete AVD profiles and SDK system images (destructive)",
    )
    group.add_argument(
        "--all",
        action="store_true",
        help="brew + dev + apps + xcode + android + system",
    )
    parser.add_argument(
        "--roots",
        default=str(HOME / "Developer"),
        help="colon-separated roots to scan for build output (default: ~/Developer)",
    )
    parser.add_argument("--apply", action="store_true", help="actually delete (default: dry run)")
    parser.add_argument("-y", "--yes", action="store_true", help="skip the final confirmation")
    parser.add_argument(
        "--no-tui",
        action="store_true",
        help="plain-text mode (also used automatically when stdout is not a TTY)",
    )
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {VERSION}")
    return parser


def main(argv=None) -> int:
    try:
        locale.setlocale(locale.LC_ALL, "")
    except locale.Error:
        pass
    opts = build_parser().parse_args(argv)
    if HOME_INVALID:
        print(f"{APP_NAME}: refusing to run: $HOME is unset or /", file=sys.stderr)
        return 1
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        print(f"{APP_NAME}: refusing to run as root; run as your normal user", file=sys.stderr)
        return 1
    if opts.no_tui or not sys.stdout.isatty():
        return cli_main(opts)
    try:
        curses.wrapper(lambda scr: App(scr, opts).run())
    except KeyboardInterrupt:
        return 130
    except curses.error as exc:
        print(f"{APP_NAME}: TUI failed ({exc}); use --no-tui", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
