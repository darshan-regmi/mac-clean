# Contributing

Thanks for helping improve mac-clean.

## Development setup

mac-clean has no runtime dependencies beyond the Python standard library.
Python 3.9 or newer is required.

```bash
git clone https://github.com/darshan-regmi/mac-clean.git
cd mac-clean
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

Run the app, the tests, and the linter:

```bash
python mac_clean.py
make test
make lint
```

## Safety invariants

mac-clean deletes files. These rules are not negotiable:

- Dry run must remain the default. Deletion requires `--apply`.
- Every deletion must resolve under `$HOME` and pass `safe_path()`.
- Never add a target that is a source tree, `.git`, a dotfile, a credential
  store, a browser profile, or anything outside `$HOME`.
- Destructive categories must be opt-in and visually marked in the TUI.
- `dist/`, `build/`, `out/`, and `coverage/` may only be removed when
  `git_ignored()` confirms they are ignored by git and contain no tracked
  files.
- New deletion behavior needs a test, including a negative test that proves
  the unsafe case is refused.

## Access and merge policy

Only the maintainer (`@darshan-regmi`) has write access to this repository.
Everyone else contributes through a fork:

1. Ask the maintainer for a **Read** invitation (the repository is private).
2. Fork the repository and create a branch in your fork.
3. Open a pull request against `main`.
4. The maintainer reviews and merges it.

Do not ask for write access. A pull request from a fork is the supported
workflow, and only the maintainer can merge into `main`. The
[`CODEOWNERS`](.github/CODEOWNERS) file requests the maintainer's review
automatically on every pull request.

## Pull requests

- Keep changes focused and explain the user-visible behavior in the PR.
- Run `make check` and `make lint` before pushing.
- Update `README.md` and `CHANGELOG.md` when behavior changes.
- Fill in the safety checklist in the pull request template.
- By contributing, you agree that your work is licensed under the MIT License.
