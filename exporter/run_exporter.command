#!/bin/sh
# Friends of Duty content exporter launcher (macOS double-clickable).
cd "$(dirname "$0")" || exit 1
exec python3 friends_of_duty_exporter.py "$@"
