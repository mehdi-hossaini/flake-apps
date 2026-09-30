import contextlib
import io
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import urllib.parse

import flake_apps as f


class SelectionTests(unittest.TestCase):
    def test_default_and_single_package_selection(self):
        self.assertEqual(f.select_package(["tool", "default"], None), "default")
        self.assertEqual(f.select_package(["tool"], None), "tool")

    def test_explicit_selection(self):
        self.assertEqual(f.select_package(["default", "tool"], "tool"), "tool")

    def test_ambiguity_missing_package_and_empty_outputs(self):
        for packages, requested in ((["a", "b"], None), ([], None), (["a"], "b")):
            with self.subTest(packages=packages), self.assertRaises(f.Error):
                f.select_package(packages, requested)

    def test_invalid_package_lists_are_rejected(self):
        for packages in ({}, [None], [1], [""]):
            with self.subTest(packages=packages), self.assertRaises(f.Error):
                f.select_package(packages, None)

    def test_attribute_names_are_literal(self):
        for package in ("app.gui", "${evil}", "café", "caret^name", "slash\\name"):
            value = f.installable("github:owner/repo", "x86_64-linux", package)
            fragment = urllib.parse.unquote(value.split("#", 1)[1])
            self.assertEqual(fragment, f'packages."x86_64-linux"."{package}"')
            self.assertNotIn("^", value)

    def test_unrepresentable_names_are_rejected(self):
        for package in ('quote"name', "", "nul\x00name"):
            with (
                self.subTest(package=package),
                self.assertRaisesRegex(f.Error, "alias"),
            ):
                f.installable("github:owner/repo", "x86_64-linux", package)

    def test_choices_preserve_distinct_names_and_bound_large_diagnostics(self):
        with self.assertRaisesRegex(f.Error, r'"a\\nb", "ab"'):
            f.select_package(["a\nb", "ab"], None)
        with self.assertRaisesRegex(f.Error, "use inspect") as error:
            f.select_package([f"package-{i}" for i in range(1000)], None)
        self.assertLess(len(str(error.exception)), 500)

    def test_packaged_and_cli_versions_match(self):
        package = (Path(__file__).resolve().parents[1] / "package.nix").read_text()
        match = re.search(r'version = "([^"]+)"', package)
        self.assertIsNotNone(match)
        if match is not None:
            self.assertEqual(match[1], f.VERSION)

    def test_github_urls_are_normalized(self):
        for value in (
            "https://github.com/owner/repo",
            "https://github.com/owner/repo.git",
        ):
            self.assertEqual(f.flake_reference(value), "github:owner/repo")

    def test_native_references_preserve_tracking_information(self):
        for value in (
            "github:owner/repo/main",
            "git+file:///tmp/flake",
            "path:/tmp/flake",
        ):
            self.assertEqual(f.flake_reference(value), value)

    def test_fragments_option_injection_and_bad_urls_are_rejected(self):
        for value in (
            "",
            "--impure",
            "github:owner/repo#tool",
            "path:/tmp/foo\nbar",
            "http://github.com/owner/repo",
            "https://github.com/owner/repo/releases",
            "https://github.com/owner/repo?ref=main",
            "https://user@github.com/owner/repo",
        ):
            with self.subTest(value=value), self.assertRaises(f.Error):
                f.flake_reference(value)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.profile = Path(self.temporary.name) / "profiles/native"

    def args(self, *arguments):
        return f.parser().parse_args(["--profile", str(self.profile), *arguments])

    def test_inspection_is_json_and_does_not_create_a_profile(self):
        with (
            patch.object(
                f,
                "nix",
                side_effect=[
                    {"url": "github:owner/repo/commit", "locked": {"rev": "commit"}},
                    ["default"],
                ],
            ) as nix,
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            f.dispatch(self.args("inspect", "github:owner/repo"))
        self.assertEqual(json.loads(output.getvalue())["packages"], ["default"])
        self.assertFalse(self.profile.parent.exists())
        for call in nix.call_args_list:
            self.assertIn("--no-update-lock-file", call.args)
            self.assertNotIn("--no-write-lock-file", call.args)
            self.assertNotIn("profile", call.args)

    def test_add_preserves_original_reference_for_future_updates(self):
        with (
            patch.object(f, "describe", return_value={"packages": ["tool"]}),
            patch.object(f, "nix") as nix,
        ):
            f.dispatch(self.args("add", "github:owner/repo/main"))
        self.assertIn(
            f.installable("github:owner/repo/main", self.args("list").system, "tool"),
            nix.call_args.args,
        )
        self.assertIn("--no-update-lock-file", nix.call_args.args)
        self.assertNotIn("--impure", nix.call_args.args)

    def test_ambiguous_add_creates_no_profile_or_build(self):
        with (
            patch.object(f, "describe", return_value={"packages": ["a", "b"]}),
            patch.object(f, "nix") as nix,
        ):
            with self.assertRaises(f.Error):
                f.dispatch(self.args("add", "github:owner/repo"))
        nix.assert_not_called()
        self.assertFalse(self.profile.parent.exists())

    def test_absent_profile_listing_is_machine_readable(self):
        with patch.object(f, "nix") as nix:
            f.dispatch(self.args("list", "--json"))
        self.assertEqual(
            nix.call_args.args, ("profile", "list", "--profile", self.profile, "--json")
        )
        self.assertFalse(self.profile.parent.exists())

    def test_absent_profile_mutations_fail(self):
        for command in (("update",), ("rollback",), ("remove", "tool")):
            with self.subTest(command=command), self.assertRaises(f.Error):
                f.dispatch(self.args(*command))

    def test_selector_cannot_become_a_nix_option(self):
        self.profile.parent.mkdir()
        self.profile.touch()
        with patch.object(f, "nix") as nix:
            f.dispatch(self.args("remove", "--", "--all"))
        self.assertEqual(nix.call_args.args[-2:], ("--", "--all"))

    def test_xdg_profile_is_separate_from_obtain_and_default_nix(self):
        with patch.dict(os.environ, {"XDG_DATA_HOME": self.temporary.name}):
            self.assertEqual(
                f.default_profile(), Path(self.temporary.name) / "flake-apps/profile"
            )

    def test_empty_relative_and_unset_xdg_use_home_fallback(self):
        fallback = Path.home() / ".local/share/flake-apps/profile"
        for value in (None, "", "relative"):
            with self.subTest(value=value), patch.dict(os.environ):
                if value is None:
                    os.environ.pop("XDG_DATA_HOME", None)
                else:
                    os.environ["XDG_DATA_HOME"] = value
                self.assertEqual(f.default_profile(), fallback)

    def test_dangling_profile_is_not_reported_empty_and_allows_recovery(self):
        self.profile.parent.mkdir(parents=True)
        self.profile.symlink_to("missing-generation")
        for command in (
            ("list", "--json"),
            ("add", "path:/tmp/flake", "--package", "tool"),
            ("update",),
            ("remove", "tool"),
        ):
            with self.subTest(command=command), patch.object(f, "nix") as nix:
                with self.assertRaisesRegex(f.Error, "recover"):
                    f.dispatch(self.args(*command))
                nix.assert_not_called()
        for command in (("history",), ("rollback", "--to", "1")):
            with self.subTest(command=command), patch.object(f, "nix") as nix:
                f.dispatch(self.args(*command))
                self.assertEqual(nix.call_args.args[1], command[0])

    def test_explicit_selection_skips_catalog_enumeration(self):
        with patch.object(f, "describe") as describe, patch.object(f, "nix") as nix:
            f.dispatch(self.args("add", "github:owner/repo", "--package", "tool"))
        describe.assert_not_called()
        self.assertEqual(nix.call_count, 1)

    def test_untrackable_selection_creates_no_profile(self):
        for package in (
            "app.gui",
            'quote"name',
            "${evil}",
            "café",
            "caret^name",
            "slash\\name",
        ):
            with self.subTest(package=package), patch.object(f, "nix") as nix:
                with self.assertRaisesRegex(f.Error, "alias"):
                    f.dispatch(
                        self.args("add", "github:owner/repo", "--package", package)
                    )
                nix.assert_not_called()
                self.assertFalse(self.profile.parent.exists())

    def test_legacy_untrackable_update_is_rejected_before_mutation(self):
        self.profile.parent.mkdir(parents=True)
        self.profile.touch()
        manifest = {
            "elements": {
                "old": {"attrPath": "packages.x86_64-linux.app.gui"},
                "safe": {"attrPath": 'packages."x86_64-linux"."default"'},
            }
        }
        with patch.object(f, "nix", return_value=manifest) as nix:
            with self.assertRaisesRegex(f.Error, "alias"):
                f.dispatch(self.args("update"))
        self.assertEqual(nix.call_count, 1)
        with patch.object(f, "nix", return_value=manifest) as nix:
            f.dispatch(self.args("update", "safe"))
        self.assertEqual(nix.call_args.args[-2:], ("--", "safe"))

    def test_empty_update_selector_never_expands_to_all(self):
        self.profile.parent.mkdir(parents=True)
        self.profile.touch()
        with patch.object(f, "nix", return_value={"elements": {}}) as nix:
            f.dispatch(self.args("update", ""))
        self.assertEqual(nix.call_args.args[-2:], ("--", ""))

    def test_nix_exit_status_is_preserved(self):
        with (
            patch.object(
                f, "dispatch", side_effect=subprocess.CalledProcessError(7, ["nix"])
            ),
            patch.object(f, "require_nix"),
        ):
            self.assertEqual(f.main(["list"]), 7)

    def test_invalid_metadata_is_an_actionable_error(self):
        for metadata in (None, [], {"url": 1}, {}):
            with (
                self.subTest(metadata=metadata),
                patch.object(f, "nix", return_value=metadata),
                self.assertRaises(f.Error),
            ):
                f.describe("github:owner/repo", "x86_64-linux")

    def test_flake_config_is_not_automatically_accepted(self):
        with patch.object(f, "run_command", return_value="{}") as run:
            self.assertEqual(f.nix("flake", "metadata", "--json", capture=True), {})
        command = run.call_args.args[0]
        self.assertEqual(command[command.index("accept-flake-config") + 1], "false")


class ProcessTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="flake-apps-process-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.cli = [sys.executable, str(Path(f.__file__).resolve())]
        self.environment = {
            **os.environ,
            "PATH": str(self.bin),
            "XDG_DATA_HOME": str(self.root / "data"),
        }

    def write_nix(self, body):
        executable = self.bin / "nix"
        executable.write_text(
            f"#!{sys.executable}\nimport json, os, signal, sys, time\n"
            "if '--version' in sys.argv:\n"
            "    print('nix (Nix) ' + os.environ.get('TEST_NIX_VERSION', '2.30.0'))\n"
            "    sys.exit(0)\n" + body
        )
        executable.chmod(0o755)

    def test_missing_and_old_nix_have_actionable_errors(self):
        result = subprocess.run(
            [*self.cli, "inspect", "path:/tmp/flake"],
            env=self.environment,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("put nix on PATH", result.stderr)
        self.write_nix("raise SystemExit('unexpected command')\n")
        result = subprocess.run(
            [*self.cli, "list"],
            env={**self.environment, "TEST_NIX_VERSION": "2.29.0"},
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("upgrade", result.stderr)
        self.assertNotIn("unexpected command", result.stderr)

    def test_help_and_version_do_not_need_nix(self):
        for arguments in (("--help",), ("--version",), ("rollback", "--help")):
            result = subprocess.run(
                [*self.cli, *arguments],
                env=self.environment,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("all packages", result.stdout)

    def test_closed_inspection_pipe_is_quiet_for_buffered_and_large_output(self):
        self.write_nix(
            "if 'metadata' in sys.argv:\n"
            "    print(json.dumps({'url': 'path:/tmp/flake', 'locked': {}}))\n"
            "else:\n"
            "    print(json.dumps(['tool-' + str(i) for i in range(int(os.environ['TEST_PACKAGE_COUNT']))]))\n"
        )
        for count in (1, 10000):
            with (
                self.subTest(count=count),
                subprocess.Popen(
                    [*self.cli, "inspect", "path:/tmp/flake"],
                    env={**self.environment, "TEST_PACKAGE_COUNT": str(count)},
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                ) as process,
            ):
                if process.stdout is not None:
                    process.stdout.close()
                self.assertEqual(process.wait(timeout=10), 0)
                stderr = process.stderr.read() if process.stderr is not None else ""
                self.assertEqual(stderr, "")

    def test_cancellation_stops_child_and_releases_profile_lock(self):
        marker = self.root / "child-pid"
        self.write_nix(
            "if os.environ.get('TEST_FAST'):\n"
            "    sys.exit(0)\n"
            "if os.environ.get('TEST_IGNORE_CANCEL'):\n"
            "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "    signal.signal(signal.SIGINT, signal.SIG_IGN)\n"
            "else:\n"
            "    signal.signal(signal.SIGINT, signal.SIG_DFL)\n"
            f"with open({str(marker) + '.tmp'!r}, 'w') as marker: marker.write(str(os.getpid()))\n"
            f"os.replace({str(marker) + '.tmp'!r}, {str(marker)!r})\n"
            "time.sleep(60)\n"
        )
        for signum, ignore in (
            (signal.SIGTERM, False),
            (signal.SIGINT, False),
            (signal.SIGTERM, True),
        ):
            marker.unlink(missing_ok=True)
            with (
                self.subTest(signum=signum, ignore=ignore),
                subprocess.Popen(
                    [*self.cli, "add", "path:/tmp/flake", "--package", "tool"],
                    env={
                        **self.environment,
                        "TEST_IGNORE_CANCEL": "1" if ignore else "",
                    },
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    start_new_session=True,
                ) as process,
            ):
                try:
                    deadline = time.monotonic() + 10
                    while not marker.exists() and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertTrue(marker.exists(), "Nix child did not start")
                    child = int(marker.read_text())
                    process.send_signal(signum)
                    _, stderr = process.communicate(timeout=10)
                    self.assertEqual(process.returncode, 128 + signum, stderr)
                    with self.assertRaises(ProcessLookupError):
                        os.kill(child, 0)
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait()
            result = subprocess.run(
                [*self.cli, "add", "path:/tmp/flake", "--package", "tool"],
                env={**self.environment, "TEST_FAST": "1"},
                capture_output=True,
                timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_cancellation_stops_descendants_when_the_leader_exits_first(self):
        ready = self.root / "descendant-ready"
        leaked = self.root / "continued-work"
        marker = self.root / "leader-ready"
        descendant = (
            "import signal, time; from pathlib import Path; "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            f"Path({str(ready)!r}).touch(); time.sleep(1); "
            f"Path({str(leaked)!r}).touch()"
        )
        self.write_nix(
            "import subprocess\nfrom pathlib import Path\n"
            "signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))\n"
            f"subprocess.Popen([sys.executable, '-c', {descendant!r}], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
            f"while not Path({str(ready)!r}).exists(): time.sleep(0.01)\n"
            f"Path({str(marker)!r}).touch()\n"
            "time.sleep(60)\n"
        )
        with subprocess.Popen(
            [*self.cli, "add", "path:/tmp/flake", "--package", "tool"],
            env=self.environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ) as process:
            try:
                deadline = time.monotonic() + 10
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(marker.exists(), "Descendant did not start")
                process.terminate()
                _, stderr = process.communicate(timeout=10)
                self.assertEqual(process.returncode, 143, stderr)
                time.sleep(1.1)
                self.assertFalse(leaked.exists(), "Orphan descendant continued working")
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()


if __name__ == "__main__":
    unittest.main()
