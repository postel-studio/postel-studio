#!/usr/bin/env bash
# 统一质量门禁。本地与 CI 跑同一份，不允许分叉。
# 用法：bash scripts/check.sh        （全部）
#       bash scripts/check.sh lint   （只跑某项：lint|format|types|test|secrets）
set -uo pipefail

FAILED=0
run() {
  local name="$1"; shift
  printf '\n=== %s ===\n' "$name"
  if "$@"; then
    printf '✅ %s\n' "$name"
  else
    printf '❌ %s\n' "$name"
    FAILED=1
  fi
}

gate_lint()   { uv run ruff check .; }
gate_format() { uv run ruff format --check .; }
gate_types()  { uv run mypy; }
gate_test()   { uv run pytest --cov --cov-report=term-missing; }
gate_secrets() {
  # 只拦真实凭据形态，不拦文档里描述扫描命令的文字
  local hits
  hits=$(git diff --cached -U0 2>/dev/null | grep -oE '(ghp_[A-Za-z0-9]{30,}|pypi-[A-Za-z0-9_-]{40,}|AKIA[0-9A-Z]{16})' || true)
  if [ -n "$hits" ]; then
    echo "发现疑似凭据（已脱敏）："
    echo "$hits" | sed -E 's/^(.{6}).*/\1…<REDACTED>/'
    return 1
  fi
  # 与 .env 中的真实值逐一比对，比正则更可靠
  if [ -f .env ]; then
    while IFS='=' read -r k v; do
      [ -n "${v:-}" ] || continue
      [ "${#v}" -gt 20 ] || continue
      if git diff --cached -U0 2>/dev/null | grep -qF -- "$v"; then
        echo "暂存内容包含 $k 的真实值"
        return 1
      fi
    done < .env
  fi
  echo "未发现凭据"
}

case "${1:-all}" in
  lint)   run "lint"   gate_lint ;;
  format) run "format" gate_format ;;
  types)  run "types"  gate_types ;;
  test)   run "test"   gate_test ;;
  secrets) run "secrets" gate_secrets ;;
  all)
    run "lint"   gate_lint
    run "format" gate_format
    run "types"  gate_types
    run "test"   gate_test
    run "secrets" gate_secrets
    ;;
  *) echo "未知目标：$1" >&2; exit 2 ;;
esac

if [ "$FAILED" -ne 0 ]; then
  printf '\n质量门禁未通过\n' >&2
  exit 1
fi
printf '\n质量门禁全部通过\n'
