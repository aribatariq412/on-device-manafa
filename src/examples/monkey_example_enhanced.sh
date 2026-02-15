#!/bin/bash

# Usage (on the device): nohup sh monkey_example_enhanced.sh &
#
# This example demonstrates the enhanced profiling modes:
#   legacy  - CPU frequency tracing (original behavior)
#   energy  - Power rails + battery counters (Android 10+)
#   memory  - System memory statistics
#   both    - Combined energy + memory profiling

PROFILE_MODE=${1:-energy}

cd ..
sh manafa.sh install "" "$PROFILE_MODE"
sh manafa.sh init
sh manafa.sh start "" "$PROFILE_MODE"
monkey -p com.android.chrome -v 1000 --throttle 100 --ignore-crashes --ignore-security-exceptions 2> /dev/null
sh manafa.sh stop "" "$PROFILE_MODE"
