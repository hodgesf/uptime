#!/usr/bin/env bash
# Deploy the latest uptime monitor code on the server.
#   Usage (on the server): ./update_uptime.sh
# Backs up the DB, pulls, reinstalls deps if requirements.txt changed,
# restarts uptime.service and confirms it came back up.
set -euo pipefail

APP_DIR=/home/ubuntu/uptime
SERVICE=uptime
BACKUP_DIR="$HOME/uptime-backups"
KEEP_BACKUPS=10

# Everything runs inside main(), which bash reads in full before running it,
# so the `git pull` below can safely replace this file mid-run.
main() {
    cd "$APP_DIR"

    if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
        echo "Local changes in $APP_DIR; commit or discard them first:" >&2
        git status --short --untracked-files=no >&2
        exit 1
    fi

    # Back up the DB with SQLite's backup API, which gives a consistent copy even
    # while the service is writing (a plain cp can catch it mid-write).
    mkdir -p "$BACKUP_DIR"
    backup="$BACKUP_DIR/uptime.db.$(date +%Y%m%d-%H%M%S)"
    venv/bin/python - "$backup" <<'EOF'
import sqlite3, sys
src = sqlite3.connect("uptime.db")
with sqlite3.connect(sys.argv[1]) as dst:
    src.backup(dst)
EOF
    echo "DB backed up to $backup"
    ls -1t "$BACKUP_DIR"/uptime.db.* | tail -n +$((KEEP_BACKUPS + 1)) | xargs -r rm --

    old_rev=$(git rev-parse HEAD)
    git pull --ff-only
    new_rev=$(git rev-parse HEAD)

    if [[ "$old_rev" == "$new_rev" ]]; then
        echo "Already up to date ($(git log -1 --format='%h %s')); restarting anyway."
    else
        echo "Updated:"
        git log --oneline "$old_rev..$new_rev"
        if ! git diff --quiet "$old_rev" "$new_rev" -- requirements.txt; then
            echo "requirements.txt changed; installing dependencies..."
            venv/bin/pip install -r requirements.txt
        fi
    fi

    sudo systemctl restart "$SERVICE"
    sleep 5

    if systemctl is-active --quiet "$SERVICE"; then
        echo "$SERVICE is running. Recent logs:"
        journalctl -u "$SERVICE" --since "-15s" --no-pager | tail -20
        echo "Follow logs with: journalctl -u $SERVICE -f"
    else
        echo "$SERVICE failed to start. Logs:" >&2
        journalctl -u "$SERVICE" -n 40 --no-pager >&2
        echo >&2
        echo "To roll back:" >&2
        echo "  sudo systemctl stop $SERVICE" >&2
        echo "  cd $APP_DIR && git checkout $old_rev && cp $backup uptime.db" >&2
        echo "  sudo systemctl start $SERVICE" >&2
        exit 1
    fi
}

main "$@"
