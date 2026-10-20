# Build a double-clickable Aria desktop app (Electron shell) and shortcuts.
#
# Portable on purpose: it copies the Electron runtime already installed in
# agent-desktop/node_modules and drops the app beside it. No installer, no
# admin rights, nothing to uninstall - deleting dist\Aria removes it.
$ErrorActionPreference = 'Stop'
$Root = $PSScriptRoot
$Src = Join-Path $Root 'agent-desktop'
$Electron = Join-Path $Src 'node_modules\electron\dist'
$Out = Join-Path $Src 'dist\Aria'
$AppDir = Join-Path $Out 'resources\app'

if (-not (Test-Path -LiteralPath (Join-Path $Electron 'electron.exe'))) {
    throw "Electron runtime missing. Run: cd agent-desktop; npm ci"
}

Write-Host "[aria] building $Out"
if (Test-Path -LiteralPath $Out) { Remove-Item -LiteralPath $Out -Recurse -Force }
New-Item -ItemType Directory -Path $Out | Out-Null
Copy-Item -Path (Join-Path $Electron '*') -Destination $Out -Recurse -Force
Move-Item -LiteralPath (Join-Path $Out 'electron.exe') -Destination (Join-Path $Out 'Aria.exe')
if (Test-Path -LiteralPath (Join-Path $Out 'resources\default_app.asar')) {
    Remove-Item -LiteralPath (Join-Path $Out 'resources\default_app.asar') -Force
}
New-Item -ItemType Directory -Path $AppDir | Out-Null
foreach ($f in @('main.cjs', 'preload.cjs', 'package.json', 'icon.png')) {
    Copy-Item -LiteralPath (Join-Path $Src $f) -Destination $AppDir -Force
}
Copy-Item -LiteralPath (Join-Path $Src 'icon.ico') -Destination $Out -Force

# The packaged main.cjs reads this to find the repo it drives; the backends
# stay where they are, so app updates and backend updates are independent.
Set-Content -LiteralPath (Join-Path $Out 'aria-root.txt') -Value $Root -Encoding ASCII

$shell = New-Object -ComObject WScript.Shell
$targets = @(
    (Join-Path ([Environment]::GetFolderPath('Desktop')) 'Aria.lnk'),
    (Join-Path ([Environment]::GetFolderPath('StartMenu')) 'Programs\Aria.lnk')
)
foreach ($lnkPath in $targets) {
    try {
        $lnk = $shell.CreateShortcut($lnkPath)
        $lnk.TargetPath = (Join-Path $Out 'Aria.exe')
        $lnk.WorkingDirectory = $Out
        $lnk.IconLocation = (Join-Path $Out 'icon.ico')
        $lnk.Description = 'Aria agent console'
        $lnk.Save()
        Write-Host "[aria] shortcut $lnkPath"
    } catch {
        Write-Warning "could not write $lnkPath : $_"
    }
}
Write-Host "[aria] done - launch $Out\Aria.exe"
