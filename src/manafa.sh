#!/bin/bash

# Script: manafa.sh
# Description: Provides an interface to manage the lifecycle of the inner services: battery stats, Perfetto, and log management.
#              Supports enhanced profiling modes: legacy, energy, memory, both, method.
# Author: Rui Rua
# Date: August 20, 2023

# Usage: sh ./manafa.sh [command] [run_id] [profile_mode] [duration_ms]
# Commands:
#   install       Install all managed services on the device
#   export        Export results from all managed services
#   init          Initialize all managed services
#   start         Start all managed services
#   stop          Stop all managed services
#   clean         Clean up all managed services
#   push          Push necessary files to the device
#   clean_local   Clean up local result files
#   query         Query Perfetto for available data sources on this device
#
# Profile Modes (optional, default: legacy):
#   legacy    CPU frequency tracing only (original behavior)
#   energy    Power rails + battery counters (requires Android 10+, real device)
#   memory    System memory statistics
#   both      Combined energy + memory profiling
#   method    CPU callstack sampling + scheduling events (per-function profiling)
#
# Duration (optional, default: 30000ms):
#   duration_ms   Profiling duration in milliseconds (e.g. 60000 for 60 seconds)

# Dependencies: adb, isOnDevice (from utils.sh), getCurrentTimestamp (from utils.sh), emanafa (external tool)

source ./utils.sh

batteryStatsService="sh ./batterystats_service.sh"
perfettoService="sh ./perfetto_service.sh"
logService="sh ./log_service.sh"

CMD=$1
RESULTS_DIR="../results"
export IS_ON_DEVICE=$(isOnDevice)
test -z "$1" && CMD=start
test "$CMD" == "export" && CMD=export_results

if [[ "$CMD" == "start" ]]; then
    PROFILE_MODE=${2:-legacy}
    DURATION_MS=${3:-30000}
    RUN_ID=""
elif [[ "$CMD" == "stop" ]]; then
    RUN_ID=$(getCurrentTimestamp)
    PROFILE_MODE=${2:-legacy}
    DURATION_MS=30000
else
    RUN_ID=$2
    PROFILE_MODE=${3:-legacy}
    DURATION_MS=${4:-30000}
fi

# Function: export_results
# Description: Export results from all managed services
function export_results(){
    $perfettoService export_results
    $batteryStatsService export_results
    $logService export_results
}

# Function: analyze
# Description: Analyze exported results using emanafa python tool
function analyze(){
    emanafa -d $RESULTS_DIR
}

# Function: install
# Description: Install all managed services on the device
function install(){
    if [[ "$IS_ON_DEVICE" == "0" ]]; then
        push
    fi
    $perfettoService install "" "$PROFILE_MODE"
    $batteryStatsService install
    $logService install
}

# Function: init
# Description: Initialize all managed services
function init(){
    $perfettoService init
    $batteryStatsService init
    $logService init
}

# Function: query
# Description: forwards to perfetto_service to list what data sources are available on this device
function query(){
    $perfettoService query
}

# Function: start
# Description: Start all managed services with selected profile mode
function start(){
    echo "========================================"
    echo " on-device-manafa Profiling"
    echo "========================================"
    echo " Profile Mode: $PROFILE_MODE"
    echo " Duration: ${DURATION_MS}ms"
    echo "========================================"
    $perfettoService start "" "$PROFILE_MODE" "$DURATION_MS"
    $batteryStatsService start
    $logService start
}

# Function: stop
# Description: Stop all managed services
function stop(){
    $perfettoService stop "$RUN_ID" "$PROFILE_MODE"
    $batteryStatsService stop "$RUN_ID"
    $logService stop "$RUN_ID"
}

# Function: clean
# Description: Clean up all managed services
function clean(){
    $perfettoService clean
    $batteryStatsService clean
    $logService clean
}

# Function: push
# Description: Push necessary files to the device
function push(){
    adb shell mkdir -p /sdcard/manafa/results
    adb push ../src /sdcard/manafa
    adb push ../resources /sdcard/manafa
}

# Function: clean_local
# Description: Clean up local result files
function clean_local(){
    find $RESULTS_DIR -type f | xargs rm
}

$CMD
