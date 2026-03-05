param(
    [int]$Count = 5
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$pythonPath = Join-Path $projectRoot ".venv\Scripts\python.exe"
$envFile = Join-Path $projectRoot ".env"

Push-Location $projectRoot
try {
    if (Test-Path $envFile) {
        # Load simple KEY=VALUE pairs from .env into current process environment.
        Get-Content $envFile | ForEach-Object {
            $line = $_.Trim()
            if (-not $line -or $line.StartsWith("#")) {
                return
            }

            $parts = $line -split "=", 2
            if ($parts.Count -ne 2) {
                return
            }

            $key = $parts[0].Trim()
            $value = $parts[1].Trim().Trim('"')
            if ($key) {
                [Environment]::SetEnvironmentVariable($key, $value, "Process")
            }
        }
    }

    if (-not (Test-Path $pythonPath)) {
        throw "Python executable not found at $pythonPath"
    }

    if ($Count -lt 0) {
        throw "Count must be >= 0"
    }

    for ($i = 1; $i -le $Count; $i++) {
        Write-Host "Saving task $i/$Count ..."

        # Send save then quit to the interactive prompt.
        "s`nq" | & $pythonPath autocurator.py next

        if ($LASTEXITCODE -ne 0) {
            throw "autocurator.py next failed at iteration $i"
        }

        Start-Sleep -Milliseconds 250
    }

    Write-Host "Done. Saved up to $Count tasks."
    Write-Host "Run: .\.venv\Scripts\python.exe autocurator.py saved"
}
finally {
    Pop-Location
}
