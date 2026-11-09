#!/usr/bin/env bash
# Physarum VPS installer — idempotent, no secrets, no root daemons.
# Tested on Ubuntu 24.04 (1.6 GB RAM is enough; the whole thing is stdlib + sqlite).
#
#   sudo bash scripts/install-vps.sh
#
set -euo pipefail

APP_DIR=${APP_DIR:-/opt/physarum}
DATA_DIR=${DATA_DIR:-/var/lib/physarum}
LOG_DIR=${LOG_DIR:-/var/log/physarum}
ENV_DIR=${ENV_DIR:-/etc/physarum}
SERVICE_USER=${SERVICE_USER:-physarum}
SRC_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

say "packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip git sqlite3 curl >/dev/null

say "service user ${SERVICE_USER}"
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
  useradd --system --create-home --home-dir "$DATA_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
fi

say "layout"
install -d -m 0755 "$APP_DIR" "$LOG_DIR" "$ENV_DIR"
install -d -m 0700 -o "$SERVICE_USER" -g "$SERVICE_USER" "$DATA_DIR"

say "code -> ${APP_DIR}"
if [ "$SRC_DIR" != "$APP_DIR" ]; then
  rsync -a --delete \
    --exclude .venv --exclude .git --exclude '*.db*' --exclude '.env*' \
    "$SRC_DIR"/ "$APP_DIR"/
fi

say "virtualenv"
if [ ! -x "$APP_DIR/.venv/bin/python" ]; then
  python3 -m venv "$APP_DIR/.venv"
fi
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip
"$APP_DIR/.venv/bin/pip" install -q -e "$APP_DIR"

say "environment file (fill this in, it is never committed)"
if [ ! -f "$ENV_DIR/physarum.env" ]; then
  cp "$APP_DIR/.env.example" "$ENV_DIR/physarum.env"
  sed -i "s#^# #" /dev/null 2>/dev/null || true
  perl -0pi -e "s#PHYSARUM_HOME=.*#PHYSARUM_HOME=$DATA_DIR#; s#PHYSARUM_DB=.*#PHYSARUM_DB=$DATA_DIR/physarum.db#; s#PHYSARUM_INBOX=.*#PHYSARUM_INBOX=$DATA_DIR/inbox#; s#GARMINTOKENS=.*#GARMINTOKENS=$DATA_DIR/.garminconnect#" "$ENV_DIR/physarum.env"
  chmod 600 "$ENV_DIR/physarum.env"
  echo "  created $ENV_DIR/physarum.env — add PHYSARUM_LLM_KEY there"
fi

say "database"
sudo -u "$SERVICE_USER" env \
  PHYSARUM_HOME="$DATA_DIR" PHYSARUM_DB="$DATA_DIR/physarum.db" \
  "$APP_DIR/.venv/bin/phy" init

say "systemd units"
install -m 0644 "$APP_DIR/scripts/systemd/physarum-sync.service" /etc/systemd/system/
install -m 0644 "$APP_DIR/scripts/systemd/physarum-sync.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now physarum-sync.timer

say "done"
cat <<EOF

  code     $APP_DIR
  data     $DATA_DIR/physarum.db
  env      $ENV_DIR/physarum.env      <- put PHYSARUM_LLM_KEY here
  logs     $LOG_DIR/daily.{json,err}
  timer    systemctl list-timers physarum-sync

Garmin tokens (copy from the machine that ran the device login):

  sudo install -d -m 700 -o $SERVICE_USER -g $SERVICE_USER $DATA_DIR/.garminconnect
  sudo cp ~/.garminconnect/garmin_tokens.json $DATA_DIR/.garminconnect/
  sudo chown $SERVICE_USER $DATA_DIR/.garminconnect/garmin_tokens.json
  sudo chmod 600 $DATA_DIR/.garminconnect/garmin_tokens.json

Then:

  sudo -u $SERVICE_USER env \$(cat $ENV_DIR/physarum.env | xargs) $APP_DIR/.venv/bin/phy doctor
  sudo -u $SERVICE_USER env \$(cat $ENV_DIR/physarum.env | xargs) $APP_DIR/.venv/bin/phy daily

EOF
