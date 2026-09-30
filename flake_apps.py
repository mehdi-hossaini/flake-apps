#!/usr/bin/env python3
"""Manage native Nix flake packages through Nix's own profile operations."""

from __future__ import annotations

import argparse
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, nullcontext, suppress
import fcntl
import json
import os
from pathlib import Path
import platform
import re
import signal
import subprocess
import sys
from types import FrameType
from typing import Any, TypedDict, cast
import urllib.parse

VERSION = "0.1.0"
MINIMUM_NIX = (2, 30)
EVALUATION_FLAGS = (
    "--no-update-lock-file",
    "--option",
    "allow-import-from-derivation",
    "false",
)


class Error(Exception):
    pass


class Terminated(Exception):
    def __init__(self, signum: int):
        self.signum = signum


class Description(TypedDict):
    source: str
    locked_source: str
    locked: dict[str, Any]
    system: str
    packages: list[str]


def clean(value: object) -> str:
    return "".join(char for char in str(value) if char.isprintable())


def flake_reference(value: str) -> str:
    if not value or value.startswith("-") or "#" in value:
        raise Error("Supply a flake reference without a fragment; use --package NAME.")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise Error("Flake references must not contain control characters.")
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme in ("https", "http") and parsed.hostname == "github.com":
        repository = parsed.path.strip("/").removesuffix(".git")
        if (
            parsed.scheme != "https"
            or parsed.netloc != "github.com"
            or parsed.query
            or not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*",
                repository,
            )
        ):
            raise Error("Use a repository URL: https://github.com/OWNER/REPO.")
        return "github:" + repository
    return value


def package_names(value: object) -> list[str]:
    if not isinstance(value, list) or any(
        not isinstance(name, str) or not name for name in value
    ):
        raise Error("Nix returned an invalid package list.")
    return cast(list[str], value)


def select_package(packages: object, requested: str | None) -> str:
    packages = package_names(packages)
    if requested is not None:
        if requested in packages:
            return requested
    elif "default" in packages:
        return "default"
    elif len(packages) == 1:
        return packages[0]
    raise Error(
        "Choose --package from: "
        + (
            ", ".join(json.dumps(name) for name in packages[:20])
            + ("; use inspect to see all packages" if len(packages) > 20 else "")
            or "no packages for this system"
        )
    )


def attribute_path(system: str, package: str | None = None) -> str:
    names = ["packages", system] + ([package] if package is not None else [])
    if any(not name or '"' in name or "\x00" in name for name in names):
        raise Error(
            "Nix cannot select empty attribute names or names containing a double "
            "quote or NUL; ask upstream to expose a selectable alias."
        )
    # Nix's selection parser treats quoted contents literally, without JSON or
    # Nix-expression escapes. URL encoding protects the fragment from ^ output
    # selectors, # delimiters, and URL parsing before Nix decodes the attribute.
    return "packages." + ".".join('"' + name + '"' for name in names[1:])


def installable(reference: str, system: str, package: str | None = None) -> str:
    return (
        reference + "#" + urllib.parse.quote(attribute_path(system, package), safe="")
    )


def validate_profile_attributes(system: str, package: str) -> None:
    # Nix profiles serialize AttrPath components without quoting and rebuild a
    # URL from them. Keep tracking/update behavior portable across host versions.
    for name in (system, package):
        if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            raise Error(
                "Nix profiles cannot reliably track attribute "
                + json.dumps(name)
                + "; use an upstream alias containing only ASCII letters, digits, "
                "underscores, and hyphens."
            )


def run_command(arguments: Sequence[str], *, capture: bool = False) -> str | None:
    with subprocess.Popen(
        arguments,
        stdout=subprocess.PIPE if capture else None,
        text=True,
        encoding="utf-8",
        start_new_session=True,
    ) as process:
        try:
            stdout, _ = process.communicate()
        except (KeyboardInterrupt, Terminated) as error:
            signum = (
                signal.SIGINT if isinstance(error, KeyboardInterrupt) else error.signum
            )
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signum)
            try:
                # This grace period applies only to cancellation, never a build.
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            # The leader can exit while descendants ignore the forwarded signal.
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            raise
        if process.returncode:
            raise subprocess.CalledProcessError(process.returncode, arguments)
        return stdout


def require_nix() -> None:
    try:
        version = run_command(["nix", "--version"], capture=True)
    except FileNotFoundError as error:
        raise Error(
            "Nix 2.30+ is required; install Nix and put nix on PATH."
        ) from error
    match = re.search(r"\bNix\) (\d+)\.(\d+)", version or "")
    if match is None or tuple(map(int, match.groups())) < MINIMUM_NIX:
        raise Error("Nix 2.30+ is required; upgrade the host's Nix installation.")


def nix(*arguments: str | Path, capture: bool = False) -> Any:
    stdout = run_command(
        [
            "nix",
            "--extra-experimental-features",
            "nix-command flakes",
            "--option",
            "accept-flake-config",
            "false",
            *map(str, arguments),
        ],
        capture=capture,
    )
    return json.loads(stdout or "") if capture else None


def describe(reference: str, system: str) -> Description:
    metadata = nix(
        "flake", "metadata", "--json", *EVALUATION_FLAGS, reference, capture=True
    )
    if not isinstance(metadata, dict) or not isinstance(metadata.get("url"), str):
        raise Error("Nix returned invalid flake metadata.")
    packages = nix(
        "eval",
        "--json",
        *EVALUATION_FLAGS,
        installable(metadata["url"], system),
        "--apply",
        "builtins.attrNames",
        capture=True,
    )
    packages = package_names(packages)
    locked = metadata.get("locked", {})
    if not isinstance(locked, dict):
        raise Error("Nix returned invalid locked source metadata.")
    return {
        "source": reference,
        "locked_source": metadata["url"],
        "locked": locked,
        "system": system,
        "packages": packages,
    }


def default_profile() -> Path:
    override = os.environ.get("XDG_DATA_HOME", "")
    base = (
        Path(override)
        if override and Path(override).is_absolute()
        else Path.home() / ".local/share"
    )
    return base / "flake-apps/profile"


@contextmanager
def profile_lock(profile: Path) -> Iterator[None]:
    # Never unlink this file: waiting processes must keep locking the same inode.
    # Resolve only the parent, since the profile symlink changes each generation.
    lock_path = profile.parent / ("." + profile.name + ".flake-apps.lock")
    descriptor = os.open(
        lock_path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(descriptor, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if sys.stderr.isatty():
                print(f"Waiting for another command using {profile}.", file=sys.stderr)
            fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def positive_generation(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("Generation numbers must be positive.")
    return number


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog="flake-apps",
        description="Manage native Nix flake packages in a Nix profile.",
        epilog="Put --profile and --system before the command. Requires host Nix 2.30+.",
    )
    result.add_argument("--version", action="version", version=f"flake-apps {VERSION}")
    result.add_argument(
        "--profile",
        type=Path,
        default=default_profile(),
        help="Independent Nix profile (default: %(default)s)",
    )
    result.add_argument(
        "--system",
        default=f"{platform.machine()}-{sys.platform}",
        help="Flake package architecture (default: %(default)s)",
    )
    commands = result.add_subparsers(dest="command", required=True)
    inspect = commands.add_parser(
        "inspect", help="Print locked metadata and packages as JSON"
    )
    inspect.add_argument(
        "flake",
        type=flake_reference,
        help="Flake reference, e.g. github:helix-editor/helix",
    )
    add = commands.add_parser(
        "add",
        help="Install a native flake package",
        description="Install the default or sole package; use --package when choices are ambiguous.",
    )
    add.add_argument(
        "flake",
        type=flake_reference,
        help="Flake reference, e.g. github:helix-editor/helix",
    )
    add.add_argument(
        "--package",
        help="Literal direct attribute of packages.SYSTEM; explicit choices are validated by Nix",
    )
    listing = commands.add_parser(
        "list", help="List packages using Nix's profile manifest"
    )
    listing.add_argument(
        "--json", action="store_true", help="Print the host Nix's JSON manifest"
    )
    update = commands.add_parser("update", help="Update a package or every package")
    update.add_argument(
        "name", nargs="?", help="Name printed by list; omit to update every package"
    )
    remove = commands.add_parser(
        "remove", help="Remove a package using its name from list"
    )
    remove.add_argument("name", help="Exact package name printed by list")
    rollback = commands.add_parser(
        "rollback",
        help="Restore a retained profile generation",
        description="Restore all packages in this profile to a retained generation.",
    )
    rollback.add_argument(
        "--to",
        type=positive_generation,
        help="Positive generation number from history; default: previous generation",
    )
    commands.add_parser("history", help="Show retained Nix profile generations")
    return result


def dispatch(args: argparse.Namespace) -> None:
    if args.command == "inspect":
        print(json.dumps(describe(args.flake, args.system), indent=2))
        return
    requested_profile = args.profile.expanduser().absolute()
    profile = requested_profile.parent.resolve() / requested_profile.name
    if args.command == "add":
        package = args.package
        if package is None:
            package = select_package(
                describe(args.flake, args.system)["packages"], None
            )
        validate_profile_attributes(args.system, package)
        target = installable(args.flake, args.system, package)
        profile.parent.mkdir(parents=True, exist_ok=True)
        # Preserve the tracking reference. Nix records both the original source
        # and its exact locked revision/hash in the resulting profile manifest.
        with profile_lock(profile):
            check_profile(profile)
            nix("profile", "add", "--profile", profile, *EVALUATION_FLAGS, target)
        return
    if args.command == "list":
        check_profile(profile)
        if not profile.exists() and not args.json:
            print("No packages installed.")
            return
        # Let the host Nix own the schema, including an absent profile's JSON.
        nix("profile", "list", "--profile", profile, *(["--json"] if args.json else []))
        return
    if args.command != "history" and not profile.parent.exists():
        raise Error(
            f"No profile at {profile}; install a package with 'flake-apps add'."
        )
    with nullcontext() if args.command == "history" else profile_lock(profile):
        if args.command in ("update", "remove"):
            check_profile(profile)
            if not profile.exists():
                raise Error(
                    f"No profile at {profile}; install a package with 'flake-apps add'."
                )
        arguments = ["profile", args.command, "--profile", str(profile)]
        if args.command == "update":
            validate_profile_updates(profile, args.name)
            arguments[1] = "upgrade"
            arguments.extend(EVALUATION_FLAGS)
            arguments.extend(["--", args.name] if args.name is not None else ["--all"])
        elif args.command == "remove":
            arguments.extend(["--", args.name])
        elif args.command == "rollback" and args.to is not None:
            arguments.extend(["--to", str(args.to)])
        nix(*arguments)


def check_profile(profile: Path) -> None:
    if profile.is_symlink() and not profile.exists():
        raise Error(
            f"Profile at {profile} points to a missing generation; use history and rollback --to GENERATION to recover."
        )


def validate_profile_updates(profile: Path, name: str | None) -> None:
    manifest = nix("profile", "list", "--profile", profile, "--json", capture=True)
    if not isinstance(manifest, dict) or not isinstance(manifest.get("elements"), dict):
        raise Error("Nix returned an invalid profile manifest.")
    elements = manifest["elements"]
    selected = [elements.get(name)] if name is not None else elements.values()
    for element in selected:
        if not isinstance(element, dict):
            continue
        path = element.get("attrPath")
        if isinstance(path, str) and path.startswith("packages."):
            parts = path.split(".", 2)
            if len(parts) != 3:
                raise Error(
                    "Unsupported profile attribute; install a simple upstream alias."
                )
            names = [
                part[1:-1] if part.startswith('"') and part.endswith('"') else part
                for part in parts[1:]
            ]
            validate_profile_attributes(names[0], names[1])


@contextmanager
def handle_termination() -> Iterator[None]:
    def terminate(signum: int, _frame: FrameType | None) -> None:
        raise Terminated(signum)

    previous = signal.signal(signal.SIGTERM, terminate)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        with handle_termination():
            try:
                args = parser().parse_args(argv)
            except SystemExit as error:
                status = error.code if isinstance(error.code, int) else 1
            else:
                require_nix()
                dispatch(args)
                status = 0
            # Catch buffered output failures here, before interpreter shutdown.
            sys.stdout.flush()
            return status
    except BrokenPipeError:
        with open(os.devnull, "w") as sink:
            os.dup2(sink.fileno(), sys.stdout.fileno())
        return 0
    except subprocess.CalledProcessError as error:
        # Nix has already streamed its diagnostics to stderr.
        return error.returncode if error.returncode > 0 else 128 - error.returncode
    except (Error, OSError, ValueError) as error:
        print("flake-apps: " + clean(error), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    except Terminated as error:
        return 128 + error.signum


if __name__ == "__main__":
    sys.exit(main())
