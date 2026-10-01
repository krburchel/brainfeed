#!/usr/bin/env bash
# BrainFeed for Hermes Agent: install | activate | status | test | uninstall
#
# Run "install" from a checkout of a specific, verified commit of the repo:
#   bash hermes/setup.sh install
# install keeps a copy at ~/.hermes/skills/productivity/brainfeed/scripts/setup.sh,
# which is what activate / status / test / uninstall should be run from.
#
# Touches only:
#   $HERMES_HOME/skills/productivity/brainfeed/   (the skill + helper)
#   $HERMES_HOME/scripts/brainfeed-reminders.sh   (cron entry point)
#   $BRAINFEED_CONFIG_DIR (default ~/.config/brainfeed): config.json, token, state.json, inbox/
#   one Hermes cron job named "brainfeed-reminders"
# It never prints the token, opens no ports and changes no system settings.
set -euo pipefail
umask 077

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
SKILL_DIR="$HERMES_HOME/skills/productivity/brainfeed"
CRON_SCRIPT_NAME="brainfeed-reminders.sh"
CRON_SCRIPT="$HERMES_HOME/scripts/$CRON_SCRIPT_NAME"
CFG_DIR="${BRAINFEED_CONFIG_DIR:-$HOME/.config/brainfeed}"
BASE_URL="https://bzvibdjrknqvmurwjroq.supabase.co/functions/v1/brainfeed-api"
JOB="brainfeed-reminders"
SCHEDULE="${BRAINFEED_SCHEDULE:-every 2m}"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPER="$SKILL_DIR/scripts/brainfeed.py"
INSTALLED_SETUP="$SKILL_DIR/scripts/setup.sh"

say() { printf '%s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }
helper() { BRAINFEED_CONFIG_DIR="$CFG_DIR" python3 "$HELPER" "$@"; }
job_exists() { hermes cron list 2>/dev/null | grep -qw "$JOB"; }
confirm() { # confirm "question" default(y|n); no terminal => safe "no"
  [[ -t 0 ]] || return 1
  local ans; read -r -p "$1 " ans || ans=""
  ans="${ans:-$2}"; [[ "$ans" =~ ^[Yy] ]]
}

need() {
  command -v python3 >/dev/null || die "python3 is required"
  python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' || die "python3 3.8+ is required"
  command -v hermes >/dev/null || die "the hermes CLI is not on PATH"
}

cmd_install() {
  need
  [[ -f "$SRC/brainfeed/SKILL.md" && -f "$SRC/brainfeed/scripts/brainfeed.py" ]] || die "run this from the repo's hermes/ directory"

  say "• Installing skill to $SKILL_DIR"
  mkdir -p "$(dirname "$SKILL_DIR")" "$HERMES_HOME/scripts"
  rm -rf "$SKILL_DIR.new"
  cp -R "$SRC/brainfeed" "$SKILL_DIR.new"
  cp "$SRC/setup.sh" "$SKILL_DIR.new/scripts/setup.sh"  # so status/uninstall work after the download is deleted
  chmod 700 "$SKILL_DIR.new/scripts/brainfeed.py" "$SKILL_DIR.new/scripts/setup.sh"
  rm -rf "$SKILL_DIR"; mv "$SKILL_DIR.new" "$SKILL_DIR"

  say "• Writing cron entry point $CRON_SCRIPT"
  cat > "$CRON_SCRIPT" <<EOF
#!/usr/bin/env bash
# BrainFeed reminder delivery (Hermes no-agent cron job "$JOB").
# stdout is delivered as the message; empty output = nothing due.
export BRAINFEED_CONFIG_DIR="$CFG_DIR"
exec python3 "$HELPER" deliver
EOF
  chmod 700 "$CRON_SCRIPT"

  mkdir -p "$CFG_DIR/inbox"; chmod 700 "$CFG_DIR" "$CFG_DIR/inbox"
  if [[ ! -f "$CFG_DIR/config.json" ]]; then
    printf '{"base_url": "%s"}\n' "$BASE_URL" > "$CFG_DIR/config.json"
  fi
  chmod 600 "$CFG_DIR/config.json"

  if [[ -f "$CFG_DIR/token" ]]; then
    say "• Keeping existing token. Fingerprint:"
    helper fingerprint
  else
    say "• Created a new 256-bit token in $CFG_DIR/token (mode 600). Fingerprint:"
    helper init-token
  fi
  say ""
  say "Next: Kevin adds that sha256:… fingerprint in BrainFeed → menu → Settings → Connected agents."
  say "Then run:  bash $INSTALLED_SETUP activate --deliver telegram"
}

cmd_activate() {
  need
  local deliver="${1:-telegram}"
  [[ "$deliver" =~ ^[a-z]+(:[^,]+)?(,[a-z]+(:[^,]+)?)*$ ]] || die "invalid --deliver target"
  helper status || die "BrainFeed rejected the token. Is the fingerprint added in Settings → Connected agents?"
  if job_exists; then
    say "• Cron job '$JOB' already exists; leaving it as is."
  else
    hermes cron create "$SCHEDULE" --no-agent --script "$CRON_SCRIPT_NAME" --deliver "$deliver" --name "$JOB"
    say "• Created cron job '$JOB' ($SCHEDULE, delivering to $deliver)."
  fi
}

cmd_status() {
  [[ -f "$SKILL_DIR/SKILL.md" ]] && say "skill:    installed ($(grep -m1 '^version:' "$SKILL_DIR/SKILL.md"))" || say "skill:    not installed"
  [[ -x "$CRON_SCRIPT" ]] && say "script:   $CRON_SCRIPT" || say "script:   missing"
  if command -v hermes >/dev/null && job_exists; then say "cron job: $JOB present"; else say "cron job: not found"; fi
  if [[ -f "$CFG_DIR/token" ]]; then
    say "token:    $(helper fingerprint)"
    helper status || true
  else
    say "token:    none"
  fi
}

cmd_test() {
  need
  helper status
  say "• Read-only search:"
  helper search --limit 1 >/dev/null && say "  search works ✓"
  say "• Reminder list:"
  helper reminders --limit 3
  if job_exists; then say "• Cron job '$JOB' is registered ✓ (to see runs: find its ID with 'hermes cron list', then 'hermes cron runs <id>')"; else say "• Cron job '$JOB' not registered (run: setup.sh activate)"; fi
}

cmd_uninstall() {
  local revoke="" delcfg=""
  for a in "$@"; do case "$a" in
    --revoke) revoke=y ;; --keep-token) revoke=n ;;
    --delete-config) delcfg=y ;; --keep-config) delcfg=n ;;
    *) die "unknown uninstall option: $a" ;;
  esac; done
  if command -v hermes >/dev/null && job_exists; then
    hermes cron remove "$JOB" && say "• Removed cron job '$JOB'"
  fi
  if [[ -z "$revoke" ]]; then confirm "Revoke this token on the BrainFeed server too? [Y/n]" y && revoke=y || revoke=n; fi
  if [[ "$revoke" == y && -f "$CFG_DIR/token" && -f "$HELPER" ]]; then
    helper revoke-token || say "  (couldn't revoke; revoke it in BrainFeed → Settings → Connected agents)"
  fi
  rm -rf "$SKILL_DIR" "$CRON_SCRIPT" && say "• Removed skill and cron script"
  if [[ -z "$delcfg" ]]; then confirm "Delete local BrainFeed config and token in $CFG_DIR? [y/N]" n && delcfg=y || delcfg=n; fi
  if [[ "$delcfg" == y && -d "$CFG_DIR" ]]; then
    rm -rf "$CFG_DIR" && say "• Removed $CFG_DIR"
  elif [[ -d "$CFG_DIR" ]]; then
    say "• Kept $CFG_DIR (token still valid unless revoked). Remove later with: uninstall --delete-config"
  fi
  say ""
  say "Kevin's notes and reminders in BrainFeed were not touched."
  say "To delete them, export first, then delete them in the web app. Nothing here can delete them."
}

case "${1:-}" in
  install)   cmd_install ;;
  activate)  shift; [[ "${1:-}" == "--deliver" ]] && { cmd_activate "${2:-}"; } || cmd_activate ;;
  status)    cmd_status ;;
  test)      cmd_test ;;
  uninstall) shift; cmd_uninstall "$@" ;;
  *) say "usage: setup.sh install | activate [--deliver telegram|discord|telegram,discord] | status | test | uninstall [--revoke|--keep-token] [--delete-config|--keep-config]"; exit 2 ;;
esac
