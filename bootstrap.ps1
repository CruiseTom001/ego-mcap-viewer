param([string]$ResultFile = '')

# Self-healing Windows 10/11 bootstrapper. This file intentionally contains
# ASCII only so Windows PowerShell 5.1 can parse it without a UTF-8 BOM.
$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath($PSScriptRoot)
$requirements = Join-Path $projectRoot 'requirements-win10.txt'
$pythonCheck = Join-Path $projectRoot 'pycheck.py'
$minimumBuild = 17763
$pinnedPip = '26.2.1'

function Test-DirectoryWritable([string]$Path) {
    try {
        if (-not (Test-Path -LiteralPath $Path)) {
            New-Item -ItemType Directory -Path $Path -Force | Out-Null
        }
        $probe = Join-Path $Path ('.write-probe-' + $PID)
        $stream = [IO.File]::Open($probe, [IO.FileMode]::CreateNew,
            [IO.FileAccess]::Write, [IO.FileShare]::None)
        $stream.Dispose()
        Remove-Item -LiteralPath $probe -Force
        return $true
    } catch {
        return $false
    }
}

$localAppData = [Environment]::GetFolderPath('LocalApplicationData')
$fallbackRoot = Join-Path $localAppData 'MCAPViewer'
$runtimeParent = if (Test-DirectoryWritable $projectRoot) { $projectRoot } else { $fallbackRoot }
if (-not (Test-Path -LiteralPath $runtimeParent)) {
    New-Item -ItemType Directory -Path $runtimeParent -Force | Out-Null
}
$runtimeDir = Join-Path $runtimeParent 'runtime'
$runtimePython = Join-Path $runtimeDir 'Scripts\python.exe'
$runtimePythonw = Join-Path $runtimeDir 'Scripts\pythonw.exe'
$startupLog = Join-Path $runtimeParent 'startup-error.log'
$candidateLog = Join-Path $projectRoot 'python-candidates.log'

function Write-ResultPath {
    if ($ResultFile) {
        $utf8NoBom = New-Object Text.UTF8Encoding($false)
        [IO.File]::WriteAllText([IO.Path]::GetFullPath($ResultFile), $runtimePythonw,
            $utf8NoBom)
    }
}

function Write-StartupLog([string]$Message) {
    try {
        $stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
        Add-Content -LiteralPath $startupLog -Encoding UTF8 -Value ("[$stamp] $Message")
    } catch {}
}

function Get-WindowsBuild {
    try {
        $item = Get-ItemProperty -LiteralPath 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion'
        return [int]$item.CurrentBuildNumber
    } catch {
        return [Environment]::OSVersion.Version.Build
    }
}

function Remove-RuntimeTree([string]$Path) {
    # Only a direct child with an explicit temporary-runtime name may be removed.
    $full = [IO.Path]::GetFullPath($Path)
    $parent = [IO.Path]::GetFullPath((Split-Path -Parent $full))
    $leaf = Split-Path -Leaf $full
    if ($parent -ne $runtimeParent -or
        ($leaf -notlike 'runtime.building-*' -and $leaf -notlike 'runtime.broken-*')) {
        throw "Refusing to remove an unsafe path: $full"
    }
    if (Test-Path -LiteralPath $full) {
        Remove-Item -LiteralPath $full -Recurse -Force
    }
}

function Test-Python([string]$Path, [switch]$Runtime) {
    if (-not $Path -or -not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return $false
    }
    try {
        if ($Runtime) {
            $output = & $Path $pythonCheck --runtime --requirements $requirements --log 2>&1
        } else {
            $output = & $Path $pythonCheck --log 2>&1
        }
        $code = $LASTEXITCODE
        if ($code -eq 0 -and $Runtime) {
            $desktopCheck = Join-Path $projectRoot 'desktop.py'
            $desktopOutput = & $Path $desktopCheck --self-check 2>&1
            $code = $LASTEXITCODE
            if ($desktopOutput) { $output = @($output) + @($desktopOutput) }
        }
        if ($code -eq 0) {
            if ($output) { Write-Host ($output -join [Environment]::NewLine) }
            return $true
        }
        if ($Runtime -and $output) {
            Write-StartupLog ("Runtime validation failed:`n" +
                ($output -join [Environment]::NewLine))
        }
    } catch {
        if ($Runtime) { Write-StartupLog "Cannot run existing runtime: $($_.Exception.Message)" }
    }
    return $false
}

function Add-Candidate($List, $Seen, [string]$Path) {
    if (-not $Path) { return }
    $candidate = [Environment]::ExpandEnvironmentVariables($Path.Trim().Trim('"'))
    try { $candidate = [IO.Path]::GetFullPath($candidate) } catch { return }
    if ((Test-Path -LiteralPath $candidate -PathType Leaf) -and $Seen.Add($candidate)) {
        $List.Add($candidate)
    }
}

function Find-BasePython {
    $items = New-Object 'Collections.Generic.List[string]'
    $seen = New-Object 'Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)

    $hint = Join-Path $projectRoot 'python.txt'
    if (Test-Path -LiteralPath $hint) {
        $line = Get-Content -LiteralPath $hint -ErrorAction SilentlyContinue |
            Where-Object { $_.Trim() } | Select-Object -First 1
        if ($line) { Add-Candidate $items $seen $line }
    }

    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($launcher) {
        foreach ($tag in @('3.12', '3.11', '3.13', '3.10')) {
            try {
                $found = & $launcher.Source "-$tag" -c 'import sys;print(sys.executable)' 2>$null
                if ($LASTEXITCODE -eq 0 -and $found) {
                    Add-Candidate $items $seen ($found | Select-Object -Last 1)
                }
            } catch {}
        }
    }

    try {
        Get-Command python.exe -All -ErrorAction SilentlyContinue | ForEach-Object {
            Add-Candidate $items $seen $_.Source
        }
    } catch {}

    # A runnable old venv can disclose its real base interpreter.
    if (Test-Path -LiteralPath $runtimePython) {
        try {
            $base = & $runtimePython -c 'import os,sys;print(os.path.join(sys.base_prefix,"python.exe"))' 2>$null
            if ($LASTEXITCODE -eq 0 -and $base) {
                Add-Candidate $items $seen ($base | Select-Object -Last 1)
            }
        } catch {}
    }

    $userDir = [Environment]::GetFolderPath('UserProfile')
    $localDir = [Environment]::GetFolderPath('LocalApplicationData')
    $programDataDir = [Environment]::GetFolderPath('CommonApplicationData')
    $programFilesDir = [Environment]::GetFolderPath('ProgramFiles')
    foreach ($path in @(
        (Join-Path $userDir 'anaconda3\python.exe'),
        (Join-Path $userDir 'miniconda3\python.exe'),
        (Join-Path $localDir 'Programs\Python\Python312\python.exe'),
        (Join-Path $localDir 'Programs\Python\Python311\python.exe'),
        (Join-Path $localDir 'Programs\Python\Python313\python.exe'),
        (Join-Path $localDir 'Programs\Python\Python310\python.exe'),
        (Join-Path $programDataDir 'anaconda3\python.exe'),
        (Join-Path $programDataDir 'miniconda3\python.exe'),
        (Join-Path $programFilesDir 'Python312\python.exe'),
        (Join-Path $programFilesDir 'Python311\python.exe'),
        (Join-Path $programFilesDir 'Python313\python.exe'),
        (Join-Path $programFilesDir 'Python310\python.exe')
    )) { Add-Candidate $items $seen $path }

    foreach ($item in $items) {
        if (Test-Python $item) { return $item }
    }
    return $null
}

$lockPath = Join-Path $runtimeParent '.bootstrap.lock'
$lockStream = $null
try {
    for ($attempt = 0; $attempt -lt 120 -and $null -eq $lockStream; $attempt++) {
        try {
            $lockStream = [IO.File]::Open($lockPath, [IO.FileMode]::OpenOrCreate,
                [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
        } catch {
            if ($attempt -eq 0) { Write-Host 'Another launch is preparing the runtime. Waiting...' }
            Start-Sleep -Seconds 5
        }
    }
    if ($null -eq $lockStream) { throw 'Timed out waiting for the runtime build lock.' }

    if (-not [Environment]::Is64BitOperatingSystem) {
        throw 'This application requires 64-bit Windows.'
    }
    $build = Get-WindowsBuild
    if ($build -lt $minimumBuild) {
        throw "Windows build $build is too old. Windows 10 1809 (17763) or newer is required."
    }
    Write-Host "Windows build $build x64: OK"

    if (Test-Python $runtimePython -Runtime) {
        Write-Host 'Existing runtime validation: OK'
        Write-ResultPath
        exit 0
    }

    $basePython = Find-BasePython
    if (-not $basePython) {
        throw ('No supported 64-bit Python was found. Install Python 3.12 x64, ' +
               'or put the full path to python.exe in python.txt.')
    }
    Write-Host "Base interpreter: $basePython"

    $building = Join-Path $runtimeParent ("runtime.building-$PID")
    # Clean up stale runtime.building-* directories whose owner PID is gone
    # (previous crash / kill). Never touch dirs owned by live processes.
    Get-ChildItem -LiteralPath $runtimeParent -Directory -Filter 'runtime.building-*' `
        -ErrorAction SilentlyContinue | ForEach-Object {
        $ownerPid = $null
        if ($_.Name -match '^runtime\.building-(\d+)$') { $ownerPid = [int]$Matches[1] }
        if ($null -ne $ownerPid -and $ownerPid -ne $PID) {
            $alive = $true
            try { $null = Get-Process -Id $ownerPid -ErrorAction Stop } catch { $alive = $false }
            if (-not $alive) {
                try {
                    Remove-RuntimeTree $_.FullName
                    Write-Host ("Cleaned stale build dir: " + $_.Name)
                } catch {
                    Write-StartupLog ("Stale build dir cleanup failed: " + $_.Exception.Message)
                }
            }
        }
    }
    if (Test-Path -LiteralPath $building) { Remove-RuntimeTree $building }
    Write-Host 'Creating an isolated runtime...'
    & $basePython -m venv $building
    if ($LASTEXITCODE -ne 0) { throw 'Failed to create the runtime.' }

    $buildingPython = Join-Path $building 'Scripts\python.exe'
    Write-Host "Installing pip==$pinnedPip..."
    & $buildingPython -m pip install --disable-pip-version-check --only-binary=:all: "pip==$pinnedPip"
    if ($LASTEXITCODE -ne 0) { throw 'Failed to install the pinned pip version.' }

    Write-Host 'Installing locked Windows dependencies (about 140 MB on first run)...'
    & $buildingPython -m pip install --disable-pip-version-check --only-binary=:all: -r $requirements
    if ($LASTEXITCODE -ne 0) { throw 'Failed to install runtime dependencies.' }
    if (-not (Test-Python $buildingPython -Runtime)) { throw 'New runtime validation failed.' }

    $backup = Join-Path $runtimeParent ("runtime.broken-$(Get-Date -Format 'yyyyMMdd-HHmmss')-$PID")
    if (Test-Path -LiteralPath $runtimeDir) {
        Move-Item -LiteralPath $runtimeDir -Destination $backup
    }
    try {
        Move-Item -LiteralPath $building -Destination $runtimeDir
    } catch {
        if ((Test-Path -LiteralPath $backup) -and -not (Test-Path -LiteralPath $runtimeDir)) {
            Move-Item -LiteralPath $backup -Destination $runtimeDir
        }
        throw
    }
    if (Test-Path -LiteralPath $backup) {
        try { Remove-RuntimeTree $backup } catch { Write-StartupLog "Old runtime cleanup failed: $_" }
    }
    Write-Host 'Runtime is ready.'
    Write-ResultPath
    exit 0
} catch {
    # Failure: clean up our own runtime.building-<PID> so nothing is left behind.
    if ((Get-Variable -Name building -ErrorAction SilentlyContinue) -and $building) {
        if (Test-Path -LiteralPath $building) {
            try { Remove-RuntimeTree $building } catch { }
        }
    }
    $message = $_.Exception.Message
    Write-StartupLog $message
    Write-Host ''
    Write-Host "[STARTUP FAILED] $message" -ForegroundColor Red
    Write-Host "Details: $startupLog"
    exit 1
} finally {
    if ($lockStream) { $lockStream.Dispose() }
}
