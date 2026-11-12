#!/bin/sh
# Build one complete variant and its single-file suite, then run it.
set -eu
BUILD_DIR="${1:-build}"
python3 build.py doctor
python3 build.py dist --model 303 --programs main --output "$BUILD_DIR/dist/jorek-suite"
"$BUILD_DIR/dist/jorek-suite" --info
"$BUILD_DIR/dist/jorek-suite" --model 303 --print-command
