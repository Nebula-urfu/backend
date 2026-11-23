{
  description = "Nebula procurement API";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/2786a2a72088ecf32aea5b3ec52099e6a7cff78f";

  outputs = { self, nixpkgs }:
    let
      system = "x86_64-linux";
      pkgs = import nixpkgs { inherit system; };
      python = pkgs.python314;
    in {
      devShells.${system}.default = pkgs.mkShell {
        packages = [ python pkgs.uv pkgs.postgresql_17 pkgs.curl ];
        UV_PYTHON_DOWNLOADS = "never";
      };
      checks.${system}.sources = pkgs.runCommand "nebula-backend-sources" {
        nativeBuildInputs = [ python ];
      } ''
        cp -r ${self} source
        chmod -R u+w source
        cd source
        python -m compileall -q app migrations scripts tests
        python -m json.tool openapi.json >/dev/null
        python -c 'import tomllib; tomllib.load(open("uv.lock", "rb"))'
        touch "$out"
      '';
    };
}
