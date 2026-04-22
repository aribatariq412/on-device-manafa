#!/bin/bash

# Script: batterystats_service.sh
# Description: Provides an interface to a service that manages Android battery statistics (batterystats).
# Author: Rui Rua
# Date: August 20, 2023

# Usage: sh batterystats_service.sh [command] [run_id]
# Commands:
#   install   Install the battery stats management service on the device
#   export    Export battery stats results from the device to local directory
#   init      Initialize the battery stats management service
#   start     Start tracking battery statistics
#   stop      Stop tracking battery statistics and save results
#   clean     Clean up battery stats files on the device and local directory

# Dependencies: adb, isOnDevice (from utils.sh), getCurrentTimestamp (from utils.sh), getBootTime (from utils.sh)

# Global Variables:
#   PREFIX            - Command prefix based on whether the script is run on a device or not
#   RESULTS_DIR       - Directory where battery stats files are stored on the device
#   BATTERY_START_TMP - Temp file used to store the battery level recorded at start

source ./utils.sh

CMD=$1
PREFIX=""
WK_DIR=""
RUN_ID=$2
IS_ON_DEVICE=$(isOnDevice)
RESULTS_DIR="/sdcard/manafa/results/batterystats"
BATTERY_START_TMP="$RESULTS_DIR/battery_start.tmp"
test -z $1 && CMD=start
test -z $2 && test $CMD=="stop" && RUN_ID=$(getCurrentTimestamp)
test $IS_ON_DEVICE == "0" && PREFIX="adb shell "

# Function: get_battery_level
# Description: pulls the current battery % from dumpsys battery, works whether running on-device or through adb
function get_battery_level(){
    if [[ "$IS_ON_DEVICE" == "0" ]]; then
        adb shell dumpsys battery | grep "  level:" | awk '{print $2}'
    else
        dumpsys battery | grep "  level:" | awk '{print $2}'
    fi
}

# Function: install
# Description: Installs the battery stats management service on the device.
function install(){
    $PREFIX mkdir -p $RESULTS_DIR
}

# Function: export_results
# Description: Exports battery stats results from the device to the local directory.
function export_results(){
    if [[ "$IS_ON_DEVICE" != "0" ]]; then
        echo "Unable to export results from device."
        exit 3
    fi
    $PREFIX find $RESULTS_DIR -type f | xargs -I {} adb pull "{}" ../results/batterystats
}

# Function: init
# Description: Initializes the battery stats management service.
function init(){
    echo ""
}

# Function: start
# Description: Resets battery statistics and saves the starting battery level for drain tracking.
function start(){
    $PREFIX dumpsys batterystats --reset
    local level
    level=$(get_battery_level)
    if [[ -n "$level" ]]; then
        if [[ "$IS_ON_DEVICE" == "0" ]]; then
            adb shell "echo $level > $BATTERY_START_TMP"
        else
            echo "$level" > "$BATTERY_START_TMP"
        fi
        echo "[battery] start level: ${level}%"
    fi
}

# Function: stop
# Description: Saves battery statistics and computes battery drain percentage since start.
function stop(){
    local boot_time
    boot_time=$(getBootTime)
    filename="bstats-$RUN_ID-${boot_time}.log"
    echo "Batterystats filename: $filename"
    if [[ "$IS_ON_DEVICE" == "0" ]]; then
        $PREFIX "dumpsys batterystats --history > $RESULTS_DIR/$filename"
    else
        $PREFIX dumpsys batterystats --history > "$RESULTS_DIR/$filename"
    fi

    local end_level
    local start_level
    end_level=$(get_battery_level)
    if [[ "$IS_ON_DEVICE" == "0" ]]; then
        start_level=$(adb shell "cat $BATTERY_START_TMP 2>/dev/null" | tr -d '[:space:]')
    else
        start_level=$(cat "$BATTERY_START_TMP" 2>/dev/null | tr -d '[:space:]')
    fi

    if [[ -n "$start_level" && -n "$end_level" ]]; then
        local drain=$(( start_level - end_level ))
        local drain_file="bstats-drain-$RUN_ID-${boot_time}.log"
        if [[ "$IS_ON_DEVICE" == "0" ]]; then
            adb shell "printf 'battery_start=%s\nbattery_end=%s\nbattery_drain=%s%%\n' '$start_level' '$end_level' '$drain' > $RESULTS_DIR/$drain_file"
        else
            printf "battery_start=%s\nbattery_end=%s\nbattery_drain=%s%%\n" "$start_level" "$end_level" "$drain" > "$RESULTS_DIR/$drain_file"
        fi
        echo "[battery] drain: ${drain}% (${start_level}% -> ${end_level}%)"
    fi
}

# Function: clean
# Description: Cleans up battery stats files on the device and local directory.
function clean(){
    $PREFIX find $RESULTS_DIR  -type f | xargs -I {} rm "{}" 2> /dev/null
}

$CMD
