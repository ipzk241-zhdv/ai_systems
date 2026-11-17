#!/usr/bin/env bash
# One-time setup: bash mkr/deploy/bootstrap_vm.sh /path/to/deployment-key.pub
set -euo pipefail

repo=/home/ubuntu/ai_systems
script_dir="$(cd -- "$(dirname -- "$0")" && pwd)"
public_key="${1:?Provide the deployment public-key file (.pub)}"

test "$(id -un)" = ubuntu
test -d "$repo/.git"
test "$(systemctl show news-bot --property=WorkingDirectory --value)" = "$repo/mkr"
test -f "$public_key"
read -r key_type key_data key_comment < "$public_key"
test "$key_type" = ssh-ed25519
test -n "$key_data"

sudo install -D -o root -g root -m 0755 "$script_dir/deploy_vm.py" /usr/local/lib/news-bot/deploy_vm.py
wrapper="$(mktemp)"
trap 'rm -f "$wrapper"' EXIT
cat > "$wrapper" <<'WRAPPER'
#!/bin/sh
exec /usr/bin/python3 -B /usr/local/lib/news-bot/deploy_vm.py
WRAPPER
sudo install -o root -g root -m 0755 "$wrapper" /usr/local/bin/news-bot-deploy

mkdir -p "$HOME/.ssh"
chmod 700 "$HOME/.ssh"
touch "$HOME/.ssh/authorized_keys"
chmod 600 "$HOME/.ssh/authorized_keys"
if ! grep -Fq -- "$key_data" "$HOME/.ssh/authorized_keys"; then
  printf '\nrestrict,command="/usr/local/bin/news-bot-deploy" %s %s github-actions-news-bot\n' \
    "$key_type" "$key_data" >> "$HOME/.ssh/authorized_keys"
fi
mkdir -p "$HOME/.local/state/news-bot-deploy"
chmod 700 "$HOME/.local/state/news-bot-deploy"
echo 'Deployment handler and restricted SSH public key installed. Bot was not restarted.'
