#!/usr/bin/env bash
# 代码地图保鲜门禁：src/ 或 tests/ 变了，docs/codemap/ 也必须变。
#
# 只回答一个问题：「这一刀动了代码，却没动代码地图，可以合并吗？」
# 不评判地图内容好坏——那是审核者的事。门禁只拦「明显没更新」。
#
# 用法：bash scripts/gate-codemap.sh                （相对 main 的全部改动：提交+暂存+未暂存）
#       bash scripts/gate-codemap.sh --staged       （只看暂存区，给 pre-commit 用）
#       bash scripts/gate-codemap.sh --base <ref>   （换基准分支）
#
# 覆盖范围是个坑：只比 `BASE...HEAD` 会漏掉还没提交的改动，而 pre-commit 时
# 改动恰好还在暂存区。所以默认把三者取并集，任何一处改了代码都算。
set -uo pipefail

MAP_DIR="docs/codemap"
CODE_PREFIXES=(src/ tests/)
BASE_REF="${CODEMAP_BASE_REF:-main}"
COMPARE_BASE=""

usage() {
  echo "用法：bash scripts/gate-codemap.sh [--staged] [--base <ref>]" >&2
  exit 2
}

MODE="worktree"
while [ $# -gt 0 ]; do
  case "$1" in
    --staged) MODE="staged"; shift ;;
    --base)
      [ $# -ge 2 ] || usage
      COMPARE_BASE="$2"
      shift 2
      ;;
    *) usage ;;
  esac
done

# git 不可用时（导出 tarball、CI 浅克隆）跳过，而不是误报失败
if ! git rev-parse --git-dir >/dev/null 2>&1; then
  echo "⚠️  不在 git 仓库内，代码地图门禁跳过"
  exit 0
fi

changed=""
add() { changed="$changed$1"$'\n'; }

if [ "$MODE" = "staged" ]; then
  while IFS= read -r f; do [ -n "$f" ] && add "$f"; done < <(git diff --cached --name-only)
else
  # CI 的 pull_request 事件里 origin/HEAD 指向 main，优先用它
  base=""
  for candidate in "${COMPARE_BASE:-$BASE_REF}" origin/main main origin/HEAD; do
    [ -n "$candidate" ] || continue
    if git rev-parse --verify --quiet "$candidate" >/dev/null; then
      base="$candidate"
      break
    fi
  done
  if [ -n "$base" ]; then
    while IFS= read -r f; do [ -n "$f" ] && add "$f"; done < <(git diff --name-only "$base"...HEAD)
  else
    echo "⚠️  找不到基准分支，只比对未提交改动（建议 --base <ref>）"
  fi
  # 未提交的两段也必须算进来：这是「改完了还没 commit」最常见的时刻
  while IFS= read -r f; do [ -n "$f" ] && add "$f"; done < <(git diff --cached --name-only)
  while IFS= read -r f; do [ -n "$f" ] && add "$f"; done < <(git diff --name-only)
fi

code_hits=""
map_hits=""
while IFS= read -r f; do
  [ -n "$f" ] || continue
  for p in "${CODE_PREFIXES[@]}"; do
    case "$f" in
      "$p"*) code_hits="$code_hits  $f"$'\n'; continue 2 ;;
    esac
  done
  case "$f" in
    "$MAP_DIR"/*) map_hits="$map_hits  $f"$'\n' ;;
  esac
done <<<"$changed"

if [ -n "$code_hits" ] && [ -z "$map_hits" ]; then
  echo "❌ 改了代码却没更新代码地图"
  printf '代码改动：\n%s' "$code_hits"
  cat >&2 <<'EOF'

docs/codemap/ 是这份代码的自上而下索引。代码变了而地图不变，地图就开始撒谎，
比没有地图更危险。逐条对照 codemap §维护规则：

  - 新增模块         → 新增 docs/codemap/03-modules/<name>.md 并挂进 README 的树
  - 新增/删除公开符号 → 更新该模块页的符号表（行号）
  - 改跨模块调用      → 更新 02-layers.md 的允许边表
  - 改安全相关行为    → 逐条过 05-change-impact.md 的安全红线
  - 修掉/遇到陷阱     → 06-pitfalls.md 记一条（修掉的标「已修」，不要删）

确有正当理由不更新时（例如纯格式化、纯注释错字），在 PR 描述里写一行
「codemap: N/A - <原因>」。这是给人看的，本门禁不做机器豁免。

规则出处：docs/codemap/README.md §维护规则，CONTRIBUTING.md §代码地图
EOF
  exit 1
fi

if [ -n "$code_hits" ]; then
  echo "✅ 代码与代码地图同时改动"
else
  echo "✅ 未改代码，无需动代码地图"
fi
exit 0