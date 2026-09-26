# claude-statusline: PowerShell version (Windows PowerShell 5.1+ or PowerShell 7).
# https://github.com/TheDyXer/claude-statusline
#
# Usage in settings.json:
#   pwsh -NoProfile -ExecutionPolicy Bypass -File C:/Users/<you>/.claude/statusline.ps1 [-Label NAME]
#
# Keep in step with statusline.py: tests/test_parity.py checks that both print identical output.
param([string]$Label)

$ErrorActionPreference = 'SilentlyContinue'

$inv = [System.Globalization.CultureInfo]::InvariantCulture
$utf8 = [System.Text.UTF8Encoding]::new($false)
$dot = [char]0x00B7

# ANSI color setup
$e = [char]27
$reset = "$e[0m"
$dim = "$e[90m"
$boldCyan = "$e[1;36m"
$green = "$e[32m"
$yellow = "$e[33m"
$red = "$e[31m"
$badge = "$e[30;48;5;208m"

# low and unknown levels stay uncolored
$effortColors = @{
    'medium' = "$e[34m"
    'high'   = "$e[94m"
    'xhigh'  = "$e[35m"
    'max'    = "$e[1;95m"
}

# STATUSLINE_NOW (unix seconds) pins the clock for tests
$now = if ($env:STATUSLINE_NOW) { [int64]$env:STATUSLINE_NOW } else { [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() }

function Get-RawColor {
    param([double]$Pct)
    if ($Pct -lt 50) { $green } elseif ($Pct -lt 80) { $yellow } else { $red }
}

# Color by projected usage at reset if the current rate holds.
# 90%+ is always red; the first 10% of a window uses raw thresholds.
function Get-PaceColor {
    param([double]$Used, [int64]$ResetsAt, [int64]$Window)
    $shown = [math]::Round($Used)
    if ($shown -ge 90) { return $red }
    if ($ResetsAt -le 0) { return (Get-RawColor $shown) }
    $elapsed = [math]::Min(1.0, ($Window - ($ResetsAt - $now)) / [double]$Window)
    if ($elapsed -lt 0.1) { return (Get-RawColor $shown) }
    $projected = $Used / $elapsed
    if ($projected -lt 80) { $green } elseif ($projected -le 100) { $yellow } else { $red }
}

function Format-Countdown {
    param([int64]$ResetsAt)
    $secsLeft = $ResetsAt - $now
    if ($secsLeft -le 0) { return $null }
    if ($secsLeft -lt 60) { return '<1m' }
    $mins = [int64][math]::Floor($secsLeft / 60)
    if ($mins -ge 1440) {
        return '{0}d {1}h' -f [int64][math]::Floor($mins / 1440), [int64][math]::Floor(($mins % 1440) / 60)
    }
    if ($mins -ge 60) {
        return '{0}h {1:D2}m' -f [int64][math]::Floor($mins / 60), [int64]($mins % 60)
    }
    return '{0}m' -f $mins
}

# 850, 1k, 15.5k, 200k, 1.2M (truncated, not rounded)
function Format-Tokens {
    param([int64]$N)
    if ($N -lt 1000) { return "$N" }
    if ($N -lt 1000000) { $t = [int64][math]::Floor($N / 100); $unit = 'k' }
    else { $t = [int64][math]::Floor($N / 100000); $unit = 'M' }
    $whole = [int64][math]::Floor($t / 10)
    $frac = $t % 10
    if ($frac -eq 0) { return "$whole$unit" }
    return "$whole.$frac$unit"
}

function Format-Limit {
    param([string]$Name, $Limit, [int64]$Window, [string]$TimeFormat)
    if (-not $Limit -or $null -eq $Limit.used_percentage) { return $null }
    $used = [double]$Limit.used_percentage
    $resetsAt = if ($Limit.resets_at) { [int64][math]::Floor([double]$Limit.resets_at) } else { [int64]0 }
    $color = Get-PaceColor -Used $used -ResetsAt $resetsAt -Window $Window
    $seg = "$dim$Name$reset $color$([math]::Round($used))%$reset"
    if ($resetsAt -gt 0) {
        $when = [DateTimeOffset]::FromUnixTimeSeconds($resetsAt).ToLocalTime().ToString($TimeFormat, $inv)
        $left = Format-Countdown -ResetsAt $resetsAt
        if ($left) { $when += " $dot $left" }
        $seg += " $dim($when)$reset"
    }
    return $seg
}

$reader = [System.IO.StreamReader]::new([Console]::OpenStandardInput(), $utf8)
$data = $reader.ReadToEnd() | ConvertFrom-Json

$parts = New-Object System.Collections.Generic.List[string]

# 1. Model display name
if ($data.model -and $data.model.display_name) {
    $parts.Add("$boldCyan$([string]$data.model.display_name)$reset")
}

# 2. Effort level
if ($data.effort -and $data.effort.level) {
    $level = [string]$data.effort.level
    $c = $effortColors[$level.ToLowerInvariant()]
    $val = if ($c) { "$c$level$reset" } else { $level }
    $parts.Add("${dim}effort:$reset $val")
}

# 3. Context window usage
$cw = $data.context_window
if ($cw -and $null -ne $cw.used_percentage) {
    $pct = [math]::Round([double]$cw.used_percentage)
    $seg = "${dim}Ctx:$reset $(Get-RawColor $pct)$pct%$reset"
    if ($null -ne $cw.total_input_tokens -and $null -ne $cw.context_window_size -and [double]$cw.context_window_size -gt 0) {
        $seg += " $dim($(Format-Tokens $cw.total_input_tokens)/$(Format-Tokens $cw.context_window_size))$reset"
    }
    $parts.Add($seg)
}

# 4 & 5. Rate limits (5-hour session + 7-day weekly)
$rl = $data.rate_limits
if ($rl) {
    $seg = Format-Limit -Name '5h' -Limit $rl.five_hour -Window 18000 -TimeFormat 'HH:mm'
    if ($seg) { $parts.Add($seg) }
    $seg = Format-Limit -Name 'wk' -Limit $rl.seven_day -Window 604800 -TimeFormat 'ddd HH:mm'
    if ($seg) { $parts.Add($seg) }
}

$line = $parts -join "$dim | $reset"
if ($Label) {
    $tag = "$badge $Label $reset"
    $line = if ($line) { "$tag $line" } else { $tag }
}

# Write UTF-8 bytes directly so the console code page can't mangle the output
$bytes = $utf8.GetBytes($line + "`n")
$out = [Console]::OpenStandardOutput()
$out.Write($bytes, 0, $bytes.Length)
$out.Flush()
