{ pkgs }:
pkgs.stdenvNoCC.mkDerivation {
  pname = "flake-apps";
  version = "0.1.0";
  src = ./flake_apps.py;
  dontUnpack = true;
  dontBuild = true;
  doInstallCheck = true;
  nativeBuildInputs = [ pkgs.makeWrapper ];
  installPhase = ''
    install -Dm644 "$src" "$out/lib/flake-apps/flake_apps.py"
    makeWrapper ${pkgs.python3}/bin/python3 "$out/bin/flake-apps" \
      --add-flags "$out/lib/flake-apps/flake_apps.py"
  '';
  installCheckPhase = ''
    runHook preInstallCheck
    "$out/bin/flake-apps" --help > /dev/null
    test "$("$out/bin/flake-apps" --version)" = "flake-apps $version"
    runHook postInstallCheck
  '';
  meta = {
    description = "Focused native Nix flake package manager";
    mainProgram = "flake-apps";
    platforms = [
      "x86_64-linux"
      "aarch64-linux"
    ];
  };
}
