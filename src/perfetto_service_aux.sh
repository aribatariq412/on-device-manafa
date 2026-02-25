#!/bin/bash
source ./utils.sh
config_file=$1
output_file=$2
duration_ms=$3
test -z "$1" && config_file="/sdcard/manafa/resources/perfetto.config.bin"
test -z "$2" && output_file="/data/misc/perfetto-traces/trace"
test -z "$3" && duration_ms=30000
prefix=""
test $(isOnDevice) == "0" && prefix="adb shell"

# Use --txt flag for text-based protobuf configs (.pbtxt), --config for binary
# For .pbtxt configs, substitute duration_ms on the device before passing to perfetto
case "$config_file" in
    *.pbtxt)
        if [[ -n "$prefix" ]]; then
            # from host: run sed + perfetto in one adb shell session so pipe is on-device
            $prefix "sed 's/duration_ms: [0-9]*/duration_ms: ${duration_ms}/' ${config_file} | perfetto --background --txt -o '${output_file}' -c -"
        else
            # on device: direct pipe
            sed 's/duration_ms: [0-9]*/duration_ms: '"${duration_ms}"'/' $config_file | perfetto --background --txt -o "$output_file" -c -
        fi
        ;;
    *)
        $prefix cat $config_file | $prefix perfetto --background -o "$output_file" --config -
        ;;
esac
