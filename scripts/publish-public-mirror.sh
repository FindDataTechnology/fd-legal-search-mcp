#!/usr/bin/env bash
# Publish this tree to the PUBLIC build mirror (scs001/fd-legal-search-mcp).
#
# Why: GitHub Actions on a private repo draws on a metered monthly allowance;
# once it is spent, runs fail in ~2 s with no runner and no logs, which reads
# like a broken workflow. Public repos are unmetered. See
# finddata/docs/IMAGE-RELEASE.md「私有仓的 Actions 额度陷阱」.
#
# The public tree carries build inputs only. Deployment manifests and anything
# that names our internal addressing stay in the private repo.
#
# Usage: ./scripts/publish-public-mirror.sh [--dry-run]
set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)"
MIRROR="${MIRROR:-/tmp/fd-legal-search-mirror}"
DRY_RUN="${1:-}"

if [ ! -d "$MIRROR/.git" ]; then
  echo "error: $MIRROR is not a git checkout (clone git@github.com:scs001/fd-legal-search-mcp.git first)" >&2
  exit 1
fi

echo "== sync $SRC -> $MIRROR"
rsync -a --delete \
  --exclude='.git' --exclude='__pycache__' --exclude='*.egg-info' \
  --exclude='.pytest_cache' --exclude='.DS_Store' \
  --exclude='.venv' --exclude='.env' --exclude='.env.*' \
  --exclude='deploy' \
  "$SRC/" "$MIRROR/"

cd "$MIRROR"

python3 - <<'PY'
from pathlib import Path
import json

def patch(path, old, new):
    p = Path(path)
    if not p.is_file():
        return
    s = p.read_text(encoding="utf-8")
    if old not in s:
        return  # already sanitised; the script is re-runnable
    p.write_text(s.replace(old, new, 1), encoding="utf-8")

# tool-surface: the upstream URL is this deployment's NodePort.
p = Path("tool-surface.json")
if p.is_file():
    d = json.loads(p.read_text(encoding="utf-8"))
    reg = d.get("registry")
    if isinstance(reg, dict) and "upstream" in reg:
        reg["upstream"] = "<your-deployment>/mcp"
        p.write_text(json.dumps(d, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

# .env.example: replace the tailnet endpoints with placeholders.
p = Path(".env.example")
if p.is_file():
    s = p.read_text(encoding="utf-8")
    s = s.replace("postgresql://postgres:***@100.64.0.5:5432/law_db",
                  "postgresql://user:pass@law-db:5432/law_db")
    s = s.replace("postgresql://postgres:***@100.64.0.5:5432/wenshu_db",
                  "postgresql://user:pass@law-db:5432/wenshu_db")
    s = s.replace("http://100.64.0.9:6333", "http://qdrant:6333")
    s = s.replace("# law_db（PostgreSQL；Windows 数据机 Tailnet 100.64.0.5:5432）",
                  "# law_db（PostgreSQL）")
    p.write_text(s, encoding="utf-8")
print("sanitised")
PY

echo "== scan for leftovers"
if grep -rnE '100\.64\.|192\.168\.|cheap3|cheap-3|xinru' \
     --exclude-dir=.git --exclude='publish-public-mirror.sh' . 2>/dev/null | head -10; then
  echo "!! leftovers above — fix before committing" >&2
  exit 1
fi
echo "clean"

if [ "$DRY_RUN" = "--dry-run" ]; then
  echo "== dry run: not committing"
  git --no-pager diff --stat
  exit 0
fi

echo "== done; review 'git status' then commit and push"
