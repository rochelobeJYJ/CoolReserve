param(
    [string]$PythonPath = '',
    [switch]$CheckOnly
)

$ErrorActionPreference = 'Stop'
$coolRoot = Split-Path -Parent $PSScriptRoot
$coolVenv = Join-Path $coolRoot '.venv'
$coolVenvPython = Join-Path $coolVenv 'Scripts\python.exe'
$coolProbe = 'import sys; assert sys.version_info[:2] in ((3,13),(3,14)); assert sys.maxsize > 2**32; print(sys.executable)'

function Test-CoolPython([string]$Candidate, [string[]]$Arguments = @()) {
    try {
        $coolResult = @(& $Candidate @Arguments -I -c $coolProbe 2>$null)
        if ($LASTEXITCODE -eq 0 -and $coolResult -and (Test-Path -LiteralPath $coolResult[-1] -PathType Leaf)) {
            return [string]$coolResult[-1]
        }
    } catch { }
    return $null
}

try {
    if (Test-Path -LiteralPath $coolVenvPython -PathType Leaf) {
        if (-not (Test-CoolPython $coolVenvPython)) { throw 'The existing .venv requires 64-bit Python 3.13 or 3.14. See docs/FIRST_RUN.md.' }
    } else {
        if ($CheckOnly) { throw 'No local environment. Run INSTALL.cmd first.' }
        $coolPython = $null
        if ($PythonPath) {
            $coolPython = Test-CoolPython $PythonPath
        } else {
            $coolLauncher = Get-Command py.exe -ErrorAction SilentlyContinue
            if ($coolLauncher) {
                foreach ($coolVersion in @('-3.14', '-3.13')) {
                    $coolPython = Test-CoolPython $coolLauncher.Source @($coolVersion)
                    if ($coolPython) { break }
                }
            }
            if (-not $coolPython) {
                $coolCommand = Get-Command python.exe -ErrorAction SilentlyContinue
                if ($coolCommand) { $coolPython = Test-CoolPython $coolCommand.Source }
            }
        }
        if (-not $coolPython) { throw 'Install 64-bit Python 3.13 or 3.14 from https://www.python.org/downloads/windows/ then run INSTALL.cmd again.' }
        Write-Output 'Creating the local Python environment...'
        & $coolPython -I -m venv $coolVenv
        if ($LASTEXITCODE -ne 0) { throw 'Could not create .venv. Use a writable folder and try again.' }
    }
    if (-not $CheckOnly) {
        Write-Output 'Installing pinned dependencies from PyPI...'
        & $coolVenvPython -I -m pip --disable-pip-version-check install -r (Join-Path $coolRoot 'requirements.txt')
        if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed. Check network access and run INSTALL.cmd again.' }
    }
    & $coolVenvPython -I -X utf8 (Join-Path $PSScriptRoot 'check_environment.py')
    if ($LASTEXITCODE -ne 0) { throw 'Environment validation failed. Run INSTALL.cmd again.' }
    Write-Output 'CoolReserve is installed. Run START.cmd.'
    exit 0
} catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 1
}
