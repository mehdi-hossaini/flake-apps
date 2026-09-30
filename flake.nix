{
  description = "Manage native Nix flake packages with Nix profiles";
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";
  outputs =
    { self, nixpkgs, ... }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
      ];
      eachSystem = nixpkgs.lib.genAttrs systems;
    in
    {
      packages = eachSystem (system: {
        default = import ./package.nix { pkgs = nixpkgs.legacyPackages.${system}; };
      });
      apps = eachSystem (system: {
        default = {
          type = "app";
          meta.description = "Manage native Nix flake packages";
          program = "${self.packages.${system}.default}/bin/flake-apps";
        };
      });
      checks = eachSystem (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
        in
        {
          unit =
            pkgs.runCommand "flake-apps-tests"
              {
                nativeBuildInputs = [ pkgs.python3 ];
                src = nixpkgs.lib.fileset.toSource {
                  root = ./.;
                  fileset = nixpkgs.lib.fileset.unions [
                    ./flake_apps.py
                    ./package.nix
                    ./tests
                  ];
                };
              }
              ''
                cp -r "$src" source
                chmod -R u+w source
                cd source
                python3 -m unittest discover -s tests -v
                touch "$out"
              '';
        }
      );
      devShells = eachSystem (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
        in
        {
          default = pkgs.mkShell {
            packages = [
              (pkgs.python3.withPackages (ps: [ ps.debugpy ]))
              pkgs.ruff
              pkgs.pyright
              pkgs.nixd
              pkgs.nixfmt
              pkgs.nix
              pkgs.git
              pkgs.just
              pkgs.actionlint
            ];
          };
        }
      );
    };
}
