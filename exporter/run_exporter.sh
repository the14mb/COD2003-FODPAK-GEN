#!/bin/sh
# Friends of Duty content exporter launcher (Linux).
cd "$(dirname "$0")" || exit 1
exec python3 friends_of_duty_exporter.py "$@"
