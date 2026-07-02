#!/usr/bin/env sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
RUST_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
BIN="$RUST_DIR/target/release/iac-code"
TMP_PARENT="$RUST_DIR/target/tmp"
mkdir -p "$TMP_PARENT"
TMP_DIR=$(mktemp -d "$TMP_PARENT/iac-code-rs-build.XXXXXX")

cleanup() {
    rm -rf "$TMP_DIR"
}
trap cleanup EXIT INT TERM

cd "$RUST_DIR"
cargo build --release -p iac-code-cli --bin iac-code

if [ ! -x "$BIN" ]; then
    echo "iac-code binary is missing or not executable: $BIN" >&2
    exit 1
fi

IAC_CODE_CONFIG_DIR="$TMP_DIR/config" "$BIN" --version >/dev/null
IAC_CODE_CONFIG_DIR="$TMP_DIR/config" "$BIN" --help >/dev/null

echo "Built local iac-code binary: $BIN"
echo "Smoke checks passed: --version, --help"
