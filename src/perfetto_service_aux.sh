#!/bin/bash
source ./utils.sh
config_file=$1
output_file=$2
test -z "$1" && config_file="/sdcard/manafa/resources/perfetto.config.bin"
test -z "$2" && output_file="/data/misc/perfetto-traces/trace"
prefix=""
test $(isOnDevice) == "0" && prefix="adb shell"

# Use --txt flag for text-based protobuf configs (.pbtxt), --config for binary
case "$config_file" in
    *.pbtxt)
        $prefix cat $config_file | $prefix perfetto --background --txt -o "$output_file" -c -
        ;;
    *)
        $prefix cat $config_file | $prefix perfetto --background -o "$output_file" --config -
        ;;
esac
