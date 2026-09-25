#!/usr/bin/env bash
# Build a standalone executable of animation.py (PyInstaller, single file).
#
# PyInstaller cannot cross-compile: the build is for the OS it runs on.
#   - Windows : run this script in Git Bash (or use build_windows.bat) -> dist/MRI_instructions.exe
#   - macOS   : -> dist/MRI_instructions.app
#
# Uses the conda env "anim" if it exists, otherwise a local venv (.venv_build).
set -euo pipefail
cd "$(dirname "$0")"

if command -v conda >/dev/null 2>&1 && conda env list | grep -q '^anim '; then
    echo ">> using conda env anim"
    PY=(conda run -n anim python)
else
    echo ">> using local venv .venv_build"
    PYTHON=${PYTHON:-$(command -v python3 || command -v python)}
    [ -d .venv_build ] || "$PYTHON" -m venv .venv_build
    if [ -f .venv_build/Scripts/python.exe ]; then PY=(.venv_build/Scripts/python.exe); else PY=(.venv_build/bin/python); fi
fi

"${PY[@]}" -m pip install -q -r requirements.txt
# macOS: folder mode (.app starts fast); Windows/Linux: single file
if [ "$(uname)" = "Darwin" ]; then MODE=--onedir; else MODE=--onefile; fi
"${PY[@]}" -m PyInstaller --noconfirm --clean $MODE --windowed \
    --name MRI_instructions --distpath dist --workpath build animation.py
cp config_example.json dist/
rm -f MRI_instructions.spec
[ "$(uname)" = "Darwin" ] && rm -rf dist/MRI_instructions      # keep only the .app

echo
echo ">> Done, files in dist/:"
ls -1 dist
