#!/usr/bin/env bash
# Save a sandbox key locally. No credentials are sent anywhere by this script.
set -euo pipefail
cd "$(dirname "$0")/.."
umask 077

printf '%s\n' \
  'Create a Stripe sandbox named payout-rescue and enable Connect.' \
  'Open https://dashboard.stripe.com/apikeys in that sandbox.' \
  'Copy its test secret key. Never use a live key.'
read -rs -p 'Sandbox key (input hidden): ' sandbox_key
printf '\n'
if [[ ! "$sandbox_key" =~ ^[sr]k_test_[A-Za-z0-9]+$ ]]; then
  printf '%s\n' 'Expected an sk_test_ or rk_test_ key.' >&2
  exit 1
fi
if [[ -L .env ]]; then
  printf '%s\n' 'Refusing to overwrite a symlink at .env.' >&2
  exit 1
fi
if [[ ! -e .env ]]; then
  cp .env.example .env
fi
task_env_tmp=$(mktemp .env.setup.XXXXXX)
trap 'rm -f "$task_env_tmp"' EXIT
# Preserve comments and other settings while replacing every assignment to this key.
awk '!/^[[:space:]]*(export[[:space:]]+)?STRIPE_SECRET_KEY[[:space:]]*=/' .env > "$task_env_tmp"
printf 'STRIPE_SECRET_KEY=%s\n' "$sandbox_key" >> "$task_env_tmp"
chmod 600 "$task_env_tmp"
mv "$task_env_tmp" .env
unset sandbox_key
printf '%s\n' \
  'Saved STRIPE_SECRET_KEY in .env with owner-only permissions.' \
  'Set STRIPE_GATEWAY=sdk and choose LLM_PROVIDER in .env.' \
  'Install the Stripe CLI and run stripe login, selecting the same sandbox.'
