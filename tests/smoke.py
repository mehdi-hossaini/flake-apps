"""Exercise real Nix lifecycle operations in temporary profiles, with offline Nix."""

import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
NIX = ["nix", "--offline", "--extra-experimental-features", "nix-command flakes"]
SPECIAL_NAMES = [
    "app.gui",
    "${evil}",
    "slash\\name",
    "café",
    "caret^name",
    "hash#name",
    "percent%name",
    'quote"name',
]


def expect(condition, message):
    if not condition:
        raise AssertionError(message)


def run(arguments, *, expected=0, cwd=None, env=None):
    result = subprocess.run(arguments, cwd=cwd, env=env, capture_output=True, text=True)
    expect(
        result.returncode == expected,
        f"{arguments!r}: expected {expected}, got {result.returncode}\n{result.stdout}\n{result.stderr}",
    )
    return result.stdout


def nix_string(value):
    return json.dumps(value, ensure_ascii=False).replace("${", "\\${")


def write_fixture(directory, system, version, *, broken=False, unlocked=False):
    def package(binary, marker, *, fail=False, delay=0):
        name = nix_string(binary + "-" + marker)
        if fail:
            return f'pkgs.runCommand {name} {{}} "exit 17"'
        body = nix_string("printf '%s\\n' " + shlex.quote(marker))
        executable = f"(pkgs.writeShellScript {name} {body})"
        prefix = nix_string(f'sleep {delay}; mkdir -p "$out/bin"; cp ')
        suffix = nix_string(f' "$out/bin/{binary}"')
        return f"pkgs.runCommand {name} {{}} ({prefix} + {executable} + {suffix})"

    default = package("native-fixture", version, fail=broken)
    attributes = {
        "default": default,
        "gui": package("native-gui", "gui-" + version),
        "other": package("native-other", "other-" + version),
        # Unique slow builds make concurrent mutation overlap before the fix.
        "first": package("native-first", directory.parent.name + "-first", delay=2),
        "second": package("native-second", directory.parent.name + "-second", delay=2),
        **{name: default for name in SPECIAL_NAMES},
    }
    input_clause = (
        "inputs.unlocked.url = "
        + nix_string("path:" + str(directory.parent / "dependency"))
        + ";"
        if unlocked
        else ""
    )
    (directory / "flake.nix").write_text(
        '{ inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05"; '
        + input_clause
        + " outputs = { self, nixpkgs, ... }: let pkgs = nixpkgs.legacyPackages."
        + nix_string(system)
        + "; in { packages."
        + nix_string(system)
        + " = { "
        + " ".join(
            nix_string(name) + " = " + value + ";" for name, value in attributes.items()
        )
        + " }; }; }\n"
    )


def main():
    actual_nix = shutil.which("nix")
    expect(actual_nix is not None, "Nix 2.30+ is required; enter nix develop first.")
    with tempfile.TemporaryDirectory(prefix="flake-apps-smoke-") as temporary:
        root = Path(temporary)
        # Every wrapper invocation also uses offline Nix. This adapter changes
        # only the executable's flags; package evaluation and profiles stay real.
        bin_directory = root / "bin"
        bin_directory.mkdir()
        offline_nix = bin_directory / "nix"
        offline_nix.write_text(
            f"#!{sys.executable}\nimport os, sys\n"
            f"os.execv({actual_nix!r}, [{actual_nix!r}, '--offline', *sys.argv[1:]])\n"
        )
        offline_nix.chmod(0o755)
        environment = {
            **os.environ,
            "PATH": str(bin_directory) + os.pathsep + os.environ.get("PATH", ""),
            "XDG_DATA_HOME": str(root / "data"),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
        }
        system = run(
            [*NIX, "eval", "--impure", "--raw", "--expr", "builtins.currentSystem"],
            env=environment,
        )
        repository = root / "upstream"
        repository.mkdir()
        lock_bytes = (ROOT / "flake.lock").read_bytes()
        (repository / "flake.lock").write_bytes(lock_bytes)
        profile = root / "data/flake-apps/profile"
        cli = [sys.executable, str(ROOT / "flake_apps.py")]
        run(
            ["git", "init", "--quiet", "--initial-branch=main", str(repository)],
            env=environment,
        )

        def commit(version, **kwargs):
            write_fixture(repository, system, version, **kwargs)
            run(
                ["git", "add", "flake.nix", "flake.lock"],
                cwd=repository,
                env=environment,
            )
            run(
                [
                    "git",
                    "-c",
                    "user.name=Fixture",
                    "-c",
                    "user.email=fixture@example.invalid",
                    "-c",
                    "commit.gpgsign=false",
                    "commit",
                    "--quiet",
                    "-m",
                    version,
                ],
                cwd=repository,
                env=environment,
            )

        def command(*arguments, expected=0):
            return run([*cli, *arguments], expected=expected, env=environment)

        def manifest(selected_profile=profile):
            return json.loads(
                command("--profile", str(selected_profile), "list", "--json")
            )

        def entry(attribute, selected_profile=profile):
            matches = [
                (name, value)
                for name, value in manifest(selected_profile)["elements"].items()
                if value["attrPath"] == f"packages.{system}.{attribute}"
            ]
            expect(
                len(matches) == 1,
                f"Expected exactly one entry for {attribute!r}: {matches}",
            )
            return matches[0]

        def binary(name, expected, selected_profile=profile):
            actual = run([str(selected_profile / "bin" / name)]).strip()
            expect(actual == expected, f"{name}: expected {expected!r}, got {actual!r}")

        commit("v1")
        report = json.loads(command("inspect", "git+file://" + str(repository)))
        expect(
            report["packages"]
            == sorted(["gui", "default", "other", "first", "second", *SPECIAL_NAMES]),
            "Inspection omitted package names",
        )
        expect(not profile.parent.exists(), "Inspection created profile state")
        empty = manifest()
        expect(empty["elements"] == {}, "Absent-profile JSON is not empty")
        expect(not profile.parent.exists(), "Absent-profile listing created state")
        reference = "git+file://" + str(repository)
        command("add", reference)
        binary("native-fixture", "v1")
        original = manifest()
        expect(len(original["elements"]) == 1, "Expected one installed package")
        default_name, default_entry = entry("default")
        expect(
            default_entry["originalUrl"].startswith("git+file:"),
            "Original tracking reference was lost",
        )
        expect("rev=" in default_entry["url"], "Resolved source is not pinned")

        literal_profile = root / "literal/profile"
        command("--profile", str(literal_profile), "add", reference, "--package", "gui")
        binary("native-gui", "gui-v1", literal_profile)
        expect(
            not (literal_profile / "bin/native-fixture").exists(),
            "Explicit selection incorrectly installed default",
        )
        entry("gui", literal_profile)
        expect(manifest() == original, "Explicit profile changed normal test profile")

        for index, name in enumerate(SPECIAL_NAMES):
            special_profile = root / f"special-{index}/profile"
            result = subprocess.run(
                [
                    *cli,
                    "--profile",
                    str(special_profile),
                    "add",
                    reference,
                    "--package",
                    name,
                ],
                env=environment,
                capture_output=True,
                text=True,
            )
            expect(
                result.returncode == 1 and "alias" in result.stderr,
                "Untrackable attribute did not produce guidance",
            )
            expect(
                not special_profile.parent.exists(),
                "Rejected attribute created profile state",
            )

        revision = run(
            ["git", "rev-parse", "HEAD"], cwd=repository, env=environment
        ).strip()
        # Older releases allowed dotted names that Nix later flattens. Refuse
        # updates before the wrong nested attribute could be installed.
        legacy_profile = root / "legacy/profile"
        legacy_profile.parent.mkdir()
        run(
            [
                *NIX,
                "profile",
                "add",
                "--profile",
                str(legacy_profile),
                "--no-update-lock-file",
                reference + f'#packages."{system}"."app.gui"',
            ],
            env=environment,
        )
        legacy_original = manifest(legacy_profile)
        command("--profile", str(legacy_profile), "update", expected=1)
        expect(
            manifest(legacy_profile) == legacy_original,
            "Rejected legacy update changed the profile",
        )
        legacy_name = next(iter(legacy_original["elements"]))
        command("--profile", str(legacy_profile), "remove", legacy_name)
        expect(manifest(legacy_profile)["elements"] == {}, "Legacy removal was blocked")
        fixed_profile = root / "fixed/profile"
        command("--profile", str(fixed_profile), "add", reference + "?rev=" + revision)
        command("add", reference, "--package", "other")
        original_both = manifest()
        _, original_other = entry("other")
        generation_match = re.fullmatch(r"profile-(\d+)-link", profile.readlink().name)
        expect(generation_match is not None, "Unexpected generation link")
        original_generation = (
            generation_match[1] if generation_match is not None else ""
        )

        commit("broken", broken=True)
        failure = subprocess.run(
            [*cli, "update"], env=environment, capture_output=True, text=True
        )
        expect(failure.returncode != 0, "Broken build unexpectedly succeeded")
        expect(manifest() == original_both, "Failed update changed the profile")
        binary("native-fixture", "v1")
        binary("native-other", "other-v1")

        commit("v2")
        command("update", default_name)
        binary("native-fixture", "v2")
        binary("native-other", "other-v1")
        expect(
            entry("other")[1] == original_other,
            "Named update changed the unrelated package",
        )
        command("update")
        binary("native-other", "other-v2")
        command("--profile", str(fixed_profile), "update")
        binary("native-fixture", "v1", fixed_profile)
        command("--profile", str(literal_profile), "update")
        binary("native-gui", "gui-v2", literal_profile)
        command("history")
        command("rollback", "--to", original_generation)
        binary("native-fixture", "v1")
        binary("native-other", "other-v1")
        command("update", default_name)
        unrelated = entry("other")[1]
        command("remove", default_name)
        expect(
            not (profile / "bin/native-fixture").exists(),
            "Removed binary remains installed",
        )
        expect(
            entry("other")[1] == unrelated,
            "Named removal changed the unrelated package",
        )
        binary("native-other", "other-v1")
        command("rollback")
        binary("native-fixture", "v2")
        binary("native-other", "other-v1")

        # Recovery must remain available with a broken current generation link.
        profile.unlink()
        profile.symlink_to("profile-999999-link")
        command("list", "--json", expected=1)
        command("history")
        command("rollback", "--to", original_generation)
        binary("native-fixture", "v1")
        binary("native-other", "other-v1")

        concurrent_profile = root / "concurrent/profile"
        concurrent_alias = root / "concurrent-alias"
        concurrent_alias.symlink_to(concurrent_profile.parent, target_is_directory=True)
        processes = [
            subprocess.Popen(
                [
                    *cli,
                    "--profile",
                    str(selected_profile),
                    "add",
                    reference,
                    "--package",
                    name,
                ],
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for name, selected_profile in (
                ("first", concurrent_profile),
                ("second", concurrent_alias / "profile"),
            )
        ]
        try:
            for process in processes:
                stdout, stderr = process.communicate(timeout=60)
                expect(
                    process.returncode == 0,
                    f"Concurrent add failed:\n{stdout}\n{stderr}",
                )
        finally:
            for process in processes:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=10)
        expect(
            len(manifest(concurrent_profile)["elements"]) == 2,
            "Concurrent add lost a successful install",
        )
        binary("native-first", root.name + "-first", concurrent_profile)
        binary("native-second", root.name + "-second", concurrent_profile)

        dependency = root / "dependency"
        dependency.mkdir()
        (dependency / "flake.nix").write_text("{ outputs = { self }: {}; }\n")
        before_unlocked = manifest()
        commit("unlocked", unlocked=True)
        failure = subprocess.run(
            [*cli, "inspect", reference],
            env=environment,
            capture_output=True,
            text=True,
        )
        expect(failure.returncode != 0, "Unlocked input unexpectedly succeeded")
        expect(
            (repository / "flake.lock").read_bytes() == lock_bytes,
            "Inspection rewrote the upstream lockfile",
        )
        expect(
            "lock file" in failure.stderr.lower(),
            "Missing actionable lockfile diagnostic",
        )
        expect(manifest() == before_unlocked, "Failed inspection changed the profile")
    print(
        "Offline native lifecycle smoke passed, including name validation, scoped operations, recovery, and concurrent installs."
    )


if __name__ == "__main__":
    main()
