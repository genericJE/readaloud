#!/usr/bin/env bash
# Build the relocatable readaloud bundle that the Homebrew formula ships.
#
#   ./scripts/build-bundle.sh <version> [outdir]
#
# Must run on macOS arm64.  The macOS version of THIS machine becomes the
# minimum macOS the bundle supports, because mlx/mlx-metal publish one wheel
# per macOS generation (macosx_14_0 / _15_0 / _26_0) and uv picks the newest
# one the build host can run.  Build on macOS 14 (GitHub Actions `macos-14`)
# and keep `depends_on macos: :sonoma` in the formula in step.
set -euo pipefail

VERSION="${1:?usage: build-bundle.sh <version> [outdir]}"
OUT="${2:-$PWD/dist}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYVER=3.12

[ "$(uname -sm)" = "Darwin arm64" ] || { echo "must build on macOS arm64" >&2; exit 1; }

WORK="$(mktemp -d)"
# macOS hands back /var/folders/... while /var is a symlink to /private/var, and
# Python derives sys.prefix from the resolved path.  Resolve WORK up front or the
# self-contained-interpreter assert below compares the two spellings and fails.
WORK="$(cd "$WORK" && pwd -P)"
trap 'rm -rf "$WORK"' EXIT
STAGE="$WORK/readaloud-$VERSION"

# 1. A standalone CPython that depends on nothing outside its own tree.
#    python-build-standalone derives sys.prefix from argv[0], so this whole
#    directory can be moved anywhere with no fix-ups.  Do NOT wrap it in a
#    venv: pyvenv.cfg bakes in an absolute `home =` that `uv venv
#    --relocatable` does not rewrite.
uv python install "$PYVER"
PYROOT="$(cd /; uv python find --no-project "$PYVER" | xargs dirname | xargs dirname)"
PYROOT="$(cd "$PYROOT" && pwd -P)"   # uv's unversioned dir is a symlink; cp -R would
mkdir -p "$STAGE"                    # copy the LINK and every later step would then
cp -R "$PYROOT/." "$STAGE/"          # operate on the real interpreter in ~/.local

# Refuse to go further unless the copy is a self-contained interpreter: if this
# assert ever fires, the `uv pip install` steps below would install into the
# machine's own uv-managed Python instead of the bundle.
"$STAGE/bin/python$PYVER" -c "import sys; assert sys.prefix == '$STAGE', sys.prefix"

# 2. Install the locked graph straight into that interpreter.
# --extra mediakeys is deliberate: a Homebrew install is meant to be complete,
# and without it the bundle ships zero PyObjC packages, so the headphone
# play/pause button silently does nothing for everyone who installs this way.
# --extra grammars ships the Python grammar the same way, so a .py file is
# read through its tree and coloured without a pip install.
uv export --directory "$REPO" --frozen --no-dev --no-emit-project \
          --extra mediakeys --extra grammars --format requirements-txt -o "$WORK/req.txt"
uv pip install --python "$STAGE/bin/python$PYVER" --break-system-packages \
               --link-mode=copy -r "$WORK/req.txt"
# 2b. readaloud itself, as one wheel built against the bundle's own interpreter:
#     this is where the Rust extension readaloud._core is compiled (cargo must
#     be on PATH).  Installed by name from a find-links directory, because
#     installing a path writes that path into the dist-info's direct_url.json.
#     Panic messages carry the source path of every crate they come from,
#     which for the dependencies is the builder's home: write it as /cargo.
export RUSTFLAGS="--remap-path-prefix=${CARGO_HOME:-$HOME/.cargo}=/cargo"
uv build --wheel --python "$STAGE/bin/python$PYVER" --out-dir "$WORK/wheel" "$REPO"
uv pip install --python "$STAGE/bin/python$PYVER" --break-system-packages \
               --link-mode=copy --no-deps --no-index --find-links "$WORK/wheel" \
               "readaloud==$VERSION"

# 3. Replace the console-script shebangs, which point at $STAGE.  The formula's
#    own shim makes them irrelevant, but a build path baked into a shipped file
#    is a landmine, so leave none behind.
for f in "$STAGE"/bin/*; do
  [ -f "$f" ] || continue
  head -1 "$f" 2>/dev/null | grep -q "^#!$STAGE" || continue
  { printf '#!/bin/sh\n'
    printf "'''exec' \"\$(dirname -- \"\$(realpath -- \"\$0\")\")\"/'python3' \"\$0\" \"\$@\"\n"
    printf "' '''\n"
    tail -n +2 "$f"
  } > "$f.new"
  chmod 755 "$f.new"; mv "$f.new" "$f"
done

# 4. No .pyc in the tarball: the formula compiles at install time, so the paths
#    recorded in each .pyc match where it ends up.  Saves 33MB of download too.
find "$STAGE" -name __pycache__ -type d -prune -exec rm -rf {} +

# 5. Refuse to ship a build path or an AppleDouble file.
#    The extension is compiled under $REPO, so that path is checked too.
if grep -rlq -e "$WORK" -e "$REPO" "$STAGE"; then
  echo "build path leaked into bundle" >&2; exit 1
fi

# 6. The extension must not raise the minimum macOS above what mlx already
#    needs (14.0, see the header): refuse a newer minos.
SO="$(ls "$STAGE"/lib/python$PYVER/site-packages/readaloud/_core*.so)"
vtool -show-build "$SO" | grep -Eq 'minos (1[0-4]|[1-9])\.' || {
  echo "$SO needs a newer macOS than 14:" >&2; vtool -show-build "$SO" >&2; exit 1
}

mkdir -p "$OUT"
TARBALL="$OUT/readaloud-$VERSION-arm64.tar.xz"
COPYFILE_DISABLE=1 tar -cf - -C "$WORK" "readaloud-$VERSION" | xz -T0 -6 > "$TARBALL"

echo
echo "url \"https://github.com/genericJE/readaloud/releases/download/v$VERSION/readaloud-$VERSION-arm64.tar.xz\""
echo "version \"$VERSION\""
echo "sha256 \"$(shasum -a 256 "$TARBALL" | cut -d' ' -f1)\""
