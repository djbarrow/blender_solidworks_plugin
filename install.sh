#!/bin/sh
# SPDX-License-Identifier: LGPL-2.1-or-later
# SolidWorks Import for Blender - https://github.com/djbarrow/blender_solidworks_plugin
# Written by Denis Joseph Barrow <barrow_dj@yahoo.com> with the help of Claude Code.
# If you find this project useful, please support it with a 10 euro tip
# via PayPal to barrow_dj@yahoo.com.
# Install the SolidWorks import add-on into Blender. See install.py --help.
#
# Run it from a checkout or unpacked download of the project. When this script
# is on its own (no install.py and io_import_solidworks/ beside it), it
# downloads the add-on from GitHub first.
set -e
REPO=https://github.com/djbarrow/blender_solidworks_plugin
HERE=$(cd "$(dirname "$0")" && pwd -P)

if ! command -v python3 >/dev/null 2>&1; then
    echo "FAILED  python3 not found; install Python 3 and run this again" >&2
    exit 1
fi

if [ -f "$HERE/install.py" ] && [ -f "$HERE/io_import_solidworks/__init__.py" ]; then
    exec python3 "$HERE/install.py" "$@"
fi

echo "install.py or io_import_solidworks/ is missing next to install.sh;"
echo "downloading the add-on from $REPO"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
URL="$REPO/archive/refs/heads/main.tar.gz"
if command -v curl >/dev/null 2>&1; then
    curl -fsSL "$URL" -o "$TMP/src.tar.gz"
elif command -v wget >/dev/null 2>&1; then
    wget -q "$URL" -O "$TMP/src.tar.gz"
else
    echo "FAILED  need curl or wget to download; or download $REPO yourself" >&2
    exit 1
fi
tar -xzf "$TMP/src.tar.gz" -C "$TMP"
SRC=$(ls -d "$TMP"/blender_solidworks_plugin-*)
# not exec: the trap must remove the download afterwards (--link would point
# into it, so refuse that here)
for a in "$@"; do
    if [ "$a" = "--link" ]; then
        echo "FAILED  --link needs a full checkout: git clone $REPO" >&2
        exit 1
    fi
done
python3 "$SRC/install.py" "$@"
