#!/bin/sh
set -eu
cd "$(dirname "$0")"
./start.sh edge-install
printf '\n完成 Edge 中的安装操作后，可以关闭此窗口。\n'
