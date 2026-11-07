#!/bin/sh
# POSIX / Git Bash shim: ./openaxpp300s.sh status
cd "$(dirname "$0")" && exec python -X utf8 -m openaxpp300s "$@"
