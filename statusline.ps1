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

$ttlUnits = @{ 's' = 1; 'm' = 60; 'h' = 3600 }

# When the prompt cache goes cold; $null hides the part.
# Yellow in the last 20% of the cache's lifetime, "cold" once it has expired.
function Format-Cache {
    param($Cache)
    if ($Cache -isnot [System.Management.Automation.PSCustomObject]) { return $null }
    if ($Cache.caching_observed -is [bool] -and -not $Cache.caching_observed) { return $null }
    $expires = $Cache.expires_at
    $isNumber = $expires -is [int] -or $expires -is [long] -or $expires -is [double] -or $expires -is [decimal]
    $left = if ($isNumber) { [int64][math]::Floor([double]$expires) - $now } else { [int64]0 }
    if (-not ($Cache.warm -is [bool] -and $Cache.warm) -or $left -le 0) {
        return "${dim}cache$reset ${red}cold$reset"
    }
    $ttl = 0
    if ($Cache.ttl -is [string] -and $Cache.ttl -cmatch '^([0-9]+)([smh])\z') {
        $ttl = [int64]$Matches[1] * $ttlUnits[$Matches[2]]
    }
    $color = if ($left * 5 -lt $ttl) { $yellow } else { $green }
    $when = [DateTimeOffset]::FromUnixTimeSeconds($now + $left).ToLocalTime().ToString('HH:mm', $inv)
    return "${dim}cache$reset $color($when $dot $(Format-Countdown -ResetsAt ($now + $left)))$reset"
}

function Test-Truthy {
    param([string]$Value)
    if (-not $Value) { return $false }
    return @('1', 'true', 'yes', 'on') -ccontains $Value.Trim(" `t`r`n").ToLowerInvariant()
}

# Parsed settings file, or $null if it's missing, unreadable or not a JSON object
function Read-SettingsFile {
    param([string]$Path)
    try {
        if (-not [System.IO.File]::Exists($Path)) { return $null }
        $text = [System.IO.File]::ReadAllText($Path)
        if (-not $text.TrimStart(" `t`r`n").StartsWith('{')) { return $null }
        return ($text | ConvertFrom-Json -ErrorAction Stop)
    } catch { return $null }
}

# How much of the session transcript to read, from the end
$tailBytes = 2 * 1024 * 1024
$commandOutput = '<local-command-stdout>'
$advisorOutput = $commandOutput + 'Advisor '
$ordinal = [System.StringComparison]::Ordinal

# The last $tailBytes of the transcript as lines, oldest first; $null if unreadable.
# A line cut off by the start of the window is blanked. Reading one byte before the
# window tells a cut-off line from one that starts exactly there.
function Read-TranscriptTail {
    param([string]$Path)
    try {
        $fs = [System.IO.File]::Open($Path, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)
        try {
            $size = $fs.Length
            $start = [math]::Max([int64]0, $size - $tailBytes)
            $begin = if ($start -gt 0) { $start - 1 } else { [int64]0 }
            [void]$fs.Seek($begin, [System.IO.SeekOrigin]::Begin)
            $buf = New-Object byte[] ($size - $begin)
            $read = 0
            while ($read -lt $buf.Length) {
                $n = $fs.Read($buf, $read, $buf.Length - $read)
                if ($n -le 0) { break }
                $read += $n
            }
        } finally { $fs.Dispose() }
    } catch { return $null }
    $lines = $utf8.GetString($buf, 0, $read).Split([char]10)
    if ($start -gt 0) { $lines[0] = '' }
    return , $lines
}

# /advisor notes that mean the session runs without an advisor
$noAdvisorNotes = @('runs without the advisor', 'runs without one', 'will not activate', 'will activate when')
# What may follow the name in "Advisor set to <name>"
$sessionSuffixes = @(' (this session only)', ' for this session')

# The advisor this session last used, from its transcript: a model name, '' for none, or
# $null if nothing in the last $tailBytes decides it. The newest decisive entry wins: every
# main-thread reply records the advisor it was sent with (advisorModel, left out when none),
# and /advisor output covers the time before the next reply. Its note about the current
# conversation beats the new setting. The /advisor texts are from Claude Code 2.1.288.
function Get-TranscriptAdvisor {
    param([string]$Path)
    $lines = Read-TranscriptTail $Path
    if ($null -eq $lines) { return $null }
    for ($i = $lines.Length - 1; $i -ge 0; $i--) {
        $line = $lines[$i].TrimEnd([char]13)
        if (-not $line.StartsWith('{', $ordinal)) { continue }
        if (-not ($line.Contains('"type":"assistant"') -or $line.Contains($advisorOutput))) { continue }
        try { $entry = $line | ConvertFrom-Json -ErrorAction Stop } catch { continue }
        if ($entry -isnot [System.Management.Automation.PSCustomObject]) { continue }
        $message = $entry.message
        if ($message -isnot [System.Management.Automation.PSCustomObject]) { continue }
        if ($entry.type -ceq 'assistant') {
            if (($entry.isSidechain -is [bool] -and $entry.isSidechain) -or
                ($entry.isAbortedMidStream -is [bool] -and $entry.isAbortedMidStream) -or
                $message.model -ceq '<synthetic>') { continue }
            $value = $entry.advisorModel
            if ($value -is [string] -and $value) { return $value }
            return ''
        }
        $content = $message.content
        if ($entry.type -ceq 'user' -and $content -is [string] -and $content.StartsWith($advisorOutput, $ordinal)) {
            $text = $content.Substring($commandOutput.Length)
            $end = $text.IndexOf('</local-command-stdout>', $ordinal)
            if ($end -ge 0) { $text = $text.Substring(0, $end) }
            if ($text -cmatch 'keeps (.+?) as its advisor(?: model)? until') { return $Matches[1] }
            foreach ($note in $noAdvisorNotes) { if ($text.Contains($note)) { return '' } }
            if ($text.StartsWith('Advisor disabled', $ordinal)) { return '' }
            if ($text.StartsWith('Advisor set to ', $ordinal)) {
                $name = $text.Substring('Advisor set to '.Length).Split([char]10)[0]
                foreach ($suffix in $sessionSuffixes) {
                    $k = $name.IndexOf($suffix, $ordinal)
                    if ($k -ge 0) { $name = $name.Substring(0, $k) }
                }
                return $name.Trim(" `t`r")
            }
        }
    }
    return $null
}

# The advisor this session uses; $null or '' is off.
# Claude Code doesn't send the advisor to status line scripts, and settings.json only holds
# the last choice saved from any session. So the session's own transcript comes first, and
# the settings files are the fallback (a new session before its first reply, or after /clear).
function Get-SessionAdvisor {
    param($Data)
    if (Test-Truthy $env:CLAUDE_CODE_DISABLE_ADVISOR_TOOL) { return $null }
    $path = $Data.transcript_path
    if ($path -is [string] -and $path) {
        $found = Get-TranscriptAdvisor $path
        if ($null -ne $found) { return $found }
    }
    return Get-AdvisorSetting $Data
}

# advisorModel in Claude Code's settings order; $null means off.
# The first file that has the key decides, even if its value is null.
function Get-AdvisorSetting {
    param($Data)
    $paths = @()
    $project = $Data.workspace.project_dir
    if ($project -is [string] -and $project) {
        $paths += [System.IO.Path]::Combine($project, '.claude', 'settings.local.json')
        $paths += [System.IO.Path]::Combine($project, '.claude', 'settings.json')
    }
    $config = if ($env:CLAUDE_CONFIG_DIR) { $env:CLAUDE_CONFIG_DIR } else { [System.IO.Path]::Combine([Environment]::GetFolderPath('UserProfile'), '.claude') }
    $paths += [System.IO.Path]::Combine($config, 'settings.json')
    foreach ($path in $paths) {
        $settings = Read-SettingsFile $path
        if ($null -eq $settings) { continue }
        $prop = $settings.PSObject.Properties | Where-Object { $_.Name -ceq 'advisorModel' } | Select-Object -First 1
        if (-not $prop) { continue }
        if ($prop.Value -is [string]) {
            $value = $prop.Value.Trim(" `t`r`n")
            if ($value) { return $value }
        }
        return $null
    }
    return $null
}

# fable -> Fable, claude-opus-5-5 -> Opus 5.5, anything else as-is
function Format-ModelId {
    param([string]$Value)
    $low = $Value.ToLowerInvariant()
    if (@('fable', 'opus', 'sonnet', 'haiku') -ccontains $low) {
        $family = $low; $version = ''
    } elseif ($low -cmatch '^claude-([a-z]+)-([0-9]{1,2})(?:-([0-9]{1,2}))?(?:-[0-9]{8})?\z') {
        $family = $Matches[1]; $version = " $($Matches[2])"
        if ($Matches[3]) { $version += ".$($Matches[3])" }
    } else {
        return $Value
    }
    return $family.Substring(0, 1).ToUpperInvariant() + $family.Substring(1) + $version
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

# 3. Advisor (Claude Code doesn't send it, so read it from the transcript or settings)
$advisor = Get-SessionAdvisor $data
$val = if ($advisor) { "$boldCyan$(Format-ModelId $advisor)$reset" }
       elseif (Test-Truthy $env:CLAUDE_CODE_DISABLE_ADVISOR_TOOL) { "${dim}off$reset" }
       else { "${yellow}none selected$reset" }
$parts.Add("${dim}advisor:$reset $val")

# 4. Context window usage
$cw = $data.context_window
if ($cw -and $null -ne $cw.used_percentage) {
    $pct = [math]::Round([double]$cw.used_percentage)
    $seg = "${dim}Ctx:$reset $(Get-RawColor $pct)$pct%$reset"
    if ($null -ne $cw.total_input_tokens -and $null -ne $cw.context_window_size -and [double]$cw.context_window_size -gt 0) {
        $seg += " $dim($(Format-Tokens $cw.total_input_tokens)/$(Format-Tokens $cw.context_window_size))$reset"
    }
    $parts.Add($seg)
}

# 5. Prompt cache: when it goes cold
$seg = Format-Cache $data.prompt_cache
if ($seg) { $parts.Add($seg) }

# 6 & 7. Rate limits (5-hour session + 7-day weekly)
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
