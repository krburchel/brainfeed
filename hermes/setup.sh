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
# Jobs named exactly $JOB, one per line: id<TAB>state<TAB>schedule<TAB>deliver<TAB>script<TAB>mode
# Parses `hermes cron list` blocks like:
#   f047a1d5e91b [active]
#   Name:      brainfeed-reminders
#   Schedule:  every 2m
#   ...
# Returns 3 if the list can't be obtained or can't be read, so callers never
# mistake "unknown" for "no job" (which could create a duplicate).
cron_jobs() {
  local out rc
  out="$(hermes cron list 2>&1)" && rc=0 || rc=$?
  if [[ $rc -ne 0 ]]; then
    printf 'hermes cron list failed (exit %s):\n%s\n' "$rc" "$out" >&2
    return 3
  fi
  local parsed
  parsed="$(JOB="$JOB" python3 -c '
import os, re, sys
job, cur, jobs = os.environ["JOB"], None, []
for line in sys.stdin.read().splitlines():
    m = re.match(r"^\s*([0-9a-f]{6,})\s+\[([A-Za-z_-]+)\]", line)
    if m:
        cur = {"id": m.group(1), "state": m.group(2).lower()}
        jobs.append(cur)
        continue
    m = re.match(r"^\s*([A-Za-z][A-Za-z ]*?):\s*(.*?)\s*$", line)
    if m and cur is not None:
        cur.setdefault(m.group(1).strip().lower(), m.group(2))
for j in jobs:
    if j.get("name") == job:
        print("\t".join([j["id"], j["state"], j.get("schedule", ""), j.get("deliver", ""), j.get("script", ""), j.get("mode", "")]))
' <<<"$out")" || return 3
  # The name appears but no block parsed: the list format is not what we expect.
  if [[ -z "$parsed" ]] && grep -Eq "(^|[^A-Za-z0-9_-])${JOB}([^A-Za-z0-9_-]|$)" <<<"$out"; then
    printf 'Could not read hermes cron list output; refusing to guess.\n' >&2
    return 3
  fi
  printf '%s' "$parsed"
}
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
  local jobs
  jobs="$(cron_jobs)" || die "Couldn't list cron jobs reliably; not creating or changing anything."
  local count; count=$(grep -c . <<<"$jobs" || true)
  if [[ "$count" -gt 1 ]]; then
    die "Found $count jobs named '$JOB' ($(cut -f1 <<<"$jobs" | tr '\n' ' ')). Remove the extras with 'hermes cron remove <id>', then re-run activate."
  fi
  if [[ "$count" -eq 0 ]]; then
    hermes cron create "$SCHEDULE" --no-agent --script "$CRON_SCRIPT_NAME" --deliver "$deliver" --name "$JOB"
    say "• Created cron job '$JOB' ($SCHEDULE, delivering to $deliver)."
    return
  fi
  # Exactly one: reconcile it.
  local id state sched dlv script mode
  IFS=$'\t' read -r id state sched dlv script mode <<<"$jobs"
  if [[ "$script" != "$CRON_SCRIPT_NAME" || ( -n "$mode" && "$mode" != "no-agent" ) ]]; then
    die "Job $id is named '$JOB' but runs '$script' (mode: ${mode:-?}). Remove it with 'hermes cron remove $id' and re-run activate."
  fi
  if [[ "$state" != "active" ]]; then
    hermes cron resume "$id" && say "• Resumed job $id (was $state)."
  fi
  if [[ "$sched" != "$SCHEDULE" ]]; then
    hermes cron edit "$id" --schedule "$SCHEDULE" && say "• Set job $id schedule to '$SCHEDULE' (was '$sched')."
  fi
  if [[ "$dlv" != "$deliver" ]]; then
    say "• Note: job $id delivers to '$dlv', not '$deliver'. Change it with Hermes's cron edit if you want; leaving it as is."
  fi
  say "• Cron job '$JOB' ($id) is in place."
}

cmd_status() {
  [[ -f "$SKILL_DIR/SKILL.md" ]] && say "skill:    installed ($(grep -m1 '^version:' "$SKILL_DIR/SKILL.md"))" || say "skill:    not installed"
  [[ -x "$CRON_SCRIPT" ]] && say "script:   $CRON_SCRIPT" || say "script:   missing"
  if command -v hermes >/dev/null; then
    local jobs; if jobs="$(cron_jobs)"; then
      if [[ -n "$jobs" ]]; then while IFS=$'\t' read -r id state sched dlv script mode; do say "cron job: $id [$state] $sched → $dlv ($script, $mode)"; done <<<"$jobs"
      else say "cron job: not found"; fi
    else say "cron job: unknown (couldn't list jobs)"; fi
  else say "cron job: hermes CLI not found"; fi
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
  local jobs
  if ! jobs="$(cron_jobs)"; then say "• Couldn't list cron jobs reliably (see message above)"
  elif [[ -z "$jobs" ]]; then say "• Cron job '$JOB' not registered (run: setup.sh activate)"
  else
    local n; n=$(grep -c . <<<"$jobs")
    local id; id=$(head -1 <<<"$jobs" | cut -f1)
    if [[ "$n" -gt 1 ]]; then say "• WARNING: $n jobs named '$JOB' ($(cut -f1 <<<"$jobs" | tr '\n' ' '))"
    else say "• Cron job '$JOB' is registered ✓ ($(head -1 <<<"$jobs" | cut -f2,3,4 | tr '\t' ' ')). Run history: hermes cron runs $id"; fi
  fi
}

cmd_uninstall() {
  local revoke="" delcfg=""
  for a in "$@"; do case "$a" in
    --revoke) revoke=y ;; --keep-token) revoke=n ;;
    --delete-config) delcfg=y ;; --keep-config) delcfg=n ;;
    *) die "unknown uninstall option: $a" ;;
  esac; done
  if command -v hermes >/dev/null; then
    local jobs
    if jobs="$(cron_jobs)"; then
      for id in $(cut -f1 <<<"$jobs"); do hermes cron remove "$id" && say "• Removed cron job $id ('$JOB')"; done
    else
      say "• Couldn't list cron jobs; remove '$JOB' yourself with 'hermes cron list' then 'hermes cron remove <id>'"
    fi
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
