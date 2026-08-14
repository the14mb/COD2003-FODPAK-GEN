#!/usr/bin/env bash
# Build the Linux exporter payload inside an x86_64 Debian 12 container.
#
#   ./packaging/build_linux_in_docker.sh      # writes dist/linux-x64
#
# PyInstaller cannot cross-compile, so a Linux payload cannot be built on a Mac
# directly -- build_exporter.py refuses a mismatched --target. A Linux container
# is the way round that without a second machine. On Apple Silicon this is
# qemu-emulated and therefore slow.
#
# What it does NOT give you is a payload anyone has run. Prefer a real Linux
# host when one is available: the machine that builds it can also test it.
#
# Debian 12 on purpose: its glibc is 2.36, and a PyInstaller binary links the
# build machine's glibc and refuses to start on anything older. SteamOS 3.x is
# Arch-based and newer than that, so building here and running there is the
# safe direction. Building on Arch and running on Debian would not be.
set -euo pipefail

REPO="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
UID_GID="$(id -u):$(id -g)"

docker run --rm --platform linux/amd64 \
    -v "$REPO":/src -w /src \
    -e PIP_DISABLE_PIP_VERSION_CHECK=1 \
    python:3.12-bookworm bash -euc "
        echo '=== host ==='
        uname -m; ldd --version | head -1
        python -c 'import tkinter, sys; print(\"tkinter\", tkinter.TkVersion)'

        echo '=== deps ==='
        apt-get update -qq
        # xvfb so the payload selftest can actually open a Tk window: the
        # build refuses to emit a payload whose GUI cannot start, and a
        # headless container has no display for it to try.
        apt-get install -y -qq --no-install-recommends xvfb >/dev/null
        pip install -q pyinstaller numpy pillow certifi

        echo '=== build ==='
        # Xvfb directly rather than xvfb-run, which additionally needs xauth
        # and is not worth another package for a display nothing looks at.
        Xvfb :99 -screen 0 1280x800x24 >/dev/null 2>&1 &
        export DISPLAY=:99
        sleep 2
        python packaging/build_exporter.py \
            --output dist/linux-x64 --target linux-x64

        echo '=== ownership back to the user ==='
        chown -R ${UID_GID} dist Builds 2>/dev/null || true
    "
