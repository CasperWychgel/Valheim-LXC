# Valheim LXC for Proxmox VE

Create an unprivileged Debian 13 LXC, install SteamCMD and Valheim Dedicated
Server, and manage it through a private English (United States), UTF-8 web
panel.

Project repository: [FuBoByte/Valheim-LXC](https://github.com/FuBoByte/Valheim-LXC)

The project is built around the public SteamCMD, Valheim, Debian, and Proxmox
interfaces documented by their respective maintainers. It is maintained as a
non-profit, community-oriented repository and uses a permissive MIT license.
The English-only base can be adapted for other SteamCMD game servers.

## What it installs

- The newest Debian 13 standard template currently offered by `pveam`.
- An unprivileged, auto-starting LXC with a configurable hostname, interface,
  IP/subnet, gateway, VLAN, and root disk size.
- IPv6 disabled in both the Proxmox network configuration and Debian guest.
- The `en_US.UTF-8` locale at both operating-system and service level.
- A dedicated `valheim` service account and a separately unprivileged
  `valheim-panel` account.
- SteamCMD from Valve's official Linux archive.
- Valheim Dedicated Server (`Steam App ID 896660`) using anonymous login.
- Clean `systemd` lifecycle handling with `SIGINT`, so Valheim can save while
  stopping.
- Automatic Valheim build checks and portable archive timers.
- A responsive local admin panel on TCP 2460 by default.

The panel provides server start/stop/restart/update actions, live container
metrics, server settings and world modifiers, access lists, world upload and
selection, backup download/restore/delete, service logs, and a password-change
screen.

## Recommended resources

For a vanilla server with up to ten players:

- An amd64/x86-64 Proxmox VE host; ARM64 hosts are not supported by this project
- 4 modern CPU cores with good single-core performance
- 4 GB RAM
- 60 GB SSD-backed storage

The interactive installer uses these defaults. Increase memory to at least 8 GB
for a modded deployment. Valheim currently uses the selected base UDP port and
the next UDP port; the default range is 2456-2457.

## Online installation on a Proxmox VE host

Open the Proxmox VE shell, become `root`, and run:

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/FuBoByte/Valheim-LXC/main/install.sh)"
```

The installer keeps standard input connected to the terminal, so the interactive
questions work normally. It downloads the remaining setup and panel files from
the public `main` branch before creating the container.

If you prefer to inspect the installer first:

```bash
curl -fsSLo /tmp/valheim-lxc-install.sh \
  https://raw.githubusercontent.com/FuBoByte/Valheim-LXC/main/install.sh
less /tmp/valheim-lxc-install.sh
bash /tmp/valheim-lxc-install.sh
```

### Installation from a local checkout

Clone or copy the complete project directory to the Proxmox host, then run:

```bash
chmod +x install.sh
./install.sh
```

The interactive setup asks for:

1. Container ID and hostname
2. NIC name and Proxmox bridge
3. IPv4 address (or DHCP), subnet prefix, and gateway
4. Optional VLAN ID
5. Disk size, CPU cores, and memory

It displays a complete summary before creating anything. On success, the final
output includes the container address and generated game/panel passwords.

### Non-interactive example

```bash
./install.sh --non-interactive \
  --ctid 240 \
  --hostname valheim-prod \
  --nic-name eth0 \
  --bridge vmbr0 \
  --ip 192.168.20.40 \
  --subnet 24 \
  --gateway 192.168.20.1 \
  --vlan 20 \
  --disk 60 \
  --cores 4 \
  --memory 4096
```

Run `./install.sh --help` for every flag. The same values can be supplied as
environment variables.

### Non-interactive online example

```bash
CTID=240 \
LXC_HOSTNAME=valheim-prod \
IP_ADDRESS=192.168.20.40 \
SUBNET=24 \
GATEWAY=192.168.20.1 \
VLAN_ID=20 \
INTERACTIVE=0 \
bash -c "$(curl -fsSL https://raw.githubusercontent.com/FuBoByte/Valheim-LXC/main/install.sh)"
```

`REPO_RAW` already points to this repository's public `main` branch. Set it only
to test another branch or use a fork, for example
`REPO_RAW=https://raw.githubusercontent.com/OWNER/REPOSITORY/BRANCH`.

## Installation inside an existing Debian 13 system

The container creation and in-guest setup are deliberately separate. To install
only the server stack into an existing clean amd64 Debian 13 system:

```bash
sudo bash setup.sh
```

Without a local checkout, download and run the standalone setup script. It will
retrieve its required panel files from this repository:

```bash
curl -fsSL https://raw.githubusercontent.com/FuBoByte/Valheim-LXC/main/setup.sh \
  | sudo bash
```

Optional environment variables include `SERVER_NAME`, `WORLD_NAME`,
`GAME_PASSWORD`, `GAME_PORT`, `PANEL_USERNAME`, `PANEL_PASSWORD`, and
`PANEL_PORT`.

## Admin panel

Open `http://CONTAINER_IP:2460`. Use the generated credentials printed by the
installer, then change the panel password under **Security**.

The panel is a control plane: it can replace world files and stop the game.
Keep it on a trusted LAN or access it through a VPN. Do not forward its TCP port
directly from the Internet.

The game password and other launch settings live in
`/etc/valheim/server.env`. The panel account can edit this file but cannot run
arbitrary root commands. Privileged actions pass through the root-owned,
allow-listed `valheimctl` helper.

## Network behavior

- **IPv6:** disabled; the container uses IPv4 only.
- **Steam backend (default):** forward the configured UDP base port and the
  following port to the container when Internet players must connect.
- **Crossplay backend:** enable Crossplay in the panel, restart the server, and
  use the PlayFab join code or public address. Local-IP and loopback joining are
  not supported by Valheim in Crossplay mode.
- **Panel:** TCP 2460 by default, private LAN or VPN only.

The installer enables the Proxmox firewall flag on the LXC interface but does
not create Datacenter/Node/Guest firewall policy. Apply rules appropriate for
your Proxmox environment.

Debian 13 uses systemd 257, so the installer enables the Proxmox LXC
`nesting=1` feature. Proxmox documents that nesting exposes some host `procfs`
and `sysfs` contents to the guest; the container therefore remains
unprivileged and should only run the intended server stack.

## Container startup troubleshooting

If an older installer selected a template whose name ends in `_arm64.tar.zst`,
that container cannot be used for this Valheim installation. Check the host and
failed container with:

```bash
dpkg --print-architecture
pct config 101
pct start 101 --debug
```

The host must report `amd64`. The current installer filters explicitly for a
Debian 13 `_amd64` template, enables nesting before the first start, and stops
immediately if the container cannot start.

## Operations and recovery

```bash
# Run inside the LXC
systemctl status valheim valheim-panel
journalctl -u valheim -n 100 --no-pager
valheim-panel-password admin 'A-new-password-with-12-or-more-characters'
```

Important paths:

```text
/opt/valheim/steamcmd/          SteamCMD runtime
/opt/valheim/server/            Valheim server application
/opt/valheim/data/              Saved worlds and access lists
/opt/valheim/backups/           Portable .tar.gz archives (30 retained)
/opt/valheim/logs/              SteamCMD and Valheim file logs
/opt/valheim/panel/             English admin panel
/etc/valheim/server.env         Server launch settings
/etc/valheim/panel.json         Hashed panel credentials and session secret
```

The game itself also creates its own rolling backups according to the values in
**Settings → Built-in world backups**. The separate archive timer packages the
world directory and access lists every two hours for download or restoration.

## Design notes

- The installer never deletes a partially created container automatically after
  an error. This preserves logs and avoids destructive cleanup surprises.
- Existing server and panel configuration files are preserved when `setup.sh`
  is run again.
- World names, uploaded filenames, backup names, settings, and platform IDs are
  validated before reaching the filesystem or privileged helper.
- HTML responses use a restrictive Content Security Policy and state-changing
  requests require a session CSRF token.
- Panel passwords use PBKDF2-HMAC-SHA256 with a random salt. Plaintext panel
  passwords are not stored.

## References

- [Valve SteamCMD documentation](https://developer.valvesoftware.com/wiki/SteamCMD#Linux)
- [Valheim dedicated server guide](https://valheim.com/support/a-guide-to-dedicated-servers/)
- [Proxmox `pct` documentation](https://pve.proxmox.com/pve-docs/pct.1.html)
- [Proxmox community Debian LXC script](https://community-scripts.org/scripts/debian)

Valheim is a trademark of Iron Gate AB. This project is not affiliated with or
endorsed by Valve, Iron Gate, Coffee Stain Publishing, or Proxmox Server
Solutions GmbH.
