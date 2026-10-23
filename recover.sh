#!/usr/bin/env bash
set -euo pipefail
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ ! -x "$project_dir/.venv/bin/python" ]]; then
  echo '缺少项目 .venv/bin/python，请先恢复虚拟环境。' >&2
  exit 1
fi
exec "$project_dir/.venv/bin/python" "$project_dir/scripts/recover_services.py" "$@"
