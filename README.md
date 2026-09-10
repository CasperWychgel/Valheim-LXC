# Valheim LXC

<div align="center">

**Your own Valheim realm on Proxmox or Debian — installed with SteamCMD and managed from a clean web panel.**

[![Debian 13](https://img.shields.io/badge/Debian-13-A81D33?logo=debian&logoColor=white)](https://www.debian.org/)
[![Proxmox VE](https://img.shields.io/badge/Proxmox-VE-E57000?logo=proxmox&logoColor=white)](https://www.proxmox.com/)
[![SteamCMD](https://img.shields.io/badge/SteamCMD-Valheim-1B2838?logo=steam&logoColor=white)](https://developer.valvesoftware.com/wiki/SteamCMD)
[![Architecture](https://img.shields.io/badge/Architecture-amd64-4C8BF5)](#before-you-start)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

One installer. Automatic updates and backups. Player-aware maintenance. No Docker stack required.

</div>

---

**Jump to:** [Proxmox quick start](#quick-start-for-proxmox-ve) ·
[Proxmox installation](#option-a-create-a-valheim-lxc-on-proxmox-ve) ·
[Debian installation](#option-b-install-on-an-existing-debian-13-system) ·
[Admin panel](#what-the-admin-panel-gives-you) ·
[Let players join](#let-players-join) ·
[Troubleshooting](#troubleshooting) · [FAQ](#faq)

## Build the realm. Skip the server chores.

Valheim LXC turns a clean Debian 13 environment into a ready-to-play dedicated
Valheim server. The preferred installation creates its own unprivileged LXC on
Proxmox VE. The same server stack can also be installed inside an existing,
dedicated Debian 13 VM or machine.

After installation you get:

- Valheim Dedicated Server installed through Valve's official SteamCMD archive
- A private, responsive web panel in English (United States), UTF-8
- Start, stop, clean restart, update check, and backup controls
- Player activity, platform IDs, bans, allowlists, and admin lists
- Safe world creation, import, switching, download, and deletion
- Automatic backups and player-aware daily maintenance
- A hardened service layout with separate game and panel accounts
- IPv4-only networking with IPv6 disabled

> [!IMPORTANT]
> This project supports **Debian 13 on amd64/x86-64 only**. ARM64 Proxmox hosts
> and ARM64 Debian systems cannot run the Valheim server binary used here.

## Pick your installation path

| I have... | Recommended path | What happens |
|---|---|---|
| A Proxmox VE host | [Create a new LXC](#option-a-create-a-valheim-lxc-on-proxmox-ve) | Creates and configures an unprivileged Debian 13 container, then installs the complete server stack |
| A clean Debian 13 VM or dedicated machine | [Install on Debian](#option-b-install-on-an-existing-debian-13-system) | Keeps the existing OS and installs SteamCMD, Valheim, services, backups, and the panel |
| An ARM64 host | Not supported | Use an amd64/x86-64 host or VM instead |

## Quick start for Proxmox VE

Open the shell of your Proxmox VE node, become `root`, and run:

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/FuBoByte/Valheim-LXC/main/install.sh)"
```

The installer asks for the container name, network, disk size, CPU, and memory.
It shows a complete summary before it creates anything.

When it finishes, keep the displayed values:

```text
Admin panel:     http://CONTAINER_IP:2460
Panel login:     admin
Panel password:  generated during installation
Game endpoint:   CONTAINER_IP:2456
Game password:   generated during installation
```

Open the panel, sign in, and your realm is ready to configure.

> [!TIP]
> Prefer reviewing scripts before running them? Use the
> [inspect-first installation](#inspect-first-installation) below.

## What the admin panel gives you

| Area | What you can do |
|---|---|
| **Overview** | View server health, address, build, CPU, memory, disk, uptime, and run server actions |
| **Players** | See online and known players, copy platform IDs or kick commands, and manage bans |
| **Settings** | Change the server name, password, visibility, Crossplay, save policy, preset, and world modifiers |
| **Access** | Maintain Valheim administrator, ban, and allow lists |
| **Worlds** | Create a fresh world, import Valheim 1.0 archives or legacy world pairs, migrate, switch safely, download, back up, or delete inactive worlds |
| **Backups** | Create, upload, validate, download, restore, and delete portable server-data archives |
| **Logs** | Read recent Valheim service output without opening a shell |
| **Security** | Change the panel password and control player-name visibility on the sign-in page |

The sign-in page can show the server name, online state, current player count,
and only the names of players currently online. Player IDs and history remain
behind authentication. Online player names can be hidden under **Security**.

## Quality-of-life automation

### Player-aware daily maintenance

Maintenance begins at **05:00 server time**. If players are still connected,
it waits 30 minutes and checks again. Once the server is empty, it checks Steam
for a newer Valheim build.

- Update available: install it and perform one clean restart
- No update available: perform the regular daily restart
- Player detection unavailable: fail safely and retry in 30 minutes
- Server already stopped: check for updates without starting it

A manual **Check for Valheim updates** button is also available on the Overview
page.

### Backups without world juggling

Valheim keeps its own rolling world backups. In addition, this project creates
portable `.tar.gz` archives containing all worlds and the three access lists.
The newest 30 archives are retained and can be downloaded or restored from the
panel. A downloaded archive can later be uploaded back into the Backup Vault.
Uploads are validated before they become restorable, and a restore creates a
fresh safety snapshot before replacing the server data. Server name, game
password, network settings, and panel credentials are never imported from a
portable backup.

### Safe world changes

World switches are blocked while a player is online. Before a switch, the
panel creates a snapshot, updates the active world, and restarts Valheim. If
that restart fails, it restores the previous configuration and attempts to
restart the previous world.

Valheim 1.0 stores each world in its own folder with `_main.*` metadata and
chunk files. To move an existing 1.0 world, archive exactly that complete
folder as `.tar.gz`, `.tgz`, or `.zip`, then upload it under **Worlds → Import
or migrate a world → Valheim 1.0**.

Pre-1.0 worlds remain supported. Upload the matching `WORLDNAME.db` and
`WORLDNAME.fwl` files under **Legacy → World migration**. The World Library
marks the pair as **Ready to migrate**. When you choose **Migrate & activate**,
the panel takes a snapshot and lets the current Valheim server perform its own
official conversion during startup.

The panel never reimplements or modifies Valheim's world format. It never
overwrites an existing world, the active world cannot be deleted, and automatic
`*_backup_auto-*` folders stay out of the playable World Library. Valheim does
not document a dedicated-server startup option for entering a seed directly,
so the workflow avoids presenting a setting the server does not officially
provide.

## Before you start

### Supported platform

- Proxmox VE with an amd64/x86-64 host, or a dedicated amd64 Debian 13 system
- Root access
- Internet access for Debian packages, SteamCMD, and Valheim
- A local IPv4 address for the server
- SSD-backed storage strongly recommended

### Suggested starting resources

These are practical project defaults for a vanilla server with up to ten
players, not a guarantee for every world or mod collection.

| Resource | Starting point |
|---|---:|
| CPU | 4 modern cores |
| Memory | 4 GB RAM |
| Disk | 60 GB SSD-backed storage |
| Swap | 512 MB |
| Architecture | amd64/x86-64 |

Large worlds and modded servers may need additional memory and storage.

### Default ports

| Purpose | Protocol | Default | Exposure |
|---|---|---:|---|
| Valheim game traffic | UDP | `2456-2457` | Forward to the server for Internet players when using the Steam backend |
| Admin panel | TCP | `2460` | Trusted LAN or VPN only; do not expose directly to the Internet |

## Option A: Create a Valheim LXC on Proxmox VE

This is the easiest and most isolated route. The installer:

1. Refreshes the Proxmox appliance catalog.
2. Selects the newest Debian 13 amd64 standard template.
3. Creates an unprivileged, auto-starting LXC with nesting enabled.
4. Configures IPv4 and disables IPv6 in both LXC configuration and guest.
5. Installs SteamCMD, Valheim, the service accounts, maintenance, backups, and
   the admin panel.
6. Verifies that Valheim and the panel services are running.

### Interactive installation

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/FuBoByte/Valheim-LXC/main/install.sh)"
```

You will be asked for:

- Container ID and hostname
- Interface name and Proxmox bridge
- DHCP or static IPv4 address, subnet, and gateway
- Optional VLAN ID
- Root disk size
- CPU cores and memory

Proxmox storage is selected automatically. Storage, swap, and custom game or
panel ports can be supplied through command-line flags or environment variables.

### Inspect-first installation

```bash
curl -fsSLo /tmp/valheim-lxc-install.sh \
  https://raw.githubusercontent.com/FuBoByte/Valheim-LXC/main/install.sh
less /tmp/valheim-lxc-install.sh
bash /tmp/valheim-lxc-install.sh
```

### Install from a local checkout

```bash
git clone https://github.com/FuBoByte/Valheim-LXC.git
cd Valheim-LXC
chmod +x install.sh
./install.sh
```

### Automated Proxmox example

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

Run `./install.sh --help` for every available flag. The same values can be
provided as environment variables. `REPO_RAW` can point to another branch or a
fork for testing.

## Option B: Install on an existing Debian 13 system

Use this path for a clean, dedicated Debian 13 VM or machine where you want to
manage the operating system yourself.

> [!WARNING]
> `setup.sh` disables IPv6 system-wide, installs system packages, creates users,
> writes systemd units, and uses `/opt/valheim` plus `/etc/valheim`. Use a
> dedicated VM or server. Review the script before running it on a machine that
> already hosts other workloads.

### From a cloned repository

```bash
sudo bash setup.sh
```

### Standalone online installation

Download the script first so you can supply a secure game password:

```bash
curl -fsSLo /tmp/valheim-setup.sh \
  https://raw.githubusercontent.com/FuBoByte/Valheim-LXC/main/setup.sh

sudo SERVER_NAME="My Valheim Realm" \
  WORLD_NAME="Dedicated" \
  GAME_PASSWORD="choose-a-game-password" \
  PANEL_USERNAME="admin" \
  bash /tmp/valheim-setup.sh
```

If `PANEL_PASSWORD` is omitted, a strong panel password is generated and shown
at the end. Other supported variables include `GAME_PORT`, `PANEL_PORT`, and
`REPO_RAW`.

Existing `/etc/valheim/server.env` and `/etc/valheim/panel.json` files are
preserved when setup is run again.

## Your first five minutes after installation

1. Save the generated game and panel credentials.
2. Open `http://SERVER_IP:2460` from your trusted network.
3. Sign in and change the panel password under **Security**.
4. Review **Settings**, then restart Valheim if you changed launch options.
5. Create or import your preferred world under **Worlds**.
6. Add administrator platform IDs under **Access**.
7. Configure router and firewall rules only if friends connect over the
   Internet.

## Let players join

### Steam backend

The Steam backend is enabled by default. Players connect to the server's IPv4
address and base game port. For players outside your network, forward UDP
`2456-2457`—or your configured base port and the following port—to the server.

Do not forward TCP `2460`; that is the management panel.

### Crossplay backend

Enable **Crossplay backend** under Settings and restart the server. Players can
then use the PlayFab join code or the public server address. Valheim's Crossplay
mode does not support joining through a local IP address or loopback address.

The installer enables the Proxmox firewall flag on the LXC interface but does
not invent Datacenter, Node, or Guest firewall policies. Apply rules that match
your own Proxmox and network design.

## Day-to-day administration

Most tasks belong in the web panel. For shell-based diagnosis inside the LXC or
Debian system:

```bash
systemctl status valheim valheim-panel
journalctl -u valheim -n 100 --no-pager
journalctl -u valheim-panel -n 100 --no-pager
valheim-panel-password admin 'a-new-password-with-12-or-more-characters'
```

### Refresh an existing installation from GitHub

Run the current setup again. Existing game and panel configuration is retained:

```bash
# Inside the Debian system or LXC
curl -fsSL https://raw.githubusercontent.com/FuBoByte/Valheim-LXC/main/setup.sh \
  | sudo bash
```

From a Proxmox host, replace `101` with the correct container ID:

```bash
pct exec 101 -- bash -lc \
  'curl -fsSL https://raw.githubusercontent.com/FuBoByte/Valheim-LXC/main/setup.sh | bash'
```

## Troubleshooting

### The LXC does not start

```bash
dpkg --print-architecture
pct config 101
pct start 101 --debug
```

The host must report `amd64`. Debian 13 uses systemd 257, so the LXC requires
`nesting=1`; the current installer enables it before the first start.

### The panel does not open

```bash
pct exec 101 -- systemctl status valheim-panel --no-pager
pct exec 101 -- curl -4 -I http://127.0.0.1:2460/login
```

If the local request works, check the container IPv4 address, VLAN, Proxmox
firewall, and your client network.

### Valheim is not running

```bash
pct exec 101 -- systemctl status valheim --no-pager
pct exec 101 -- journalctl -u valheim -n 100 --no-pager
```

SteamCMD installation output is stored in
`/opt/valheim/logs/steamcmd-install.log`.

### Player names do not appear

The panel derives identities from Valheim's own connection log. A player must
complete a connection at least once before appearing in local history.

```bash
pct exec 101 -- grep -Ei \
  'handshake|character|disconnect|socket|peer' \
  /opt/valheim/logs/valheim.log | tail -n 100
```

### A world switch is refused

This is expected while players are online or when the panel cannot safely
verify player activity. Wait for the server to become empty and refresh the
Worlds page. A Valheim 1.0 import must contain a complete world folder; a
legacy import requires a matching `.db` and `.fwl` pair.

## Security model

- The LXC is unprivileged and dedicated to the game server.
- Valheim and the panel run as separate non-root service accounts.
- The panel cannot execute arbitrary root commands; privileged operations go
  through the root-owned, allow-listed `valheimctl` helper.
- Panel passwords use salted PBKDF2-HMAC-SHA256 hashes. Plaintext panel
  passwords are not stored.
- State-changing forms require CSRF tokens.
- HTML responses include a restrictive Content Security Policy.
- Uploaded world names, settings, backup names, and platform IDs are validated.
- World and backup archives reject path traversal, links, duplicate paths,
  unsupported layouts, incomplete saves, and excessive expanded sizes.
- Portable backups intentionally exclude server settings and panel credentials.

The panel is still a powerful control plane: it can stop the game, switch
worlds, and restore backups. Keep it on a trusted LAN or behind a VPN.

Proxmox nesting exposes some host `procfs` and `sysfs` content to the guest. The
container therefore remains unprivileged and should run only the intended
server stack.

## Important paths

```text
/opt/valheim/steamcmd/          SteamCMD runtime
/opt/valheim/server/            Valheim server application
/opt/valheim/data/              Worlds and access lists
/opt/valheim/backups/           Portable server archives
/opt/valheim/logs/              SteamCMD and Valheim logs
/opt/valheim/panel/             Admin panel
/var/lib/valheim-panel/         Local player activity database
/etc/valheim/server.env         Valheim launch settings
/etc/valheim/panel.json         Hashed panel credentials and session secret
```

## Technical notes and limitations

- SteamCMD uses a 32-bit bootstrapper, so the installer enables Debian's `i386`
  architecture and installs the required 32-bit runtime libraries.
- Valve's official SteamCMD archive is used directly. The installer does not
  add Debian `non-free` repository components.
- Debian 12 repository notes may still be useful background, but Debian 12 is
  not a supported target for this project.
- Vanilla Valheim does not expose a remote RCON interface. The panel can copy an
  in-game `kick PLAYERNAME` command, but an administrator must run it from the
  in-game F5 console.
- Character levels and active Forsaken powers are not present in the dedicated
  server connection log, so the panel does not invent or guess them.
- The installer preserves a partially created container after a failure so its
  logs remain available for diagnosis.

## FAQ

<details>
<summary><strong>Can I use an existing Valheim world?</strong></summary>

Yes. Upload a complete Valheim 1.0 world-folder archive under **Worlds**. For a
pre-1.0 save, upload its matching `.db` and `.fwl` files; selecting it lets
Valheim migrate the save during startup. World changes are available only while
the server is empty.

</details>

<details>
<summary><strong>Can I enter a world seed directly in the panel?</strong></summary>

No direct seed field is offered because the official dedicated-server startup
options do not document one. Create the seed in a Valheim client and import the
resulting world pair.

</details>

<details>
<summary><strong>Will setup overwrite my current server password or panel login?</strong></summary>

No. Re-running setup preserves existing `server.env` and `panel.json` files.

</details>

<details>
<summary><strong>Does the server update while people are playing?</strong></summary>

Automatic daily maintenance waits until no players are online. A manually
requested update from the panel can restart the server when an update is
available, so the panel displays a warning when players are connected.

</details>

<details>
<summary><strong>Can I expose the admin panel to the Internet?</strong></summary>

It is intentionally designed for a trusted LAN or VPN. Do not directly
port-forward the panel's TCP port.

</details>

## Project and license

This repository is maintained as a non-profit, community-oriented project and
is released under the [MIT License](LICENSE). The non-profit statement describes
the maintainers' intent; it does not restrict the permissions granted by MIT,
including commercial use.

Contributions and focused bug reports are welcome through the repository's
[GitHub issue tracker](https://github.com/FuBoByte/Valheim-LXC/issues).

### References

- [Valheim dedicated server guide](https://valheim.com/support/a-guide-to-dedicated-servers/)
- [Valve SteamCMD documentation](https://developer.valvesoftware.com/wiki/SteamCMD#Linux)
- [Proxmox `pct` documentation](https://pve.proxmox.com/pve-docs/pct.1.html)
- [Proxmox community Debian LXC script](https://community-scripts.org/scripts/debian)

Valheim is a trademark of Iron Gate AB. This project is not affiliated with or
endorsed by Valve, Iron Gate, Coffee Stain Publishing, or Proxmox Server
Solutions GmbH.
