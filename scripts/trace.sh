#!/usr/bin/env bash
# Quick CLI runner for VectorCraft Image Trace
set -e
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BIN="$DIR/vectorcraft/target/release/vectorcraft-cli"
if [ ! -f "$BIN" ]; then
    BIN="$HOME/Documents/Developer/vectorcraft/target/release/vectorcraft-cli"
fi

if [ ! -f "$BIN" ]; then
    echo "Error: vectorcraft-cli binary not found."
    echo "Please build it with: cd $DIR/vectorcraft && cargo build --release -p vectorcraft-cli"
    exit 1
fi

IN="${1:?Usage: trace.sh <input_image> [output_svg] [preset]}"
OUT="${2:-${IN%.*}.svg}"
PRESET="${3:-Flat Logo}"

"$BIN" run \
    --in "$IN" \
    --cmd select.all \
    --cmd imageTrace.makeAndExpand \
    --params "{\"preset\":\"$PRESET\",\"params\":{\"ignoreWhite\":true,\"method\":\"overlapping\",\"shapes\":true,\"snapCurvesToLines\":true}}" \
    --export "$OUT"
echo "Vectorized $IN -> $OUT"
