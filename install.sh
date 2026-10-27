#!/usr/bin/env bash
# Codex 模型切换器 —— 一键安装（macOS / Linux）
#
#   bash install.sh            # 不需要可执行位，最稳
#   ./install.sh               # 需要可执行位（git 里已标 100755）
#
# 做四件事：
#   1. 检查 Python 3.11+（要 tomllib）
#   2. 装 mcp 依赖
#   3. 把本仓库注册成 Codex 本地 marketplace 并安装插件
#   4. 跑 doctor.py --fix 自动体检 + 修环境
#
# ⚠️ 本脚本**不需要**可执行位，用 `bash install.sh` 调最保险
#    （从 zip 下载解压出来的文件常常没有可执行位）。
set -uo pipefail
REPO="$(cd "$(dirname "$0")" && pwd)"

echo "==> model-switcher 安装"
echo "    仓库：$REPO"

# 1) Python 3.11+
PY=""
for cand in python3 python; do
  if command -v "$cand" >/dev/null 2>&1 && "$cand" -c "import tomllib" >/dev/null 2>&1; then
    PY="$cand"; break
  fi
done
if [ -z "$PY" ]; then
  echo "!! 找不到 Python 3.11+（需要 tomllib）。请先安装 Python 3.11 或更高版本。" >&2
  exit 1
fi
echo "    Python：$($PY -V 2>&1)"

# 2) 依赖 mcp（装不上不要中断，后面 doctor 会再报）
if ! "$PY" -c "import mcp" >/dev/null 2>&1; then
  echo "==> 安装 mcp 依赖"
  if ! "$PY" -m pip install --quiet mcp 2>/dev/null; then
    if ! "$PY" -m pip install --quiet --user mcp 2>/dev/null; then
      if ! "$PY" -m pip install --quiet --break-system-packages mcp 2>/dev/null; then
        echo "!! pip 装不上 mcp。请手动处理后重跑 doctor.py --fix：" >&2
        echo "   $PY -m pip install mcp" >&2
        echo "   （若是 externally-managed-environment 报错，可先建虚拟环境）" >&2
      fi
    fi
  fi
fi

# 3) 注册 marketplace + 安装插件
CODEX_BIN="${CODEX_BIN:-codex}"
if ! command -v "$CODEX_BIN" >/dev/null 2>&1; then
  # Git Bash / WSL 等场景 codex 不在 PATH，试着从常见安装位置找
  for cand in "$HOME/AppData/Local/OpenAI/Codex/bin"/*/codex.exe \
              "$HOME/.local/share/codex/bin/codex" \
              "/Applications/Codex.app/Contents/Resources/codex"; do
    if [ -x "$cand" ]; then CODEX_BIN="$cand"; break; fi
  done
fi
if command -v "$CODEX_BIN" >/dev/null 2>&1 || [ -x "$CODEX_BIN" ]; then
  echo "==> 注册 marketplace"
  if ! "$CODEX_BIN" plugin marketplace add "$REPO" 2>&1; then
    echo "!! 注册失败。若提示网络/代理错误（如 Failed to connect to github.com over proxy），" >&2
    echo "   说明这台机器的 git 代理配了但代理没在跑，或直连 GitHub 被重置。" >&2
    echo "   排查：git config --global --get http.proxy" >&2
  fi
  echo "==> 安装插件"
  if ! "$CODEX_BIN" plugin add model-switcher@codex-model-switcher 2>&1; then
    echo "!! 安装插件失败，可手动执行：" >&2
    echo "   $CODEX_BIN plugin marketplace add \"$REPO\"" >&2
    echo "   $CODEX_BIN plugin add model-switcher@codex-model-switcher" >&2
  fi
else
  echo "!! 找不到 codex 命令。请手动执行："
  echo "   codex plugin marketplace add \"$REPO\""
  echo "   codex plugin add model-switcher@codex-model-switcher"
fi

# 4) 体检 + 自动修
echo "==> 环境体检"
"$PY" "$REPO/doctor.py" --fix || true

cat <<'EOF'

安装完成。

下一步：
  1. 完全退出并重开 Codex 桌面版
  2. 接入密钥**别贴在对话里**：在厂商控制台复制 key，然后对 agent 说
     「从剪贴板更新 DEEPSEEK_API_KEY」——插件会从剪贴板读走并立刻清空
  3. 接入后建议再跑一次 doctor.py 确认

EOF
