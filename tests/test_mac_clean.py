"""Tests for mac-clean.

Run with:

    python3 -m unittest discover -s tests -v

The integration tests always use a temporary $HOME and a non-interactive
stdin, so they never touch the real home directory.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "mac_clean.py"
sys.path.insert(0, str(REPO))

import mac_clean  # noqa: E402


class HumanKbTests(unittest.TestCase):
    def test_zero_and_negative(self):
        self.assertEqual(mac_clean.human_kb(0), "0 B")
        self.assertEqual(mac_clean.human_kb(-5), "0 B")

    def test_units(self):
        self.assertEqual(mac_clean.human_kb(512), "512 KB")
        self.assertEqual(mac_clean.human_kb(1024), "1.0 MB")
        self.assertEqual(mac_clean.human_kb(150 * 1024), "150 MB")
        self.assertEqual(mac_clean.human_kb(9.3 * 1024 * 1024), "9.3 GB")


class ClipTests(unittest.TestCase):
    def test_short_text_is_unchanged(self):
        self.assertEqual(mac_clean.clip("hello", 10), "hello")

    def test_truncation_respects_width(self):
        out = mac_clean.clip("hello world", 6)
        self.assertEqual(len(out), 6)
        self.assertTrue(out.endswith(("\u2026", "~")))

    def test_zero_width(self):
        self.assertEqual(mac_clean.clip("hello", 0), "")


class FakeHomeTestCase(unittest.TestCase):
    """Point the module at a temporary home for safe filesystem tests."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name).resolve()
        self._old_home = mac_clean.HOME
        self._old_deny = mac_clean.DENY
        self._old_invalid = mac_clean.HOME_INVALID
        mac_clean.HOME = self.home
        mac_clean.HOME_INVALID = False
        mac_clean.DENY = {self.home} | {
            self.home / name
            for name in (
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
        }

    def tearDown(self):
        mac_clean.HOME = self._old_home
        mac_clean.DENY = self._old_deny
        mac_clean.HOME_INVALID = self._old_invalid
        self._tmp.cleanup()


class SafePathTests(FakeHomeTestCase):
    def test_allows_path_under_home(self):
        self.assertTrue(mac_clean.safe_path(self.home / "projects" / "app" / "node_modules"))

    def test_allows_nonexistent_path_under_home(self):
        self.assertTrue(mac_clean.safe_path(self.home / "projects" / "not-created-yet"))

    def test_rejects_home_itself(self):
        self.assertFalse(mac_clean.safe_path(self.home))

    def test_rejects_denylisted_paths(self):
        self.assertFalse(mac_clean.safe_path(self.home / "Library"))
        self.assertFalse(mac_clean.safe_path(self.home / ".Trash"))
        self.assertFalse(mac_clean.safe_path(self.home / ".ssh"))

    def test_rejects_outside_home(self):
        self.assertFalse(mac_clean.safe_path(Path("/tmp")))
        self.assertFalse(mac_clean.safe_path(Path("/etc")))

    def test_rejects_relative_paths(self):
        self.assertFalse(mac_clean.safe_path(Path("relative/path")))

    def test_rejects_symlink(self):
        target = self.home / "target"
        target.mkdir()
        link = self.home / "link"
        link.symlink_to(target)
        self.assertFalse(mac_clean.safe_path(link))


class ExecuteTargetTests(FakeHomeTestCase):
    def _make_target(self):
        path = self.home / "projects" / "demo"
        path.mkdir(parents=True)
        (path / "blob.bin").write_bytes(b"x" * 64_000)
        return path, mac_clean.Target(
            label="demo",
            kind="path",
            path=path,
            size_kb=mac_clean.du_kb(path),
        )

    def test_dry_run_keeps_files(self):
        path, target = self._make_target()
        result = mac_clean.execute_target(target, True, lambda _message: None)
        self.assertEqual(result.status, "dry")
        self.assertGreater(result.freed_kb, 0)
        self.assertTrue(path.exists())

    def test_apply_removes_files(self):
        path, target = self._make_target()
        result = mac_clean.execute_target(target, False, lambda _message: None)
        self.assertEqual(result.status, "ok")
        self.assertGreater(result.freed_kb, 0)
        self.assertFalse(path.exists())

    def test_refuses_path_outside_home(self):
        target = mac_clean.Target(label="tmp", kind="path", path=Path("/tmp"))
        result = mac_clean.execute_target(target, True, lambda _message: None)
        self.assertEqual(result.status, "fail")
        self.assertIn("unsafe", result.message)

    def test_note_targets_are_skipped(self):
        target = mac_clean.Target(label="keep", kind="note", detail="not deletable")
        result = mac_clean.execute_target(target, False, lambda _message: None)
        self.assertEqual(result.status, "skip")


class FindBuildDirsTests(unittest.TestCase):
    def test_classifies_safe_and_ambiguous_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            for name in ("node_modules", ".next", "build", "src"):
                (root / name).mkdir()
            (root / "src" / "node_modules").mkdir()

            safe, ambiguous = mac_clean.find_build_dirs(root)

            self.assertIn(root / "node_modules", safe)
            self.assertIn(root / ".next", safe)
            self.assertIn(root / "src" / "node_modules", safe)
            self.assertEqual(ambiguous, [root / "build"])


@unittest.skipUnless(shutil.which("git"), "git is not installed")
class GitIgnoredTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name).resolve()
        self.git("init", "-q")
        (self.repo / ".gitignore").write_text("dist/\n", encoding="utf-8")
        (self.repo / "dist").mkdir()
        (self.repo / "dist" / "a.js").write_text("built\n", encoding="utf-8")
        (self.repo / "build").mkdir()
        (self.repo / "build" / "keep.txt").write_text("keep\n", encoding="utf-8")
        (self.repo / "out").mkdir()
        (self.repo / "out" / "o.txt").write_text("out\n", encoding="utf-8")
        self.git("add", ".gitignore", "build/keep.txt", "out/o.txt")
        self.git(
            "-c",
            "user.email=test@example.com",
            "-c",
            "user.name=Test",
            "commit",
            "-qm",
            "init",
        )

    def tearDown(self):
        self._tmp.cleanup()

    def git(self, *args):
        return subprocess.run(
            ["git", "-C", str(self.repo), *args],
            check=True,
            capture_output=True,
            text=True,
        )

    def test_ignored_and_untracked_is_deletable(self):
        self.assertTrue(mac_clean.git_ignored(self.repo / "dist"))

    def test_tracked_directory_is_rejected(self):
        self.assertFalse(mac_clean.git_ignored(self.repo / "build"))

    def test_unignored_directory_is_rejected(self):
        self.assertFalse(mac_clean.git_ignored(self.repo / "out"))

    def test_repository_root_is_rejected(self):
        self.assertFalse(mac_clean.git_ignored(self.repo))

    def test_directory_outside_a_repo_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(mac_clean.git_ignored(Path(tmp) / "dist"))


class SelectionTests(unittest.TestCase):
    def parse(self, *args):
        return mac_clean.build_parser().parse_args(list(args))

    def test_default_selection(self):
        self.assertEqual(
            mac_clean.selected_keys(self.parse()),
            ["brew", "dev", "apps"],
        )

    def test_all_excludes_destructive_categories(self):
        keys = mac_clean.selected_keys(self.parse("--all"))
        self.assertEqual(keys, ["brew", "dev", "apps", "xcode", "android", "system"])
        self.assertNotIn("android_deep", keys)
        self.assertNotIn("docker", keys)

    def test_android_deep_implies_android(self):
        self.assertEqual(
            mac_clean.selected_keys(self.parse("--android-deep")),
            ["android", "android_deep"],
        )


class VersionTests(unittest.TestCase):
    def test_pyproject_version_matches_module(self):
        text = (REPO / "pyproject.toml").read_text(encoding="utf-8")
        match = re.search(r'^version = "([^"]+)"', text, re.MULTILINE)
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), mac_clean.VERSION)

    def test_version_flag(self):
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--version"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(mac_clean.VERSION, proc.stdout)


class CliTests(unittest.TestCase):
    def run_cli(self, home: Path, *args: str):
        env = os.environ.copy()
        env["HOME"] = str(home)
        env["TERM"] = "dumb"
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--no-tui", *args],
            capture_output=True,
            text=True,
            env=env,
            stdin=subprocess.DEVNULL,
            timeout=180,
        )

    def make_fake_home(self) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = Path(tmp.name).resolve()

        cache = home / "Library" / "Caches" / "com.spotify.client"
        cache.mkdir(parents=True)
        (cache / "cache.bin").write_bytes(b"x" * 200_000)

        trash = home / ".Trash"
        trash.mkdir()
        (trash / "junk.bin").write_bytes(b"y" * 100_000)

        (home / "Library" / "Logs").mkdir(parents=True)
        return home

    def test_dry_run_does_not_delete(self):
        home = self.make_fake_home()
        proc = self.run_cli(home, "--apps", "--system")

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("DRY RUN", proc.stdout)
        self.assertIn("Dry run - nothing was deleted", proc.stdout)
        self.assertTrue((home / "Library/Caches/com.spotify.client/cache.bin").exists())
        self.assertTrue((home / ".Trash/junk.bin").exists())

    def test_apply_deletes_and_writes_log(self):
        home = self.make_fake_home()
        proc = self.run_cli(home, "--apps", "--system", "--apply", "--yes")

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Reclaimed", proc.stdout)
        self.assertFalse((home / "Library/Caches/com.spotify.client").exists())
        self.assertEqual(list((home / ".Trash").iterdir()), [])

        logs = list((home / "Library/Logs").glob("mac-clean-*.log"))
        self.assertEqual(len(logs), 1)
        self.assertIn("APPLY", logs[0].read_text(encoding="utf-8"))

    def test_apply_without_yes_refuses_in_non_tty(self):
        home = self.make_fake_home()
        proc = self.run_cli(home, "--apps", "--apply")

        self.assertEqual(proc.returncode, 2)
        self.assertIn("Refusing to delete", proc.stderr)
        self.assertTrue((home / "Library/Caches/com.spotify.client/cache.bin").exists())


if __name__ == "__main__":
    unittest.main()
