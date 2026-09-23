#!/bin/sh
set -eu
cd "$(dirname "$0")"
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH"
if ! command -v uv >/dev/null 2>&1; then
  echo '请先安装 uv：https://docs.astral.sh/uv/getting-started/installation/'
  exit 1
fi
uv sync --locked --extra local --python 3.12
echo 'AutoGrab 轻量监控已安装。原有配置、基线和邮件设置已保留。'
echo '运行 ./start.sh bandwagon；查看详细结果加 --json。'
echo 'Apple 先运行 ./start.sh apple-configure。Edge 助手仍为可选组件。'
echo 'LIVE OFF / ARM OFF。安装过程不会查询商家或发送邮件。'
