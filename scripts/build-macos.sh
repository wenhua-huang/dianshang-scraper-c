#!/usr/bin/env bash
# Build a versioned macOS arm64 package for dianshang-scraper-c.
# Usage: bash scripts/build-macos.sh [test|prod|both]  (default: both)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$ROOT_DIR"

# ── 1. Read version from pyproject.toml ──────────────────────────────────────
VERSION=$(python3 -c "
from pathlib import Path
import tomllib
data = tomllib.loads(Path('pyproject.toml').read_text(encoding='utf-8'))
print(data['project']['version'])
")
if [[ -z "$VERSION" ]]; then
  echo "[build] ERROR: could not parse version from pyproject.toml" >&2
  exit 1
fi
echo "[build] version = $VERSION"

# ── 2. Re-install editable package so metadata matches pyproject ─────────────
echo "[build] pip install -e ."
.venv/bin/python -m pip install -e . -q

# ── 3. Determine targets ─────────────────────────────────────────────────────
TARGET_ARG="${1:-both}"
case "$TARGET_ARG" in
  test)  TARGETS=("test") ;;
  prod)  TARGETS=("prod") ;;
  both)  TARGETS=("test" "prod") ;;
  *)
    echo "[build] ERROR: unknown target '$TARGET_ARG'. Use test|prod|both." >&2
    exit 1
    ;;
esac

ARCH="$(uname -m)"   # arm64 or x86_64
PLATFORM="macos"

# ── 4. Build executable ───────────────────────────────────────────────────────
echo "[build] pyinstaller ..."
.venv/bin/pyinstaller \
  --onefile --console \
  --name scraper-client \
  src/scraper_client/app/main.py \
  --distpath ./dist \
  --workpath ./build \
  --copy-metadata dianshang-scraper-c \
  --noconfirm

# ── 5. Stage versioned packages ───────────────────────────────────────────────
for TARGET in "${TARGETS[@]}"; do
  ENV_FILE=".env.$TARGET"
  if [[ ! -f "$ENV_FILE" ]]; then
    echo "[build] ERROR: missing $ENV_FILE" >&2
    exit 1
  fi

  PKG_DIR="staging/scraper-client-${VERSION}-${PLATFORM}-${ARCH}-${TARGET}"
  echo "[build] staging → $PKG_DIR"
  rm -rf "$PKG_DIR"
  mkdir -p "$PKG_DIR"

  # Rename executable to include target suffix (mirrors Windows convention)
  cp dist/scraper-client "$PKG_DIR/scraper-client-${TARGET}"
  chmod +x "$PKG_DIR/scraper-client-${TARGET}"

  cp ".env.example"  "$PKG_DIR/.env.example"
  cp ".env.example"  "$PKG_DIR/env.example"
  cp "$ENV_FILE"     "$PKG_DIR/$ENV_FILE"
  cp "$ENV_FILE"     "$PKG_DIR/env.$TARGET"
  printf '%s' "$TARGET" > "$PKG_DIR/.package-env"
  printf '%s' "$TARGET" > "$PKG_DIR/package-env"
done

# ── 6. Smoke check ────────────────────────────────────────────────────────────
for TARGET in "${TARGETS[@]}"; do
  PKG_DIR="staging/scraper-client-${VERSION}-${PLATFORM}-${ARCH}-${TARGET}"
  EXE="$PKG_DIR/scraper-client-${TARGET}"
  echo "[build] smoke check: $EXE --help"
  "$EXE" --help

  SCRAPER_SERVER_BASE_URL="http://127.0.0.1:8000/api/v1" \
  SCRAPER_INTERNAL_API_KEY="ci-smoke-key" \
  PLAYWRIGHT_CDP_URL="http://127.0.0.1:9222" \
  SCRAPER_SKIP_BACKEND_CHECK=1 \
  "$EXE" "start-${TARGET}" --help
done

echo "[build] done  →  staging/scraper-client-${VERSION}-${PLATFORM}-${ARCH}-{target}"
