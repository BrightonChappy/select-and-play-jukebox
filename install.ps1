# Select & Play installer. Run by "Install Select & Play.cmd" (double-click that, not this).
# It installs everything the jukebox needs, then opens it:
#   1. the jukebox program        -> %LOCALAPPDATA%\Programs\SelectAndPlay
#   2. Python 3.12 (if missing)   -> per-user install, no admin needed
#   3. the Python add-ons it uses (mutagen, pywebview, imageio-ffmpeg, pillow, yt-dlp)
#   4. Microsoft Edge WebView2 (if missing; it is already on almost every Windows 10/11 PC)
#   5. Desktop + Start menu shortcuts, and it waits in the tray (by the clock) whenever Windows starts
# Running it again later updates the program. Your music, favourites and podcasts are kept.

# 'Continue', not 'Stop': in Windows PowerShell 5 a harmless warning from pip or Python on
# stderr counts as an error under 'Stop' and would close this window without a word.
# Real failures are checked for by hand below, and anything unexpected is caught by the trap.
param([switch]$Update)
$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'   # makes downloads much faster
$Log = Join-Path $env:TEMP 'SelectAndPlay-install-log.txt'
try { Start-Transcript -Path $Log -Force | Out-Null } catch {}
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$Repo     = 'BrightonChappy/select-and-play-jukebox'
$AppName  = 'Select & Play'
$Dest     = if ($env:SAP_TEST) { Join-Path $env:SAP_TEST 'Programs' } else { Join-Path $env:LOCALAPPDATA 'Programs\SelectAndPlay' }
$Temp     = Join-Path $env:TEMP 'SelectAndPlaySetup'
$PyUrl    = 'https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe'
$WV2Url   = 'https://go.microsoft.com/fwlink/p/?LinkId=2124703'
$Packages = @('mutagen', 'pywebview', 'pystray', 'imageio-ffmpeg', 'pillow', 'yt-dlp')

function Say($text, $colour = 'White') { Write-Host $text -ForegroundColor $colour }
function Step($n, $text) { Write-Host ''; Write-Host "[$n/5] $text" -ForegroundColor Yellow }
function Fail($text) {
    Write-Host ''; Write-Host "Sorry, something went wrong: $text" -ForegroundColor Red
    Write-Host 'Take a photo of this window and send it to whoever gave you the jukebox.' -ForegroundColor Red
    Write-Host "A full record of what happened is in: $Log" -ForegroundColor Red
    try { Stop-Transcript | Out-Null } catch {}
    Read-Host 'Press Enter to close'; exit 1
}
# anything unexpected: show it and wait, never just vanish
trap { Fail ("$($_.Exception.Message) (installer line $($_.InvocationInfo.ScriptLineNumber))") }

Clear-Host
Say '=============================================' Red
Say "   $AppName  -  jukebox installer" Yellow
Say '=============================================' Red
Say 'This sets everything up. It takes 2 to 5 minutes.'
Say 'Leave this window open until it says DONE.'
New-Item -ItemType Directory -Force -Path $Temp | Out-Null

# ---------------------------------------------------------------- 1. the program
Step 1 'Getting the jukebox program'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$src = if ($here) { Join-Path $here 'app' } else { '' }
if (-not ($src -and (Test-Path (Join-Path $src 'server.py')))) {
    # run from inside the zip, or on its own: fetch the newest copy from GitHub
    try {
        $zip = Join-Path $Temp 'app.zip'
        Invoke-WebRequest "https://github.com/$Repo/archive/refs/heads/main.zip" -OutFile $zip -UseBasicParsing -ErrorAction Stop
        $unz = Join-Path $Temp 'unzipped'
        if (Test-Path $unz) { Remove-Item $unz -Recurse -Force }
        Expand-Archive $zip -DestinationPath $unz -Force -ErrorAction Stop
        $src = Join-Path (Get-ChildItem $unz -Directory | Select-Object -First 1).FullName 'app'
    } catch { Fail "couldn't download the program from GitHub. Check the internet is working. ($($_.Exception.Message))" }
}
# stop a running copy so its files can be replaced
Get-CimInstance Win32_Process -Filter "Name like 'python%'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like "*SelectAndPlay*tray.py*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
New-Item -ItemType Directory -Force -Path $Dest | Out-Null
Copy-Item (Join-Path $src '*') $Dest -Recurse -Force
if (-not (Test-Path (Join-Path $Dest 'tray.py'))) { Fail "couldn't copy the program into $Dest" }
Copy-Item $MyInvocation.MyCommand.Path (Join-Path $Dest 'install.ps1') -Force -ErrorAction SilentlyContinue
Say "   Program is in $Dest" Green

# ---------------------------------------------------------------- 2. Python
Step 2 'Checking for Python'
function Find-Python {
    $cands = @()
    $cands += Get-ChildItem "$env:LOCALAPPDATA\Programs\Python\Python31[0-3]\python.exe" -ErrorAction SilentlyContinue | ForEach-Object FullName
    $cands += Get-ChildItem "$env:ProgramFiles\Python31[0-3]\python.exe" -ErrorAction SilentlyContinue | ForEach-Object FullName
    foreach ($c in @('py', 'python')) {
        $cmd = Get-Command $c -ErrorAction SilentlyContinue
        if ($cmd -and $cmd.Source -notlike '*WindowsApps*') {
            try { $cands += (& $cmd.Source -c 'import sys; print(sys.executable)' 2>$null) } catch {}
        }
    }
    foreach ($p in ($cands | Where-Object { $_ } | Select-Object -Unique)) {
        try {
            $v = & $p -c 'import sys; print(*sys.version_info[:2], sep=chr(46))' 2>$null   # no quote marks: PowerShell 5 strips them
            if ($v -in @('3.10', '3.11', '3.12', '3.13')) { return $p }
        } catch {}
    }
    return $null
}
$py = Find-Python
if ($py) {
    Say "   Found Python: $py" Green
} else {
    Say '   Python is not installed. Installing it now (about 25 MB)...'
    $ok = $false
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        try {
            winget install --id Python.Python.3.12 -e --scope user --silent --accept-package-agreements --accept-source-agreements | Out-Null
            $py = Find-Python; $ok = [bool]$py
        } catch {}
    }
    if (-not $ok) {
        try {
            $exe = Join-Path $Temp 'python-setup.exe'
            Invoke-WebRequest $PyUrl -OutFile $exe -UseBasicParsing -ErrorAction Stop
            Start-Process $exe -ArgumentList '/quiet InstallAllUsers=0 PrependPath=1 Include_test=0 Include_launcher=1' -Wait
            $py = Find-Python
        } catch {}
    }
    if (-not $py) { Fail "Python didn't install. You can install it yourself from https://www.python.org/downloads/ (tick 'Add python.exe to PATH'), then run this installer again." }
    Say "   Python installed: $py" Green
}
$pyw = Join-Path (Split-Path $py) 'pythonw.exe'

# ---------------------------------------------------------------- 3. add-ons
Step 3 'Installing the add-ons the jukebox uses (this is the slow bit)'
if (-not $env:SAP_TEST) { & $py -m pip install --user --upgrade --quiet --disable-pip-version-check --no-warn-script-location @Packages 2>&1 | Out-Host }
& $py -c 'import mutagen, webview, pystray, imageio_ffmpeg, PIL' 2>$null
if ($LASTEXITCODE -ne 0) {
    # try once more and show what goes wrong
    & $py -m pip install --user --upgrade @Packages
    & $py -c 'import mutagen, webview, pystray, imageio_ffmpeg, PIL'
    if ($LASTEXITCODE -ne 0) { Fail 'the add-ons did not install (see the messages above).' }
}
Say '   Add-ons ready' Green

# ---------------------------------------------------------------- 4. WebView2 (the window the jukebox draws in)
Step 4 'Checking the Microsoft Edge WebView2 component'
$wvKeys = @('HKLM:\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}',
            'HKLM:\SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}',
            'HKCU:\SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}')
$hasWV = $wvKeys | Where-Object { (Get-ItemProperty $_ -ErrorAction SilentlyContinue).pv -and (Get-ItemProperty $_).pv -ne '0.0.0.0' }
if ($hasWV) { Say '   Already there' Green } else {
    try {
        $wv = Join-Path $Temp 'webview2.exe'
        Invoke-WebRequest $WV2Url -OutFile $wv -UseBasicParsing -ErrorAction Stop
        Start-Process $wv -ArgumentList '/silent /install' -Wait
        Say '   Installed' Green
    } catch { Say "   Couldn't install it automatically. If the jukebox window stays blank, get it from https://developer.microsoft.com/microsoft-edge/webview2/" Red }
}

# ---------------------------------------------------------------- 5. shortcuts
if ($env:SAP_TEST) { Say 'TEST MODE: stopping before shortcuts' Green; exit 0 }
Step 5 'Adding shortcuts'
$shell = New-Object -ComObject WScript.Shell
function Make-Shortcut($path, $target, $arguments, $icon) {
    $s = $shell.CreateShortcut($path)
    $s.TargetPath = $target; $s.Arguments = $arguments; $s.WorkingDirectory = $Dest
    $s.IconLocation = $icon; $s.Description = "$AppName jukebox"; $s.Save()
}
$icon = Join-Path $Dest 'wurlitzer.ico'
$runArgs = '"' + (Join-Path $Dest 'tray.py') + '"'
$startMenu = Join-Path ([Environment]::GetFolderPath('Programs')) $AppName
New-Item -ItemType Directory -Force -Path $startMenu | Out-Null
Make-Shortcut (Join-Path ([Environment]::GetFolderPath('Desktop')) "$AppName.lnk") $pyw $runArgs $icon
Make-Shortcut (Join-Path $startMenu "$AppName.lnk") $pyw $runArgs $icon
Make-Shortcut (Join-Path $startMenu "Uninstall $AppName.lnk") 'powershell.exe' ('-NoProfile -ExecutionPolicy Bypass -File "' + (Join-Path $Dest 'uninstall.ps1') + '"') $icon
Make-Shortcut (Join-Path $startMenu "Update $AppName.lnk") 'powershell.exe' ('-NoProfile -ExecutionPolicy Bypass -File "' + (Join-Path $Dest 'install.ps1') + '"') $icon
Say '   Desktop and Start menu shortcuts added' Green
$startup = Join-Path ([Environment]::GetFolderPath('Startup')) "$AppName.lnk"
# starts hidden in the tray when Windows starts (turn off in Task Manager > Startup apps)
Make-Shortcut $startup $pyw ($runArgs + ' --hidden') $icon
Say '   It will wait in the tray (by the clock) whenever Windows starts' Green

Write-Host ''
Say '=============================================' Green
Say '   DONE. Opening the jukebox now...' Green
Say '=============================================' Green
Say "It lives in the tray by the clock: click its icon to open it."
Say "Closing the window hides it to the tray. To stop it, right-click the tray icon and choose Quit."
try { Stop-Transcript | Out-Null } catch {}
Start-Process $pyw -ArgumentList $runArgs -WorkingDirectory $Dest
Start-Sleep -Seconds 6
