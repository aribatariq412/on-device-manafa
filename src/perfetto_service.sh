#!/bin/bash

# Script: perfetto_service.sh
# Description: Provides an interface to a service that manages Perfetto traces.
#              Supports multiple profiling modes: legacy, energy, memory, both, method.
# Author: Rui Rua
# Date: August 20, 2023

# Usage: sh perfetto_service.sh [command] [run_id] [profile_mode] [duration_ms]
# Commands:
#   install   Install the Perfetto management service on the device
#   export    Export Perfetto trace results from the device to local directory
#   init      Initialize the Perfetto management service
#   start     Start capturing a Perfetto trace
#   stop      Stop capturing the Perfetto trace and save results
#   clean     Clean up Perfetto trace files on the device and local directory
#   query     Query Perfetto for all available data sources on this device
#
# Profile Modes (optional, default: legacy):
#   legacy    CPU frequency tracing only (original behavior, binary config)
#   energy    Power rails + battery counters (requires Android 10+, real device)
#   memory    System memory statistics (meminfo counters)
#   both      Combined energy + memory profiling
#   method    CPU callstack sampling + scheduling events (per-function profiling)

# Dependencies: adb, isOnDevice (from utils.sh), getCurrentTimestamp (from utils.sh), getBootTime (from utils.sh)

source ./utils.sh

CMD=$1
RUN_ID=$2
PROFILE_MODE=$3
DURATION_MS=$4
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
CONFIG_FILE_METHOD="$RESOURCES_DIR/perfetto_config_method_tracing.pbtxt"

test -z $1 && CMD=start
test -z $2 && test "$CMD" == "stop" && RUN_ID=$(getCurrentTimestamp)
test -z "$3" && PROFILE_MODE="legacy"
test -z "$4" && DURATION_MS=30000
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
        method)
            echo "$CONFIG_FILE_METHOD"
            ;;
        legacy|*)
            echo "$CONFIG_FILE_LEGACY"
            ;;
    esac
}

# helper that runs perfetto --query on the device and returns the raw text output
function query_perfetto_raw(){
    if [[ "$IS_ON_DEVICE" == "0" ]]; then
        adb shell "perfetto --query 2>/dev/null"
    else
        perfetto --query 2>/dev/null
    fi
}

# queries the perfetto daemon and prints all registered data sources - useful for seeing what's available before picking a mode
function query(){
    echo "querying perfetto for available data sources..."
    local raw
    raw=$(query_perfetto_raw)
    if [[ -z "$raw" ]]; then
        echo "[WARNING] no output from perfetto --query (is traced running? run 'init' first)"
        return 1
    fi
    echo "available data sources:"
    echo "$raw" | awk '/DATA SOURCES REGISTERED:/{found=1; next} /TRACING SESSIONS:/{found=0} found && /^[a-z]/{print "  " $1}' | sort -u
}

# Function: check_power_rails
# Description: Returns 1 if android.power data source is registered with perfetto, 0 if not. Falls back to emulator detection if traced isn't running yet.
function check_power_rails(){
    local raw
    raw=$(query_perfetto_raw)
    if [[ -n "$raw" ]]; then
        if echo "$raw" | grep -q 'android\.power'; then
            echo "1"
        else
            echo "0"
        fi
        return
    fi
    # fallback to checking build props if perfetto query gave nothing
    local characteristics=$($PREFIX getprop ro.build.characteristics 2>/dev/null)
    if echo "$characteristics" | grep -qi "emulator"; then
        echo "0"
    else
        echo "1"
    fi
}

# checks if linux.perf is registered as a data source, which is needed for callstack sampling in method mode
function check_method_tracing(){
    local raw
    raw=$(query_perfetto_raw)
    if [[ -z "$raw" ]]; then
        echo "1"
        return
    fi
    if echo "$raw" | grep -q 'linux\.perf'; then
        echo "1"
    else
        echo "0"
    fi
}

function install(){
    $PREFIX mkdir -p $RESULTS_DIR
    if [[ "$IS_ON_DEVICE" == "0" ]]; then
        # Push all config files to device
        adb push "../resources/perfetto.config.bin" "$CONFIG_FILE_LEGACY"
        adb push "../resources/perfetto_config_power_rails.pbtxt" "$CONFIG_FILE_ENERGY" 2>/dev/null
        adb push "../resources/perfetto_config_memory_only.pbtxt" "$CONFIG_FILE_MEMORY" 2>/dev/null
        adb push "../resources/perfetto_config_both.pbtxt" "$CONFIG_FILE_BOTH" 2>/dev/null
        adb push "../resources/perfetto_config_method_tracing.pbtxt" "$CONFIG_FILE_METHOD" 2>/dev/null
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
    if [[ "$PROFILE_MODE" == "energy" || "$PROFILE_MODE" == "both" ]]; then
        if [[ "$(check_power_rails)" == "0" ]]; then
            echo "[WARNING] power rails not supported on this device"
            if [[ "$PROFILE_MODE" == "both" ]]; then
                echo "[WARNING] falling back to memory mode"
                PROFILE_MODE="memory"
            else
                echo "[WARNING] falling back to legacy mode"
                PROFILE_MODE="legacy"
            fi
        fi
    fi
    if [[ "$PROFILE_MODE" == "method" ]]; then
        if [[ "$(check_method_tracing)" == "0" ]]; then
            echo "[WARNING] linux.perf not available on this device, falling back to legacy mode"
            PROFILE_MODE="legacy"
        fi
    fi
    local config_file=$(get_config_file)
    echo "Starting Perfetto with profile mode: $PROFILE_MODE"
    echo "Config: $config_file"
    sh ./perfetto_service_aux.sh "$config_file" "$DEFAULT_OUTPUT_FILE" "$DURATION_MS"
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
