#!/bin/bash

# Script: perfetto_service.sh
# Description: Provides an interface to a service that manages Perfetto traces.
#              Supports multiple profiling modes: legacy, energy, memory, both.
# Author: Rui Rua (original), enhanced for capstone project
# Date: August 20, 2023 (original), February 2026 (enhanced)

# Usage: sh perfetto_service.sh [command] [run_id] [profile_mode]
# Commands:
#   install   Install the Perfetto management service on the device
#   export    Export Perfetto trace results from the device to local directory
#   init      Initialize the Perfetto management service
#   start     Start capturing a Perfetto trace
#   stop      Stop capturing the Perfetto trace and save results
#   clean     Clean up Perfetto trace files on the device and local directory
#
# Profile Modes (optional, default: legacy):
#   legacy    CPU frequency tracing only (original behavior, binary config)
#   energy    Power rails + battery counters (requires Android 10+, modern device)
#   memory    System memory statistics (meminfo counters)
#   both      Combined energy + memory profiling

# Dependencies: adb, isOnDevice (from utils.sh), getCurrentTimestamp (from utils.sh), getBootTime (from utils.sh)

source ./utils.sh

CMD=$1
RUN_ID=$2
PROFILE_MODE=$3
PREFIX=""
WK_DIR=""
IS_ON_DEVICE=$(isOnDevice)
RESULTS_DIR="/sdcard/manafa/results/perfetto"
DEFAULT_OUT_DIR="/data/misc/perfetto-traces"
DEFAULT_OUTPUT_FILE="$DEFAULT_OUT_DIR/trace"
RESOURCES_DIR="/sdcard/manafa/resources"

# Config file selection based on profile mode
CONFIG_FILE_LEGACY="$RESOURCES_DIR/perfetto.config.bin"
CONFIG_FILE_ENERGY="$RESOURCES_DIR/perfetto_config_power_rails.pbtxt"
CONFIG_FILE_MEMORY="$RESOURCES_DIR/perfetto_config_memory_only.pbtxt"
CONFIG_FILE_BOTH="$RESOURCES_DIR/perfetto_config_both.pbtxt"

test -z $1 && CMD=start
test -z $2 && test "$CMD" == "stop" && RUN_ID=$(getCurrentTimestamp)
test -z "$3" && PROFILE_MODE="legacy"
test $IS_ON_DEVICE == "0" && PREFIX="adb shell "

# Function: get_config_file
# Description: Returns the config file path based on selected profile mode
function get_config_file(){
    case "$PROFILE_MODE" in
        energy)
            echo "$CONFIG_FILE_ENERGY"
            ;;
        memory)
            echo "$CONFIG_FILE_MEMORY"
            ;;
        both)
            echo "$CONFIG_FILE_BOTH"
            ;;
        legacy|*)
            echo "$CONFIG_FILE_LEGACY"
            ;;
    esac
}

function install(){
    $PREFIX mkdir -p $RESULTS_DIR
    if [[ "$IS_ON_DEVICE" == "0" ]]; then
        # Push all config files to device
        adb push "../resources/perfetto.config.bin" "$CONFIG_FILE_LEGACY"
        adb push "../resources/perfetto_config_power_rails.pbtxt" "$CONFIG_FILE_ENERGY" 2>/dev/null
        adb push "../resources/perfetto_config_memory_only.pbtxt" "$CONFIG_FILE_MEMORY" 2>/dev/null
        adb push "../resources/perfetto_config_both.pbtxt" "$CONFIG_FILE_BOTH" 2>/dev/null
    fi
}

function export_results(){
    if [[ "$IS_ON_DEVICE" != "0" ]]; then
        echo "Unable to export results from device."
        exit 3
    fi
    $PREFIX find $RESULTS_DIR -type f | xargs -I {} adb pull "{}" ../results/perfetto
}

function init(){
    $PREFIX setprop persist.traced.enable 1
}

function start(){
    local config_file=$(get_config_file)
    echo "Starting Perfetto with profile mode: $PROFILE_MODE"
    echo "Config: $config_file"
    sh ./perfetto_service_aux.sh "$config_file" "$DEFAULT_OUTPUT_FILE"
}

function stop(){
    $PREFIX killall perfetto
    sleep 1
    # Use .perfetto-trace extension for enhanced modes to preserve counter data
    if [[ "$PROFILE_MODE" != "legacy" ]]; then
        filename="trace-$RUN_ID-$(getBootTime).perfetto-trace"
    else
        filename="trace-$RUN_ID-$(getBootTime)"
    fi
    echo "Trace filename: $filename"
    $PREFIX cp $DEFAULT_OUTPUT_FILE $RESULTS_DIR/$filename
}

function clean(){
    $PREFIX find $RESULTS_DIR  -type f | xargs -I {} rm "{}" 2> /dev/null
    $PREFIX killall perfetto
}

$CMD
