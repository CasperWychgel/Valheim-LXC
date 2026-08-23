#!/usr/bin/env bash
# Valheim LXC installer for Proxmox VE.
# Run this file as root on a Proxmox VE host.
set -Eeuo pipefail

PROJECT_NAME="Valheim LXC"
REQUIRED_ARCH="amd64"
DEFAULT_REPO_RAW="https://raw.githubusercontent.com/FuBoByte/Valheim-LXC/main"
REPO_RAW=${REPO_RAW:-$DEFAULT_REPO_RAW}
REPO_RAW=${REPO_RAW%/}
CTID=${CTID:-}
CT_HOSTNAME=${LXC_HOSTNAME:-valheim}
NIC_NAME=${NIC_NAME:-eth0}
BRIDGE=${BRIDGE:-vmbr0}
IP_ADDRESS=${IP_ADDRESS:-dhcp}
SUBNET=${SUBNET:-24}
GATEWAY=${GATEWAY:-}
VLAN_ID=${VLAN_ID:-}
DISK_GB=${DISK_GB:-60}
CORES=${CORES:-4}
MEMORY_MB=${MEMORY_MB:-4096}
SWAP_MB=${SWAP_MB:-512}
ROOTFS_STORAGE=${ROOTFS_STORAGE:-}
TEMPLATE_STORAGE=${TEMPLATE_STORAGE:-}
PANEL_PORT=${PANEL_PORT:-2460}
GAME_PORT=${GAME_PORT:-2456}
INTERACTIVE=${INTERACTIVE:-auto}

PROJECT_FILES=(
  setup.sh
  panel/app.py
  panel/templates/login.html
  panel/templates/dashboard.html
  panel/static/style.css
  panel/static/app.js
)

green='\033[1;32m'
yellow='\033[1;33m'
red='\033[1;31m'
reset='\033[0m'

info() { printf "%b==>%b %s\n" "$green" "$reset" "$*"; }
warn() { printf "%bWarning:%b %s\n" "$yellow" "$reset" "$*" >&2; }
die() { printf "%bError:%b %s\n" "$red" "$reset" "$*" >&2; exit 1; }
on_error() {
  local line=$1
  printf "%bInstallation failed at line %s.%b\n" "$red" "$line" "$reset" >&2
  if [[ -n "${CTID:-}" ]] && pct status "$CTID" >/dev/null 2>&1; then
    printf "Container %s was kept for diagnosis. It was not deleted automatically.\n" "$CTID" >&2
  fi
}
trap 'on_error "$LINENO"' ERR

usage() {
  cat <<'EOF'
Valheim LXC installer for Proxmox VE (Debian 13, en_US.UTF-8).

Usage: bash install.sh [options]

  --ctid ID                 Container ID (default: next free ID)
  --hostname NAME           Container hostname
  --nic-name NAME           Interface name inside the LXC (default: eth0)
  --bridge NAME             Proxmox bridge (default: vmbr0)
  --ip ADDRESS              IPv4 address or "dhcp"
  --subnet PREFIX           IPv4 CIDR prefix, 0-32 (default: 24)
  --gateway ADDRESS         IPv4 gateway for a static address
  --vlan ID                 Optional VLAN tag, 1-4094
  --disk GB                 Root disk size in GB (default: 60)
  --cores COUNT             CPU cores (default: 4)
  --memory MB               Memory in MB (default: 4096)
  --swap MB                 Swap in MB (default: 512)
  --storage NAME            Root filesystem storage
  --template-storage NAME   Storage used for the Debian template
  --panel-port PORT         Admin panel TCP port (default: 2460)
  --game-port PORT          Valheim base UDP port (default: 2456)
  --non-interactive         Use flags/environment values without prompts
  -h, --help                Show this help

Every setting can also be supplied as an environment variable. Online installs
download supporting files from the project's public main branch. Set REPO_RAW
only when you need to use a fork or another branch.
EOF
}

while (($#)); do
  case "$1" in
    --ctid) CTID=${2:?Missing value for --ctid}; shift 2 ;;
    --hostname) CT_HOSTNAME=${2:?Missing value for --hostname}; shift 2 ;;
    --nic-name) NIC_NAME=${2:?Missing value for --nic-name}; shift 2 ;;
    --bridge) BRIDGE=${2:?Missing value for --bridge}; shift 2 ;;
    --ip) IP_ADDRESS=${2:?Missing value for --ip}; shift 2 ;;
    --subnet) SUBNET=${2:?Missing value for --subnet}; shift 2 ;;
    --gateway) GATEWAY=${2:?Missing value for --gateway}; shift 2 ;;
    --vlan) VLAN_ID=${2:?Missing value for --vlan}; shift 2 ;;
    --disk) DISK_GB=${2:?Missing value for --disk}; shift 2 ;;
    --cores) CORES=${2:?Missing value for --cores}; shift 2 ;;
    --memory) MEMORY_MB=${2:?Missing value for --memory}; shift 2 ;;
    --swap) SWAP_MB=${2:?Missing value for --swap}; shift 2 ;;
    --storage) ROOTFS_STORAGE=${2:?Missing value for --storage}; shift 2 ;;
    --template-storage) TEMPLATE_STORAGE=${2:?Missing value for --template-storage}; shift 2 ;;
    --panel-port) PANEL_PORT=${2:?Missing value for --panel-port}; shift 2 ;;
    --game-port) GAME_PORT=${2:?Missing value for --game-port}; shift 2 ;;
    --non-interactive) INTERACTIVE=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "Unknown option: $1 (use --help)" ;;
  esac
done

command -v pct >/dev/null 2>&1 || die "pct was not found. Run this installer on a Proxmox VE host."
command -v pveam >/dev/null 2>&1 || die "pveam was not found. Run this installer on a Proxmox VE host."
[[ $(id -u) -eq 0 ]] || die "Run this installer as root."

HOST_ARCH=$(dpkg --print-architecture 2>/dev/null || true)
[[ $HOST_ARCH == "$REQUIRED_ARCH" ]] || die \
  "This project requires an amd64 (x86-64) Proxmox host. Detected host architecture: ${HOST_ARCH:-unknown}. Valheim's Linux server cannot run natively in an ARM64 LXC."

SOURCE_ROOT=""
if [[ -n ${BASH_SOURCE[0]:-} && -f ${BASH_SOURCE[0]:-} ]]; then
  SOURCE_ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd -P || true)
fi

USE_LOCAL_SOURCE=1
for relative in "${PROJECT_FILES[@]}"; do
  if [[ -z $SOURCE_ROOT || ! -f $SOURCE_ROOT/$relative ]]; then
    USE_LOCAL_SOURCE=0
    break
  fi
done

if ((USE_LOCAL_SOURCE == 0)); then
  command -v curl >/dev/null 2>&1 || die "curl is required for an online installation."
  info "Checking the online installation files"
  for relative in "${PROJECT_FILES[@]}"; do
    if ! curl --retry 3 --retry-delay 2 --connect-timeout 15 -fsSL \
      "$REPO_RAW/$relative" -o /dev/null; then
      die "Could not download $relative from $REPO_RAW. No container was created."
    fi
  done
fi

if [[ $INTERACTIVE == auto ]]; then
  if [[ -t 0 ]]; then INTERACTIVE=1; else INTERACTIVE=0; fi
fi

next_ctid=$(pvesh get /cluster/nextid 2>/dev/null || printf '100')
[[ -n $CTID ]] || CTID=$next_ctid

prompt() {
  local variable=$1 label=$2 default=$3 answer
  read -r -p "$label [$default]: " answer
  printf -v "$variable" '%s' "${answer:-$default}"
}

prompt_optional() {
  local variable=$1 label=$2 current=$3 answer
  if [[ -n $current ]]; then
    read -r -p "$label [$current]: " answer
    printf -v "$variable" '%s' "${answer:-$current}"
  else
    read -r -p "$label (leave blank for none): " answer
    printf -v "$variable" '%s' "$answer"
  fi
}

if [[ $INTERACTIVE == 1 ]]; then
  printf "\n%s — interactive setup\n\n" "$PROJECT_NAME"
  prompt CTID "Container ID" "$CTID"
  prompt CT_HOSTNAME "Hostname" "$CT_HOSTNAME"
  prompt NIC_NAME "NIC name inside the LXC" "$NIC_NAME"
  prompt BRIDGE "Proxmox network bridge" "$BRIDGE"
  prompt IP_ADDRESS "IPv4 address (or dhcp)" "$IP_ADDRESS"
  if [[ ${IP_ADDRESS,,} != dhcp ]]; then
    prompt SUBNET "Subnet prefix (CIDR)" "$SUBNET"
    prompt GATEWAY "Gateway" "$GATEWAY"
  else
    GATEWAY=""
  fi
  prompt_optional VLAN_ID "VLAN ID" "$VLAN_ID"
  prompt DISK_GB "Disk size in GB" "$DISK_GB"
  prompt CORES "CPU cores" "$CORES"
  prompt MEMORY_MB "Memory in MB" "$MEMORY_MB"
fi

[[ $CTID =~ ^[1-9][0-9]{2,8}$ ]] || die "Container ID must be a number of at least 100."
[[ $CT_HOSTNAME =~ ^[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?$ ]] || die "Hostname is not valid."
[[ $NIC_NAME =~ ^[a-zA-Z0-9_.-]{1,15}$ ]] || die "NIC name is not valid."
[[ $BRIDGE =~ ^[a-zA-Z0-9_.-]{1,32}$ ]] || die "Bridge name is not valid."
if [[ ! $DISK_GB =~ ^[0-9]+$ ]] || ((DISK_GB < 8)); then die "Disk size must be at least 8 GB."; fi
if [[ ! $CORES =~ ^[0-9]+$ ]] || ((CORES < 1 || CORES > 128)); then die "CPU cores must be between 1 and 128."; fi
if [[ ! $MEMORY_MB =~ ^[0-9]+$ ]] || ((MEMORY_MB < 2048)); then die "Memory must be at least 2048 MB."; fi
[[ $SWAP_MB =~ ^[0-9]+$ ]] || die "Swap must be a number."
if [[ ! $PANEL_PORT =~ ^[0-9]+$ ]] || ((PANEL_PORT < 1024 || PANEL_PORT > 65535)); then die "Panel port is invalid."; fi
if [[ ! $GAME_PORT =~ ^[0-9]+$ ]] || ((GAME_PORT < 1024 || GAME_PORT > 65533)); then die "Game port is invalid."; fi
if [[ -n $VLAN_ID ]]; then
  if [[ ! $VLAN_ID =~ ^[0-9]+$ ]] || ((VLAN_ID < 1 || VLAN_ID > 4094)); then die "VLAN ID must be between 1 and 4094."; fi
fi

valid_ipv4() {
  local address=$1 octet
  local -a octets
  IFS=. read -r -a octets <<<"$address"
  [[ ${#octets[@]} -eq 4 ]] || return 1
  for octet in "${octets[@]}"; do
    [[ $octet =~ ^[0-9]{1,3}$ ]] && ((10#$octet <= 255)) || return 1
  done
}

if [[ ${IP_ADDRESS,,} != dhcp ]]; then
  valid_ipv4 "$IP_ADDRESS" || die "Static IPv4 address is invalid."
  if [[ ! $SUBNET =~ ^[0-9]+$ ]] || ((SUBNET < 0 || SUBNET > 32)); then die "Subnet prefix must be between 0 and 32."; fi
  valid_ipv4 "$GATEWAY" || die "A valid gateway is required for a static address."
fi
if pct status "$CTID" >/dev/null 2>&1; then
  die "Container ID $CTID already exists."
fi

if [[ -z $ROOTFS_STORAGE ]]; then
  ROOTFS_STORAGE=$(pvesm status -content rootdir 2>/dev/null | awk 'NR == 2 {print $1}')
fi
[[ -n $ROOTFS_STORAGE ]] || die "No enabled storage accepting container root filesystems was found."

if [[ -z $TEMPLATE_STORAGE ]]; then
  TEMPLATE_STORAGE=$(pvesm status -content vztmpl 2>/dev/null | awk 'NR == 2 {print $1}')
fi
[[ -n $TEMPLATE_STORAGE ]] || die "No enabled storage accepting LXC templates was found."

if [[ $INTERACTIVE == 1 ]]; then
  if [[ ${IP_ADDRESS,,} == dhcp ]]; then
    NETWORK_DISPLAY="dhcp"
  else
    NETWORK_DISPLAY="$IP_ADDRESS/$SUBNET"
  fi
  cat <<EOF

Configuration summary
  Container:  $CTID ($CT_HOSTNAME)
  Network:    $NIC_NAME on $BRIDGE, $NETWORK_DISPLAY
  Gateway:    ${GATEWAY:-automatic}
  VLAN:       ${VLAN_ID:-none}
  Resources:  $CORES cores, $MEMORY_MB MB RAM, $DISK_GB GB disk
  Storage:    $ROOTFS_STORAGE (template: $TEMPLATE_STORAGE)
  Platform:   amd64, unprivileged LXC with nesting enabled

EOF
  read -r -p "Create the container now? [Y/n]: " confirm
  [[ ${confirm:-Y} =~ ^[Yy]$ ]] || { echo "Cancelled."; exit 0; }
fi

info "Refreshing the Proxmox appliance catalog"
pveam update >/dev/null
TEMPLATE=$(pveam available --section system 2>/dev/null \
  | awk '$2 ~ /^debian-13-standard_.*_amd64\.tar\.(zst|xz|gz)$/ {print $2}' \
  | sort -V \
  | tail -n 1)
[[ -n $TEMPLATE ]] || die "No Debian 13 amd64 standard LXC template is available from pveam."

if ! pveam list "$TEMPLATE_STORAGE" 2>/dev/null | grep -Fq "/$TEMPLATE"; then
  info "Downloading the latest Debian 13 template: $TEMPLATE"
  pveam download "$TEMPLATE_STORAGE" "$TEMPLATE"
fi

NET_CONFIG="name=$NIC_NAME,bridge=$BRIDGE,firewall=1"
if [[ ${IP_ADDRESS,,} == dhcp ]]; then
  NET_CONFIG+=",ip=dhcp,ip6=manual"
else
  NET_CONFIG+=",ip=$IP_ADDRESS/$SUBNET,gw=$GATEWAY,ip6=manual"
fi
[[ -z $VLAN_ID ]] || NET_CONFIG+=",tag=$VLAN_ID"

info "Creating unprivileged Debian 13 amd64 LXC $CTID"
pct create "$CTID" "$TEMPLATE_STORAGE:vztmpl/$TEMPLATE" \
  --arch "$REQUIRED_ARCH" \
  --ostype debian \
  --hostname "$CT_HOSTNAME" \
  --cores "$CORES" \
  --memory "$MEMORY_MB" \
  --swap "$SWAP_MB" \
  --rootfs "$ROOTFS_STORAGE:$DISK_GB" \
  --net0 "$NET_CONFIG" \
  --unprivileged 1 \
  --features nesting=1 \
  --onboot 1 \
  --start 0

info "Starting container $CTID"
if ! pct start "$CTID"; then
  die "Container $CTID could not be started. Run 'pct start $CTID --debug' on the Proxmox host for the detailed cause."
fi

info "Waiting for networking inside the container"
CONTAINER_IP=""
for _ in $(seq 1 45); do
  CONTAINER_IP=$(pct exec "$CTID" -- hostname -I 2>/dev/null | awk '{print $1}') || true
  [[ -n $CONTAINER_IP ]] && break
  sleep 2
done
[[ -n $CONTAINER_IP ]] || die "The container did not receive an IP address within 90 seconds."

REMOTE_STAGE=/tmp/valheim-lxc-installer
pct exec "$CTID" -- mkdir -p "$REMOTE_STAGE/panel/templates" "$REMOTE_STAGE/panel/static"

push_local_project() {
  local relative
  for relative in "${PROJECT_FILES[@]}"; do
    [[ -f "$SOURCE_ROOT/$relative" ]] || die "Required project file is missing: $relative"
    pct push "$CTID" "$SOURCE_ROOT/$relative" "$REMOTE_STAGE/$relative"
  done
}

push_remote_project() {
  local relative temp_file
  temp_file=$(mktemp)
  for relative in "${PROJECT_FILES[@]}"; do
    if ! curl --retry 3 --retry-delay 2 --connect-timeout 15 -fsSL \
      "$REPO_RAW/$relative" -o "$temp_file"; then
      rm -f "$temp_file"
      die "Could not download $relative from $REPO_RAW."
    fi
    pct push "$CTID" "$temp_file" "$REMOTE_STAGE/$relative"
  done
  rm -f "$temp_file"
}

if ((USE_LOCAL_SOURCE == 1)); then
  push_local_project
else
  push_remote_project
fi

random_value() {
  local alphabet=$1 length=$2 value
  value=$(head -c 256 /dev/urandom | base64 | tr -dc "$alphabet")
  printf '%s' "${value:0:length}"
}
GAME_PASSWORD=${GAME_PASSWORD:-$(random_value 'A-Za-z0-9' 12)}
PANEL_PASSWORD=${PANEL_PASSWORD:-$(random_value 'A-Za-z0-9!@#%+=' 18)}

info "Installing SteamCMD, Valheim, and the admin panel"
pct exec "$CTID" -- env \
  SOURCE_DIR="$REMOTE_STAGE" \
  SERVER_NAME="${SERVER_NAME:-Valheim Dedicated Server}" \
  WORLD_NAME="${WORLD_NAME:-Dedicated}" \
  GAME_PASSWORD="$GAME_PASSWORD" \
  GAME_PORT="$GAME_PORT" \
  PANEL_USERNAME="${PANEL_USERNAME:-admin}" \
  PANEL_PASSWORD="$PANEL_PASSWORD" \
  PANEL_PORT="$PANEL_PORT" \
  bash "$REMOTE_STAGE/setup.sh"

cat <<EOF

Installation complete.

  Container:       $CTID ($CT_HOSTNAME)
  Address:         $CONTAINER_IP
  Admin panel:     http://$CONTAINER_IP:$PANEL_PORT
  Panel login:     ${PANEL_USERNAME:-admin}
  Panel password:  $PANEL_PASSWORD
  Game endpoint:   $CONTAINER_IP:$GAME_PORT (UDP $GAME_PORT-$((GAME_PORT + 1)))
  Game password:   $GAME_PASSWORD

Keep the panel on a trusted LAN or behind a VPN. For the Steam backend, forward
UDP $GAME_PORT-$((GAME_PORT + 1)) to $CONTAINER_IP when Internet access is required.
Change both generated passwords after the first login and store them securely.

EOF
