param(
    [string]$DataDirectory = '',
    [string]$PythonPath = '',
    [switch]$Offline,
    [switch]$NoBrowser
)

$ErrorActionPreference = 'Stop'
$coolRoot = Split-Path -Parent $PSScriptRoot
if (-not $PythonPath) { $PythonPath = Join-Path $coolRoot '.venv\Scripts\python.exe' }
if (-not $DataDirectory) { $DataDirectory = Join-Path $env:LOCALAPPDATA 'CoolReserve' }
$DataDirectory = [IO.Path]::GetFullPath($DataDirectory)
$coolSessionPath = Join-Path $DataDirectory 'session.json'
$script:coolVerifiedSessionUrl = $null

function Test-CoolService {
    $script:coolVerifiedSessionUrl = $null
    try {
        $coolSession = Get-Content -LiteralPath $coolSessionPath -Raw -Encoding UTF8 | ConvertFrom-Json
        $coolUri = [Uri]$coolSession.url
        if ($coolUri.Scheme -ne 'http' -or $coolUri.Host -ne '127.0.0.1' -or
            $coolUri.AbsolutePath -ne '/' -or $coolUri.Query -or $coolUri.UserInfo -or
            -not $coolUri.Fragment) { return $false }
        $coolState = Invoke-RestMethod -Uri ($coolUri.GetLeftPart([UriPartial]::Authority) + '/api/state') `
            -Headers @{'X-Cool-Token' = $coolUri.Fragment.Substring(1)} -TimeoutSec 2
        $coolMatches = ($coolState.version -and
            [IO.Path]::GetFullPath($coolState.data_dir) -eq $DataDirectory -and
            [bool]$coolState.offline -eq [bool]$Offline)
        if ($coolMatches) {
            $script:coolVerifiedSessionUrl = $coolUri.AbsoluteUri
            return $true
        }
        return $false
    } catch { return $false }
}

try {
    if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
        throw 'Python environment is missing. See README.md.'
    }
    # Reuse before spawning. A delayed duplicate child could otherwise acquire
    # the lock just after the existing server is asked to shut down.
    if (Test-CoolService) {
        if (-not $NoBrowser) {
            try {
                Start-Process -FilePath $script:coolVerifiedSessionUrl
            } catch {
                # Start-Process errors can include FilePath and its secret token.
                throw 'CoolReserve is running, but the browser could not be opened. Run START.cmd again.'
            }
        }
        Write-Output 'CoolReserve is ready.'
        exit 0
    }
    # Start-Process joins arguments on Windows: quote each path explicitly.
    $coolArguments = @('-X', 'utf8', ('"' + (Join-Path $coolRoot 'server.py') + '"'),
        '--data-dir', ('"' + $DataDirectory + '"'))
    if ($Offline) { $coolArguments += '--offline' }
    if (-not $NoBrowser) { $coolArguments += '--open' }
    $coolChild = Start-Process -FilePath $PythonPath -ArgumentList $coolArguments `
        -WorkingDirectory $coolRoot -WindowStyle Hidden -PassThru
    $coolDeadline = [DateTime]::UtcNow.AddSeconds(20)
    do {
        $coolChild.Refresh()
        if ($coolChild.HasExited -and $coolChild.ExitCode -ne 0) {
            throw 'CoolReserve could not start. Run .venv\Scripts\python.exe server.py to see the error.'
        }
        if (Test-CoolService) { Write-Output 'CoolReserve is ready.'; exit 0 }
        Start-Sleep -Milliseconds 150
    } while ([DateTime]::UtcNow -lt $coolDeadline)
    throw 'CoolReserve did not become ready. Run START.cmd again or check README.md.'
} catch {
    Write-Error $_
    exit 1
}
