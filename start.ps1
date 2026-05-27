# start.ps1 - Loads .env, starts Lavalink, then starts the bot

$Root = $PSScriptRoot

# Load .env into current process environment
$EnvFile = Join-Path $Root ".env"
if (Test-Path $EnvFile) {
    Get-Content $EnvFile | ForEach-Object {
        $line = $_.Trim()
        if ($line -and -not $line.StartsWith("#") -and $line -match "^([^=]+)=(.*)$") {
            $key   = $Matches[1].Trim()
            $value = $Matches[2].Trim()
            [System.Environment]::SetEnvironmentVariable($key, $value, "Process")
            Write-Host "  Loaded: $key" -ForegroundColor DarkGray
        }
    }
    Write-Host "[OK] .env loaded" -ForegroundColor Green
} else {
    Write-Host "[WARN] .env not found - copy .env.example to .env and fill in your values" -ForegroundColor Yellow
}

Write-Host ""

# Start Lavalink in a new window
$LavalinkDir = Join-Path $Root "lavalink"
Write-Host "[1/2] Starting Lavalink..." -ForegroundColor Cyan
Start-Process cmd -ArgumentList "/k java -jar Lavalink.jar" -WorkingDirectory $LavalinkDir

# Wait for Lavalink to be ready
Write-Host "      Waiting 15 seconds for Lavalink to be ready..." -ForegroundColor Cyan
Start-Sleep -Seconds 15

# Start the bot
Write-Host "[2/2] Starting bot..." -ForegroundColor Green
Set-Location $Root
python bot.py

Write-Host ""
Write-Host "Bot stopped." -ForegroundColor Yellow
