#!/bin/sh
# The issue fixer's box at start: its GitHub key from the secret, its own clone of the private repo on the
# volume, then the loop (a run every quarter hour). The key secret is unset before the loop starts, so
# neither the fixer nor its Claude Code session ever holds it in its environment.
set -e
mkdir -p /root/.ssh /data/state /data/work
if [ -n "$ISSUE_FIXER_GITHUB_KEY_B64" ]; then
  printf '%s' "$ISSUE_FIXER_GITHUB_KEY_B64" | base64 -d > /root/.ssh/id_ed25519
  chmod 600 /root/.ssh/id_ed25519
fi
unset ISSUE_FIXER_GITHUB_KEY_B64
# The public release (Ty, 2026-11-24: a release whenever a fix goes out): a second deploy key, the only one
# that can push to the PUBLIC repo, used through its own host name so it never stands in for the private one.
if [ -n "$ISSUE_FIXER_PUBLIC_KEY_B64" ]; then
  printf '%s' "$ISSUE_FIXER_PUBLIC_KEY_B64" | base64 -d > /root/.ssh/id_public_release
  chmod 600 /root/.ssh/id_public_release
  printf 'Host github-public\n  HostName github.com\n  HostKeyAlias github.com\n  User git\n  IdentityFile /root/.ssh/id_public_release\n  IdentitiesOnly yes\n' > /root/.ssh/config
  chmod 600 /root/.ssh/config
fi
unset ISSUE_FIXER_PUBLIC_KEY_B64
# GitHub's published ed25519 host key, pinned rather than trusted on first sight.
echo "github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl" > /root/.ssh/known_hosts
git config --global user.name "SiftStack issue fixer"
git config --global user.email "siftstack-issue-fixer@users.noreply.github.com"
git config --global core.autocrlf false
if [ ! -d /data/repo/.git ]; then
  git clone --branch agents-infra git@github.com:DataSift-Ty-Personal/SiftStack-private.git /data/repo
fi
if [ -f /root/.ssh/id_public_release ]; then
  git -C /data/repo remote remove public 2>/dev/null || true
  git -C /data/repo remote add public git@github-public:DataSift-Ty-Personal/SiftStack.git
fi
exec python -u /app/tools/issue_fixer/fixer.py loop
