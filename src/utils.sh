#!/bin/bash

# Script: utils.sh
# Description: Contains utility functions used by other scripts.
# Author: Rui Rua
# Date: August 20, 2023

# Function: isOnDevice
# Description: Checks if the script is being run on an Android device via adb shell.
# Returns:
#   0 if the script is on a device, 1 if not
function isOnDevice(){
    if [[ -n "$IS_ON_DEVICE" ]]; then
        echo "$IS_ON_DEVICE"
        return
    fi
    # On Android (inside adb shell), /system/build.prop exists
    if [[ -f "/system/build.prop" ]]; then
        echo 1
    else
        # On macOS/Linux host, assume device is connected via adb
        echo 0
    fi
}

# Function: getBootTime
# Description: Retrieves the system boot time from /proc/stat.
# Returns:
#   System boot time in seconds
function getBootTime(){
    prefix=""
    test $(isOnDevice) == "0" && prefix="adb shell" 
    echo $($prefix cat /proc/stat | grep btime | awk '{print $2}')
}

# Function: getCurrentTimestamp
# Description: Retrieves the current timestamp in seconds.
# Returns:
#   Current timestamp in seconds
function getCurrentTimestamp(){ 
    echo $(date +%s)
}
