#!/usr/bin/env bash
# Installs SteamCMD, Valheim Dedicated Server, and the local admin panel.
# Run as root inside a fresh Debian 13 system, or let install.sh invoke it.
set -Eeuo pipefail

VALHEIM_HOME=${VALHEIM_HOME:-/opt/valheim}
SCRIPT_DIR=""
if [[ -n ${BASH_SOURCE[0]:-} && -f ${BASH_SOURCE[0]:-} ]]; then
  SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
fi
SOURCE_DIR=${SOURCE_DIR:-$SCRIPT_DIR}
DEFAULT_REPO_RAW="https://raw.githubusercontent.com/FuBoByte/Valheim-LXC/main"
REPO_RAW=${REPO_RAW:-$DEFAULT_REPO_RAW}
REPO_RAW=${REPO_RAW%/}
APP_ID=896660
SERVER_NAME=${SERVER_NAME:-Valheim Dedicated Server}
WORLD_NAME=${WORLD_NAME:-Dedicated}
GAME_PASSWORD=${GAME_PASSWORD:-Valheim123}
GAME_PORT=${GAME_PORT:-2456}
PANEL_USERNAME=${PANEL_USERNAME:-admin}
PANEL_PASSWORD=${PANEL_PASSWORD:-}
PANEL_PORT=${PANEL_PORT:-2460}

green='\033[1;32m'
yellow='\033[1;33m'
red='\033[1;31m'
reset='\033[0m'
step=0
total_steps=9

say() { step=$((step + 1)); printf "%b[%s/%s]%b %s\n" "$green" "$step" "$total_steps" "$reset" "$*"; }
die() { printf "%bError:%b %s\n" "$red" "$reset" "$*" >&2; exit 1; }
trap 'printf "\033[1;31mSetup failed at line %s.\033[0m\n" "$LINENO" >&2' ERR

[[ $(id -u) -eq 0 ]] || die "Run setup.sh as root."
[[ -r /etc/os-release ]] || die "Cannot identify the operating system."
# shellcheck disable=SC1091
. /etc/os-release
if [[ ${ID:-} != debian || ${VERSION_ID:-} != 13* ]]; then
  [[ ${ALLOW_UNSUPPORTED_OS:-0} == 1 ]] || die "Debian 13 is required (detected: ${PRETTY_NAME:-unknown})."
fi
[[ $(dpkg --print-architecture) == amd64 ]] || die "Valheim Dedicated Server requires an amd64 container."
if [[ ! $GAME_PORT =~ ^[0-9]+$ ]] || ((GAME_PORT < 1024 || GAME_PORT > 65533)); then die "GAME_PORT is invalid."; fi
if [[ ! $PANEL_PORT =~ ^[0-9]+$ ]] || ((PANEL_PORT < 1024 || PANEL_PORT > 65535)); then die "PANEL_PORT is invalid."; fi
(( ${#GAME_PASSWORD} >= 5 )) || die "The Valheim game password must contain at least five characters."
(( ${#GAME_PASSWORD} <= 64 )) || die "The Valheim game password must not exceed 64 characters."
[[ $GAME_PASSWORD != *$'\n'* && $GAME_PASSWORD != *$'\r'* ]] || die "The Valheim game password contains a line break."
[[ -n $SERVER_NAME && ${#SERVER_NAME} -le 64 && $SERVER_NAME != *$'\n'* && $SERVER_NAME != *$'\r'* ]] || die "SERVER_NAME is invalid."
[[ $WORLD_NAME =~ ^[A-Za-z0-9][A-Za-z0-9._\ -]{0,63}$ ]] || die "WORLD_NAME is invalid."

if [[ -z $PANEL_PASSWORD ]]; then
  random_source=$(head -c 256 /dev/urandom | base64 | tr -dc 'A-Za-z0-9!@#%+=' || true)
  PANEL_PASSWORD=${random_source:0:18}
fi
(( ${#PANEL_PASSWORD} >= 12 )) || die "The panel password must contain at least 12 characters."

say "Configuring the en_US.UTF-8 locale and disabling IPv6"
export DEBIAN_FRONTEND=noninteractive
export LANG=C.UTF-8
export LC_ALL=C.UTF-8
apt-get update -qq
apt-get install -y -qq --no-install-recommends locales ca-certificates curl procps >/dev/null
if grep -Eq '^# *en_US.UTF-8 UTF-8' /etc/locale.gen; then
  sed -i -E 's/^# *(en_US.UTF-8 UTF-8)/\1/' /etc/locale.gen
elif ! grep -Eq '^en_US.UTF-8 UTF-8' /etc/locale.gen; then
  printf 'en_US.UTF-8 UTF-8\n' >>/etc/locale.gen
fi
locale-gen en_US.UTF-8 >/dev/null
update-locale LANG=en_US.UTF-8 LANGUAGE=en_US:en
cat >/etc/sysctl.d/99-valheim-disable-ipv6.conf <<'EOF'
net.ipv6.conf.all.disable_ipv6 = 1
net.ipv6.conf.default.disable_ipv6 = 1
net.ipv6.conf.lo.disable_ipv6 = 1
EOF
if [[ -d /proc/sys/net/ipv6 ]]; then
  if ! sysctl -q -p /etc/sysctl.d/99-valheim-disable-ipv6.conf; then
    die "IPv6 could not be disabled inside the container."
  fi
fi

say "Installing Steam and Valheim runtime dependencies"
dpkg --add-architecture i386
apt-get update -qq
apt-get install -y -qq --no-install-recommends \
  bash coreutils findutils grep gawk sed util-linux \
  tar gzip unzip jq sudo \
  lib32gcc-s1 lib32stdc++6 libc6-i386 \
  libatomic1 libpulse0 libpulse-dev libpulse-mainloop-glib0 \
  python3 python3-flask python3-waitress python3-psutil >/dev/null

say "Creating dedicated service accounts and directories"
getent group valheim-admin >/dev/null 2>&1 || groupadd --system valheim-admin
if ! id -u valheim >/dev/null 2>&1; then
  useradd --system --create-home --home-dir "$VALHEIM_HOME" --shell /usr/sbin/nologin valheim
fi
if ! id -u valheim-panel >/dev/null 2>&1; then
  useradd --system --home-dir "$VALHEIM_HOME/panel" --shell /usr/sbin/nologin valheim-panel
fi
usermod -a -G valheim-admin valheim
usermod -a -G valheim-admin valheim-panel
install -d -o valheim -g valheim-admin -m 2770 \
  "$VALHEIM_HOME" \
  "$VALHEIM_HOME/steamcmd" \
  "$VALHEIM_HOME/server" \
  "$VALHEIM_HOME/data" \
  "$VALHEIM_HOME/data/worlds_local" \
  "$VALHEIM_HOME/backups" \
  "$VALHEIM_HOME/logs" \
  "$VALHEIM_HOME/bin"
install -d -o valheim-panel -g valheim-admin -m 0750 \
  "$VALHEIM_HOME/panel" \
  "$VALHEIM_HOME/panel/templates" \
  "$VALHEIM_HOME/panel/static" \
  /var/lib/valheim-panel
install -d -o root -g valheim-admin -m 2770 /etc/valheim

say "Installing SteamCMD from Valve"
if [[ ! -x $VALHEIM_HOME/steamcmd/steamcmd.sh ]]; then
  steam_archive=$(mktemp)
  if ! curl --retry 3 --retry-delay 2 --connect-timeout 15 -fsSL \
    https://steamcdn-a.akamaihd.net/client/installer/steamcmd_linux.tar.gz \
    -o "$steam_archive"; then
    rm -f "$steam_archive"
    die "Could not download SteamCMD from Valve."
  fi
  chown valheim:valheim-admin "$steam_archive"
  chmod 0600 "$steam_archive"
  if ! runuser -u valheim -- tar -xzf "$steam_archive" -C "$VALHEIM_HOME/steamcmd"; then
    rm -f "$steam_archive"
    die "The SteamCMD archive could not be extracted as the valheim user."
  fi
  rm -f "$steam_archive"
fi
chown -R valheim:valheim-admin "$VALHEIM_HOME/steamcmd"

say "Installing Valheim Dedicated Server with anonymous Steam login"
printf "%bNote:%b The Valheim download may take several minutes. Please keep this window open.\n" \
  "$yellow" "$reset"
# The first SteamCMD invocation may update and re-exec itself. Warm it up before app_update.
runuser -u valheim -- env HOME="$VALHEIM_HOME" LANG=en_US.UTF-8 \
  "$VALHEIM_HOME/steamcmd/steamcmd.sh" +login anonymous +quit \
  >"$VALHEIM_HOME/logs/steamcmd-install.log" 2>&1 || true
runuser -u valheim -- env HOME="$VALHEIM_HOME" LANG=en_US.UTF-8 \
  "$VALHEIM_HOME/steamcmd/steamcmd.sh" \
  +force_install_dir "$VALHEIM_HOME/server" \
  +login anonymous \
  +app_update "$APP_ID" validate \
  +quit 2>&1 | tee -a "$VALHEIM_HOME/logs/steamcmd-install.log"
[[ -x $VALHEIM_HOME/server/valheim_server.x86_64 ]] || die "SteamCMD did not install the Valheim server successfully. See $VALHEIM_HOME/logs/steamcmd-install.log."

# SteamCMD is a 32-bit bootstrapper even on amd64 Debian. Keep the conventional
# Steam SDK paths available for Valheim and other dedicated server binaries.
install -d -o valheim -g valheim-admin -m 0750 \
  "$VALHEIM_HOME/.steam" \
  "$VALHEIM_HOME/.steam/sdk32" \
  "$VALHEIM_HOME/.steam/sdk64"
for sdk_bits in 32 64; do
  steam_client="$VALHEIM_HOME/steamcmd/linux$sdk_bits/steamclient.so"
  if [[ -f $steam_client ]]; then
    runuser -u valheim -- ln -sfn "$steam_client" \
      "$VALHEIM_HOME/.steam/sdk$sdk_bits/steamclient.so"
    runuser -u valheim -- ln -sfn "$steam_client" \
      "$VALHEIM_HOME/.steam/sdk$sdk_bits/steamservice.so"
  fi
done

say "Writing the server configuration and maintenance tools"
SERVER_CONFIG_CREATED=0
if [[ ! -f /etc/valheim/server.env ]]; then
  SERVER_CONFIG_CREATED=1
  env_quote() {
    local value=$1
    value=${value//\\/\\\\}
    value=${value//\"/\\\"}
    printf '"%s"' "$value"
  }
  {
    printf 'SERVER_NAME=%s\n' "$(env_quote "$SERVER_NAME")"
    printf 'WORLD_NAME=%s\n' "$(env_quote "$WORLD_NAME")"
    printf 'SERVER_PASSWORD=%s\n' "$(env_quote "$GAME_PASSWORD")"
    printf 'GAME_PORT="%s"\n' "$GAME_PORT"
    cat <<'EOF'
PUBLIC="0"
CROSSPLAY="0"
SAVE_INTERVAL="1800"
BACKUPS="4"
BACKUP_SHORT="7200"
BACKUP_LONG="43200"
PRESET=""
COMBAT=""
DEATH_PENALTY=""
RESOURCES=""
RAIDS=""
PORTALS=""
NO_BUILD_COST="0"
PLAYER_EVENTS="0"
PASSIVE_MOBS="0"
NO_MAP="0"
EOF
  } >/etc/valheim/server.env
fi
chown root:valheim-admin /etc/valheim/server.env
chmod 0660 /etc/valheim/server.env

for access_file in adminlist bannedlist permittedlist; do
  if [[ ! -f $VALHEIM_HOME/data/$access_file.txt ]]; then
    printf '// One platform user ID per line.\n' >"$VALHEIM_HOME/data/$access_file.txt"
  fi
done
chown -R valheim:valheim-admin "$VALHEIM_HOME/data" "$VALHEIM_HOME/backups" "$VALHEIM_HOME/logs"
chmod 2770 "$VALHEIM_HOME/data" "$VALHEIM_HOME/data/worlds_local" "$VALHEIM_HOME/backups" "$VALHEIM_HOME/logs"
find "$VALHEIM_HOME/data" -type f -exec chmod 0660 {} +

cat >"$VALHEIM_HOME/bin/start-server" <<'EOF'
#!/usr/bin/env bash
set -Eeuo pipefail
: "${SERVER_NAME:=Valheim Dedicated Server}"
: "${WORLD_NAME:=Dedicated}"
: "${SERVER_PASSWORD:=Valheim123}"
: "${GAME_PORT:=2456}"
: "${PUBLIC:=0}"
: "${CROSSPLAY:=0}"
: "${SAVE_INTERVAL:=1800}"
: "${BACKUPS:=4}"
: "${BACKUP_SHORT:=7200}"
: "${BACKUP_LONG:=43200}"

cd /opt/valheim/server
export HOME=/opt/valheim
export SteamAppId=892970
export LD_LIBRARY_PATH="/opt/valheim/server/linux64:${LD_LIBRARY_PATH:-}"

bepinex_root=/opt/valheim/server/BepInEx
doorstop_dir=/opt/valheim/server/doorstop_libs
doorstop_lib="$doorstop_dir/libdoorstop_x64.so"
preloader_dll="$bepinex_root/core/BepInEx.Preloader.dll"

if [[ -f "$doorstop_lib" && -f "$preloader_dll" ]]; then
  export DOORSTOP_ENABLED=1
  export DOORSTOP_TARGET_ASSEMBLY="$preloader_dll"
  export LD_LIBRARY_PATH="$doorstop_dir:${LD_LIBRARY_PATH}"
  export LD_PRELOAD="libdoorstop_x64.so${LD_PRELOAD:+:$LD_PRELOAD}"
  bepinex_state=enabled
else
  # Ensure vanilla start when BepInEx files are missing or incomplete.
  unset DOORSTOP_ENABLED DOORSTOP_TARGET_ASSEMBLY
  bepinex_state=disabled
  if [[ -d "$bepinex_root" || -d "$doorstop_dir" ]]; then
    printf 'BepInEx files look incomplete; starting vanilla without Doorstop.\n' >&2
  fi
fi

args=(
  -nographics
  -batchmode
  -name "$SERVER_NAME"
  -port "$GAME_PORT"
  -world "$WORLD_NAME"
  -password "$SERVER_PASSWORD"
  -savedir /opt/valheim/data
  -public "$PUBLIC"
  -saveinterval "$SAVE_INTERVAL"
  -backups "$BACKUPS"
  -backupshort "$BACKUP_SHORT"
  -backuplong "$BACKUP_LONG"
  -logFile /opt/valheim/logs/valheim.log
)
[[ ${CROSSPLAY:-0} == 1 ]] && args+=(-crossplay)
[[ -z ${PRESET:-} ]] || args+=(-preset "$PRESET")
[[ -z ${COMBAT:-} ]] || args+=(-modifier Combat "$COMBAT")
[[ -z ${DEATH_PENALTY:-} ]] || args+=(-modifier DeathPenalty "$DEATH_PENALTY")
[[ -z ${RESOURCES:-} ]] || args+=(-modifier Resources "$RESOURCES")
[[ -z ${RAIDS:-} ]] || args+=(-modifier Raids "$RAIDS")
[[ -z ${PORTALS:-} ]] || args+=(-modifier Portals "$PORTALS")
[[ ${NO_BUILD_COST:-0} == 1 ]] && args+=(-setkey nobuildcost)
[[ ${PLAYER_EVENTS:-0} == 1 ]] && args+=(-setkey playerevents)
[[ ${PASSIVE_MOBS:-0} == 1 ]] && args+=(-setkey passivemobs)
[[ ${NO_MAP:-0} == 1 ]] && args+=(-setkey nomap)

printf 'Starting Valheim: name=%q world=%q port=%q public=%q crossplay=%q bepinex=%q\n' \
  "$SERVER_NAME" "$WORLD_NAME" "$GAME_PORT" "$PUBLIC" "$CROSSPLAY" "$bepinex_state"
exec ./valheim_server.x86_64 "${args[@]}"
EOF

cat >"$VALHEIM_HOME/bin/backup-server" <<'EOF'
#!/usr/bin/env bash
set -Eeuo pipefail
source_dir=/opt/valheim/data
backup_dir=/opt/valheim/backups
timestamp=$(date -u +%Y%m%dT%H%M%SZ)
target="$backup_dir/valheim-$timestamp.tar.gz"
temporary="$target.partial"
install -d -o valheim -g valheim-admin -m 2770 "$backup_dir"
tar -czf "$temporary" -C "$source_dir" worlds_local adminlist.txt bannedlist.txt permittedlist.txt
mv "$temporary" "$target"
chown valheim:valheim-admin "$target"
chmod 0660 "$target"
find "$backup_dir" -maxdepth 1 -type f -name 'valheim-*.tar.gz' -printf '%T@ %p\n' \
  | sort -nr | awk 'NR > 30 {sub(/^[^ ]+ /, ""); print}' | xargs -r rm -f --
printf 'Created %s\n' "$target"
EOF

cat >"$VALHEIM_HOME/bin/update-server" <<'EOF'
#!/usr/bin/env bash
set -Eeuo pipefail
mode=${1:-manual}
[[ $# -le 1 && $mode =~ ^(manual|--defer-if-players)$ ]] \
  || { echo "Usage: update-server [--defer-if-players]" >&2; exit 2; }
app_id=896660
home=/opt/valheim
manifest="$home/server/steamapps/appmanifest_$app_id.acf"
installed=$(grep -m1 -oE '"buildid"[[:space:]]+"[0-9]+"' "$manifest" 2>/dev/null \
  | grep -oE '[0-9]+' || true)

# Read the public branch metadata without changing the installed server files.
# SteamCMD emits Valve Data Format; the state transitions below select only the
# public branch and return its first numeric build ID.
metadata_file=$(mktemp)
trap 'rm -f "$metadata_file"' EXIT
runuser -u valheim -- env HOME="$home" LANG=en_US.UTF-8 \
  "$home/steamcmd/steamcmd.sh" \
  +login anonymous +app_info_update 1 +app_info_print "$app_id" +quit \
  >"$metadata_file" 2>/dev/null
latest=$(awk '
  /^[[:space:]]*"branches"[[:space:]]*$/ { branches_pending = 1; next }
  branches_pending && /^[[:space:]]*\{/ { in_branches = 1; branches_pending = 0; next }
  in_branches && /^[[:space:]]*"public"[[:space:]]*$/ { public_pending = 1; next }
  public_pending && /^[[:space:]]*\{/ { in_public = 1; public_pending = 0; next }
  in_public && /"buildid"[[:space:]]*"[0-9]+"/ {
    line = $0
    sub(/^.*"buildid"[[:space:]]*"/, "", line)
    sub(/".*$/, "", line)
    print line
    exit
  }
' "$metadata_file")
if [[ -n $installed && -n $latest && $installed == "$latest" ]]; then
  printf 'Valheim is up to date (build %s).\n' "$installed"
  exit 0
fi
[[ -n $latest ]] || { echo 'Could not determine the current Steam build.' >&2; exit 1; }
if [[ $mode == --defer-if-players ]]; then
  if ! online_players=$(runuser -u valheim-panel -- env \
    VALHEIM_PANEL_CONFIG=/etc/valheim/panel.json \
    VALHEIM_SERVER_CONFIG=/etc/valheim/server.env \
    VALHEIM_HOME=/opt/valheim \
    VALHEIM_DATA_DIR=/opt/valheim/data \
    VALHEIM_LOG=/opt/valheim/logs/valheim.log \
    VALHEIM_PLAYER_DB=/var/lib/valheim-panel/players.sqlite3 \
    python3 /opt/valheim/panel/app.py --online-player-count); then
    echo 'Could not verify the online player count; update deferred.' >&2
    exit 75
  fi
  if ((online_players > 0)); then
    printf 'Update deferred because %s player(s) are online.\n' "$online_players"
    exit 75
  fi
fi
was_active=0
systemctl is-active --quiet valheim.service && was_active=1
((was_active == 0)) || systemctl stop valheim.service
if ! runuser -u valheim -- env HOME="$home" LANG=en_US.UTF-8 \
  "$home/steamcmd/steamcmd.sh" +force_install_dir "$home/server" \
  +login anonymous +app_update "$app_id" validate +quit \
  >"$home/logs/steamcmd-update.log" 2>&1; then
  ((was_active == 0)) || systemctl start valheim.service
  echo "Steam update failed. See $home/logs/steamcmd-update.log." >&2
  exit 1
fi
((was_active == 0)) || systemctl start valheim.service
printf 'Updated Valheim from build %s to build %s.\n' "${installed:-unknown}" "$latest"
EOF

cat >"$VALHEIM_HOME/bin/daily-maintenance" <<'EOF'
#!/usr/bin/env bash
set -Eeuo pipefail
home=/opt/valheim
state_file=/var/lib/valheim-panel/daily-maintenance-date
today=$(date +%F)
hour=$(date +%H)

# The timer runs every 30 minutes. Before 05:00, and after a successful
# maintenance run on the same server-local date, there is nothing to do.
((10#$hour >= 5)) || exit 0
[[ $(cat "$state_file" 2>/dev/null || true) != "$today" ]] || exit 0

online_player_count() {
  runuser -u valheim-panel -- env \
    VALHEIM_PANEL_CONFIG=/etc/valheim/panel.json \
    VALHEIM_SERVER_CONFIG=/etc/valheim/server.env \
    VALHEIM_HOME=/opt/valheim \
    VALHEIM_DATA_DIR=/opt/valheim/data \
    VALHEIM_LOG=/opt/valheim/logs/valheim.log \
    VALHEIM_PLAYER_DB=/var/lib/valheim-panel/players.sqlite3 \
    python3 /opt/valheim/panel/app.py --online-player-count
}

defer_maintenance() {
  printf 'Daily maintenance deferred. The next check is in 30 minutes.\n'
  exit 0
}

if ! online_players=$(online_player_count); then
  echo 'Daily maintenance could not verify player activity.' >&2
  defer_maintenance
fi
if ((online_players > 0)); then
  printf 'Daily maintenance deferred because %s player(s) are online.\n' "$online_players"
  defer_maintenance
fi

was_active=0
systemctl is-active --quiet valheim.service && was_active=1
set +e
update_output=$("$home/bin/update-server" --defer-if-players 2>&1)
update_status=$?
set -e
printf '%s\n' "$update_output"
((update_status != 75)) || defer_maintenance
((update_status == 0)) || exit "$update_status"

updated=0
grep -q '^Updated Valheim from build ' <<<"$update_output" && updated=1
if ((was_active == 1 && updated == 0)); then
  if ! online_players=$(online_player_count); then
    echo 'Daily restart could not recheck player activity.' >&2
    defer_maintenance
  fi
  if ((online_players > 0)); then
    printf 'Daily restart deferred because %s player(s) connected during the update check.\n' "$online_players"
    defer_maintenance
  fi
  systemctl restart valheim.service
  echo 'Daily Valheim restart completed.'
elif ((was_active == 1)); then
  echo "The update restart also completed today's daily restart."
else
  echo 'Valheim was already stopped; the update check completed without starting it.'
fi

temporary=$(mktemp "$state_file.XXXXXX")
printf '%s\n' "$today" >"$temporary"
chown valheim-panel:valheim-admin "$temporary"
chmod 0640 "$temporary"
mv -f "$temporary" "$state_file"
EOF

cat >/usr/local/sbin/valheimctl <<'EOF'
#!/usr/bin/env bash
set -Eeuo pipefail
action=${1:-}
argument=${2:-}
backup_dir=/opt/valheim/backups
data_dir=/opt/valheim/data

case "$action" in
  start|stop|restart)
    [[ $# -eq 1 ]] || { echo "Unexpected argument." >&2; exit 2; }
    systemctl "$action" valheim.service
    ;;
  update)
    [[ $# -eq 1 ]] || { echo "Unexpected argument." >&2; exit 2; }
    /opt/valheim/bin/update-server
    ;;
  backup)
    [[ $# -eq 1 ]] || { echo "Unexpected argument." >&2; exit 2; }
    /opt/valheim/bin/backup-server
    ;;
  restore)
    [[ $# -eq 2 && $argument =~ ^valheim-[0-9]{8}T[0-9]{6}Z\.tar\.gz$ ]] || { echo "Invalid backup name." >&2; exit 2; }
    archive="$backup_dir/$argument"
    [[ -f $archive ]] || { echo "Backup does not exist." >&2; exit 2; }
    staging=$(mktemp -d /opt/valheim/.restore.XXXXXX)
    rollback=$(mktemp -d /opt/valheim/.restore-rollback.XXXXXX)
    [[ $staging == /opt/valheim/.restore.* && $rollback == /opt/valheim/.restore-rollback.* ]] \
      || { echo "Unsafe restore workspace." >&2; exit 2; }
    was_active=0
    systemctl is-active --quiet valheim.service && was_active=1
    restore_complete=0
    cleanup_restore() {
      status=$?
      set +e
      if ((restore_complete == 0)); then
        if [[ -d $rollback/data ]]; then
          [[ ! -e $data_dir ]] || mv -- "$data_dir" "$rollback/failed-data"
          mv -- "$rollback/data" "$data_dir"
          chown -R valheim:valheim-admin "$data_dir"
        fi
        ((was_active == 0)) || systemctl start valheim.service
      fi
      rm -rf -- "$staging" "$rollback"
      exit "$status"
    }
    trap cleanup_restore EXIT
    archive_copy="$staging/portable-backup.tar.gz"
    install -o root -g root -m 0600 "$archive" "$archive_copy"
    python3 /opt/valheim/panel/app.py --validate-backup-archive "$archive_copy"
    ((was_active == 0)) || systemctl stop valheim.service
    /opt/valheim/bin/backup-server
    tar --extract --gzip --file "$archive_copy" --directory "$staging" \
      --no-same-owner --no-same-permissions
    rm -f -- "$archive_copy"
    for item in worlds_local adminlist.txt bannedlist.txt permittedlist.txt; do
      [[ -e $staging/$item ]] || { echo "Backup is missing $item." >&2; exit 2; }
    done
    mv -- "$data_dir" "$rollback/data"
    mv -- "$staging" "$data_dir"
    chown -R valheim:valheim-admin "$data_dir"
    find "$data_dir" -type d -exec chmod 2770 {} +
    find "$data_dir" -type f -exec chmod 0660 {} +
    ((was_active == 0)) || systemctl start valheim.service
    restore_complete=1
    printf 'Restored %s after creating a safety snapshot. Server and panel settings were unchanged.\n' "$argument"
    ;;
  logs)
    [[ $# -eq 1 ]] || { echo "Unexpected argument." >&2; exit 2; }
    journalctl -u valheim.service --no-pager -n 250
    ;;
  *)
    echo "Usage: valheimctl {start|stop|restart|update|backup|restore NAME|logs}" >&2
    exit 2
    ;;
esac
EOF

chmod 0750 "$VALHEIM_HOME/bin/start-server" "$VALHEIM_HOME/bin/backup-server" \
  "$VALHEIM_HOME/bin/update-server" "$VALHEIM_HOME/bin/daily-maintenance"
chown root:valheim-admin "$VALHEIM_HOME/bin/start-server" "$VALHEIM_HOME/bin/backup-server" \
  "$VALHEIM_HOME/bin/update-server" "$VALHEIM_HOME/bin/daily-maintenance"
chmod 0755 /usr/local/sbin/valheimctl
chown root:root /usr/local/sbin/valheimctl

cat >/etc/sudoers.d/valheim-panel <<'EOF'
valheim-panel ALL=(root) NOPASSWD: /usr/local/sbin/valheimctl *
EOF
chmod 0440 /etc/sudoers.d/valheim-panel
visudo -cf /etc/sudoers.d/valheim-panel >/dev/null

say "Installing the English admin panel"
copy_asset() {
  local relative=$1 destination=$2
  if [[ -n $SOURCE_DIR && -f $SOURCE_DIR/$relative ]]; then
    install -o valheim-panel -g valheim-admin -m 0640 "$SOURCE_DIR/$relative" "$destination"
  else
    if ! curl --retry 3 --retry-delay 2 --connect-timeout 15 -fsSL \
      "$REPO_RAW/$relative" -o "$destination"; then
      die "Could not download panel asset: $relative"
    fi
    chown valheim-panel:valheim-admin "$destination"
    chmod 0640 "$destination"
  fi
}
copy_asset panel/app.py "$VALHEIM_HOME/panel/app.py"
copy_asset panel/templates/login.html "$VALHEIM_HOME/panel/templates/login.html"
copy_asset panel/templates/dashboard.html "$VALHEIM_HOME/panel/templates/dashboard.html"
copy_asset panel/static/style.css "$VALHEIM_HOME/panel/static/style.css"
copy_asset panel/static/app.js "$VALHEIM_HOME/panel/static/app.js"

PANEL_CONFIG_CREATED=0
if [[ ! -f /etc/valheim/panel.json ]]; then
  PANEL_CONFIG_CREATED=1
  PANEL_USERNAME="$PANEL_USERNAME" PANEL_PASSWORD="$PANEL_PASSWORD" python3 - <<'PY'
import hashlib
import json
import os
import secrets
from pathlib import Path

salt = secrets.token_bytes(16)
iterations = 390000
password = os.environ["PANEL_PASSWORD"].encode("utf-8")
digest = hashlib.pbkdf2_hmac("sha256", password, salt, iterations)
config = {
    "username": os.environ["PANEL_USERNAME"],
    "password_hash": f"pbkdf2_sha256${iterations}${salt.hex()}${digest.hex()}",
    "session_secret": secrets.token_urlsafe(48),
    "show_player_names_on_login": True,
}
Path("/etc/valheim/panel.json").write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
PY
fi
chown valheim-panel:valheim-admin /etc/valheim/panel.json
chmod 0600 /etc/valheim/panel.json

cat >/usr/local/sbin/valheim-panel-password <<'EOF'
#!/usr/bin/env bash
set -Eeuo pipefail
[[ $# -eq 2 ]] || { echo "Usage: valheim-panel-password USERNAME NEW_PASSWORD" >&2; exit 2; }
(( ${#2} >= 12 )) || { echo "Password must contain at least 12 characters." >&2; exit 2; }
runuser -u valheim-panel -- env \
  VALHEIM_PANEL_CONFIG=/etc/valheim/panel.json \
  python3 /opt/valheim/panel/app.py --set-password "$1" "$2"
systemctl restart valheim-panel.service
EOF
chmod 0750 /usr/local/sbin/valheim-panel-password

say "Creating and enabling system services"
cat >/etc/systemd/system/valheim.service <<'EOF'
[Unit]
Description=Valheim Dedicated Server
Documentation=https://valheim.com/support/a-guide-to-dedicated-servers/
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=valheim
Group=valheim-admin
Environment=HOME=/opt/valheim
Environment=LANG=en_US.UTF-8
EnvironmentFile=/etc/valheim/server.env
WorkingDirectory=/opt/valheim/server
ExecStart=/opt/valheim/bin/start-server
Restart=on-failure
RestartSec=10
KillSignal=SIGINT
TimeoutStopSec=120
LimitNOFILE=100000
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ReadWritePaths=/opt/valheim

[Install]
WantedBy=multi-user.target
EOF

cat >/etc/systemd/system/valheim-panel.service <<EOF
[Unit]
Description=Valheim Admin Panel
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=valheim-panel
Group=valheim-admin
SupplementaryGroups=valheim-admin
Environment=LANG=en_US.UTF-8
Environment=VALHEIM_PANEL_PORT=$PANEL_PORT
Environment=VALHEIM_PANEL_CONFIG=/etc/valheim/panel.json
Environment=VALHEIM_LOG=/opt/valheim/logs/valheim.log
Environment=VALHEIM_PLAYER_DB=/var/lib/valheim-panel/players.sqlite3
WorkingDirectory=/opt/valheim/panel
ExecStart=/usr/bin/python3 -m waitress --host=0.0.0.0 --port=$PANEL_PORT app:app
Restart=always
RestartSec=5
PrivateTmp=true
ProtectHome=true
ProtectSystem=full
ReadWritePaths=/etc/valheim /opt/valheim/data /opt/valheim/backups /var/lib/valheim-panel

[Install]
WantedBy=multi-user.target
EOF

cat >/etc/systemd/system/valheim-backup.service <<'EOF'
[Unit]
Description=Create a Valheim world archive
After=local-fs.target

[Service]
Type=oneshot
ExecStart=/opt/valheim/bin/backup-server
EOF

cat >/etc/systemd/system/valheim-backup.timer <<'EOF'
[Unit]
Description=Create a Valheim world archive every two hours

[Timer]
OnBootSec=15min
OnUnitActiveSec=2h
Persistent=true

[Install]
WantedBy=timers.target
EOF

systemctl disable --now valheim-update.timer >/dev/null 2>&1 || true
rm -f /etc/systemd/system/valheim-update.timer /etc/systemd/system/valheim-update.service

cat >/etc/systemd/system/valheim-maintenance.service <<'EOF'
[Unit]
Description=Run player-aware daily Valheim maintenance
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/opt/valheim/bin/daily-maintenance
TimeoutStartSec=45min
EOF

cat >/etc/systemd/system/valheim-maintenance.timer <<'EOF'
[Unit]
Description=Check for due Valheim maintenance every 30 minutes

[Timer]
OnCalendar=*-*-* *:00:00
OnCalendar=*-*-* *:30:00
AccuracySec=1min
Persistent=true

[Install]
WantedBy=timers.target
EOF

maintenance_state=/var/lib/valheim-panel/daily-maintenance-date
if [[ ! -f $maintenance_state ]]; then
  date +%F >"$maintenance_state"
fi
chown valheim-panel:valheim-admin "$maintenance_state"
chmod 0640 "$maintenance_state"

systemctl daemon-reload
systemctl enable valheim.service valheim-panel.service valheim-backup.timer valheim-maintenance.timer >/dev/null
systemctl restart valheim-panel.service
systemctl restart valheim.service
systemctl start valheim-backup.timer valheim-maintenance.timer

say "Verifying the installation"
[[ ! -s /proc/net/if_inet6 ]] || die "IPv6 is still active inside the container."
printf "%bNote:%b Waiting up to 30 seconds for the admin panel to become ready.\n" \
  "$yellow" "$reset"
panel_ready=0
for _ in $(seq 1 30); do
  if systemctl is-active --quiet valheim-panel.service \
    && curl -4 -fs "http://127.0.0.1:$PANEL_PORT/login" >/dev/null 2>&1; then
    panel_ready=1
    break
  fi
  sleep 1
done
if ((panel_ready == 0)); then
  systemctl status valheim-panel.service --no-pager >&2 || true
  journalctl -u valheim-panel.service -n 30 --no-pager >&2 || true
  die "The admin panel did not pass its HTTP health check."
fi
if ! systemctl is-active --quiet valheim.service; then
  systemctl status valheim.service --no-pager >&2 || true
  journalctl -u valheim.service -n 30 --no-pager >&2 || true
  die "The Valheim server did not start."
fi
systemctl is-active --quiet valheim-maintenance.timer \
  || die "The daily maintenance timer is not active."

host_ip=$(hostname -I | awk '{print $1}')
if ((PANEL_CONFIG_CREATED == 1)); then
  panel_login_display=$PANEL_USERNAME
  panel_password_display=$PANEL_PASSWORD
else
  panel_login_display='<unchanged>'
  panel_password_display='<unchanged; reset with valheim-panel-password>'
fi
if ((SERVER_CONFIG_CREATED == 1)); then
  game_password_display=$GAME_PASSWORD
else
  game_password_display='<unchanged; manage it in the panel>'
fi
cat <<EOF

Valheim is installed.

  Locale:          en_US.UTF-8
  Admin panel:     http://$host_ip:$PANEL_PORT
  Panel username:  $panel_login_display
  Panel password:  $panel_password_display
  Game endpoint:   $host_ip:$GAME_PORT
  Game password:   $game_password_display
  Maintenance:     Daily from 05:00 server time; 30-minute player deferral

Reset the panel login from the LXC console with:
  valheim-panel-password USERNAME NEW_PASSWORD

EOF
