#!/usr/bin/env bash
# Builds Lense for the platform you run it on (Linux/macOS). The Windows .exe has to
# be built on Windows -- PyInstaller does not cross-compile -- see build_windows.bat.
set -euo pipefail

echo "=== Lense build ($(uname -s)) ==="
python3 -m pip install -r requirements.txt
rm -rf build dist
python3 -m PyInstaller --noconfirm Lense.spec
echo
cp "READ ME FIRST.txt" dist/
ls -lh dist/Lense
echo "Done: dist/Lense (quick-start sheet copied beside it)"
