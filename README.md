# CloudBox

A Discord control plane for LXC containers. Members get a real Linux box with
root SSH inside an unprivileged container; operators get nodes, quotas, leases,
moderation and an audit trail.

This is an original implementation, not a reskin: slash commands instead of
prefix commands, an ephemeral per-user console instead of button-heavy embeds,
a signed agent protocol instead of `?api_key=` query strings, and instance
passwords encrypted at rest.

## What it does

| Area | Behaviour |
| --- | --- |
| Provisioning | `lxc init` → isolation profile → boot → SSH bootstrap → reverse tunnel |
| Access | password login as `root` through a pinggy.io tunnel |
| Power | start / stop / restart from the console |
| Rebuild | wipe and redeploy any configured image, ports are re-attached |
| Ports | publish a container port on the host for TCP and UDP |
| Sharing | owner grants another member console access |
| Quotas | per-member instance / memory / disk / port limits |
| Leases | optional expiry that stops and locks containers automatically |
| Nodes | local host plus remote agents, capacity aware placement |
| Audit | every operator action stored and mirrored to a channel |

## Requirements

* Python 3.11+
* LXD or Incus installed on the host, with `lxc` on `PATH`
* A storage pool (`lxc storage list`)
* A Discord application with a bot token

## Install

```bash
git clone <your-repo> cloudbox && cd cloudbox
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
$EDITOR .env
python run.py
```

Minimum `.env`:

```dotenv
DISCORD_TOKEN=...
GUILD_ID=123456789012345678
OWNER_IDS=111111111111111111
```

`GUILD_ID` makes commands appear instantly. Without it they are registered
globally and can take up to an hour to show up.

Invite the bot with the `bot` and `applications.commands` scopes. It needs the
**Server Members** privileged intent enabled in the developer portal.

## First run

1. `/panel` posts the member-facing panel in the current channel.
2. `/vps new memory:2048 vcpu:2 disk:20` creates a container as staff.
3. `/staff node-list` shows placement and load.
4. `/staff quota-set member:@someone instances:3 memory:6144 disk:60 ports:6`
   opens self-service for that member, if `SELF_SERVICE=true`.

## Commands

**Everyone**

| Command | Purpose |
| --- | --- |
| `/vps list` | picker → console for anything you own or share |
| `/vps new` | request a container (staff, or members when `SELF_SERVICE=true`) |
| `/vps console <name>` | open a console directly |
| `/vps password <name>` | rotate the root password |
| `/vps reconnect <name>` | re-establish the SSH tunnel |
| `/vps share` / `/vps unshare` | grant or revoke a member's console access |
| `/ports add <name> <port>` | publish a container port |
| `/ports list` / `/ports remove` | inspect or delete forwards |
| `/guide`, `/status`, `/ping` | help, health, latency |

**Staff**

| Command | Purpose |
| --- | --- |
| `/staff node-add` | register the local host or a remote agent |
| `/staff node-list`, `node-toggle`, `node-remove` | node administration |
| `/staff quota-set`, `quota-show` | limits and usage |
| `/staff suspend`, `unsuspend` | lock or unlock a container |
| `/staff extend <name> <hours>` | push out a lease |
| `/staff inspect <name>` | everything about one instance |
| `/staff audit` | recent operator actions |

Console buttons: **Start**, **Stop**, **Restart**, **Login details**, **Ports**,
**New password**, **Share**, **Rebuild**, **Delete**. Destructive buttons ask you
to type the container name before anything happens.

## Remote nodes

On the machine that will host containers:

```bash
pip install aiohttp psutil
AGENT_SECRET='<secret printed by /staff node-add>' \
AGENT_HOST=127.0.0.1 AGENT_PORT=8099 python agent/agent.py
```

Put TLS in front of it and register the public URL:

```
/staff node-add name:eu-1 region:Frankfurt endpoint:https://eu-1.example.com capacity:30
```

The agent accepts only `lxc` argv lists, verifies an HMAC-SHA256 signature with a
five minute replay window, and buffers commands through a semaphore so a burst of
creates cannot fork-bomb the host. The bot never shells out to build remote
commands.

## Security notes

Read these before you hand boxes to strangers.

* **Privileged mode is off by default.** `security.nesting=true` (the `docker`
  option) is safe enough for normal use. `ALLOW_PRIVILEGED=true` plus the
  `privileged` flag disables AppArmor and capability dropping, which makes
  container escape *easy*. Only enable it if every member is trusted.
* **Root passwords are encrypted at rest** with a key in `data/secret.key`.
  Back that file up together with the database, and never commit either.
* **Tunnels are public.** pinggy hands out an address that anyone with the URL
  and password can reach. Rotate passwords with `/vps password`.
* **Run the bot as a user that can run `lxc`**, not as a shell for arbitrary
  members. The bot only issues `lxc` commands, but `lxc` on the host is root
  equivalent by design.
* **The agent must not face the internet directly.** HMAC covers integrity, not
  confidentiality or replay of an eavesdropped body within the window.

## Layout

```
run.py                     entrypoint
cloudbox/
  config.py                env → Settings
  store.py                 SQLite schema + queries
  vault.py                 Fernet encryption for secrets
  nodes.py                 local and remote LXC executors
  provision.py             lifecycle: create/rebuild/power/ports
  tunnel.py                pinggy reverse tunnels
  ui.py                    embeds, panel, console, modals
  bot.py                   service wiring, error handling
  cogs/core.py             panel, status, lease reaper
  cogs/vps.py              member commands
  cogs/staff.py            operator commands
agent/agent.py             remote node agent
```
