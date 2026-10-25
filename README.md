# mac-clean

[![CI](https://github.com/darshan-regmi/mac-clean/actions/workflows/ci.yml/badge.svg)](https://github.com/darshan-regmi/mac-clean/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

An interactive TUI cleaner for macOS. It finds caches and rebuildable files
across your home directory, shows their real sizes, and deletes only the
categories you select.

**Dry run by default.** Nothing is deleted until you switch to APPLY mode and
confirm. It is a single Python file with no runtime dependencies.

```
mac-clean/
├── mac_clean.py          # the executable TUI
├── tests/                # unit and end-to-end tests
├── .github/workflows/    # CI
├── pyproject.toml        # packaging and the mac-clean console script
├── Makefile              # run / test / lint / install
├── README.md
├── CHANGELOG.md
├── CONTRIBUTING.md
├── SECURITY.md
└── LICENSE
```

## Requirements

- macOS
- Python 3.9 or newer (the system Python is enough)

## Install

### pipx (recommended)

```bash
brew install pipx
git clone https://github.com/darshan-regmi/mac-clean.git
cd mac-clean
pipx install .
```

`mac-clean` is then available on your `PATH`.

### Symlink (no packaging)

```bash
git clone https://github.com/darshan-regmi/mac-clean.git
cd mac-clean
mkdir -p ~/.local/bin
ln -sf "$PWD/mac_clean.py" ~/.local/bin/mac-clean
```

Make sure `~/.local/bin` is on your `PATH`.

### Run without installing

```bash
git clone https://github.com/darshan-regmi/mac-clean.git
cd mac-clean
python3 mac_clean.py
```

## Run

```bash
mac-clean                    # TUI, dry run: Homebrew + Dev + Apps
mac-clean --all              # TUI, dry run: everything safe
mac-clean --all --apply      # TUI, apply mode (still asks for confirmation)
```

From a clone, use `python3 mac_clean.py` with the same flags.

## TUI keys

| Key | Action |
|---|---|
| `↑` `↓` / `j` `k` | Move between categories |
| `space` / `enter` | Toggle the focused category |
| `a` | Select all available categories |
| `n` | Clear selection |
| `m` | Toggle **DRY RUN** ↔ **APPLY** |
| `r` | Run the selected categories |
| `s` | Rescan everything |
| `?` | Help and safety notes |
| `q` | Quit |

In APPLY mode, `r` opens a confirmation box. `--yes` skips that box.

## Categories

| Category | What it removes | Notes |
|---|---|---|
| **Homebrew** | `brew cleanup --prune=all -s`, `brew autoremove` | Cached downloads go; unused dependencies may be removed. Installed formulae/casks stay |
| **Dev artifacts** | `node_modules`, `.next`, `.turbo`, `.expo`, `__pycache__`, test output, package-manager caches | Scans `~/Developer` by default; `--roots` changes it |
| **App caches** | Spotify, Google/Chrome, Stremio, Maps, Safari, VS Code, Cursor, Discord/Notion/Obsidian/Claude caches | Rebuild automatically; quit apps for best results |
| **Xcode** | DerivedData, iOS DeviceSupport, SwiftUI previews, simulator caches, unavailable simulators | Archives are kept |
| **Android** | Gradle caches, Android SDK cache, Android Studio indexes | AVDs and system images are kept |
| **Android emulators** | AVD profiles and SDK system images | Destructive; see below |
| **System** | Logs older than 7 days, crash reports older than 30 days, Trash | Trash is emptied automatically |
| **Docker** | `docker system prune -af --volumes` | Destructive; only when the daemon is running |

The safe set is `brew + dev + apps`. `--all` adds `xcode + android + system`.
Docker and Android emulators are opt-in.

## Android deep mode

`--android-deep` (or the red **Android emulators** category in the TUI) deletes:

- `~/.android/avd` — every emulator profile, including user data and snapshots
- `~/Library/Android/sdk/system-images` — the downloaded Android system images

On the development machine that is about **3.1 GB**. It is destructive:

- every AVD disappears; recreate them in Android Studio's Device Manager
- the system images must be downloaded again through SDK Manager
- project SDK settings are untouched, but emulators will not start until restored

It is **off by default** and is a separate category, so it is never included by
`--all` or the default selection.

## Safety model

- **Dry run is the default.** `--apply` is required to delete anything.
- Every deletion must resolve to a path **under `$HOME`**. Anything outside is refused.
- Exact-path denylist: `~`, `~/Developer`, `~/Documents`, `~/Desktop`,
  `~/Downloads`, `~/Library`, `~/Pictures`, `~/Movies`, `~/Music`, `~/.ssh`,
  `~/.gnupg`, `~/.config`, `~/.local`, `~/.cache`, `~/.npm`, `~/.gradle`,
  `~/.android`, `~/.Trash`, plus `~/Library/Caches`,
  `~/Library/Application Support`, `~/Library/Logs` and `~/Library/Android`.
- `rm -rf --` is used, so an empty variable can never expand to `rm -rf /`.
- Symlinks and mount points are refused, so a link cannot redirect a delete
  outside `$HOME`.
- `.git`, dotfiles, credentials, keychains and browser profiles are never
  targeted.
- `dist/`, `build/`, `out/` and `coverage/` are deleted **only** when
  `git check-ignore` says they are ignored **and** `git ls-files` finds no
  tracked files inside them.
- `~/Library/Logs` is only touched for individual files older than 7 days;
  crash reports older than 30 days are handled separately.
- `--system` empties Trash automatically. There is no separate Trash prompt;
  the only confirmation is the global APPLY confirmation (unless `--yes`).
- The tool refuses to run as root or with an unset/`/` `$HOME`.
- Every APPLY run writes a log to `~/Library/Logs/mac-clean-<timestamp>.log`.

## Plain-text mode

The TUI is the default. `--no-tui` (also selected automatically when stdout is
not a TTY) prints the same report and can apply without curses:

```bash
./mac_clean.py --no-tui --all                    # dry-run report
./mac_clean.py --no-tui --all --apply            # asks for confirmation
./mac_clean.py --no-tui --system --apply --yes   # old logs + Trash, no prompt
```

| Flag | Meaning |
|---|---|
| `--brew` `--dev` `--apps` `--xcode` `--android` `--system` `--docker` | Pick categories |
| `--all` | `brew + dev + apps + xcode + android + system` |
| `--android-deep` | Add AVDs and SDK system images (destructive) |
| `--roots a:b` | Colon-separated roots to scan for build output (default `~/Developer`) |
| `--apply` | Actually delete (default is dry run) |
| `-y`, `--yes` | Skip the final confirmation |
| `--no-tui` | Plain-text mode |
| `--version` | Print version |

Examples:

```bash
./mac_clean.py --system --apply          # TUI, Trash automatic, asks once
./mac_clean.py --android-deep            # dry run, include emulator images
./mac_clean.py --docker --apply          # TUI, Docker prune after confirmation
./mac_clean.py --roots ~/Developer:~/Projects --dev
```

## Development

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"

make test     # unit and integration tests
make lint     # ruff
make check    # compile + tests
make run      # run the TUI
```

The tests always use a temporary `$HOME` and a non-interactive stdin, so they
never touch real files. See [CONTRIBUTING.md](CONTRIBUTING.md) for the safety
invariants that every change must preserve.

## Measured on the development machine

Dry run of `--all` reported approximately **13.7 GB** reclaimable, led by the
**9.3 GB** Homebrew download cache. Adding `--android-deep` would add about
**3.1 GB** of emulator images, for roughly **16.8 GB** total.

Dry-run sizes are estimates. APPLY mode measures what was actually freed.

## Notes

- **Quit apps first** (Spotify, Chrome, Discord, editors) so their caches are
  not locked. Locked files are reported as failures and left alone.
- `brew cleanup` can take a minute; the TUI shows progress and keeps the UI
  responsive.
- `pnpm store prune` only removes unreferenced packages; it does not wipe the
  store. `npm cache clean --force` clears npm's content cache.
- `dist/build/out/coverage` in a repo that is not gitignored are listed as
  "kept" rather than deleted.
- No data leaves the machine; there is no network access.

## License

[MIT](LICENSE) © 2026 Darshan Regmi
