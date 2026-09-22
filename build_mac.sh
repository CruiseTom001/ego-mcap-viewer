#!/bin/bash
# Build the macOS app bundle: release/MCAP视频查看器.app
#
# Run ON macOS (PyInstaller cannot cross-compile).  Expects the runtime from
# bootstrap.sh to exist (./runtime or ~/Library/Application Support/MCAPViewer/runtime).
#
# Usage:  ./build_mac.sh

set -e
cd "$(dirname "$0")"

if [ -x ./runtime/bin/python ]; then
    PY=./runtime/bin/python
else
    PY="$HOME/Library/Application Support/MCAPViewer/runtime/bin/python"
fi
if [ ! -x "$PY" ]; then
    echo "runtime not found - run ./bootstrap.sh --check-only first" >&2
    exit 1
fi

echo "== installing PyInstaller (build-only dependency) =="
"$PY" -m pip install --disable-pip-version-check --only-binary=:all: -r requirements-build.txt

echo "== building .app (ASCII name first, renamed afterwards) =="
"$PY" -m PyInstaller \
    --noconfirm --clean --windowed --noupx \
    --name MCAPVIEWER \
    --osx-bundle-identifier cn.mcapviewer.desktop \
    --distpath release --workpath build/pyinstaller --specpath build \
    --collect-all mcap --collect-all lz4 --collect-all zstandard \
    --hidden-import PySide6.QtMultimedia \
    --exclude-module tkinter \
    desktop.py

APP="release/MCAP视频查看器.app"
rm -rf "$APP"
mv release/MCAPVIEWER.app "$APP"

echo "== zipping =="
STAMP="$(date +%Y%m%d)"
ZIP="../MCAP视频查看器-macOS-$STAMP.zip"
rm -f "$ZIP"
ditto -c -k --sequesterRsrc --keepParent "$APP" "$ZIP"

echo ""
echo "app : $APP"
echo "zip : $ZIP"
shasum -a 256 "$ZIP" | awk '{print "zip sha256:", $1}'
find "$APP/Contents/MacOS" -type f -maxdepth 1 -exec shasum -a 256 {} \; | head -3
