# flake-apps

A small CLI for native Nix flake packages. Nix owns package builds, source locks,
profile changes, and retained generations. There is no separate package database.
GitHub release files belong in Obtain.

## Use

Requires host Nix 2.30+ on PATH, checked before running commands. Running the
source also requires Python 3.10+; the Nix package supplies Python automatically.
Supports `packages.x86_64-linux` and `packages.aarch64-linux`. A default or sole
package is selected automatically; ambiguous choices require `--package`.

Enable `nix-command flakes` in your Nix configuration for the short `nix run`
examples below. To start without changing configuration:

```sh
nix --extra-experimental-features 'nix-command flakes' run path:. -- --help
```

The CLI enables these features for its own Nix commands.

```sh
nix run path:. -- inspect github:OWNER/REPO
nix run path:. -- add github:OWNER/REPO --package PACKAGE
nix run path:. -- list
nix run path:. -- update
nix run path:. -- history
nix run path:. -- rollback
nix run path:. -- remove NAME
```

Replace placeholders with an upstream native flake and a name printed by `list`.
GitHub repository URLs are also accepted. Other Nix flake references, including
`git+file:` and `path:`, are passed to Nix. Attribute fragments are selected with
`--package`. Package and system attribute names must contain only ASCII letters,
digits, underscores, and hyphens. Host Nix profiles flatten quoted attribute paths
and reconstruct unescaped URLs: dotted names can lose their literal meaning on
update, and Unicode or punctuation can fail during installation. These names are
rejected before profile creation with guidance to request a simple upstream alias.
`inspect` still reports all names as escaped JSON. Explicit `--package` selection
goes directly to Nix for validation, avoiding catalog enumeration.
Updates also reject previously installed untrackable names; install a supported
upstream alias, then remove the old entry using its name from `list`.

By default, packages use `$XDG_DATA_HOME/flake-apps/profile`, with XDG data defaulting
to `~/.local/share` when unset, empty, or relative. Add its `bin` directory to PATH
to run installed programs. For a shell setup that follows the same defaults:

```sh
case "${XDG_DATA_HOME-}" in
  /*) flake_apps_data="$XDG_DATA_HOME" ;;
  *) flake_apps_data="$HOME/.local/share" ;;
esac
flake_apps_profile="$flake_apps_data/flake-apps/profile"
export PATH="$flake_apps_profile/bin:$PATH"
```

Add those lines to your shell startup file, such as `~/.zshrc`.
Use `--profile /absolute/path` before a command for an independent profile; this
never changes your default Nix or Obtain profile. All packages in one profile
share its generations, so rollback restores that profile as a whole.

`inspect` prints JSON and creates no profile. `list --json` uses Nix's own manifest.
`update NAME` changes one package; `update` changes all packages in this profile.
Branch references follow updates, while exact commit references remain fixed.
Every installed generation records the resolved source revision and hash in Nix.
Removed packages remain in retained generations until you use Nix to delete them.

CLI mutations are serialized per profile using a persistent sibling lock file.
Direct Nix commands do not acquire that lock; coordinate them with CLI mutations.
If a profile points to a missing generation, `list` reports the broken state.
Use `history`, then `rollback --to GENERATION`, to restore a retained generation.

To inspect which old generations would be removed, use the profile variable from
the shell setup above (or set it to your custom profile):

```sh
nix --extra-experimental-features 'nix-command flakes' profile wipe-history \
  --profile "$flake_apps_profile" --older-than 30d --dry-run
```

Remove `--dry-run` to prune them. Pruned generations are no longer available for
rollback; Nix garbage collection can subsequently reclaim unreferenced packages.

Input lock updates and builds during evaluation are forbidden. Upstream flakes
must already lock their inputs. Builds use pure flake evaluation and do not
automatically accept upstream binary-cache configuration. Errors and build
diagnostics go to stderr; failed Nix commands keep their exit status.
Cancellation forwards SIGINT/SIGTERM to the active Nix process group and waits for
cleanup; children that ignore cancellation are killed after a five-second grace
period. There is no build-duration timeout. A consumer closing stdout early is
handled quietly with exit status 0.

## Development

```sh
nix --extra-experimental-features 'nix-command flakes' develop --no-update-lock-file
just run --help
just verify
just fmt
./result/bin/flake-apps --help
```

Alternatively, trust this checkout with `direnv allow` to enter the same shell.
The lockfile reuses Obtain's tested Nixpkgs pin. Python, Ruff, Pyright, nixd,
nixfmt, debugpy, Nix, Git, just, and actionlint are project-local development tools. Launch
your editor from the shell. `just fmt` formats source; `just debug --help` waits
for a DAP client at `127.0.0.1:5678`.

`just check-unit` checks the host architecture. `just verify` runs static and unit
checks, the packaged unit check, the real lifecycle smoke test, and the package
build. Pyright checks basic types against Python 3.10; a unit check keeps CLI and
package release versions synchronized.

GitHub CI runs the same verification on x86_64 and ARM64 Linux, including the
smoke check under optimized Python, and separately runs unit and process tests
on Python 3.10. Package builds check the installed command's help and version.
`just check` also validates the GitHub workflow. Actions use fixed commit pins,
and Dependabot proposes grouped monthly updates to those pins.

Enter the development shell first to prepare the pinned dependencies; this step
may download sources or packages. The smoke check then forces every Nix command
offline and fails if required dependencies are unavailable. It builds tiny local
flakes and uses only temporary profiles. It checks distinct package selection,
rejection of untrackable names, named-operation isolation, failure preservation,
whole-profile rollback, broken-link recovery, concurrent installs, and unlocked
input rejection. Its assertions remain active under optimized Python. Unit tests
also exercise child cancellation and early output-pipe closure.

Keep contributions focused and include regression tests for changed behavior.
Run `just verify` before opening a pull request. For a bug report, include the
command, expected and actual behavior, Linux architecture, `nix --version`, and
a minimal flake example; redact private references and credentials from logs.
For dependency maintenance, run `nix flake update nixpkgs`, review `flake.lock`,
then enter the updated development shell and run `just verify` on both supported
architectures before accepting the update.

## Scope and provenance

This project separates native flake package management from Obtain. Package
selection originated in Obtain's native flake implementation at commit
`26880595d52af7cf33328567838cbe9d7b361806`; profile operations are delegated
to Nix rather than copying Obtain's database, release downloads, wrappers, or
recovery journal. Obtain retains compatibility for reading and removing earlier
flake entries. To migrate one, use its locked flake URL and package from
`obtain info NAME`, install it here, then remove the old Obtain entry.

## Licensing

No license has been selected; this repository currently has no license file.
