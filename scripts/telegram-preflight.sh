#!/usr/bin/env bash
# Checks whether this machine is ready to run the Telegram channel for Claude Code.
# Safe to run at any point, before or after setup. Never prints your bot token.
# See docs/TELEGRAM_CHANNEL.md

set -uo pipefail

STATE_DIR="${TELEGRAM_STATE_DIR:-$HOME/.claude/channels/telegram}"
pass=0; warn=0; fail=0

if [ -t 1 ]; then
  G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[31m'; D=$'\033[2m'; N=$'\033[0m'
else
  G=""; Y=""; R=""; D=""; N=""
fi

ok()   { printf '%s  ok  %s %s\n' "$G" "$N" "$1"; pass=$((pass+1)); }
note() { printf '%s warn %s %s\n' "$Y" "$N" "$1"; warn=$((warn+1)); }
bad()  { printf '%s fail %s %s\n' "$R" "$N" "$1"; fail=$((fail+1)); }
hint() { printf '       %s%s%s\n' "$D" "$1" "$N"; }

echo "Telegram channel preflight"
echo "state dir: $STATE_DIR"
echo

# 1. Bun — the channel server is a Bun script and will not start without it.
if command -v bun >/dev/null 2>&1; then
  ok "Bun $(bun --version 2>/dev/null)"
else
  bad "Bun not found"
  hint "curl -fsSL https://bun.sh/install | bash"
fi

# 2. Claude Code.
if command -v claude >/dev/null 2>&1; then
  ok "Claude Code $(claude --version 2>/dev/null | head -1)"
else
  bad "claude not found on PATH"
  hint "https://code.claude.com/docs/en/quickstart"
fi

# 3. Bot token. Presence only — the value is never read or printed.
if [ -f "$STATE_DIR/.env" ] && grep -q '^TELEGRAM_BOT_TOKEN=.' "$STATE_DIR/.env" 2>/dev/null; then
  ok "bot token configured"
elif [ -n "${TELEGRAM_BOT_TOKEN:-}" ]; then
  ok "bot token set in the environment"
else
  bad "no bot token"
  hint "/telegram:configure <token>   (get one from @BotFather)"
fi

# 4. Access policy. This is the security-relevant check: 'pairing' hands a
#    pairing code to any stranger who finds the bot's username.
acl="$STATE_DIR/access.json"
if [ -f "$acl" ]; then
  if command -v jq >/dev/null 2>&1; then
    policy=$(jq -r '.dmPolicy // "pairing"' "$acl" 2>/dev/null)
    allowed=$(jq -r '(.allowFrom // []) | length' "$acl" 2>/dev/null)
  else
    policy=$(sed -n 's/.*"dmPolicy"[[:space:]]*:[[:space:]]*"\([a-z]*\)".*/\1/p' "$acl" | head -1)
    policy="${policy:-pairing}"
    allowed="?"
  fi

  case "$policy" in
    allowlist) ok  "DM policy: allowlist ($allowed approved)" ;;
    disabled)  note "DM policy: disabled — nothing gets through, including you" ;;
    *)         note "DM policy: $policy — strangers who find the bot get a pairing code"
               hint "/telegram:access policy allowlist   (do this once you are paired)" ;;
  esac
else
  note "no access.json yet — expected until your first pairing"
fi

# 5. Plugin install. Path is not contractual, so this is advisory only.
if find "$HOME/.claude" -maxdepth 4 -type d -name telegram 2>/dev/null | grep -q plugins; then
  ok "telegram plugin present"
else
  note "could not confirm the plugin is installed"
  hint "run /plugin in Claude Code to check"
fi

echo
printf '%s passed, %s warnings, %s blocking\n' "$pass" "$warn" "$fail"
if [ "$fail" -eq 0 ]; then
  echo
  echo "Launch with:"
  echo "  claude --channels plugin:telegram@claude-plugins-official --permission-mode acceptEdits"
fi
exit $(( fail > 0 ? 1 : 0 ))
