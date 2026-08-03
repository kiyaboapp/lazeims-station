# LAZEIMS Offline Station - single launcher.
# Handles first-time setup AND daily start.
# Double-click either BAT file - this does the right thing automatically.

function Show-Info  ([string]$m) { Write-Host "  * $m" -ForegroundColor Cyan }
function Show-Step  ([string]$m) { Write-Host ""; Write-Host "  >> $m" -ForegroundColor White }
function Show-Ok    ([string]$m) { Write-Host "     OK   $m" -ForegroundColor Green }
function Show-Warn  ([string]$m) { Write-Host "     WARN $m" -ForegroundColor Yellow }
function Show-Fail  ([string]$m) { Write-Host "     FAIL $m" -ForegroundColor Red }
function Show-Detail([string]$m) { Write-Host "          $m" -ForegroundColor DarkGray }
function Show-Sep   ()           { Write-Host "  --------------------------------------------------------" -ForegroundColor DarkGray }

function Run-Safe {
    param([string]$Exe, [string[]]$ExeArgs)
    $out = & $Exe @ExeArgs 2>&1
    return @{ Code = $LASTEXITCODE; Out = ($out -join "`n") }
}

function Die([string]$Code, [string]$Msg, [string]$Fix = '') {
    Write-Host ""
    Show-Fail ("[$Code] $Msg")
    if ($Fix) { Show-Detail $Fix }
    Show-Detail ("Log: $SetupLog")
    Write-Host ""
    try { Stop-Transcript | Out-Null } catch {}
    exit 1
}

# ---- Paths ------------------------------------------------------------------
$KitRoot     = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$WinBase     = if ($Env:LOCALAPPDATA) { $Env:LOCALAPPDATA } else { Join-Path $env:USERPROFILE 'AppData\Local' }
$LazHome     = Join-Path $WinBase 'LAZEIMS'
$LauncherDir = Join-Path $LazHome 'launcher'
$EnvDir      = Join-Path $LazHome 'env'
$LogDir      = Join-Path $LazHome 'logs'
$AppDir      = Join-Path $LazHome 'app'
$Vpy         = Join-Path $EnvDir 'Scripts\python.exe'
$PinnedLock  = Join-Path $AppDir 'requirements.lock'
$KitLock     = Join-Path $KitRoot 'requirements.lock'
$SetupLog    = Join-Path $LogDir ("station-{0:yyyyMMdd-HHmmss}.log" -f (Get-Date))

New-Item -ItemType Directory -Force -Path $LazHome, $LauncherDir, $LogDir, $AppDir | Out-Null
Start-Transcript -Path $SetupLog | Out-Null

Write-Host ""
Write-Host "  ========================================================" -ForegroundColor Cyan
Write-Host "              LAZEIMS OFFLINE STATION                     " -ForegroundColor Cyan
Write-Host "  ========================================================" -ForegroundColor Cyan
Write-Host ""
Show-Info ("Computer : " + $env:COMPUTERNAME)
Show-Info ("Log      : " + $SetupLog)
Show-Sep

# ---- Determine if setup is needed ------------------------------------------
$LockHash    = if (Test-Path $KitLock) {
    (Get-FileHash -Algorithm SHA256 $KitLock).Hash
} elseif (Test-Path $PinnedLock) {
    (Get-FileHash -Algorithm SHA256 $PinnedLock).Hash
} else { '' }
$Marker      = Join-Path $EnvDir '.deps-hash'
$OldHash     = if (Test-Path $Marker) { (Get-Content $Marker -Raw -ErrorAction SilentlyContinue).Trim() } else { '' }
$NeedsPackages = (-not (Test-Path $Vpy)) -or ($OldHash -ne $LockHash)
# Always copy station files so updates ship correctly - only pip install is skipped when up to date
$NeedsSetup  = $true

if ($NeedsSetup) {
    Write-Host "  Setting up for the first time (or updating)..." -ForegroundColor Cyan
    Write-Host ""

    # ==========================================================================
    # STEP 1  Copy files
    # ==========================================================================
    Show-Step "STEP 1/4  Copying Station files"
    Show-Detail ("From : $KitRoot")
    Show-Detail ("To   : $AppDir")
    try {
        Copy-Item -Recurse -Force -Path (Join-Path $KitRoot 'station') -Destination $AppDir
        Show-Detail "  station app ............. done"
        Copy-Item -Recurse -Force -Path (Join-Path $KitRoot 'vendor') -Destination $AppDir
        Show-Detail "  shared libraries ........ done"
        Copy-Item -Force -Path $KitLock -Destination $AppDir
        Show-Detail "  dependency list ......... done"
        if (Test-Path (Join-Path $KitRoot 'lazeims-public-key.pem')) {
            Copy-Item -Force -Path (Join-Path $KitRoot 'lazeims-public-key.pem') -Destination $AppDir
            Show-Detail "  verification key ........ done"
        }
        if (Test-Path (Join-Path $KitRoot 'wheelhouse')) {
            Copy-Item -Recurse -Force -Path (Join-Path $KitRoot 'wheelhouse') -Destination $AppDir
            Show-Detail "  offline packages ........ done"
        }
        Get-ChildItem -Path (Join-Path $KitRoot 'launcher') | ForEach-Object {
            Copy-Item -Force -Path $_.FullName -Destination $LauncherDir
        }
        Show-Detail "  launcher scripts ........ done"
        if (Test-Path (Join-Path $KitRoot 'packages')) {
            Copy-Item -Recurse -Force -Path (Join-Path $KitRoot 'packages') -Destination $AppDir
            Show-Detail "  exam packages ........... done"
        }
        Show-Ok "All files copied"
    } catch {
        Die 'FILE-001' "Could not copy files" $_.Exception.Message
    }

    # ==========================================================================
    # STEP 2  Find Python
    # ==========================================================================
    Show-Step "STEP 2/4  Checking for Python 3.11+"
    Show-Detail "Searching this computer..."

    $PyBin  = $null
    $PyFlag = $null
    $Searches = @(
        @{ Bin = 'py'; Flags = @('-3.13', '-3.12', '-3.11', '-3') },
        @{ Bin = 'python3.13'; Flags = @('') },
        @{ Bin = 'python3.12'; Flags = @('') },
        @{ Bin = 'python3.11'; Flags = @('') },
        @{ Bin = 'python3';    Flags = @('') },
        @{ Bin = 'python';     Flags = @('') }
    )
    foreach ($S in $Searches) {
        foreach ($F in $S.Flags) {
            $TryArgs = if ($F) { @($F, '--version') } else { @('--version') }
            $R = Run-Safe -Exe $S.Bin -ExeArgs $TryArgs
            if ($R.Code -eq 0 -and $R.Out -match 'Python 3\.(11|12|13)') {
                $PyBin = $S.Bin; $PyFlag = $F; break
            }
        }
        if ($PyBin) { break }
    }

    if (-not $PyBin) {
        Show-Warn "Python 3.11+ not found - downloading from python.org..."
        $Installer = Join-Path $env:TEMP 'python-3.12.6-amd64.exe'
        try {
            Invoke-WebRequest -Uri 'https://www.python.org/ftp/python/3.12.6/python-3.12.6-amd64.exe' -OutFile $Installer -UseBasicParsing
        } catch {
            Die 'NET-001' "Could not download Python" "Install Python 3.11+ from python.org then run this again."
        }
        Start-Process -FilePath $Installer -ArgumentList 'InstallAllUsers=0 PrependPath=1 Include_test=0 Include_launcher=1 /quiet' -Wait
        $env:PATH = [System.Environment]::GetEnvironmentVariable('PATH','Machine') + ';' + [System.Environment]::GetEnvironmentVariable('PATH','User')
        $PyBin = 'py'; $PyFlag = '-3.12'
    }

    # Resolve actual python.exe path (avoids py-launcher flag issues with venv)
    $ResolveArgs = if ($PyFlag) { @($PyFlag, '-c', 'import sys; print(sys.executable)') } else { @('-c', 'import sys; print(sys.executable)') }
    $ResolveR    = Run-Safe -Exe $PyBin -ExeArgs $ResolveArgs
    $ActualPy    = $ResolveR.Out.Trim().Split("`n")[0].Trim()
    if ($ActualPy -and (Test-Path $ActualPy)) { $PyBin = $ActualPy; $PyFlag = $null }

    $VerArgs = if ($PyFlag) { @($PyFlag, '--version') } else { @('--version') }
    $VerR    = Run-Safe -Exe $PyBin -ExeArgs $VerArgs
    Show-Ok ("Found: " + ($VerR.Out.Trim()))
    Show-Detail ("Path : $PyBin")

    # ==========================================================================
    # STEP 3  Virtualenv + packages
    # ==========================================================================
    Show-Step "STEP 3/4  Installing Station engine"

    # Wipe env if hash changed (previous install was incomplete or lock updated)
    if ((Test-Path $Vpy) -and ($OldHash -ne $LockHash)) {
        Show-Detail "Requirements changed - clearing old environment..."
        Remove-Item -Recurse -Force -Path $EnvDir -ErrorAction SilentlyContinue
        Remove-Item -Force -Path $Marker -ErrorAction SilentlyContinue
        Show-Ok "Cleared"
    }

    if (-not (Test-Path $Vpy)) {
        try { Stop-Transcript | Out-Null } catch {}
        Write-Host ""
        Write-Host "  Creating Python environment (30-60 seconds, no output is normal)..." -ForegroundColor Cyan
        Write-Host ("  Location: $EnvDir") -ForegroundColor DarkGray
        Write-Host ""
        & $PyBin '-m' 'venv' $EnvDir
        if ($LASTEXITCODE -ne 0) {
            try { Start-Transcript -Path $SetupLog -Append | Out-Null } catch {}
            Die 'VENV-001' "Could not create Python environment"
        }
        try { Start-Transcript -Path $SetupLog -Append | Out-Null } catch {}
        Show-Ok "Python environment created"
    } else {
        Show-Ok "Python environment exists"
    }

    if (-not $NeedsPackages) {
        Show-Ok "Packages already up to date - skipping install"
    } else {

    $Wheelhouse = Join-Path $AppDir 'wheelhouse'
    $HasOffline = (Test-Path $Wheelhouse) -and (Get-ChildItem $Wheelhouse -ErrorAction SilentlyContinue)

    $Packages = @(
        @{ Name = 'fastapi';          Label = 'FastAPI         (web framework)' },
        @{ Name = 'uvicorn';          Label = 'Uvicorn         (server)' },
        @{ Name = 'argon2-cffi';      Label = 'Argon2          (password hashing)' },
        @{ Name = 'itsdangerous';     Label = 'ItsDangerous    (session security)' },
        @{ Name = 'python-multipart'; Label = 'Multipart       (file uploads)' },
        @{ Name = 'pydantic';         Label = 'Pydantic        (data validation)' },
        @{ Name = 'cryptography';     Label = 'Cryptography    (package signing)' },
        @{ Name = 'qrcode';           Label = 'QRCode          (QR for Data Enterers)' }
    )
    $Total = $Packages.Count

    try { Stop-Transcript | Out-Null } catch {}
    Write-Host ""
    Write-Host "  ========================================================" -ForegroundColor Cyan
    Write-Host ("  Installing {0} packages - watch every step:" -f $Total) -ForegroundColor Cyan
    Write-Host "  ========================================================" -ForegroundColor Cyan

    $Idx = 0
    foreach ($Pkg in $Packages) {
        $Idx++
        Write-Host ""
        Write-Host ("  ---- [{0}/{1}]  {2}" -f $Idx, $Total, $Pkg.Label) -ForegroundColor Yellow
        Write-Host ""
        & $Vpy '-m' 'pip' 'install' $Pkg.Name
        $ExCode = $LASTEXITCODE
        if ($ExCode -ne 0 -and $HasOffline) {
            Write-Host ("  WARN: online failed - trying offline bundle...") -ForegroundColor Yellow
            & $Vpy '-m' 'pip' 'install' '--no-index' "--find-links=$Wheelhouse" $Pkg.Name
            $ExCode = $LASTEXITCODE
        }
        if ($ExCode -eq 0) {
            Write-Host ("  OK  [{0}/{1}]  {2}" -f $Idx, $Total, $Pkg.Label) -ForegroundColor Green
        } else {
            Write-Host ("  FAILED  {0}" -f $Pkg.Label) -ForegroundColor Red
            Write-Host "  Check internet connection and run this again." -ForegroundColor Yellow
            exit 1
        }
    }

    try { Start-Transcript -Path $SetupLog -Append | Out-Null } catch {}
    Write-Host ""
    Write-Host "  ========================================================" -ForegroundColor Green
    Write-Host ("  All {0} packages installed" -f $Total) -ForegroundColor Green
    Write-Host "  ========================================================" -ForegroundColor Green
    $LockHash | Set-Content -Path $Marker -NoNewline

    } # end if NeedsPackages

    # ==========================================================================
    # STEP 4  Firewall + stage exam packages
    # ==========================================================================
    Show-Step "STEP 4/4  Finishing up"

    try {
        $Fw = Get-NetFirewallRule -DisplayName 'LAZEIMS Offline Station' -ErrorAction SilentlyContinue
        if (-not $Fw) {
            New-NetFirewallRule -DisplayName 'LAZEIMS Offline Station' -Direction Inbound `
                -LocalPort 8080 -Protocol TCP -Action Allow -Profile Private -ErrorAction SilentlyContinue | Out-Null
            Show-Detail "Firewall: port 8080 opened"
        }
    } catch { Show-Warn "Could not set firewall rule - ask IT to allow TCP 8080 inbound" }

    $PkgsDir = Join-Path $AppDir 'packages'
    if (Test-Path $PkgsDir) {
        $PkgZips = Get-ChildItem -Path $PkgsDir -Filter '*.zip' -ErrorAction SilentlyContinue
        if ($PkgZips) {
            Show-Detail "Staging exam packages for automatic import..."
            $PyHelper = Join-Path $env:TEMP 'laz_manifest.py'
            @('import zipfile, json, sys',
              'with zipfile.ZipFile(sys.argv[1]) as z:',
              '    m = json.loads(z.read("manifest.json"))',
              'print(m.get("station_code","") + "|" + m.get("exam_id",""))') -join "`n" |
                Set-Content -Path $PyHelper -Encoding UTF8
            foreach ($PkgZip in $PkgZips) {
                $R = Run-Safe -Exe $Vpy -ExeArgs @($PyHelper, $PkgZip.FullName)
                if ($R.Code -eq 0) {
                    $Parts = $R.Out.Trim() -split '\|'
                    $StnCode = $Parts[0].Trim()
                    $ExId    = if ($Parts.Count -gt 1) { $Parts[1].Trim() } else { '' }
                    if ($StnCode -and $ExId) {
                        $Pending = Join-Path $LazHome "stations\$StnCode\exams\$ExId\imports\pending"
                        New-Item -ItemType Directory -Force -Path $Pending | Out-Null
                        Copy-Item -Force -Path $PkgZip.FullName -Destination $Pending
                        Show-Ok ("Exam data staged: station=$StnCode")
                    } else { Show-Warn ("Could not read station/exam from: " + $PkgZip.Name) }
                } else { Show-Warn ("Could not read manifest from: " + $PkgZip.Name) }
            }
            Remove-Item -Path $PyHelper -ErrorAction SilentlyContinue
        }
    }

    Show-Ok "Setup complete"
    Write-Host ""

} else {
    Show-Ok "Already installed and up to date - starting Station..."
    Write-Host ""
}

# ==========================================================================
# START the Station server
# ==========================================================================
$RunLog = Join-Path $LogDir ("station-run-{0:yyyyMMdd}.log" -f (Get-Date))

# Detect LAN IP
# Priority: Get-NetIPAddress (offline-safe) -> Python UDP trick (no packet sent)
#           -> ipconfig parsing (any IPv4) -> loopback
$LanIp = ''

# 1. Get-NetIPAddress: skip loopback, APIPA, and virtual/tunnel adapters
try {
    $LanIp = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
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
        Select-Object -First 1 -ExpandProperty IPAddress
} catch {}

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

if (-not $LanIp) { $LanIp = '127.0.0.1' }
$LanUrl = "http://$LanIp:8080"

Write-Host "  ========================================================" -ForegroundColor Cyan
Write-Host "           LAZEIMS STATION IS RUNNING                     " -ForegroundColor Cyan
Write-Host "  ========================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "  This computer : " -ForegroundColor DarkGray -NoNewline
Write-Host "http://127.0.0.1:8080" -ForegroundColor White
Write-Host "  Other devices : " -ForegroundColor DarkGray -NoNewline
Write-Host $LanUrl -ForegroundColor Yellow
Write-Host ""
Write-Host "  Scan to connect from any device on this network:" -ForegroundColor Cyan
Write-Host ""

# Generate QR code for the LAN URL
$QrScript = Join-Path $env:TEMP 'laz_qr.py'
@('import sys',
  'try:',
  '    import qrcode',
  '    qr = qrcode.QRCode(border=1)',
  '    qr.add_data(sys.argv[1])',
  '    qr.make(fit=True)',
  '    qr.print_ascii(invert=True)',
  'except Exception as e:',
  '    print("  (QR unavailable: install qrcode package)")') -join "`n" |
    Set-Content -Path $QrScript -Encoding UTF8
& $Vpy $QrScript $LanUrl
Remove-Item -Path $QrScript -ErrorAction SilentlyContinue

Write-Host ""
Write-Host "  Leave this window OPEN while Data Enterers are working." -ForegroundColor DarkGray
Write-Host "  Press Ctrl+C or close window to stop." -ForegroundColor DarkGray
Write-Host ""

# Open browser
Start-Job -ScriptBlock { Start-Sleep -Seconds 3; Start-Process 'http://127.0.0.1:8080' } | Out-Null

# Start server
try { Stop-Transcript | Out-Null } catch {}
$env:PYTHONPATH = $AppDir + ';' + (Join-Path $AppDir 'vendor\lazeims-common')
& $Vpy '-m' 'uvicorn' 'station.main:app' '--host' '0.0.0.0' '--port' '8080' '--workers' '1'

if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "  The Station stopped unexpectedly." -ForegroundColor Red
    Write-Host "  Common causes:" -ForegroundColor DarkGray
    Write-Host "    - Port 8080 already in use" -ForegroundColor DarkGray
    Write-Host "    - Antivirus blocked the process" -ForegroundColor DarkGray
    Write-Host ("  Log: $RunLog") -ForegroundColor DarkGray
}
