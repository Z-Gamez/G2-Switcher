# Copies the built exe to %LOCALAPPDATA%\Programs\G2 Switcher and adds Start menu
# and desktop shortcuts. Run after build.bat:  powershell -ExecutionPolicy Bypass -File install.ps1
$ErrorActionPreference = "Stop"
$src = Join-Path $PSScriptRoot "dist\G2 Switcher.exe"
if (-not (Test-Path $src)) { throw "Build first: run build.bat" }

$dir = Join-Path $env:LOCALAPPDATA "Programs\G2 Switcher"
New-Item -ItemType Directory -Force $dir | Out-Null
Get-Process "G2 Switcher" -ErrorAction SilentlyContinue | Stop-Process -Force
$exe = Join-Path $dir "G2 Switcher.exe"
Copy-Item $src $exe -Force

$shell = New-Object -ComObject WScript.Shell
$links = @(
    (Join-Path ([Environment]::GetFolderPath("Programs")) "G2 Switcher.lnk"),
    (Join-Path ([Environment]::GetFolderPath("Desktop")) "G2 Switcher.lnk")
)
foreach ($path in $links) {
    $lnk = $shell.CreateShortcut($path)
    $lnk.TargetPath = $exe
    $lnk.WorkingDirectory = $dir
    $lnk.IconLocation = "$exe,0"
    $lnk.Description = "Switch Even Terminal between Claude, OpenRouter and Ollama"
    $lnk.Save()
}
Write-Host "Installed to $exe"
Write-Host "To pin: open G2 Switcher, right-click its taskbar icon, choose 'Pin to taskbar'."
