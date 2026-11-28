#!/usr/bin/env bash
# 把仓库里受版本管理的钩子装进 .git/hooks（.git/hooks 本身不进 git，换台机器就得重装）。
# 用法：bash scripts/install-hooks.sh
set -euo pipefail
repo_root=$(git rev-parse --show-toplevel)
target="$repo_root/.git/hooks"
mkdir -p "$target"
for hook in "$repo_root"/scripts/hooks/*; do
  name=$(basename "$hook")
  install -m 755 "$hook" "$target/$name"
  echo "已安装 $name -> .git/hooks/$name"
done
