#!/usr/bin/env bash
# LAZEIMS Offline Station — Linux/macOS first-time setup.
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

KIT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LAZ_HOME="${LAZEIMS_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/lazeims}"
LAUNCHER_HOME="$LAZ_HOME/launcher"
ENV_DIR="$LAZ_HOME/env"
APP_DIR="$LAZ_HOME/app"
LOG_DIR="$LAZ_HOME/logs"
PINNED="$KIT_ROOT/requirements.lock"
mkdir -p "$LAZ_HOME" "$LAUNCHER_HOME" "$ENV_DIR" "$APP_DIR" "$LOG_DIR"
SETUP_LOG="$LOG_DIR/setup-$(date +%Y%m%d-%H%M%S).log"
exec > >(tee -a "$SETUP_LOG") 2>&1

printf "\n%s╔══════════════════════════════════════════════════════════════╗%s\n" "$c_cyan" "$c_reset"
printf   "%s║                 LAZEIMS OFFLINE STATION                     ║%s\n" "$c_cyan" "$c_reset"
printf   "%s║                    First-time setup                         ║%s\n" "$c_cyan" "$c_reset"
printf   "%s╚══════════════════════════════════════════════════════════════╝%s\n\n" "$c_cyan" "$c_reset"
info "Computer  : $(hostname)"
info "Setup log : $SETUP_LOG"
echo

step "Installing Station files on this computer"
cp -R "$KIT_ROOT/station" "$APP_DIR/"
cp -R "$KIT_ROOT/vendor"  "$APP_DIR/"
cp    "$KIT_ROOT/requirements.lock" "$APP_DIR/"
[[ -f "$KIT_ROOT/lazeims-public-key.pem" ]] && cp "$KIT_ROOT/lazeims-public-key.pem" "$APP_DIR/"
cp -R "$KIT_ROOT/launcher/." "$LAUNCHER_HOME/"
ok "Station files installed"

step "Checking for Python 3.11+"
PY=""
for cand in python3.12 python3.11 python3 python; do
    if command -v "$cand" >/dev/null 2>&1; then
        ver=$("$cand" --version 2>&1 || true)
        if [[ "$ver" =~ Python\ 3\.(11|12|13) ]]; then PY="$cand"; break; fi
    fi
done
if [[ -z "$PY" ]]; then
    fail "Python 3.11+ is not installed on this computer"
    dim "Install python3.11 or newer with your package manager and re-run this setup."
    exit 1
fi
ok "Python detected: $($PY --version)"

step "Preparing the Station environment (once per computer)"
if [[ ! -x "$ENV_DIR/bin/python" ]]; then
    "$PY" -m venv "$ENV_DIR"
fi
VPY="$ENV_DIR/bin/python"

LOCK_HASH=$(sha256sum "$PINNED" | awk '{print $1}')
if [[ ! -f "$ENV_DIR/.deps-hash" ]] || [[ "$(cat "$ENV_DIR/.deps-hash" 2>/dev/null)" != "$LOCK_HASH" ]]; then
    step "Installing required Station components"
    "$VPY" -m pip install --upgrade pip >/dev/null
    # Use the bundled wheelhouse for a fully-offline install when available.
    WHEELHOUSE="$APP_DIR/wheelhouse"
    if [[ -d "$WHEELHOUSE" ]] && [[ -n "$(ls -A "$WHEELHOUSE" 2>/dev/null)" ]]; then
        info "Using bundled offline packages (no internet required)"
        "$VPY" -m pip install --no-index --find-links="$WHEELHOUSE" -r "$PINNED"
    else
        "$VPY" -m pip install -r "$PINNED"
    fi
    printf '%s' "$LOCK_HASH" > "$ENV_DIR/.deps-hash"
    ok "Components installed"
else
    ok "Components already present — reusing them"
fi

echo
info "LAZEIMS Station is ready for its exam package."
dim  "Opening the local Station page. Sign in as station admin and import the exam .zip."
echo

# ── Auto-stage any bundled exam packages ─────────────────────────────────────
# If this kit was downloaded as a Complete Bundle (with a specific exam package
# pre-placed in packages/), copy each .zip to the right imports/pending/ path
# so the Station imports it automatically on first boot.
PKGS_DIR="$APP_DIR/packages"
if [[ -d "$PKGS_DIR" ]] && compgen -G "$PKGS_DIR/*.zip" > /dev/null 2>&1; then
    step "Staging bundled exam package(s) for automatic import"
    for pkg_zip in "$PKGS_DIR"/*.zip; do
        # Read station_code and exam_id from manifest.json inside the zip
        pkg_station=$(python3 -c "
import zipfile, json, sys
try:
    with zipfile.ZipFile('$pkg_zip') as z:
        m = json.loads(z.read('manifest.json'))
    print(m.get('station_code',''))
except Exception as e:
    sys.exit(1)
" 2>/dev/null || true)
        pkg_exam=$(python3 -c "
import zipfile, json, sys
try:
    with zipfile.ZipFile('$pkg_zip') as z:
        m = json.loads(z.read('manifest.json'))
    print(m.get('exam_id',''))
except Exception as e:
    sys.exit(1)
" 2>/dev/null || true)
        if [[ -n "$pkg_station" ]] && [[ -n "$pkg_exam" ]]; then
            PENDING="$LAZ_HOME/stations/$pkg_station/exams/$pkg_exam/imports/pending"
            mkdir -p "$PENDING"
            cp "$pkg_zip" "$PENDING/"
            ok "Exam package staged: $(basename "$pkg_zip") → $PENDING"
        else
            warn "Could not read station_code/exam_id from $(basename "$pkg_zip") — skipping auto-stage"
        fi
    done
fi

exec "$LAUNCHER_HOME/start.sh"
