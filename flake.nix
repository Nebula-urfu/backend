{
  description = "Nebula procurement API";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/2786a2a72088ecf32aea5b3ec52099e6a7cff78f";

  outputs = { self, nixpkgs }:
    let
      system = "x86_64-linux";
      pkgs = import nixpkgs { inherit system; };
      inherit (pkgs) lib;
      python = pkgs.python314;
      lock = builtins.fromTOML (builtins.readFile ./uv.lock);
      dependencies = lib.filter (package: package.source ? registry) lock.package;
      supportsLinux = wheel:
        let file = baseNameOf wheel.url;
        in lib.hasSuffix "-none-any.whl" file ||
          (lib.hasInfix "manylinux" file && lib.hasSuffix "x86_64.whl" file &&
            (lib.hasInfix "-cp314-cp314-" file || lib.hasInfix "-abi3-" file));
      wheelhouse = pkgs.linkFarm "nebula-python-wheels" (lib.concatMap
        (package: map (wheel: {
          name = baseNameOf wheel.url;
          path = pkgs.fetchurl {
            inherit (wheel) url;
            sha256 = lib.removePrefix "sha256:" wheel.hash;
          };
        }) (lib.filter supportsLinux (package.wheels or [ ])))
        dependencies);
      requirements = pkgs.writeText "nebula-requirements.txt" (lib.concatMapStringsSep "\n"
        (package: "${package.name}==${package.version}") dependencies);
    in {
      devShells.${system}.default = pkgs.mkShell {
        packages = [ python pkgs.uv pkgs.postgresql_17 pkgs.curl ];
        UV_PYTHON_DOWNLOADS = "never";
      };
      checks.${system}.backend = pkgs.runCommand "nebula-backend-check" {
        nativeBuildInputs = [ python pkgs.uv ];
        LD_LIBRARY_PATH = lib.makeLibraryPath [ pkgs.stdenv.cc.cc.lib ];
      } ''
        cp -r ${self} source
        chmod -R u+w source
        cd source
        export UV_CACHE_DIR="$TMPDIR/uv-cache" UV_PYTHON_DOWNLOADS=never
        uv venv --offline --python ${python}/bin/python
        uv pip install --offline --no-index --find-links ${wheelhouse} --no-deps -r ${requirements}
        .venv/bin/python -m compileall -q app migrations scripts tests
        PYTHONPATH=. .venv/bin/python scripts/export_openapi.py > "$TMPDIR/openapi.json"
        diff -u openapi.json "$TMPDIR/openapi.json"
        touch "$out"
      '';
    };
}
