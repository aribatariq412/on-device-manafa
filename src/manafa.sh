#!/bin/bash

# Script: manafa.sh
# Description: Provides an interface to manage the lifecycle of the inner services: battery stats, Perfetto, and log management.
#              Supports enhanced profiling modes: legacy, energy, memory, both.
# Author: Rui Rua (original), enhanced for capstone project
# Date: August 20, 2023 (original), February 2026 (enhanced)

# Usage: sh ./manafa.sh [command] [run_id] [profile_mode]
# Commands:
#   install       Install all managed services on the device
#   export        Export results from all managed services
#   init          Initialize all managed services
#   start         Start all managed services
#   stop          Stop all managed services
#   clean         Clean up all managed services
#   push          Push necessary files to the device
#   clean_local   Clean up local result files
#
# Profile Modes (optional, default: legacy):
#   legacy    CPU frequency tracing only (original behavior)
#   energy    Power rails + battery counters (requires Android 10+)
#   memory    System memory statistics
#   both      Combined energy + memory profiling

# Dependencies: adb, isOnDevice (from utils.sh), getCurrentTimestamp (from utils.sh), emanafa (external tool)

source ./utils.sh

batteryStatsService="sh ./batterystats_service.sh"
perfettoService="sh ./perfetto_service.sh"
logService="sh ./log_service.sh"

CMD=$1
RUN_ID=$2
PROFILE_MODE=$3
RESULTS_DIR="../results"
IS_ON_DEVICE=$(isOnDevice)
test -z $1 && CMD=start
test -z $2 && test "$1" == "stop" && RUN_ID=$(getCurrentTimestamp)
test -z "$3" && PROFILE_MODE="legacy"

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

# Function: start
# Description: Start all managed services with selected profile mode
function start(){
    echo "========================================"
    echo " on-device-manafa Profiling"
    echo "========================================"
    echo " Profile Mode: $PROFILE_MODE"
    echo "========================================"
    $perfettoService start "" "$PROFILE_MODE"
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
    # Push all resource files (configs) to device
    adb push ../resources /sdcard/manafa
}

# Function: clean_local
# Description: Clean up local result files
function clean_local(){
    find $RESULTS_DIR -type f | xargs rm
}

$CMD
