#!/usr/bin/env bash
# ForgeFlow one-command installer for macOS and Linux.
set -euo pipefail
VERSION="${FORGEFLOW_VERSION:-3.11.0}"
REPO="KevinXia69/ForgeFlow"
BASE="https://github.com/${REPO}/releases/download/v${VERSION}"

case "$(uname -s)" in
  Darwin) PLATFORM="macOS" ;;
  Linux) PLATFORM="Linux" ;;
  *) printf '%s\n' "Unsupported operating system: $(uname -s)" >&2; exit 2 ;;
esac
ASSET="ForgeFlow-v${VERSION}-${PLATFORM}.zip"
DEST="${FORGEFLOW_HOME:-$HOME/ForgeFlow}"

if [ -e "$DEST" ]; then
  printf '%s\n' "Destination already exists: $DEST" >&2
  printf '%s\n' "Set FORGEFLOW_HOME to another directory or remove it after backing up your data." >&2
  exit 2
fi

for tool in curl unzip; do
  command -v "$tool" >/dev/null 2>&1 || { printf '%s\n' "Missing required tool: $tool" >&2; exit 2; }
done
command -v docker >/dev/null 2>&1 || { printf '%s\n' 'Docker is required. Install Docker Desktop or Docker Engine with Compose v2.' >&2; exit 2; }

TMP="$(mktemp -d "${TMPDIR:-/tmp}/forgeflow-install.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT
printf '%s\n' "Downloading ${ASSET}..."
curl -fL --retry 3 --retry-delay 2 -o "$TMP/$ASSET" "$BASE/$ASSET"
curl -fL --retry 3 --retry-delay 2 -o "$TMP/SHA256SUMS" "$BASE/SHA256SUMS"

EXPECTED="$(awk -v name="$ASSET" '$2 == name {print $1}' "$TMP/SHA256SUMS" | head -n1)"
[ -n "$EXPECTED" ] || { printf '%s\n' "No checksum found for ${ASSET}." >&2; exit 2; }
if command -v shasum >/dev/null 2>&1; then ACTUAL="$(shasum -a 256 "$TMP/$ASSET" | awk '{print $1}')"; else ACTUAL="$(sha256sum "$TMP/$ASSET" | awk '{print $1}')"; fi
[ "$EXPECTED" = "$ACTUAL" ] || { printf '%s\n' 'Checksum verification failed; refusing to install.' >&2; exit 2; }

unzip -q "$TMP/$ASSET" -d "$TMP/unpacked"
ROOT="$(find "$TMP/unpacked" -mindepth 1 -maxdepth 1 -type d | head -n1)"
[ -n "$ROOT" ] || { printf '%s\n' 'Unexpected installer archive layout.' >&2; exit 2; }
mkdir -p "$(dirname "$DEST")"
mv "$ROOT" "$DEST"
chmod +x "$DEST/Start-ForgeFlow.command" "$DEST/start.sh" 2>/dev/null || true
printf '%s\n' "ForgeFlow ${VERSION} installed at: $DEST"
printf '%s\n' 'Starting the one-time local setup...'
if [ "$PLATFORM" = "macOS" ]; then
  exec "$DEST/Start-ForgeFlow.command"
else
  exec bash "$DEST/start.sh"
fi
