# ─────────────────────────────────────────────────────────────────────────────
# Conch installer for Windows (PowerShell)
# Adds LLM-assisted shell commands to your terminal.
# ─────────────────────────────────────────────────────────────────────────────

$ErrorActionPreference = "Stop"
$ConchDir = Split-Path -Parent $MyInvocation.MyCommand.Path

function Info($msg) { Write-Host "  > $msg" -ForegroundColor Cyan }
function Ok($msg)   { Write-Host "  + $msg" -ForegroundColor Green }
function Warn($msg) { Write-Host "  ! $msg" -ForegroundColor Yellow }
function Err($msg)  { Write-Host "  x $msg" -ForegroundColor Red }

Write-Host ""
Write-Host "  Conch installer" -ForegroundColor Cyan -NoNewline
Write-Host " (Windows)" -ForegroundColor DarkGray
Write-Host "  LLM-assisted shell" -ForegroundColor DarkGray
Write-Host ""

# ── Python check ────────────────────────────────────────────────────────────

$python = $null
foreach ($cmd in @("python", "python3", "py")) {
    try {
        $ver = & $cmd -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
        if ($ver -and [int]($ver.Split('.')[0]) -ge 3) {
            $python = $cmd
            break
        }
    } catch {}
}

if (-not $python) {
    Err "Python 3 is required. Install from https://python.org/downloads/"
    exit 1
}
Ok "Python $ver found ($python)"

# ── pip install ─────────────────────────────────────────────────────────────

Info "Installing conch-shell..."
& $python -m pip install -e "$ConchDir" --quiet 2>$null
if ($LASTEXITCODE -eq 0) {
    Ok "conch-shell installed (editable mode)"
} else {
    & $python -m pip install -e "$ConchDir" --break-system-packages --quiet 2>$null
    if ($LASTEXITCODE -eq 0) {
        Ok "conch-shell installed"
    } else {
        Warn "pip install failed -- try running as Administrator"
    }
}

# ── API key ─────────────────────────────────────────────────────────────────

$envFile = Join-Path $ConchDir ".env"
$existingKey = $env:CEREBRAS_API_KEY
if (-not $existingKey -and (Test-Path $envFile)) {
    $match = Select-String -Path $envFile -Pattern 'CEREBRAS_API_KEY="?([^"]*)"?' -AllMatches
    if ($match.Matches.Count -gt 0) {
        $existingKey = $match.Matches[0].Groups[1].Value
    }
}

if ($existingKey) {
    $masked = $existingKey.Substring(0, [Math]::Min(8, $existingKey.Length)) + "..."
    Ok "Cerebras API key found: $masked"
    $apiKey = $existingKey
} else {
    Write-Host ""
    Info "Enter your Cerebras API key (or press Enter to skip):"
    Write-Host "  Get one at https://inference.cerebras.ai" -ForegroundColor DarkGray
    $apiKey = Read-Host "  Key"
    if (-not $apiKey) {
        Warn "No API key set. Set CEREBRAS_API_KEY later."
    }
}

if ($apiKey) {
    Set-Content -Path $envFile -Value "CEREBRAS_API_KEY=$apiKey"
    Ok "API key saved to $envFile"
    [Environment]::SetEnvironmentVariable("CEREBRAS_API_KEY", $apiKey, "User")
    $env:CEREBRAS_API_KEY = $apiKey
    Ok "CEREBRAS_API_KEY set in user environment"
}

# ── Config ──────────────────────────────────────────────────────────────────

$configDir = if ($env:XDG_CONFIG_HOME) { "$env:XDG_CONFIG_HOME\conch" } else { "$env:USERPROFILE\.config\conch" }
$configFile = Join-Path $configDir "config"

if (-not (Test-Path $configFile)) {
    New-Item -ItemType Directory -Path $configDir -Force | Out-Null
    $exampleConfig = Join-Path $ConchDir "config.example"
    if (Test-Path $exampleConfig) {
        Copy-Item $exampleConfig $configFile
    }
    Ok "Config created: $configFile"
} else {
    Ok "Config exists: $configFile"
}

# ── PowerShell profile ─────────────────────────────────────────────────────

$profileBlock = @"

# Conch: LLM-assisted shell
`$env:CONCH_DIR = "$ConchDir"
function ask { & conch-ask @args }
function chat { & conch @args }
"@

$profilePath = $PROFILE.CurrentUserAllHosts
if (-not (Test-Path $profilePath)) {
    New-Item -ItemType File -Path $profilePath -Force | Out-Null
}

$profileContent = Get-Content $profilePath -Raw -ErrorAction SilentlyContinue
if ($profileContent -and $profileContent.Contains("Conch:")) {
    Ok "Already in PowerShell profile"
} else {
    Add-Content -Path $profilePath -Value $profileBlock
    Ok "Added to $profilePath"
}

# ── Test ────────────────────────────────────────────────────────────────────

Write-Host ""
Info "Testing conch..."
try {
    $testResult = & $python -m conch.cli "list files" 2>&1
    Ok "conch-ask works: $testResult"
} catch {
    Warn "conch-ask test failed (may be an API key issue)"
}

# ── Done ────────────────────────────────────────────────────────────────────

Write-Host ""
Write-Host "  + Conch installed!" -ForegroundColor Green
Write-Host ""
Write-Host "  Open a new terminal, then:" -ForegroundColor White
Write-Host ""
Write-Host "    ask list files" -ForegroundColor Cyan -NoNewline
Write-Host "     -> get a shell command" -ForegroundColor DarkGray
Write-Host "    chat" -ForegroundColor Cyan -NoNewline
Write-Host "              -> multi-turn conversation" -ForegroundColor DarkGray
Write-Host "    conch" -ForegroundColor Cyan -NoNewline
Write-Host "             -> same as chat" -ForegroundColor DarkGray
Write-Host ""
Write-Host "  Config:  $configFile" -ForegroundColor DarkGray
Write-Host ""
