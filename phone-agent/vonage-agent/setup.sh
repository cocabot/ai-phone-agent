#!/usr/bin/env bash
# One-shot setup: venv + deps + .env template + cloudflared binary (if missing).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

PYTHON="${PYTHON:-python3}"
if [[ ! -x .venv/bin/python ]]; then
  echo "==> creating venv"
  "$PYTHON" -m venv .venv
fi
echo "==> installing requirements"
.venv/bin/python -m pip --version >/dev/null 2>&1 || .venv/bin/python -m ensurepip -q
.venv/bin/python -m pip install -q --upgrade pip
.venv/bin/python -m pip install -q -r requirements.txt

if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "==> created .env from .env.example (値を入れてください)"
fi

if ! command -v cloudflared >/dev/null 2>&1 && [[ ! -x bin/cloudflared ]]; then
  os="$(uname -s | tr '[:upper:]' '[:lower:]')"
  arch="$(uname -m)"
  case "$arch" in
    x86_64|amd64) arch=amd64 ;;
    aarch64|arm64) arch=arm64 ;;
    armv7l|armv6l) arch=arm ;;
  esac
  base="https://github.com/cloudflare/cloudflared/releases/latest/download"
  mkdir -p bin
  echo "==> downloading cloudflared ($os/$arch)"
  if [[ "$os" == "darwin" ]]; then
    curl -fsSL "$base/cloudflared-darwin-$arch.tgz" | tar -xz -C bin cloudflared
  else
    curl -fsSL -o bin/cloudflared "$base/cloudflared-linux-$arch"
  fi
  chmod +x bin/cloudflared
fi

chmod +x agent
echo
echo "setup done. 次:"
echo "  1) .env を編集（VONAGE_* と GEMINI_API_KEY）し、秘密鍵を ./private.key に置く"
echo "  2) ./agent doctor"
echo "  3) ./agent start"
