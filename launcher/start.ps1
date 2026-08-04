# LAZEIMS Offline Station - daily launcher (Windows).
# Double-click "Start LAZEIMS Station.bat" to run this.

function Line-Info  ([string]$m) { Write-Host "  * $m" -ForegroundColor Cyan }
function Line-Ok    ([string]$m) { Write-Host "  OK  $m" -ForegroundColor Green }
function Line-Warn  ([string]$m) { Write-Host "  !   $m" -ForegroundColor Yellow }
function Line-Fail  ([string]$m) { Write-Host "  ERR $m" -ForegroundColor Red }
function Detail     ([string]$m) { Write-Host "      $m" -ForegroundColor DarkGray }

$WinBase     = if ($Env:LOCALAPPDATA) { $Env:LOCALAPPDATA } else { Join-Path $env:USERPROFILE 'AppData\Local' }
$LazHome     = Join-Path $WinBase 'LAZEIMS'
$EnvDir      = Join-Path $LazHome 'env'
$AppDir      = Join-Path $LazHome 'app'
$LogDir      = Join-Path $LazHome 'logs'
$LauncherDir = Join-Path $LazHome 'launcher'
$Vpy         = Join-Path $EnvDir 'Scripts\python.exe'
$RunLog      = Join-Path $LogDir ("station-{0:yyyyMMdd}.log" -f (Get-Date))

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

# If setup has never run, delegate to setup.ps1
if (-not (Test-Path $Vpy)) {
    Line-Warn "Station not set up yet - running setup first..."
    $SetupScript = Join-Path $LauncherDir 'setup.ps1'
    if (-not (Test-Path $SetupScript)) {
        $SetupScript = Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) 'setup.ps1'
    }
    & powershell -NoProfile -ExecutionPolicy Bypass -File $SetupScript
    exit $LASTEXITCODE
}

Start-Transcript -Path $RunLog -Append | Out-Null

# Verify station files exist
if (-not (Test-Path (Join-Path $AppDir 'station'))) {
    Line-Fail "Station files missing - please run Setup LAZEIMS Station.bat again"
    Stop-Transcript | Out-Null
    exit 1
}

# Detect LAN IP
# Priority: Get-NetIPAddress (offline-safe) -> Python UDP trick (no packet sent)
#           -> ipconfig parsing (any IPv4) -> loopback
$LanIp = ''

# 1. Get-NetIPAddress: skip loopback, APIPA, and virtual/tunnel adapters
try {
    $LanIp = [string](Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object {
            $_.IPAddress -ne '127.0.0.1' -and
            $_.IPAddress -notlike '169.254.*' -and
            $_.InterfaceAlias -notmatch 'Loopback|Teredo|isatap|6TO4|Virtual|vEthernet'
        } |
        Sort-Object {
            # Prefer Ethernet > Wi-Fi > everything else
            switch -Wildcard ($_.InterfaceAlias) {
                'Ethernet*' { 0 }
                'Wi-Fi*'    { 1 }
                default     { 2 }
            }
        } |
        Select-Object -First 1 -ExpandProperty IPAddress)
    if ($LanIp -and $LanIp -notmatch '^\d{1,3}(\.\d{1,3}){3}$') { $LanIp = '' }
} catch { $LanIp = '' }

# 2. Python UDP trick: connect() to a private addr - no packet is ever sent,
#    but the OS picks the correct source interface. Works fully offline.
if (-not $LanIp) {
    try {
        $LanIp = (& $Vpy '-c' @'
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
try:
    s.connect(('192.168.0.1', 1))
    print(s.getsockname()[0])
finally:
    s.close()
'@ 2>&1).Trim()
        if ($LanIp -notmatch '^\d{1,3}(\.\d{1,3}){3}' -or $LanIp -eq '0.0.0.0') { $LanIp = '' }
    } catch {}
}

# 3. ipconfig parsing - accept any routable IPv4, not just RFC1918
if (-not $LanIp) {
    try {
        $iplines = (& ipconfig 2>&1) -join "`n"
        $allMatches = [regex]::Matches($iplines, 'IPv4[^:]*:\s*(\d{1,3}(?:\.\d{1,3}){3})')
        foreach ($m in $allMatches) {
            $candidate = $m.Groups[1].Value.Trim()
            if ($candidate -ne '127.0.0.1' -and $candidate -notlike '169.254.*') {
                $LanIp = $candidate
                break
            }
        }
    } catch {}
}

if (-not $LanIp -or $LanIp -eq '' -or $LanIp -eq '127.0.0.1') {
    try {
        $hostEntry = [System.Net.Dns]::GetHostEntry([System.Net.Dns]::GetHostName())
        $candidate = $hostEntry.AddressList |
            Where-Object { $_.AddressFamily -eq 'InterNetwork' -and $_.ToString() -ne '127.0.0.1' } |
            Select-Object -First 1
        if ($candidate) { $LanIp = $candidate.ToString() }
    } catch {}
}
if (-not $LanIp -or $LanIp -eq '') { $LanIp = '' }
$LanUrl = if ($LanIp) { "http://${LanIp}:8080" } else { '' }

Write-Host ""
Write-Host "  ========================================================" -ForegroundColor Cyan
Write-Host "           LAZEIMS OFFLINE STATION                        " -ForegroundColor Cyan
Write-Host "  ========================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "  This computer : " -ForegroundColor DarkGray -NoNewline
Write-Host "http://127.0.0.1:8080" -ForegroundColor White
Write-Host "  Other devices : " -ForegroundColor DarkGray -NoNewline
if ($LanUrl) {
    Write-Host $LanUrl -ForegroundColor Yellow
} else {
    Write-Host "(no network detected — connect to WiFi or Ethernet for LAN access)" -ForegroundColor DarkYellow
}
Write-Host ""
Write-Host "  Data Enterers should use the yellow address above." -ForegroundColor DarkGray
Write-Host "  Leave this window OPEN while entry is in progress." -ForegroundColor DarkGray
Write-Host ""

# Generate QR code for the LAN URL so Data Enterers can scan with their phone
$QrScript = Join-Path $env:TEMP 'laz_qr.py'
@(
    'import sys',
    'try:',
    '    import qrcode',
    '    qr = qrcode.QRCode(border=1)',
    '    qr.add_data(sys.argv[1])',
    '    qr.make(fit=True)',
    '    qr.print_ascii(invert=True)',
    'except Exception as e:',
    '    print("(QR unavailable: " + str(e) + ")")'
) -join "`n" | Set-Content -Path $QrScript -Encoding UTF8

Write-Host "  Scan to connect from any device on this network:" -ForegroundColor Cyan
Write-Host ""
if ($LanUrl) {
    & $Vpy $QrScript $LanUrl
} else {
    Write-Host "  (No QR — connect this machine to a network first)" -ForegroundColor DarkYellow
}
Write-Host ""
Remove-Item -Path $QrScript -ErrorAction SilentlyContinue

# Set PYTHONPATH so station code and vendor library are importable
$env:PYTHONPATH = $AppDir + ';' + (Join-Path $AppDir 'vendor\lazeims-common')

# Open browser after a short delay so the server can start
Start-Job -ScriptBlock {
    Start-Sleep -Seconds 3
    Start-Process 'http://127.0.0.1:8080'
} | Out-Null

# Start the station server - runs in foreground, Ctrl+C to stop
$ErrorActionPreference = 'SilentlyContinue'
& $Vpy '-m' 'uvicorn' 'station.main:app' '--host' '0.0.0.0' '--port' '8080' '--workers' '1'
$ExitCode = $LASTEXITCODE
$ErrorActionPreference = 'Stop'

if ($ExitCode -ne 0) {
    Write-Host ""
    Line-Fail "The Station stopped unexpectedly (exit code $ExitCode)"
    Detail ("Log: " + $RunLog)
    Detail "Common causes:"
    Detail "  - Port 8080 already in use (close other apps and retry)"
    Detail "  - Antivirus blocked the process (add an exception)"
}

Stop-Transcript | Out-Null
