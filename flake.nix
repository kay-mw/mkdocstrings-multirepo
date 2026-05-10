{
  description = "mkdocstrings-multirepo";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/6308c3b21396534d8aaeac46179c14c439a89b8a";
  };

  outputs =
    { self, nixpkgs, ... }:
    let
      system = "x86_64-linux";
    in
    {
      devShells."${system}".default =
        let
          pkgs = import nixpkgs { inherit system; };
        in
        pkgs.mkShell {
          packages = with pkgs; [
            python314
            uv
          ];
          shellHook = ''
            VENV=.venv
              if ! [ -d $VENV ]; then
              uv venv .venv --no-managed-python
            fi

            source .venv/bin/activate

            export LD_LIBRARY_PATH=$NIX_LD_LIBRARY_PATH
          '';
        };

    };
}
