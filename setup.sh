#!/usr/bin/env bash
#
# CloudBox installer for a Linux host that will run the bot + LXD.
#
#   ./setup.sh                 interactive, asks about systemd
#   ./setup.sh --systemd       install and start the systemd unit
#   ./setup.sh --no-systemd    never touch systemd
#   ./setup.sh --test          also run the offline smoke test
#
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

# --------------------------------------------------------------------- output
if [ -t 1 ]; then
    B=$'\033[1m'; D=$'\033[2m'; R=$'\033[31m'; G=$'\033[32m'
    Y=$'\033[33m'; C=$'\033[36m'; N=$'\033[0m'
else
    B=""; D=""; R=""; G=""; Y=""; C=""; N=""
fi

step() { printf '\n%s%s==>%s %s\n' "$B" "$C" "$N" "$*"; }
ok()   { printf '  %s✓%s %s\n' "$G" "$N" "$*"; }
note() { printf '  %s·%s %s\n' "$D" "$N" "$*"; }
warn() { printf '  %s!%s %s\n' "$Y" "$N" "$*"; }
die()  { printf '\n%s✗ %s%s\n' "$B$R" "$*" "$N" >&2; exit 1; }

usage() {
    cat <<'EOF'
CloudBox installer

Usage: ./setup.sh [options]

  --systemd      install and start cloudbox.service without asking
  --no-systemd   skip the systemd question entirely
  --test         run tests/smoke.py after installing dependencies
  -h, --help     show this text

The script is safe to re-run: it never overwrites an existing .env or .venv.
EOF
}

# ---------------------------------------------------------------------- flags
WANT_SYSTEMD="ask"
RUN_TEST=0
for arg in "$@"; do
    case "$arg" in
        --systemd)    WANT_SYSTEMD=1 ;;
        --no-systemd) WANT_SYSTEMD=0 ;;
        --test)       RUN_TEST=1 ;;
        -h|--help)    usage; exit 0 ;;
        *)            die "unknown option: $arg (try --help)" ;;
    esac
done

is_tty() { [ -t 0 ] && [ -t 1 ]; }

ask() {
    local label="$1" default="${2-}" answer=""
    if [ -n "$default" ]; then
        read -r -p "  $label [$default]: " answer || true
        printf '%s' "${answer:-$default}"
    else
        read -r -p "  $label: " answer || true
        printf '%s' "$answer"
    fi
}

ask_secret() {
    local label="$1" answer=""
    read -r -s -p "  $label (hidden): " answer || true
    printf '\n' >&2
    printf '%s' "$answer"
}

env_value() {
    local key="$1" fallback="${2-}" line=""
    [ -f .env ] && line=$(grep -E "^${key}=" .env | tail -n 1 || true)
    if [ -n "$line" ]; then
        printf '%s' "${line#*=}" | tr -d '\r'
    else
        printf '%s' "$fallback"
    fi
}

# Rewrite one KEY=value line without touching the rest of the file.
set_env() {
    "$PYTHON" - "$1" "$2" <<'PY'
import pathlib, sys

key, value = sys.argv[1], sys.argv[2]
path = pathlib.Path(".env")
lines = path.read_text().splitlines() if path.exists() else []
out, replaced = [], False
for line in lines:
    if line.startswith(key + "="):
        out.append(f"{key}={value}")
        replaced = True
    else:
        out.append(line)
if not replaced:
    out.append(f"{key}={value}")
path.write_text("\n".join(out).rstrip("\n") + "\n", newline="\n")
PY
}

# -------------------------------------------------------------------- prelude
printf '%s\n' "${B}${C}"
printf '  ╭───────────────────────────────────────────╮\n'
printf '  │  CloudBox · Discord container control     │\n'
printf '  ╰───────────────────────────────────────────╯\n'
printf '%s\n' "${N}"

[ -f run.py ] || die "run.py not found — run this script from the repository root"
[ -f requirements.txt ] || die "requirements.txt not found"
ok "project root: $ROOT"

if [ "$(id -u)" -eq 0 ]; then
    warn "running as root; the bot only needs your normal user in the lxd group"
fi

# --------------------------------------------------------------------- python
step "Checking Python"

PYTHON="${PYTHON:-python3}"
if ! command -v "$PYTHON" >/dev/null 2>&1; then
    die "python3 not found. Debian/Ubuntu: apt-get install -y python3 python3-venv python3-pip"
fi

PYVER="$("$PYTHON" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')"
if ! "$PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then
    die "Python 3.11+ is required, found $PYVER"
fi
ok "Python $PYVER"

venv_ready() { "$PYTHON" -c 'import venv, ensurepip' >/dev/null 2>&1; }

install_venv_support() {
    local sudo=""
    if [ "$(id -u)" -ne 0 ]; then
        command -v sudo >/dev/null 2>&1 || return 1
        sudo="sudo"
    fi

    if command -v apt-get >/dev/null 2>&1; then
        $sudo apt-get update -qq && $sudo apt-get install -y -qq python3-venv python3-pip
    elif command -v dnf >/dev/null 2>&1; then
        $sudo dnf install -y -q python3-pip
    elif command -v apk >/dev/null 2>&1; then
        $sudo apk add --no-cache python3 py3-pip
    elif command -v pacman >/dev/null 2>&1; then
        $sudo pacman -Sy --noconfirm python-pip
    else
        return 1
    fi
}

if venv_ready; then
    ok "venv module available"
else
    warn "python3-venv is not installed, so .venv cannot be created"
    if [ -t 0 ] && [ -t 1 ]; then
        printf '  Install it now? [Y/n]: '
        read -r REPLY || true
        case "${REPLY:-y}" in
            [Yy]*)
                step "Installing python3-venv"
                if install_venv_support && venv_ready; then
                    ok "venv module available"
                else
                    die "could not install it automatically. Run: apt-get install -y python3-venv python3-pip"
                fi
                ;;
            *)
                die "install it first: apt-get install -y python3-venv python3-pip   then re-run ./setup.sh"
                ;;
        esac
    else
        die "install it first: apt-get install -y python3-venv python3-pip   then re-run ./setup.sh"
    fi
fi

# ------------------------------------------------------------------------ lxd
step "Checking LXD"

if ! command -v lxc >/dev/null 2>&1; then
    die "lxc not found. Install LXD first: snap install lxd && sudo lxd init"
fi
ok "lxc $("lxc" --version 2>/dev/null | head -n 1)"

LXC_PROBE=""
if ! LXC_PROBE=$(lxc info 2>&1); then
    case "$LXC_PROBE" in
        *[Pp]ermission*denied*|*"not authorized"*)
            die "cannot talk to LXD as $(id -un). Run: sudo usermod -aG lxd $(id -un)  (then log out and back in)" ;;
        *)
            die "LXD is not initialised or not running. Run: sudo lxd init    ($LXC_PROBE)" ;;
    esac
fi
ok "LXD is reachable"

POOL="$(env_value STORAGE_POOL default)"
POOLS="$(lxc storage list --format csv 2>/dev/null | cut -d, -f1 | paste -sd ', ' - || true)"
if [ -n "$POOLS" ] && printf '%s\n' "$POOLS" | tr ',' '\n' | tr -d ' ' | grep -qx "$POOL"; then
    ok "storage pool '$POOL' exists"
else
    warn "storage pool '$POOL' not found (available: ${POOLS:-none})"
    note "create one with: lxc storage create $POOL dir"
    note "or change STORAGE_POOL in .env"
fi

# ------------------------------------------------------------------------ env
step "Configuring .env"

if [ -f .env ]; then
    ok ".env already exists, leaving it alone"
else
    [ -f .env.example ] || die ".env.example is missing"
    cp .env.example .env
    ok "created .env from .env.example"
fi

if is_tty; then
    if [ -z "$(env_value DISCORD_TOKEN)" ]; then
        TOKEN="$(ask_secret "Discord bot token")"
        if [ -n "$TOKEN" ]; then
            set_env DISCORD_TOKEN "$TOKEN"
            ok "token stored"
        else
            warn "no token entered — edit .env before starting"
        fi
    else
        ok "DISCORD_TOKEN already set"
    fi

    if [ -z "$(env_value GUILD_ID)" ]; then
        GUILD="$(ask "Guild ID (blank = register commands globally)")"
        if [ -n "$GUILD" ]; then
            if [[ "$GUILD" =~ ^[0-9]+$ ]]; then
                set_env GUILD_ID "$GUILD"
                ok "commands will sync instantly to guild $GUILD"
            else
                warn "guild id must be numeric, skipped"
            fi
        fi
    fi

    if [ -z "$(env_value OWNER_IDS)" ]; then
        OWNERS="$(ask "Your Discord user ID (comma separated for several)")"
        if [[ "$OWNERS" =~ ^[0-9]+(,[0-9]+)*$ ]]; then
            set_env OWNER_IDS "$OWNERS"
            ok "owner access configured"
        elif [ -n "$OWNERS" ]; then
            warn "owner ids must be numbers separated by commas, skipped"
        else
            warn "no owner set — nobody will be able to run /staff"
        fi
    else
        ok "OWNER_IDS already set"
    fi
else
    note "non-interactive shell: fill in .env yourself"
fi

# ---------------------------------------------------------------------- venv
step "Installing dependencies"

if [ -d .venv ]; then
    ok "reusing .venv"
else
    "$PYTHON" -m venv .venv
    ok "created .venv"
fi

.venv/bin/python -m pip install --quiet --upgrade pip setuptools wheel
.venv/bin/python -m pip install --quiet -r requirements.txt
ok "packages installed"

.venv/bin/python - <<'PY'
import aiohttp, cryptography, discord, psutil
print(f"  \u2713 discord.py {discord.__version__}, aiohttp {aiohttp.__version__}, psutil {psutil.__version__}")
PY

if [ "$RUN_TEST" -eq 1 ]; then
    step "Running the offline smoke test"
    .venv/bin/python tests/smoke.py
fi

# ------------------------------------------------------------------ systemd
install_service() {
    local unit="/etc/systemd/system/cloudbox.service"
    local user group sudo=""

    user="$(id -un)"
    group="$(id -gn)"

    if ! command -v systemctl >/dev/null 2>&1; then
        warn "systemd not found on this host; run the bot under your own supervisor"
        return 0
    fi

    if [ "$(id -u)" -ne 0 ]; then
        if command -v sudo >/dev/null 2>&1; then
            sudo="sudo"
        else
            warn "sudo not available; cannot install the service"
            return 0
        fi
    fi

    $sudo tee "$unit" >/dev/null <<EOF
[Unit]
Description=CloudBox Discord LXD control plane
Documentation=file://$ROOT/README.md
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$user
Group=$group
WorkingDirectory=$ROOT
EnvironmentFile=$ROOT/.env
ExecStart=$ROOT/.venv/bin/python $ROOT/run.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

    $sudo systemctl daemon-reload
    if $sudo systemctl enable --now cloudbox.service; then
        ok "systemd unit installed and started"
        note "logs: journalctl -u cloudbox -f"
    else
        warn "unit installed but could not be started — check: systemctl status cloudbox"
    fi
}

step "Service"

if [ "$WANT_SYSTEMD" = "ask" ]; then
    if is_tty; then
        read -r -p "  Install CloudBox as a systemd service? [y/N]: " REPLY || true
        if [[ "${REPLY:-n}" =~ ^[Yy]$ ]]; then
            WANT_SYSTEMD=1
        else
            WANT_SYSTEMD=0
        fi
    else
        WANT_SYSTEMD=0
    fi
fi

if [ "$WANT_SYSTEMD" -eq 1 ]; then
    install_service
else
    note "skipped (run ./setup.sh --systemd later if you change your mind)"
fi

# ------------------------------------------------------------------- summary
LOW_PORT="${PORT_RANGE:-$(env_value PORT_RANGE 20000-40000)}"
LOW="${LOW_PORT%%-*}"
HIGH="${LOW_PORT##*-}"

step "Done"

if [ -z "$(env_value DISCORD_TOKEN)" ]; then
    warn "DISCORD_TOKEN is still empty: nano .env"
fi

printf '\n  Start in the foreground:\n'
printf '    %s.venv/bin/python run.py%s\n' "$B" "$N"
printf '\n  Then in Discord:\n'
printf '    %s/panel%s   post the member panel in a channel\n' "$B" "$N"
printf '    %s/staff node-list%s   confirm the host is registered\n' "$B" "$N"
printf '\n  If you enabled a firewall, open the forward range:\n'
printf '    %ssudo ufw allow %s-%s/tcp && sudo ufw allow %s-%s/udp%s\n' \
    "$D" "$LOW" "$HIGH" "$LOW" "$HIGH" "$N"
printf '\n  Remote host? install the agent on it and register it with:\n'
printf '    %s/staff node-add name:node-2 region:eu endpoint:https://node-2.example.com%s\n' "$D" "$N"
printf '\n'
