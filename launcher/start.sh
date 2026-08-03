#!/usr/bin/env bash
# LAZEIMS Offline Station — daily launcher (Linux/macOS).
set -Eeo pipefail

esc() { printf '\033[%sm' "$1"; }
c_cyan="$(esc '1;36')"; c_blue="$(esc '1;34')"; c_green="$(esc '1;32')"
c_yellow="$(esc '1;33')"; c_red="$(esc '1;31')"; c_gray="$(esc '2;37')"; c_reset="$(esc '0')"

info() { printf "  %s●%s %s\n" "$c_cyan" "$c_reset" "$1"; }
step() { printf "  %s→%s %s\n" "$c_blue" "$c_reset" "$1"; }
ok()   { printf "  %s✓%s %s\n" "$c_green" "$c_reset" "$1"; }
warn() { printf "  %s!%s %s\n" "$c_yellow" "$c_reset" "$1"; }
fail() { printf "  %s✗%s %s\n" "$c_red" "$c_reset" "$1"; }
dim()  { printf "       %s%s%s\n" "$c_gray" "$1" "$c_reset"; }

LAZ_HOME="${LAZEIMS_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/lazeims}"
ENV_DIR="$LAZ_HOME/env"
APP_DIR="$LAZ_HOME/app"
LOG_DIR="$LAZ_HOME/logs"
VPY="$ENV_DIR/bin/python"
mkdir -p "$LOG_DIR"

if [[ ! -x "$VPY" ]]; then
    warn "Station has not been set up on this computer yet — running first-time setup"
    exec "$(dirname "$0")/setup.sh"
fi

printf "\n%s╔══════════════════════════════════════════════════════════════╗%s\n" "$c_cyan" "$c_reset"
printf   "%s║                 LAZEIMS OFFLINE STATION                     ║%s\n" "$c_cyan" "$c_reset"
printf   "%s╚══════════════════════════════════════════════════════════════╝%s\n\n" "$c_cyan" "$c_reset"

step "Checking Station files"
[[ -d "$APP_DIR/station" ]] || { fail "Station files are missing — run setup again"; exit 1; }
ok "Verified"

step "Detecting LAN address"
IP=""

# 1. ip route: finds the interface used for LAN traffic (works offline, Linux/macOS)
if [[ -z "$IP" ]]; then
    IP=$(ip route get 192.168.0.1 2>/dev/null | awk '/src/{for(i=1;i<=NF;i++) if($i=="src") print $(i+1)}' || true)
    [[ "$IP" == "127.0.0.1" || "$IP" == "0.0.0.0" ]] && IP=""
fi

# 2. hostname -I (Linux): returns all IPs space-separated; pick the first non-loopback/APIPA
if [[ -z "$IP" ]]; then
    for candidate in $(hostname -I 2>/dev/null); do
        [[ "$candidate" == "127.0.0.1" ]] && continue
        [[ "$candidate" == 169.254.* ]] && continue
        IP="$candidate"
        break
    done
fi

# 3. Python UDP trick: connect() picks the right interface without sending any packet
if [[ -z "$IP" ]]; then
    IP=$("$VPY" -c "
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
try:
    s.connect(('192.168.0.1', 1))
    print(s.getsockname()[0])
finally:
    s.close()
" 2>/dev/null || true)
    [[ "$IP" == "0.0.0.0" ]] && IP=""
fi

# 4. ifconfig fallback (macOS / older Linux)
if [[ -z "$IP" ]]; then
    IP=$(ifconfig 2>/dev/null | awk '/inet /{ip=$2; gsub(/addr:/,"",ip); if(ip!="127.0.0.1" && ip!~/^169\.254\./) {print ip; exit}}' || true)
fi

[[ -z "$IP" ]] && IP="127.0.0.1"
ok "LAN address : http://$IP:8080"

step "Starting LAZEIMS Station"
printf "\n"
printf " %s┌────────────────────────────────────────────────────────────┐%s\n" "$c_cyan" "$c_reset"
printf " %s│ STATION IS RUNNING                                        │%s\n" "$c_cyan" "$c_reset"
printf " %s│                                                            │%s\n" "$c_cyan" "$c_reset"
printf " %s│ This computer : http://127.0.0.1:8080                     │%s\n" "$c_cyan" "$c_reset"
printf " %s│ Other devices : http://%s:%-15s │%s\n" "$c_cyan" "$IP" "8080" "$c_reset"
printf " %s│                                                            │%s\n" "$c_cyan" "$c_reset"
printf " %s│ Data Enterers should open the \"Other devices\" address.   │%s\n" "$c_cyan" "$c_reset"
printf " %s└────────────────────────────────────────────────────────────┘%s\n\n" "$c_cyan" "$c_reset"
dim "Leave this window OPEN while Data Enterers are working."
dim "Close it (Ctrl+C) to stop the Station."
echo

export PYTHONPATH="$APP_DIR:$APP_DIR/vendor/lazeims-common"
exec "$VPY" -m uvicorn station.main:app --host 0.0.0.0 --port 8080
