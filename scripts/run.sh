#!/usr/bin/env bash
# 统一入口：从仓库根目录加载环境变量文件后再跑 CLI，避免把 key 明文写进命令行。
#
# 为什么不直接 `DEEPSEEK_API_KEY=sk-... python -m agent_system.cli`：
#   明文写在命令行里会留在 shell history、进程列表和 CI 日志中，等于泄露。
#
# 用法：
#   scripts/run.sh batch --config config.deepseek.json BV1Ltsf6xEpV
#   scripts/run.sh --config config.verify_new4.json batch BV1Ltsf6xEpV BV11Ssf6gEUV
#   scripts/run.sh audit
#
# 读取顺序：.env（核查开关 / WSA）→ .secrets.env（模型 key），缺失文件自动跳过。
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

for env_file in .env .secrets.env; do
  if [[ -f "$env_file" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "$env_file"
    set +a
  fi
done

exec python3 -m agent_system.cli "$@"
