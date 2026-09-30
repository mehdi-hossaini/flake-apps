set positional-arguments

default:
    @just --list

run *args="--help":
    python3 flake_apps.py "$@"

test:
    python3 -m unittest discover -s tests -q

check:
    ruff check .
    ruff format --check .
    pyright
    nixfmt --check *.nix
    just --fmt --check --unstable
    actionlint .github/workflows/ci.yml
    just test

fmt:
    ruff format .
    nixfmt *.nix
    just --fmt --unstable

build:
    nix --extra-experimental-features 'nix-command flakes' build path:. --no-update-lock-file

check-unit:
    nix --extra-experimental-features 'nix-command flakes' flake check path:. --no-update-lock-file -L

# Uses offline Nix calls; enter nix develop first to prepare dependencies.
smoke:
    python3 tests/smoke.py

# Full host verification, including real Nix lifecycle and concurrency checks.
verify: check check-unit smoke build

debug *args="--help":
    python3 -Xfrozen_modules=off -m debugpy --listen 127.0.0.1:5678 --wait-for-client flake_apps.py "$@"
