## Summary

<!-- What changed and why? -->

## Testing

<!-- How did you verify it? Include commands or screenshots. -->

## Safety checklist

- [ ] Dry run is still the default; deletion still requires `--apply`.
- [ ] Every new deletion target resolves under `$HOME` and passes `safe_path()`.
- [ ] No source trees, `.git`, dotfiles, credentials, or browser profiles are targeted.
- [ ] Destructive behavior is opt-in and clearly marked in the TUI.
- [ ] `dist/`, `build/`, `out/`, and `coverage/` still require the git-ignore and tracked-file checks.
- [ ] Tests cover both the allowed and the refused cases.
- [ ] `make check` and `make lint` pass.
- [ ] README and CHANGELOG are updated if behavior changed.
