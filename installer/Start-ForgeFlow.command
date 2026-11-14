#!/bin/bash
cd -- "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
./start.sh "$@"
result=$?
if [ "$result" -ne 0 ]; then read -r -p 'Startup failed. Press Enter to close. ' _; fi
exit "$result"
