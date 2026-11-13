#!/usr/bin/env bash
# Install the gold report and health timers for the current user. Run on the machine that runs the bot.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p ~/.config/systemd/user
cp deploy/systemd/gold-*.service deploy/systemd/gold-*.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now gold-report.timer gold-health.timer
loginctl show-user "$USER" -p Linger
systemctl --user list-timers --no-pager | grep -E "gold|NEXT"
