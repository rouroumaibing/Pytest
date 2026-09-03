#!/usr/bin/env bash
#
# clean.sh - 删除本项目所有可再生的临时产物（缓存 / 测试产物 / 构建元数据）
#
# 安全边界：
#   - 只匹配明确的临时模式（__pycache__、.pytest_cache、.pytest_tmp、
#     *.egg-info、*.pyc、.coverage、构建/类型检查缓存等）
#   - 显式排除 .git/ 与 .workbuddy/，绝不触碰源码与项目数据
#   - 所有被删项均可随时重建（重新 import / 跑 pytest / pip install -e .）
#
# 用法：
#   ./scripts/clean.sh            # 删除临时产物
#   ./scripts/clean.sh --dry      # 只列出将要删除的内容，不真删
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

DRY_RUN=0
[[ "${1:-}" == "--dry" ]] && DRY_RUN=1

# 临时目录模式（整体删除）
DIR_PATTERNS=(
  "__pycache__"
  ".pytest_cache"
  ".pytest_tmp"
  ".mypy_cache"
  ".ruff_cache"
  ".cache"
  "htmlcov"
  ".tox"
  "build"
  "dist"
  ".eggs"
  "*.egg-info"
)

# 临时文件模式
FILE_PATTERNS=(
  "*.pyc"
  "*.pyo"
  "*.pyd"
  ".coverage"
  ".coverage.*"
)

EXCLUDE_PRUNE="-not -path '*/.git/*' -not -path '*/.workbuddy/*'"

echo "==> Project root: $ROOT"
if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "==> DRY RUN (no files will be removed)"
fi

removed=0

# 删除临时目录
for d in "${DIR_PATTERNS[@]}"; do
  while IFS= read -r -d '' dir; do
    [[ "$dir" == *"/.git/"* || "$dir" == *"/.workbuddy/"* ]] && continue
    if [[ "$DRY_RUN" -eq 1 ]]; then
      echo "  would remove dir: $dir"
    else
      rm -rf "$dir"
      echo "  removed dir: $dir"
    fi
    removed=$((removed + 1))
  done < <(find . -type d -name "$d" -not -path '*/.git/*' -not -path '*/.workbuddy/*' -print0)
done

# 删除临时文件
for f in "${FILE_PATTERNS[@]}"; do
  while IFS= read -r -d '' file; do
    [[ "$file" == *"/.git/"* || "$file" == *"/.workbuddy/"* ]] && continue
    if [[ "$DRY_RUN" -eq 1 ]]; then
      echo "  would remove file: $file"
    else
      rm -f "$file"
      echo "  removed file: $file"
    fi
    removed=$((removed + 1))
  done < <(find . -type f -name "$f" -not -path '*/.git/*' -not -path '*/.workbuddy/*' -print0)
done

echo "==> Done. Items processed: $removed"
if [[ "$DRY_RUN" -eq 0 ]]; then
  echo "==> All temporary artifacts removed; re-run 'pytest' to regenerate caches."
fi
