#!/usr/bin/env bash
# Render architecture.dot to PNG and SVG. Requires Graphviz (`apt-get install graphviz`).
set -euo pipefail
cd "$(dirname "$0")"
dot -Tpng -Gdpi=110 architecture.dot -o architecture.png
dot -Tsvg architecture.dot -o architecture.svg
