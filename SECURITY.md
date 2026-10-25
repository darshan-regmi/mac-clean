# Security policy

## Supported versions

| Version | Supported |
| ------- | --------- |
| 1.x     | Yes       |

## Reporting a vulnerability

Please do not open a public issue for security problems. Use GitHub's private
vulnerability reporting on this repository, or email
[regmidarshan545l@gmail.com](mailto:regmidarshan545l@gmail.com).

Include reproduction steps, the output of `mac-clean --version`, and the
macOS version. You should receive a response within a few days.

## Threat model

mac-clean deletes files, so the primary risk is unintended deletion. The
following guarantees are enforced in code and covered by tests:

- The tool runs as the invoking user and refuses to run as root.
- It refuses to run when `$HOME` is unset or `/`.
- Every deletion resolves to a path under `$HOME`; paths outside are refused.
- A denylist protects `$HOME` itself and important directories such as
  `~/Library`, `~/Developer`, `~/.ssh`, `~/.config`, and `~/.Trash`.
- Symlinks and mount points are never deleted, so a link cannot redirect a
  delete outside `$HOME`.
- `dist/`, `build/`, `out/`, and `coverage/` are removed only when git
  confirms they are ignored and contain no tracked files.
- Deletion is off by default and requires `--apply` plus confirmation.
- Destructive categories (Docker, Android emulators) are opt-in.

If you find a way to bypass any of these guarantees, treat it as a security
issue and report it privately.
