#!/usr/bin/env bash
# One-shot local setup: venv + dependencies + .env template + cloudflared check.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

PYTHON="${PYTHON:-python3}"

if [ ! -x .venv/bin/python ]; then
  echo "creating .venv with $PYTHON"
  if ! "$PYTHON" -m venv .venv 2>/dev/null; then
    echo "python3 -m venv failed (missing ensurepip?). Trying --without-pip + get-pip.py" >&2
    "$PYTHON" -m venv --without-pip .venv
    curl -fsSL https://bootstrap.pypa.io/get-pip.py | .venv/bin/python
  fi
fi

.venv/bin/python -m pip install --quiet --upgrade pip
.venv/bin/python -m pip install --quiet -r requirements.txt
echo "dependencies installed"

if [ ! -f .env ]; then
  cp .env.example .env
  echo "created .env from .env.example -> fill in VONAGE_* and GEMINI_API_KEY"
fi

mkdir -p .runtime

if command -v cloudflared >/dev/null 2>&1; then
  echo "cloudflared: $(command -v cloudflared)"
else
  cat <<'EOF'
cloudflared not found. Install it for the automatic quick tunnel, e.g.
  macOS : brew install cloudflared
  Debian: curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg | sudo tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null \
          && echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main" | sudo tee /etc/apt/sources.list.d/cloudflared.list \
          && sudo apt-get update && sudo apt-get install -y cloudflared
  Linux : download a binary from https://github.com/cloudflare/cloudflared/releases and set CLOUDFLARED_BIN=/path/to/cloudflared
(or set PUBLIC_URL to a stable hostname and skip the tunnel)
EOF
fi

echo
echo "next: ./vonage doctor"
