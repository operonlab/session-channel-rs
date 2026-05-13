#!/usr/bin/env bash
# session-channel — one-line installer
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/operonlab/session-channel/main/install.sh | bash
#
# Environment variables:
#   INSTALL_DIR       — destination directory (default: $HOME/.local/bin)
#   INSTALL_VERSION   — tag to install (default: latest release)
#   GITHUB_TOKEN      — optional, raises GitHub API rate limit (60/hr unauth)
#
# Run with --uninstall to remove the two binaries (does not touch Redis,
# config, or your shell rc).

set -euo pipefail

REPO="operonlab/session-channel"
INSTALL_DIR="${INSTALL_DIR:-$HOME/.local/bin}"
INSTALL_VERSION="${INSTALL_VERSION:-}"
BINARIES=(channel channel-service)

c_red()    { printf '\033[31m%s\033[0m' "$1"; }
c_green()  { printf '\033[32m%s\033[0m' "$1"; }
c_yellow() { printf '\033[33m%s\033[0m' "$1"; }
c_dim()    { printf '\033[2m%s\033[0m' "$1"; }

info()  { printf '%s %s\n' "$(c_dim '·')" "$*"; }
ok()    { printf '%s %s\n' "$(c_green '✓')" "$*"; }
warn()  { printf '%s %s\n' "$(c_yellow '!')" "$*"; }
fail()  { printf '%s %s\n' "$(c_red '✗')" "$*" >&2; exit 1; }

# ─────────────────────────────────────────────────────────────────────────────
# Uninstall path
# ─────────────────────────────────────────────────────────────────────────────
if [[ "${1:-}" == "--uninstall" ]]; then
  removed=0
  for bin in "${BINARIES[@]}"; do
    if [[ -f "${INSTALL_DIR}/${bin}" ]]; then
      rm -f "${INSTALL_DIR}/${bin}"
      ok "removed ${INSTALL_DIR}/${bin}"
      removed=$((removed + 1))
    fi
  done
  if [[ "${removed}" -eq 0 ]]; then
    info "nothing to remove in ${INSTALL_DIR}"
  fi
  info "Redis, ~/.zshrc, and Docker containers are left alone."
  exit 0
fi

# ─────────────────────────────────────────────────────────────────────────────
# Detect OS / arch → Rust target triple
# ─────────────────────────────────────────────────────────────────────────────
os="$(uname -s)"
arch="$(uname -m)"

case "${arch}" in
  x86_64|amd64)   arch="x86_64"  ;;
  arm64|aarch64)  arch="aarch64" ;;
  *) fail "unsupported architecture: ${arch}" ;;
esac

case "${os}" in
  Darwin)
    triple="${arch}-apple-darwin"
    ;;
  Linux)
    triple="${arch}-unknown-linux-gnu"
    ;;
  *)
    fail "unsupported OS: ${os}. Build from source: cargo install --git https://github.com/${REPO}"
    ;;
esac

info "detected ${os}/${arch} → ${triple}"

# ─────────────────────────────────────────────────────────────────────────────
# Resolve target version (default: latest GitHub Release)
# ─────────────────────────────────────────────────────────────────────────────
api_headers=(-H "Accept: application/vnd.github+json")
if [[ -n "${GITHUB_TOKEN:-}" ]]; then
  api_headers+=(-H "Authorization: Bearer ${GITHUB_TOKEN}")
fi

if [[ -z "${INSTALL_VERSION}" ]]; then
  info "querying latest release..."
  INSTALL_VERSION="$(
    curl -fsSL "${api_headers[@]}" \
      "https://api.github.com/repos/${REPO}/releases/latest" \
      | sed -n 's/.*"tag_name": *"\([^"]*\)".*/\1/p' | head -1
  )"
  if [[ -z "${INSTALL_VERSION}" ]]; then
    fail "could not resolve latest release tag. Set INSTALL_VERSION=v0.2.0 to override."
  fi
fi

info "installing ${INSTALL_VERSION}"

# ─────────────────────────────────────────────────────────────────────────────
# Download + checksum verify
# ─────────────────────────────────────────────────────────────────────────────
archive="session-channel-${INSTALL_VERSION}-${triple}.tar.gz"
url_base="https://github.com/${REPO}/releases/download/${INSTALL_VERSION}"
tmp_dir="$(mktemp -d)"
trap 'rm -rf "${tmp_dir}"' EXIT

info "downloading ${archive}"
curl -fsSL "${url_base}/${archive}"        -o "${tmp_dir}/${archive}"
curl -fsSL "${url_base}/${archive}.sha256" -o "${tmp_dir}/${archive}.sha256"

(
  cd "${tmp_dir}"
  if command -v shasum >/dev/null 2>&1; then
    shasum -a 256 -c "${archive}.sha256" >/dev/null
  elif command -v sha256sum >/dev/null 2>&1; then
    sha256sum -c "${archive}.sha256" >/dev/null
  else
    warn "neither shasum nor sha256sum present — skipping checksum verification"
  fi
)
ok "checksum verified"

# ─────────────────────────────────────────────────────────────────────────────
# Extract + install
# ─────────────────────────────────────────────────────────────────────────────
tar -xzf "${tmp_dir}/${archive}" -C "${tmp_dir}"

mkdir -p "${INSTALL_DIR}"
for bin in "${BINARIES[@]}"; do
  if [[ ! -f "${tmp_dir}/${bin}" ]]; then
    fail "tarball missing expected binary: ${bin}"
  fi
  install -m 0755 "${tmp_dir}/${bin}" "${INSTALL_DIR}/${bin}"
  ok "installed ${INSTALL_DIR}/${bin}"
done

# ─────────────────────────────────────────────────────────────────────────────
# PATH hint
# ─────────────────────────────────────────────────────────────────────────────
case ":${PATH}:" in
  *":${INSTALL_DIR}:"*)
    ok "${INSTALL_DIR} is already on \$PATH"
    ;;
  *)
    warn "${INSTALL_DIR} is not on \$PATH"
    printf '   Add this line to your shell rc (e.g. ~/.zshrc):\n\n'
    # shellcheck disable=SC2016  # documentation: single quotes intentional, $VAR shown to user verbatim
    printf '     export PATH="%s:$PATH"\n\n' "${INSTALL_DIR}"
    ;;
esac

info "next: run \`channel doctor\` to verify the environment, or"
info "      \`docker compose up -d\` to bring up Redis + service."
