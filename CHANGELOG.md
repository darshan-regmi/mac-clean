# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.0.0] - 2026-10-25

### Added

- Interactive curses TUI with per-category selection, live sizes, rescan,
  dry-run/apply toggle, confirmation dialog, and a results screen.
- Categories: Homebrew, dev artifacts, app caches, Xcode, Android, Android
  emulators (opt-in), System, and Docker (opt-in).
- Safety rails: dry run by default, `$HOME`-only deletion, exact-path
  denylist, symlink and mount-point refusal, root-user refusal, and
  git-ignore plus tracked-file checks for `dist/`, `build/`, `out/`, and
  `coverage/`.
- Plain-text `--no-tui` mode for non-interactive use and scripting.
- Apply-run logging to `~/Library/Logs/mac-clean-<timestamp>.log`.
- `--android-deep` opt-in for deleting AVDs and SDK system images.
- Packaging via `pyproject.toml` with a `mac-clean` console script.
- Unit and end-to-end tests, GitHub Actions CI, and contributor docs.

[Unreleased]: https://github.com/darshan-regmi/mac-clean/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/darshan-regmi/mac-clean/releases/tag/v1.0.0
