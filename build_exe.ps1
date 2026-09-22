param()

# ASCII-only for compatibility with Windows PowerShell 5.1.
$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath($PSScriptRoot)
$resultFile = Join-Path ([IO.Path]::GetTempPath()) ("mcapviewer-build-$PID.txt")
$bootstrap = Join-Path $projectRoot 'bootstrap.ps1'
$buildRequirements = Join-Path $projectRoot 'requirements-build.txt'
$desktop = Join-Path $projectRoot 'desktop.py'
$versionInfo = Join-Path $projectRoot 'version_info.txt'
$distPath = Join-Path $projectRoot 'release'
$workPath = Join-Path $projectRoot 'build\pyinstaller'
$specPath = Join-Path $projectRoot 'build'
$exeName = 'MCAP' + [char]0x89C6 + [char]0x9891 + [char]0x67E5 + [char]0x770B + [char]0x5668

try {
    & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $bootstrap -ResultFile $resultFile
    if ($LASTEXITCODE -ne 0) { throw 'Runtime bootstrap failed.' }
    if (-not (Test-Path -LiteralPath $resultFile)) { throw 'Bootstrap returned no runtime path.' }
    $pythonw = [IO.File]::ReadAllText($resultFile, [Text.Encoding]::UTF8).Trim()
    $python = Join-Path (Split-Path -Parent $pythonw) 'python.exe'
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
        throw "Runtime Python does not exist: $python"
    }

    Write-Host 'Installing the locked build tool...'
    & $python -m pip install --disable-pip-version-check --only-binary=:all: -r $buildRequirements
    if ($LASTEXITCODE -ne 0) { throw 'Failed to install PyInstaller.' }

    if (-not (Test-Path -LiteralPath $specPath)) {
        New-Item -ItemType Directory -Path $specPath -Force | Out-Null
    }
    # Do not let unrelated developer tools on PATH satisfy DLL imports during
    # analysis. In particular, a Poppler icuuc.dll is ABI-incompatible with
    # the Windows system ICU that Qt intentionally uses.
    $runtimeScripts = Split-Path -Parent $python
    $windowsRoot = [Environment]::GetFolderPath('Windows')
    $env:PATH = ($runtimeScripts,
                 (Join-Path $windowsRoot 'System32'),
                 $windowsRoot) -join ';'
    Write-Host "Building $exeName.exe..."
    & $python -m PyInstaller --noconfirm --clean --onefile --windowed --noupx `
        --name $exeName `
        --distpath $distPath `
        --workpath $workPath `
        --specpath $specPath `
        --version-file $versionInfo `
        --collect-all mcap `
        --collect-all lz4 `
        --collect-all zstandard `
        --exclude-module tkinter `
        $desktop
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller build failed.' }

    $exe = Join-Path $distPath ($exeName + '.exe')
    if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) { throw 'Output EXE is missing.' }
    $size = (Get-Item -LiteralPath $exe).Length
    if ($size -lt 10MB) { throw "Output EXE is unexpectedly small: $size bytes" }
    Write-Host ''
    Write-Host "BUILD OK: $exe"
    Write-Host ('SIZE: {0:N1} MB' -f ($size / 1MB))
    exit 0
} catch {
    Write-Host ''
    Write-Host "BUILD FAILED: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
} finally {
    if (Test-Path -LiteralPath $resultFile) {
        Remove-Item -LiteralPath $resultFile -Force -ErrorAction SilentlyContinue
    }
}
