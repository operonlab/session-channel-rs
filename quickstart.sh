#!/usr/bin/env bash
# session-channel — interactive first-run setup.
#
# After you have the `channel` CLI on $PATH (via brew, install.sh, or
# cargo build), run:
#
#   ./quickstart.sh              # interactive
#   ./quickstart.sh --yes        # full-auto: docker compose + random key
#
# Goal: from zero to "first message visible" in under 60 seconds.

set -euo pipefail

REPO="operonlab/session-channel"
COMPOSE_URL="https://raw.githubusercontent.com/${REPO}/main/docker-compose.yml"
YES="${YES:-no}"
if [[ "${1:-}" == "--yes" || "${1:-}" == "-y" ]]; then
  YES="yes"
fi

c_green()  { printf '\033[32m%s\033[0m' "$1"; }
c_yellow() { printf '\033[33m%s\033[0m' "$1"; }
c_red()    { printf '\033[31m%s\033[0m' "$1"; }
c_dim()    { printf '\033[2m%s\033[0m' "$1"; }

step()   { printf '\n%s %s\n' "$(c_green '→')" "$*"; }
note()   { printf '%s %s\n' "$(c_dim '·')" "$*"; }
fail()   { printf '%s %s\n' "$(c_red '✗')" "$*" >&2; exit 1; }

ask() {
  # ask "Question" "default"
  local q="$1" default="$2" reply
  if [[ "${YES}" == "yes" ]]; then
    printf '%s [auto: %s]\n' "$q" "$default"
    echo "$default"
    return
  fi
  read -r -p "$q [$default] " reply || true
  echo "${reply:-$default}"
}

choose() {
  # choose "Question" "1 2 3" "1=docker, 2=brew, 3=skip" "1"
  local q="$1" valid="$2" hint="$3" default="$4" reply
  if [[ "${YES}" == "yes" ]]; then
    printf '%s [auto: %s]\n' "$q" "$default"
    echo "$default"
    return
  fi
  while true; do
    printf '%s\n  %s\n' "$q" "$hint"
    read -r -p "Choose [$default]: " reply || true
    reply="${reply:-$default}"
    case " $valid " in
      *" $reply "*) echo "$reply"; return ;;
      *) printf 'Please enter one of: %s\n' "$valid" >&2 ;;
    esac
  done
}

# ─────────────────────────────────────────────────────────────────────────────
step "session-channel quickstart"

if ! command -v channel >/dev/null 2>&1; then
  fail "channel CLI not found on \$PATH. Install first (see README §Install)."
fi
note "channel CLI: $(command -v channel)"

# ─── Q1: how to bring Redis + service up ─────────────────────────────────────
step "How would you like to run Redis + channel-service?"
mode="$(choose "" "1 2 3" "1) docker compose (recommended; bundles Redis)
  2) brew services (start redis + run channel-service in background)
  3) skip — I already have these running" "1")"

case "$mode" in
  1)
    if ! command -v docker >/dev/null 2>&1; then
      fail "docker not installed. Install Docker Desktop, then re-run."
    fi
    if [[ ! -f docker-compose.yml ]]; then
      note "fetching docker-compose.yml"
      curl -fsSL "$COMPOSE_URL" -o docker-compose.yml
    else
      note "reusing existing docker-compose.yml"
    fi
    ;;
  2)
    if ! command -v brew >/dev/null 2>&1; then
      fail "brew not installed. Install Homebrew, then re-run with option 2."
    fi
    if ! command -v channel-service >/dev/null 2>&1; then
      fail "channel-service not on \$PATH. Install via brew or install.sh."
    fi
    ;;
  3)
    note "skipping Redis + service bring-up"
    ;;
esac

# ─── Q2: generate a random SESSION_CHANNEL_KEY? ──────────────────────────────
step "Generate a random SESSION_CHANNEL_KEY?"
note "The default 'change-me-in-production' is fine for trying it out,"
note "but you should replace it before relying on the local-key check."
gen_key="$(ask "Generate? (y/n)" "y")"

key=""
if [[ "$gen_key" =~ ^[Yy]$ ]]; then
  if command -v openssl >/dev/null 2>&1; then
    key="$(openssl rand -hex 32)"
  else
    # Fallback: /dev/urandom → hex
    key="$(head -c 32 /dev/urandom | xxd -p -c 64)"
  fi

  case "$mode" in
    1)
      # Write to .env so docker compose picks it up
      if [[ -f .env ]] && grep -q '^SESSION_CHANNEL_KEY=' .env; then
        # Replace existing line
        tmp="$(mktemp)"
        sed 's/^SESSION_CHANNEL_KEY=.*/SESSION_CHANNEL_KEY='"$key"'/' .env > "$tmp"
        mv "$tmp" .env
      else
        printf 'SESSION_CHANNEL_KEY=%s\n' "$key" >> .env
      fi
      note "wrote SESSION_CHANNEL_KEY to ./.env"
      ;;
    2|3)
      # Suggest exporting; do not silently mutate ~/.zshrc
      printf '\n  Add this to your shell rc (e.g. ~/.zshrc) and `source` it:\n'
      printf '    export SESSION_CHANNEL_KEY="%s"\n\n' "$key"
      export SESSION_CHANNEL_KEY="$key"
      note "exported for this shell session"
      ;;
  esac
fi

# ─── Bring services up ───────────────────────────────────────────────────────
case "$mode" in
  1)
    step "docker compose up -d"
    docker compose up -d
    # Wait a beat for healthchecks
    sleep 2
    ;;
  2)
    step "brew services start redis"
    brew services start redis || true
    if ! pgrep -x channel-service >/dev/null 2>&1; then
      step "channel-service & (background)"
      nohup channel-service > /tmp/session-channel.log 2>&1 &
      sleep 1
      note "channel-service started; logs: /tmp/session-channel.log"
    else
      note "channel-service already running"
    fi
    ;;
esac

# ─── Doctor verdict ──────────────────────────────────────────────────────────
step "channel doctor"
if channel doctor; then
  printf '\n%s Ready. Try:\n' "$(c_green '✓')"
  printf '    channel send broadcasts "hello"\n'
  printf '    channel read broadcasts --count 1\n'
else
  printf '\n%s Some checks failed — see FAIL lines above.\n' "$(c_yellow '!')"
  exit 1
fi
