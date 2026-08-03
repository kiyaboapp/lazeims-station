# LAZEIMS Offline Station - Windows first-time setup.
# Called by "Setup LAZEIMS Station.bat"

# ---- Helpers ----------------------------------------------------------------
function Show-Info  ([string]$m) { Write-Host "  * $m" -ForegroundColor Cyan }
function Show-Step  ([string]$m) { Write-Host ""; Write-Host "  >> $m" -ForegroundColor White }
function Show-Ok    ([string]$m) { Write-Host "     OK   $m" -ForegroundColor Green }
function Show-Warn  ([string]$m) { Write-Host "     WARN $m" -ForegroundColor Yellow }
function Show-Fail  ([string]$m) { Write-Host "     FAIL $m" -ForegroundColor Red }
function Show-Detail([string]$m) { Write-Host "          $m" -ForegroundColor DarkGray }
function Show-Sep   ()           { Write-Host "  --------------------------------------------------------" -ForegroundColor DarkGray }

function Run-Safe {
    # Run a native command and return exit code + captured output.
    # Never throws on stderr (avoids NativeCommandError).
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
$PinnedLock  = Join-Path $KitRoot 'requirements.lock'
$SetupLog    = Join-Path $LogDir ("setup-{0:yyyyMMdd-HHmmss}.log" -f (Get-Date))

New-Item -ItemType Directory -Force -Path $LazHome, $LauncherDir, $LogDir, $AppDir | Out-Null
Start-Transcript -Path $SetupLog | Out-Null

Write-Host ""
Write-Host "  ========================================================" -ForegroundColor Cyan
Write-Host "       LAZEIMS OFFLINE STATION  -  First-time Setup       " -ForegroundColor Cyan
Write-Host "  ========================================================" -ForegroundColor Cyan
Write-Host ""
Show-Info ("Computer : " + $env:COMPUTERNAME)
Show-Info ("Log      : " + $SetupLog)
Show-Sep

# ==========================================================================
# STEP 1  Copy files
# ==========================================================================
Show-Step "STEP 1/4  Copying Station files to this computer"
Show-Detail ("From : $KitRoot")
Show-Detail ("To   : $AppDir")
try {
    Copy-Item -Recurse -Force -Path (Join-Path $KitRoot 'station') -Destination $AppDir
    Show-Detail "  station app ............. done"
    Copy-Item -Recurse -Force -Path (Join-Path $KitRoot 'vendor') -Destination $AppDir
    Show-Detail "  shared libraries ........ done"
    Copy-Item -Force -Path $PinnedLock -Destination $AppDir
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
        Die 'NET-001' "Could not download Python" "Install Python 3.11+ from python.org then re-run setup."
    }
    Start-Process -FilePath $Installer -ArgumentList 'InstallAllUsers=0 PrependPath=1 Include_test=0 Include_launcher=1 /quiet' -Wait
    $env:PATH = [System.Environment]::GetEnvironmentVariable('PATH','Machine') + ';' + [System.Environment]::GetEnvironmentVariable('PATH','User')
    $PyBin = 'py'; $PyFlag = '-3.12'
}

# Resolve actual python.exe path so venv does not get py-launcher flags
$ResolveArgs = if ($PyFlag) { @($PyFlag, '-c', 'import sys; print(sys.executable)') } else { @('-c', 'import sys; print(sys.executable)') }
$ResolveR    = Run-Safe -Exe $PyBin -ExeArgs $ResolveArgs
$ActualPy    = $ResolveR.Out.Trim().Split("`n")[0].Trim()
if ($ActualPy -and (Test-Path $ActualPy)) {
    $PyBin = $ActualPy
    $PyFlag = $null
}

$VerArgs = if ($PyFlag) { @($PyFlag, '--version') } else { @('--version') }
$VerR    = Run-Safe -Exe $PyBin -ExeArgs $VerArgs
$PyVer   = if ($VerR.Out) { $VerR.Out.Trim() } else { 'Python (found)' }
Show-Ok ("Found: $PyVer")
Show-Detail ("Path : $PyBin")

# ==========================================================================
# STEP 3  Virtualenv + packages
# ==========================================================================
Show-Step "STEP 3/4  Setting up the Station engine"

$Vpy       = Join-Path $EnvDir 'Scripts\python.exe'
$LockHash  = (Get-FileHash -Algorithm SHA256 $PinnedLock).Hash
$Marker    = Join-Path $EnvDir '.deps-hash'
$OldHash   = if (Test-Path $Marker) { (Get-Content $Marker -Raw -ErrorAction SilentlyContinue).Trim() } else { '' }

# Wipe env if previous install was incomplete (marker missing = hash mismatch)
if ((Test-Path $Vpy) -and ($OldHash -ne $LockHash)) {
    Show-Detail "Previous install was incomplete - clearing environment..."
    Remove-Item -Recurse -Force -Path $EnvDir -ErrorAction SilentlyContinue
    Remove-Item -Force -Path $Marker -ErrorAction SilentlyContinue
    Show-Ok "Cleared - will reinstall cleanly"
    $OldHash = ''
}

if (-not (Test-Path $Vpy)) {
    try { Stop-Transcript | Out-Null } catch {}
    Write-Host ""
    Write-Host "  Creating Python environment - this takes 30-60 seconds..." -ForegroundColor Cyan
    Write-Host ("  Location: $EnvDir") -ForegroundColor DarkGray
    Write-Host "  (Python sets up an isolated folder - no output is normal)" -ForegroundColor DarkGray
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

if ($OldHash -ne $LockHash) {

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

    Write-Host ""
    Write-Host "  ========================================================" -ForegroundColor Cyan
    Write-Host ("  Installing {0} packages - watch every step below:" -f $Total) -ForegroundColor Cyan
    Write-Host "  ========================================================" -ForegroundColor Cyan

    # STOP transcript here so pip output goes RAW to the console
    # (transcript intercepts and hides real-time pip output)
    try { Stop-Transcript | Out-Null } catch {}

    $AllOk = $true
    $Idx   = 0
    foreach ($Pkg in $Packages) {
        $Idx++
        Write-Host ""
        Write-Host ("  ---- [{0}/{1}]  {2}" -f $Idx, $Total, $Pkg.Label) -ForegroundColor Yellow
        Write-Host ""

        # Online install - pip prints every line as it happens
        & $Vpy '-m' 'pip' 'install' $Pkg.Name
        $ExCode = $LASTEXITCODE

        # Fallback to offline bundle if online failed
        if ($ExCode -ne 0 -and $HasOffline) {
            Write-Host ""
            Write-Host ("  WARN: online failed for {0} - trying offline bundle..." -f $Pkg.Name) -ForegroundColor Yellow
            Write-Host ""
            & $Vpy '-m' 'pip' 'install' '--no-index' "--find-links=$Wheelhouse" $Pkg.Name
            $ExCode = $LASTEXITCODE
        }

        if ($ExCode -eq 0) {
            Write-Host ""
            Write-Host ("  OK  [{0}/{1}]  {2}" -f $Idx, $Total, $Pkg.Label) -ForegroundColor Green
        } else {
            Write-Host ""
            Write-Host ("  FAILED  [{0}/{1}]  {2}" -f $Idx, $Total, $Pkg.Label) -ForegroundColor Red
            Write-Host "  Check internet connection and run setup again." -ForegroundColor Yellow
            Write-Host ("  Log: $SetupLog") -ForegroundColor DarkGray
            $AllOk = $false
            exit 1
        }
    }

    # Resume transcript
    try { Start-Transcript -Path $SetupLog -Append | Out-Null } catch {}

    Write-Host ""
    Write-Host "  ========================================================" -ForegroundColor Green
    Write-Host ("  All {0} packages installed" -f $Total) -ForegroundColor Green
    Write-Host "  ========================================================" -ForegroundColor Green
    Write-Host ""

    $LockHash | Set-Content -Path $Marker -NoNewline

} else {
    Show-Ok "All packages already installed - skipping"
}

# ==========================================================================
# STEP 4  Firewall + stage exam packages
# ==========================================================================
Show-Step "STEP 4/4  Finishing up"

try {
    $Fw = Get-NetFirewallRule -DisplayName 'LAZEIMS Offline Station' -ErrorAction SilentlyContinue
    if (-not $Fw) {
        New-NetFirewallRule -DisplayName 'LAZEIMS Offline Station' -Direction Inbound `
            -LocalPort 8080 -Protocol TCP -Action Allow -Profile Private -ErrorAction SilentlyContinue | Out-Null
        Show-Detail "Firewall: port 8080 opened on private network"
    } else {
        Show-Detail "Firewall rule already exists"
    }
} catch {
    Show-Warn "Could not set firewall rule (non-fatal) - ask IT to allow TCP 8080 inbound"
}

# Stage bundled exam packages for automatic import on first boot
$PkgsDir = Join-Path $AppDir 'packages'
if (Test-Path $PkgsDir) {
    $PkgZips = Get-ChildItem -Path $PkgsDir -Filter '*.zip' -ErrorAction SilentlyContinue
    if ($PkgZips) {
        Show-Detail "Staging bundled exam packages for automatic import..."
        $PyHelper = Join-Path $env:TEMP 'laz_manifest.py'
        @(
            'import zipfile, json, sys',
            'with zipfile.ZipFile(sys.argv[1]) as z:',
            '    m = json.loads(z.read("manifest.json"))',
            'print(m.get("station_code","") + "|" + m.get("exam_id",""))'
        ) -join "`n" | Set-Content -Path $PyHelper -Encoding UTF8

        foreach ($PkgZip in $PkgZips) {
            $R = Run-Safe -Exe $Vpy -ExeArgs @($PyHelper, $PkgZip.FullName)
            if ($R.Code -eq 0) {
                $Parts   = $R.Out.Trim() -split '\|'
                $StnCode = $Parts[0].Trim()
                $ExId    = if ($Parts.Count -gt 1) { $Parts[1].Trim() } else { '' }
                if ($StnCode -and $ExId) {
                    $Pending = Join-Path $LazHome "stations\$StnCode\exams\$ExId\imports\pending"
                    New-Item -ItemType Directory -Force -Path $Pending | Out-Null
                    Copy-Item -Force -Path $PkgZip.FullName -Destination $Pending
                    Show-Ok ("Exam data ready: station=$StnCode")
                } else {
                    Show-Warn ("Could not read station/exam from: " + $PkgZip.Name)
                }
            } else {
                Show-Warn ("Could not read manifest from: " + $PkgZip.Name)
            }
        }
        Remove-Item -Path $PyHelper -ErrorAction SilentlyContinue
    }
}

Show-Ok "Setup complete"
Write-Host ""
Write-Host "  ========================================================" -ForegroundColor Green
Write-Host "       LAZEIMS STATION IS READY                           " -ForegroundColor Green
Write-Host "  ========================================================" -ForegroundColor Green
Write-Host ""
Write-Host "  Starting now. A browser window will open." -ForegroundColor White
Write-Host "  Sign in as station admin." -ForegroundColor White
Write-Host "  Data Enterers open http://<this-PC-IP>:8080" -ForegroundColor White
Write-Host ""

try { Stop-Transcript | Out-Null } catch {}

& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $LauncherDir 'start.ps1')
