# Removes Select & Play. Your music files are never touched.
$AppName = 'Select & Play'
$Dest = Join-Path $env:LOCALAPPDATA 'Programs\SelectAndPlay'
$Data = Join-Path $env:LOCALAPPDATA 'SelectAndPlay'
Write-Host "Uninstall $AppName" -ForegroundColor Yellow
Write-Host 'Your music files are NOT deleted.'
$ok = Read-Host 'Type Y and press Enter to remove the jukebox'
if ($ok -notmatch '^[yY]') { exit }
Get-CimInstance Win32_Process -Filter "Name like 'python%'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like "*SelectAndPlay*tray.py*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Remove-Item (Join-Path ([Environment]::GetFolderPath('Desktop')) "$AppName.lnk") -ErrorAction SilentlyContinue
Remove-Item (Join-Path ([Environment]::GetFolderPath('Startup')) "$AppName.lnk") -ErrorAction SilentlyContinue
Remove-Item (Join-Path ([Environment]::GetFolderPath('Programs')) $AppName) -Recurse -Force -ErrorAction SilentlyContinue
$keep = Read-Host 'Also delete your favourites, play lists and podcast list? Type Y for yes, Enter to keep them'
if ($keep -match '^[yY]') { Remove-Item $Data -Recurse -Force -ErrorAction SilentlyContinue }
Set-Location $env:TEMP
Remove-Item $Dest -Recurse -Force -ErrorAction SilentlyContinue
Write-Host 'Removed. (Python is left installed; remove it from Windows Settings > Apps if you want.)' -ForegroundColor Green
Read-Host 'Press Enter to close'
