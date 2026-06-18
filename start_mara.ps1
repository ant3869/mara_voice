param(
    [string]$AudioDevice,
    [string]$CaptureDir,
    [string]$LogLevel = "INFO",
    [string]$TtsDevice = "auto",
    [int]$HealthTimeoutSeconds = 120,
    [double]$SshTimeoutSeconds = 0,
    [double]$SshConnectTimeoutSeconds = 5,
    [double]$TtsTimeoutSeconds = 300,
    [int]$TtsInferenceTimesteps = 8,
    [double]$StatusIntervalSeconds = 10,
    [int]$SpokenReplyCharLimit = 900,
    [int]$TtsChunkCharLimit = 450,
    [int]$TtsStreamChunkCharLimit = 180,
    [string]$TtsStreamUrl,
    [string]$ConfigPath,
    [string]$HermesCommand,
    [string]$ActiveAgent = "hermes",
    [string]$AgentRouteStatePath,
    [string]$OpenClawBaseUrl = "http://192.168.0.65:8645/v1",
    [string]$OpenClawModel = "gemini 3.1 pro",
    [double]$OpenClawTimeoutSeconds = 600,
    [string]$VoiceReferencePath,
    [string]$VoiceReferenceText,
    [switch]$DisableVoiceReference,
    [switch]$RegenerateVoiceReference,
    [switch]$ForcePolling,
    [switch]$TtsStreaming,
    [switch]$NoTtsStreaming,
    [switch]$Gui,
    [switch]$NoGui,
    [string]$GuiHost = "127.0.0.1",
    [int]$GuiPort = 8765,
    [switch]$NoOptimize,
    [switch]$KeepTtsRunning
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = if ($PSScriptRoot) { $PSScriptRoot } else { (Get-Location).Path }
$venvPython = Join-Path $repoRoot "venv\Scripts\python.exe"
$askMaraScript = Join-Path $repoRoot "ask_mara.py"
$ttsScript = Join-Path $repoRoot "mara_tts_server.py"
$listenerScript = Join-Path $repoRoot "jarvis_listener.py"
$guiScript = Join-Path $repoRoot "mara_gui_server.py"
$logDir = Join-Path $repoRoot "logs"
$ttsStdoutLog = Join-Path $logDir "start_mara_tts.stdout.log"
$ttsStderrLog = Join-Path $logDir "start_mara_tts.stderr.log"
$guiStdoutLog = Join-Path $logDir "start_mara_gui.stdout.log"
$guiStderrLog = Join-Path $logDir "start_mara_gui.stderr.log"
$resolvedConfigPath = if ($ConfigPath) { $ConfigPath } else { Join-Path $repoRoot "config\mara_voice.local.json" }
$resolvedAgentRouteStatePath = if ($AgentRouteStatePath) { $AgentRouteStatePath } else { Join-Path $repoRoot "config\mara_agent_route.runtime.json" }
$ttsUrl = "http://127.0.0.1:8000/tts"
$ttsHealthUrl = "http://127.0.0.1:8000/healthz"
$guiUrl = "http://$GuiHost`:$GuiPort"
$requiredVoiceGenerationMode = "persistent_reference_only_v2"

function Write-Step {
    param([string]$Message)

    Write-Host "[mara] $Message" -ForegroundColor Cyan
}

function Import-DotEnvFile {
    param([string]$Path)

    if (-not (Test-Path -LiteralPath $Path)) {
        return
    }

    foreach ($rawLine in [System.IO.File]::ReadAllLines($Path)) {
        $line = $rawLine.Trim()
        if ([string]::IsNullOrWhiteSpace($line) -or $line.StartsWith("#")) {
            continue
        }
        if ($line.StartsWith("export ")) {
            $line = $line.Substring(7).Trim()
        }
        $equalsIndex = $line.IndexOf("=")
        if ($equalsIndex -le 0) {
            continue
        }

        $name = $line.Substring(0, $equalsIndex).Trim()
        $value = $line.Substring($equalsIndex + 1).Trim()
        if ($name -notmatch '^[A-Za-z_][A-Za-z0-9_]*$') {
            continue
        }
        if ($value.Length -ge 2 -and
            (($value.StartsWith('"') -and $value.EndsWith('"')) -or
            ($value.StartsWith("'") -and $value.EndsWith("'")))) {
            $value = $value.Substring(1, $value.Length - 2)
        }
        if ([string]::IsNullOrEmpty([Environment]::GetEnvironmentVariable($name))) {
            [Environment]::SetEnvironmentVariable($name, $value, "Process")
        }
    }
}

if ($TtsStreaming -and $NoTtsStreaming) {
    throw "Use either -TtsStreaming or -NoTtsStreaming, not both."
}

if ($Gui -and $NoGui) {
    throw "Use either -Gui or -NoGui, not both."
}

Import-DotEnvFile (Join-Path $repoRoot ".env")

$savedConfig = $null
$regenerateVoiceReferenceFromSavedConfig = $false
if (Test-Path $resolvedConfigPath) {
    try {
        $savedConfig = Get-Content -LiteralPath $resolvedConfigPath -Raw | ConvertFrom-Json
        Write-Step "Loaded saved options from $resolvedConfigPath"
    }
    catch {
        throw "Could not read saved options from $resolvedConfigPath`: $($_.Exception.Message)"
    }
}

function Test-SavedOption {
    param([string]$Name)

    return $null -ne $savedConfig -and
        ($savedConfig.PSObject.Properties.Name -contains $Name) -and
        $null -ne $savedConfig.$Name
}

function Get-EnvOption {
    param([string]$Name)

    return [Environment]::GetEnvironmentVariable($Name)
}

function Test-EnvOption {
    param([string]$Name)

    return -not [string]::IsNullOrWhiteSpace((Get-EnvOption $Name))
}

function Get-EnvBool {
    param([string]$Name)

    $value = (Get-EnvOption $Name)
    if ($null -eq $value) {
        return $false
    }
    return $value.Trim().ToLowerInvariant() -in @("1", "true", "yes", "on")
}

function ConvertTo-ProcessArgument {
    param([object]$Value)

    $text = [string]$Value
    if ($text.Length -eq 0) {
        return '""'
    }

    if ($text -notmatch '[\s"]') {
        return $text
    }

    $result = '"'
    $backslashCount = 0
    foreach ($character in $text.ToCharArray()) {
        if ($character -eq '\') {
            $backslashCount += 1
            continue
        }

        if ($character -eq '"') {
            $result += ('\' * (($backslashCount * 2) + 1))
            $result += '"'
            $backslashCount = 0
            continue
        }

        if ($backslashCount) {
            $result += ('\' * $backslashCount)
            $backslashCount = 0
        }
        $result += $character
    }

    if ($backslashCount) {
        $result += ('\' * ($backslashCount * 2))
    }
    $result += '"'
    return $result
}

function Join-ProcessArguments {
    param([object[]]$Arguments)

    return (($Arguments | ForEach-Object { ConvertTo-ProcessArgument $_ }) -join ' ')
}

function Reset-SavedRegenerateVoiceReference {
    if (-not $regenerateVoiceReferenceFromSavedConfig) {
        return
    }

    if (-not (Test-Path $resolvedConfigPath)) {
        return
    }

    try {
        $config = Get-Content -LiteralPath $resolvedConfigPath -Raw | ConvertFrom-Json
        if ($config.PSObject.Properties.Name -contains "regenerate_voice_reference") {
            $config.regenerate_voice_reference = $false
            $json = ($config | ConvertTo-Json -Depth 10) + [Environment]::NewLine
            $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
            $resolvedConfigFile = (Resolve-Path -LiteralPath $resolvedConfigPath).Path
            [System.IO.File]::WriteAllText($resolvedConfigFile, $json, $utf8NoBom)
            Write-Step "Cleared one-shot regenerate_voice_reference in saved GUI options"
        }
    }
    catch {
        Write-Step "Could not clear regenerate_voice_reference in saved GUI options: $($_.Exception.Message)"
    }
}

if (-not $PSBoundParameters.ContainsKey('AudioDevice') -and (Test-EnvOption "MARA_AUDIO_DEVICE")) {
    $AudioDevice = Get-EnvOption "MARA_AUDIO_DEVICE"
}

if (-not $PSBoundParameters.ContainsKey('CaptureDir') -and (Test-EnvOption "MARA_CAPTURE_DIR")) {
    $CaptureDir = Get-EnvOption "MARA_CAPTURE_DIR"
}

if (-not $PSBoundParameters.ContainsKey('LogLevel') -and (Test-EnvOption "MARA_LOG_LEVEL")) {
    $LogLevel = Get-EnvOption "MARA_LOG_LEVEL"
}

if (-not $PSBoundParameters.ContainsKey('TtsDevice') -and (Test-EnvOption "MARA_TTS_DEVICE")) {
    $TtsDevice = Get-EnvOption "MARA_TTS_DEVICE"
}

if (-not $PSBoundParameters.ContainsKey('SshTimeoutSeconds') -and (Test-EnvOption "MARA_SSH_TIMEOUT")) {
    $SshTimeoutSeconds = [double](Get-EnvOption "MARA_SSH_TIMEOUT")
}

if (-not $PSBoundParameters.ContainsKey('SshConnectTimeoutSeconds') -and (Test-EnvOption "MARA_SSH_CONNECT_TIMEOUT")) {
    $SshConnectTimeoutSeconds = [double](Get-EnvOption "MARA_SSH_CONNECT_TIMEOUT")
}

if (-not $PSBoundParameters.ContainsKey('TtsTimeoutSeconds') -and (Test-EnvOption "MARA_TTS_TIMEOUT")) {
    $TtsTimeoutSeconds = [double](Get-EnvOption "MARA_TTS_TIMEOUT")
}

if (-not $PSBoundParameters.ContainsKey('TtsInferenceTimesteps') -and (Test-EnvOption "MARA_TTS_INFERENCE_TIMESTEPS")) {
    $TtsInferenceTimesteps = [int](Get-EnvOption "MARA_TTS_INFERENCE_TIMESTEPS")
}

if (-not $PSBoundParameters.ContainsKey('StatusIntervalSeconds') -and (Test-EnvOption "MARA_STATUS_INTERVAL")) {
    $StatusIntervalSeconds = [double](Get-EnvOption "MARA_STATUS_INTERVAL")
}

if (-not $PSBoundParameters.ContainsKey('SpokenReplyCharLimit') -and (Test-EnvOption "MARA_SPOKEN_REPLY_CHAR_LIMIT")) {
    $SpokenReplyCharLimit = [int](Get-EnvOption "MARA_SPOKEN_REPLY_CHAR_LIMIT")
}

if (-not $PSBoundParameters.ContainsKey('TtsChunkCharLimit') -and (Test-EnvOption "MARA_TTS_CHUNK_CHAR_LIMIT")) {
    $TtsChunkCharLimit = [int](Get-EnvOption "MARA_TTS_CHUNK_CHAR_LIMIT")
}

if (-not $PSBoundParameters.ContainsKey('TtsStreamChunkCharLimit') -and (Test-EnvOption "MARA_TTS_STREAM_CHUNK_CHAR_LIMIT")) {
    $TtsStreamChunkCharLimit = [int](Get-EnvOption "MARA_TTS_STREAM_CHUNK_CHAR_LIMIT")
}

if (-not $PSBoundParameters.ContainsKey('TtsStreamUrl') -and (Test-EnvOption "MARA_TTS_STREAM_URL")) {
    $TtsStreamUrl = Get-EnvOption "MARA_TTS_STREAM_URL"
}

if (-not $PSBoundParameters.ContainsKey('TtsStreaming') -and -not $NoTtsStreaming -and (Test-EnvOption "MARA_TTS_STREAMING")) {
    $TtsStreaming = Get-EnvBool "MARA_TTS_STREAMING"
}

if (-not $PSBoundParameters.ContainsKey('HermesCommand') -and (Test-EnvOption "MARA_HERMES_COMMAND")) {
    $HermesCommand = Get-EnvOption "MARA_HERMES_COMMAND"
}

if (-not $PSBoundParameters.ContainsKey('ActiveAgent') -and (Test-EnvOption "MARA_ACTIVE_AGENT")) {
    $ActiveAgent = Get-EnvOption "MARA_ACTIVE_AGENT"
}

if (-not $PSBoundParameters.ContainsKey('OpenClawBaseUrl') -and (Test-EnvOption "MARA_OPENCLAW_BASE_URL")) {
    $OpenClawBaseUrl = Get-EnvOption "MARA_OPENCLAW_BASE_URL"
}

if (-not $PSBoundParameters.ContainsKey('OpenClawModel') -and (Test-EnvOption "MARA_OPENCLAW_MODEL")) {
    $OpenClawModel = Get-EnvOption "MARA_OPENCLAW_MODEL"
}

if (-not $PSBoundParameters.ContainsKey('OpenClawTimeoutSeconds') -and (Test-EnvOption "MARA_OPENCLAW_TIMEOUT")) {
    $OpenClawTimeoutSeconds = [double](Get-EnvOption "MARA_OPENCLAW_TIMEOUT")
}

if (-not $PSBoundParameters.ContainsKey('VoiceReferencePath') -and (Test-EnvOption "MARA_VOICE_REFERENCE_PATH")) {
    $VoiceReferencePath = Get-EnvOption "MARA_VOICE_REFERENCE_PATH"
}

if (-not $PSBoundParameters.ContainsKey('VoiceReferenceText') -and (Test-EnvOption "MARA_VOICE_REFERENCE_TEXT")) {
    $VoiceReferenceText = Get-EnvOption "MARA_VOICE_REFERENCE_TEXT"
}

if (-not $PSBoundParameters.ContainsKey('AudioDevice') -and (Test-SavedOption "audio_device")) {
    $AudioDevice = [string]$savedConfig.audio_device
}

if (-not $PSBoundParameters.ContainsKey('CaptureDir') -and (Test-SavedOption "capture_dir")) {
    $CaptureDir = [string]$savedConfig.capture_dir
}

if (-not $PSBoundParameters.ContainsKey('TtsDevice') -and (Test-SavedOption "tts_device")) {
    $savedTtsDevice = [string]$savedConfig.tts_device
    if (-not [string]::IsNullOrWhiteSpace($savedTtsDevice)) {
        $TtsDevice = $savedTtsDevice
    }
}

if (-not $PSBoundParameters.ContainsKey('SshTimeoutSeconds') -and (Test-SavedOption "ssh_timeout_seconds")) {
    $SshTimeoutSeconds = [double]$savedConfig.ssh_timeout_seconds
}

if (-not $PSBoundParameters.ContainsKey('SshConnectTimeoutSeconds') -and (Test-SavedOption "ssh_connect_timeout_seconds")) {
    $SshConnectTimeoutSeconds = [double]$savedConfig.ssh_connect_timeout_seconds
}

if (-not $PSBoundParameters.ContainsKey('TtsTimeoutSeconds') -and (Test-SavedOption "tts_timeout_seconds")) {
    $TtsTimeoutSeconds = [double]$savedConfig.tts_timeout_seconds
}

if (-not $PSBoundParameters.ContainsKey('TtsInferenceTimesteps') -and (Test-SavedOption "tts_inference_timesteps")) {
    $TtsInferenceTimesteps = [int]$savedConfig.tts_inference_timesteps
}

if (-not $PSBoundParameters.ContainsKey('StatusIntervalSeconds') -and (Test-SavedOption "status_interval_seconds")) {
    $StatusIntervalSeconds = [double]$savedConfig.status_interval_seconds
}

if (-not $PSBoundParameters.ContainsKey('SpokenReplyCharLimit') -and (Test-SavedOption "spoken_reply_char_limit")) {
    $SpokenReplyCharLimit = [int]$savedConfig.spoken_reply_char_limit
}

if (-not $PSBoundParameters.ContainsKey('TtsChunkCharLimit') -and (Test-SavedOption "tts_chunk_char_limit")) {
    $TtsChunkCharLimit = [int]$savedConfig.tts_chunk_char_limit
}

if (-not $PSBoundParameters.ContainsKey('TtsStreamChunkCharLimit') -and (Test-SavedOption "tts_stream_chunk_char_limit")) {
    $TtsStreamChunkCharLimit = [int]$savedConfig.tts_stream_chunk_char_limit
}

if ($NoTtsStreaming) {
    $TtsStreaming = $false
}
elseif (-not $PSBoundParameters.ContainsKey('TtsStreaming') -and (Test-SavedOption "tts_streaming")) {
    $TtsStreaming = [bool]$savedConfig.tts_streaming
}

if (-not $PSBoundParameters.ContainsKey('HermesCommand') -and (Test-SavedOption "hermes_command")) {
    $savedHermesCommand = [string]$savedConfig.hermes_command
    if (-not [string]::IsNullOrWhiteSpace($savedHermesCommand)) {
        $HermesCommand = $savedHermesCommand
    }
}

if (-not $PSBoundParameters.ContainsKey('ActiveAgent') -and (Test-SavedOption "active_agent")) {
    $savedActiveAgent = [string]$savedConfig.active_agent
    if (-not [string]::IsNullOrWhiteSpace($savedActiveAgent)) {
        $ActiveAgent = $savedActiveAgent
    }
}

if (-not $PSBoundParameters.ContainsKey('OpenClawBaseUrl') -and (Test-SavedOption "openclaw_base_url")) {
    $savedOpenClawBaseUrl = [string]$savedConfig.openclaw_base_url
    if (-not [string]::IsNullOrWhiteSpace($savedOpenClawBaseUrl)) {
        $OpenClawBaseUrl = $savedOpenClawBaseUrl
    }
}

if (-not $PSBoundParameters.ContainsKey('OpenClawModel') -and (Test-SavedOption "openclaw_model")) {
    $savedOpenClawModel = [string]$savedConfig.openclaw_model
    if (-not [string]::IsNullOrWhiteSpace($savedOpenClawModel)) {
        $OpenClawModel = $savedOpenClawModel
    }
}

if (-not $PSBoundParameters.ContainsKey('OpenClawTimeoutSeconds') -and (Test-SavedOption "openclaw_timeout_seconds")) {
    $OpenClawTimeoutSeconds = [double]$savedConfig.openclaw_timeout_seconds
}

$ActiveAgent = $ActiveAgent.Trim().ToLowerInvariant()
if ($ActiveAgent -notin @("hermes", "openclaw")) {
    throw "ActiveAgent must be 'hermes' or 'openclaw'."
}

if (-not $PSBoundParameters.ContainsKey('VoiceReferencePath') -and (Test-SavedOption "voice_reference_path")) {
    $savedVoiceReferencePath = [string]$savedConfig.voice_reference_path
    if (-not [string]::IsNullOrWhiteSpace($savedVoiceReferencePath)) {
        $VoiceReferencePath = $savedVoiceReferencePath
    }
}

if (-not $PSBoundParameters.ContainsKey('VoiceReferenceText') -and (Test-SavedOption "voice_reference_text")) {
    $savedVoiceReferenceText = [string]$savedConfig.voice_reference_text
    if (-not [string]::IsNullOrWhiteSpace($savedVoiceReferenceText)) {
        $VoiceReferenceText = $savedVoiceReferenceText
    }
}

if (-not $PSBoundParameters.ContainsKey('RegenerateVoiceReference') -and (Test-SavedOption "regenerate_voice_reference")) {
    $RegenerateVoiceReference = [bool]$savedConfig.regenerate_voice_reference
    $regenerateVoiceReferenceFromSavedConfig = [bool]$savedConfig.regenerate_voice_reference
}

$resolvedTtsStreamUrl = if ($TtsStreamUrl) { $TtsStreamUrl } else { "http://127.0.0.1:8000/tts/stream" }

function Get-TtsHealth {
    try {
        return Invoke-RestMethod -Uri $ttsHealthUrl -TimeoutSec 5
    }
    catch {
        return $null
    }
}

function Normalize-Whitespace {
    param([string]$Text)

    return (($Text -split '\s+') -join ' ').Trim()
}

function Test-TtsHealthUsable {
    param($Health)

    if ($null -eq $Health -or $Health.status -ne "ok") {
        return $false
    }

    if ($Health.PSObject.Properties.Name -contains "inference_timesteps") {
        if ([int]$Health.inference_timesteps -ne $TtsInferenceTimesteps) {
            return $false
        }
    }

    if ($Health.PSObject.Properties.Name -contains "device") {
        if ([string]$Health.device -ne [string]$TtsDevice) {
            return $false
        }
    }

    if ($Health.PSObject.Properties.Name -contains "optimize") {
        if ([bool]$Health.optimize -eq [bool]$NoOptimize) {
            return $false
        }
    }

    if ($TtsStreaming) {
        if (-not ($Health.PSObject.Properties.Name -contains "streaming_supported")) {
            return $false
        }

        if (-not [bool]$Health.streaming_supported) {
            return $false
        }
    }

    if ($DisableVoiceReference) {
        if ($Health.PSObject.Properties.Name -contains "voice_reference_enabled") {
            return -not [bool]$Health.voice_reference_enabled
        }
        return $false
    }

    if ($RegenerateVoiceReference) {
        return $false
    }

    if (-not ($Health.PSObject.Properties.Name -contains "voice_reference_ready")) {
        return $false
    }

    if (-not ($Health.PSObject.Properties.Name -contains "voice_generation_mode")) {
        return $false
    }

    if (-not ($Health.PSObject.Properties.Name -contains "voice_profiles")) {
        return $false
    }

    if (-not ($Health.voice_profiles.PSObject.Properties.Name -contains "openclaw")) {
        return $false
    }

    if ($Health.voice_generation_mode -ne $requiredVoiceGenerationMode) {
        return $false
    }

    if ($VoiceReferencePath -and ($Health.PSObject.Properties.Name -contains "voice_reference_path")) {
        $requestedVoiceReferencePath = [string]$VoiceReferencePath
        try {
            $requestedVoiceReferencePath = [string](Resolve-Path -LiteralPath $VoiceReferencePath -ErrorAction Stop)
        }
        catch {
        }

        if ([string]$Health.voice_reference_path -ne $requestedVoiceReferencePath) {
            return $false
        }
    }

    if ($VoiceReferenceText -and -not ($Health.PSObject.Properties.Name -contains "voice_reference_text")) {
        return $false
    }

    if ($VoiceReferenceText -and ($Health.PSObject.Properties.Name -contains "voice_reference_text")) {
        if ([string]$Health.voice_reference_text -ne (Normalize-Whitespace -Text $VoiceReferenceText)) {
            return $false
        }
    }

    return [bool]$Health.voice_reference_ready
}

function Format-TtsHealthProblem {
    param($Health)

    if ($null -eq $Health) {
        return "No health response received."
    }

    if ($RegenerateVoiceReference) {
        return "Voice reference regeneration was requested, so the TTS server must restart."
    }

    if (-not ($Health.PSObject.Properties.Name -contains "voice_reference_ready") -and -not $DisableVoiceReference) {
        return "The server does not report persistent voice-reference health. Stop the old TTS server and start again."
    }

    if ($Health.PSObject.Properties.Name -contains "device") {
        if ([string]$Health.device -ne [string]$TtsDevice) {
            return "The server uses device='$($Health.device)', but this launch requested '$TtsDevice'. Stop the old TTS server and start again."
        }
    }

    if ($Health.PSObject.Properties.Name -contains "optimize") {
        if ([bool]$Health.optimize -eq [bool]$NoOptimize) {
            return "The server optimize setting does not match this launch. Stop the old TTS server and start again."
        }
    }

    if (-not $DisableVoiceReference) {
        if (-not ($Health.PSObject.Properties.Name -contains "voice_generation_mode")) {
            return "The server does not report voice generation mode. Stop the old TTS server and start again."
        }

        if (-not ($Health.PSObject.Properties.Name -contains "voice_profiles")) {
            return "The server does not report per-agent voice profiles. Stop the old TTS server and start again."
        }

        if (-not ($Health.voice_profiles.PSObject.Properties.Name -contains "openclaw")) {
            return "The server does not report the OpenClaw voice profile. Stop the old TTS server and start again."
        }

        if ($Health.voice_generation_mode -ne $requiredVoiceGenerationMode) {
            return "The server uses voice_generation_mode='$($Health.voice_generation_mode)', but this launch requires '$requiredVoiceGenerationMode'. Stop the old TTS server and start again."
        }

        if ($VoiceReferencePath -and ($Health.PSObject.Properties.Name -contains "voice_reference_path")) {
            $requestedVoiceReferencePath = [string]$VoiceReferencePath
            try {
                $requestedVoiceReferencePath = [string](Resolve-Path -LiteralPath $VoiceReferencePath -ErrorAction Stop)
            }
            catch {
            }

            if ([string]$Health.voice_reference_path -ne $requestedVoiceReferencePath) {
                return "The server uses voice_reference_path='$($Health.voice_reference_path)', but this launch requested '$requestedVoiceReferencePath'. Stop the old TTS server and start again."
            }
        }

        if ($VoiceReferenceText -and -not ($Health.PSObject.Properties.Name -contains "voice_reference_text")) {
            return "The server does not report voice_reference_text, but this launch configured one. Stop the old TTS server and start again."
        }

        if ($VoiceReferenceText -and ($Health.PSObject.Properties.Name -contains "voice_reference_text")) {
            $requestedVoiceReferenceText = Normalize-Whitespace -Text $VoiceReferenceText
            if ([string]$Health.voice_reference_text -ne $requestedVoiceReferenceText) {
                return "The server uses a different voice_reference_text than this launch. Stop the old TTS server and start again."
            }
        }
    }
    else {
        if ($Health.PSObject.Properties.Name -contains "voice_reference_enabled" -and [bool]$Health.voice_reference_enabled) {
            return "The server has voice_reference_enabled=true, but this launch requested -DisableVoiceReference. Stop the old TTS server and start again."
        }
    }

    if ($TtsStreaming) {
        if (-not ($Health.PSObject.Properties.Name -contains "streaming_supported")) {
            return "The server does not report streaming support, but this launch requested -TtsStreaming. Stop the old TTS server and start again."
        }

        if (-not [bool]$Health.streaming_supported) {
            return "The server reports streaming_supported=false, but this launch requested -TtsStreaming."
        }
    }

    if ($Health.PSObject.Properties.Name -contains "inference_timesteps") {
        if ([int]$Health.inference_timesteps -ne $TtsInferenceTimesteps) {
            return "The server uses inference_timesteps=$($Health.inference_timesteps), but this launch requested $TtsInferenceTimesteps. Stop the old TTS server and start again."
        }
    }

    $errorText = $Health.error
    if ($Health.PSObject.Properties.Name -contains "voice_reference_error" -and $Health.voice_reference_error) {
        $errorText = $Health.voice_reference_error
    }

    return "Health endpoint returned status '$($Health.status)' with error '$errorText'."
}

function Wait-ForHealthyTts {
    param(
        [System.Diagnostics.Process]$Process,
        [int]$TimeoutSeconds
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $nextStatus = (Get-Date).AddSeconds(10)

    while ((Get-Date) -lt $deadline) {
        $health = Get-TtsHealth
        if (Test-TtsHealthUsable -Health $health) {
            return $health
        }

        if ($null -ne $Process) {
            $null = $Process.Refresh()
            if ($Process.HasExited) {
                break
            }
        }

        if ((Get-Date) -ge $nextStatus) {
            Write-Step "Still waiting for TTS health on $ttsHealthUrl"
            $nextStatus = (Get-Date).AddSeconds(10)
        }

        Start-Sleep -Milliseconds 500
    }

    return Get-TtsHealth
}

function Get-GuiHealth {
    try {
        return Invoke-RestMethod -Uri "$guiUrl/api/health" -TimeoutSec 2
    }
    catch {
        return $null
    }
}

function Wait-ForGuiHealth {
    param(
        [System.Diagnostics.Process]$Process,
        [int]$TimeoutSeconds = 20
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        $health = Get-GuiHealth
        if ($null -ne $health -and $health.status -eq "ok") {
            return $health
        }

        if ($null -ne $Process) {
            $null = $Process.Refresh()
            if ($Process.HasExited) {
                break
            }
        }

        Start-Sleep -Milliseconds 300
    }

    return Get-GuiHealth
}

function Show-LogTail {
    param([string]$Path)

    if (Test-Path $Path) {
        Write-Host "----- $Path (tail) -----" -ForegroundColor DarkYellow
        Get-Content -Path $Path -Tail 40
        Write-Host "------------------------" -ForegroundColor DarkYellow
    }
}

function Stop-ProcessSafely {
    param(
        [System.Diagnostics.Process]$Process,
        [string]$Name
    )

    if ($null -eq $Process) {
        return
    }

    $null = $Process.Refresh()
    if ($Process.HasExited) {
        return
    }

    Write-Step "Stopping $Name"
    try {
        Stop-Process -Id $Process.Id -Force -ErrorAction Stop
    }
    catch {
        Write-Step "$Name stop request failed: $($_.Exception.Message)"
    }
}

function Stop-MaraProcesses {
    Stop-ProcessSafely -Process $listenerProcess -Name "Voicebox listener"
    Stop-ProcessSafely -Process $guiProcess -Name "GUI server"
    if ($KeepTtsRunning -and $startedTtsHere) {
        Write-Step "Leaving TTS server running because -KeepTtsRunning was provided"
        return
    }

    Stop-ProcessSafely -Process $ttsProcess -Name "TTS server"
}

function Stop-ExistingLocalTtsServer {
    $stoppedAny = $false
    try {
        $connections = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
    }
    catch {
        return $false
    }

    foreach ($processId in ($connections | Select-Object -ExpandProperty OwningProcess -Unique)) {
        if (-not $processId) {
            continue
        }

        try {
            $processInfo = Get-CimInstance Win32_Process -Filter "ProcessId = $processId" -ErrorAction Stop
            $commandLine = [string]$processInfo.CommandLine
            if ($commandLine -notlike "*mara_tts_server.py*") {
                continue
            }

            Write-Step "Stopping stale Mara TTS server process $processId"
            Stop-Process -Id ([int]$processId) -Force -ErrorAction Stop
            $stoppedAny = $true
        }
        catch {
            Write-Step "Could not stop TTS process $processId`: $($_.Exception.Message)"
        }
    }

    return $stoppedAny
}

if (-not (Test-Path $venvPython)) {
    throw "Could not find the project virtualenv interpreter at $venvPython"
}

if (-not (Test-Path $ttsScript)) {
    throw "Could not find $ttsScript"
}

if (-not (Test-Path $listenerScript)) {
    throw "Could not find $listenerScript"
}

if ($Gui -and -not (Test-Path $guiScript)) {
    throw "Could not find $guiScript"
}

if (-not (Test-Path $askMaraScript)) {
    throw "Could not find $askMaraScript"
}

$null = New-Item -ItemType Directory -Path $logDir -Force
Set-Location $repoRoot

$existingHealth = Get-TtsHealth
$ttsProcess = $null
$listenerProcess = $null
$guiProcess = $null
$startedTtsHere = $false

Register-EngineEvent -SourceIdentifier PowerShell.Exiting -Action {
    foreach ($envName in @('MARA_LISTENER_PID', 'MARA_TTS_PID', 'MARA_GUI_PID')) {
        $pidText = [Environment]::GetEnvironmentVariable($envName)
        if ([string]::IsNullOrWhiteSpace($pidText)) {
            continue
        }

        try {
            Stop-Process -Id ([int]$pidText) -Force -ErrorAction SilentlyContinue
        }
        catch {
        }
    }
} | Out-Null

if ($null -ne $existingHealth -and -not (Test-TtsHealthUsable -Health $existingHealth)) {
    $healthProblem = Format-TtsHealthProblem -Health $existingHealth
    Write-Step "Existing TTS server is not usable: $healthProblem"
    if (Stop-ExistingLocalTtsServer) {
        Start-Sleep -Seconds 1
        $existingHealth = Get-TtsHealth
    }
    else {
        throw "A TTS server is already responding on $ttsHealthUrl but could not be safely restarted: $healthProblem"
    }
}

if ($null -ne $existingHealth -and (Test-TtsHealthUsable -Health $existingHealth)) {
    Write-Step "Using existing healthy TTS server on $ttsUrl"
}
elseif ($null -ne $existingHealth) {
    throw "A TTS server is already responding on $ttsHealthUrl but is not usable: $(Format-TtsHealthProblem -Health $existingHealth)"
}
else {
    Write-Step "Starting local TTS server"
    $ttsArguments = @(
        $ttsScript,
        "--log-level",
        $LogLevel,
        "--device",
        $TtsDevice,
        "--inference-timesteps",
        ([string]$TtsInferenceTimesteps)
    )

    if ($NoOptimize) {
        $ttsArguments += "--no-optimize"
    }

    if ($VoiceReferencePath) {
        $ttsArguments += @("--voice-reference-path", $VoiceReferencePath)
    }

    if ($VoiceReferenceText) {
        $ttsArguments += @("--voice-reference-text", $VoiceReferenceText)
    }

    if ($DisableVoiceReference) {
        $ttsArguments += "--disable-voice-reference"
    }

    if ($RegenerateVoiceReference) {
        $ttsArguments += "--regenerate-voice-reference"
    }

    Remove-Item -Path $ttsStdoutLog, $ttsStderrLog -ErrorAction SilentlyContinue

    $startProcessArgs = @{
        FilePath = $venvPython
        ArgumentList = (Join-ProcessArguments -Arguments $ttsArguments)
        WorkingDirectory = $repoRoot
        RedirectStandardOutput = $ttsStdoutLog
        RedirectStandardError = $ttsStderrLog
        PassThru = $true
        WindowStyle = "Hidden"
    }

    $ttsProcess = Start-Process @startProcessArgs
    $startedTtsHere = $true

    Write-Step "Waiting for TTS health on $ttsHealthUrl"
    $existingHealth = Wait-ForHealthyTts -Process $ttsProcess -TimeoutSeconds $HealthTimeoutSeconds

    if (-not (Test-TtsHealthUsable -Health $existingHealth)) {
        Show-LogTail -Path $ttsStdoutLog
        Show-LogTail -Path $ttsStderrLog
        $errorText = Format-TtsHealthProblem -Health $existingHealth
        Stop-MaraProcesses
        throw "TTS server failed to become healthy. $errorText"
    }

    Write-Step "TTS server is healthy"
}

Reset-SavedRegenerateVoiceReference

Write-Step "Running startup diagnostics"
$diagnosticsArgs = @(
    $askMaraScript,
    "--diagnose",
    "--log-level",
    $LogLevel,
    "--active-agent",
    $ActiveAgent,
    "--openclaw-base-url",
    $OpenClawBaseUrl,
    "--openclaw-model",
    $OpenClawModel,
    "--openclaw-timeout",
    ([string]$OpenClawTimeoutSeconds),
    "--tts-url",
    $ttsUrl,
    "--tts-health-url",
    $ttsHealthUrl
)

if ($AudioDevice) {
    $diagnosticsArgs += @("--audio-device", $AudioDevice)
}

if ($CaptureDir) {
    $diagnosticsArgs += @("--capture-dir", $CaptureDir)
}

& $venvPython @diagnosticsArgs
if ($LASTEXITCODE -ne 0) {
    Stop-MaraProcesses
    throw "Startup diagnostics failed with exit code $LASTEXITCODE"
}

if ($Gui) {
    $existingGuiHealth = Get-GuiHealth
    if ($null -ne $existingGuiHealth -and $existingGuiHealth.status -eq "ok") {
        Write-Step "Using existing Mara GUI on $guiUrl"
    }
    else {
        Write-Step "Starting Mara GUI on $guiUrl"
        Remove-Item -Path $guiStdoutLog, $guiStderrLog -ErrorAction SilentlyContinue
        $guiArguments = @(
            $guiScript,
            "--host",
            $GuiHost,
            "--port",
            ([string]$GuiPort),
            "--config-path",
            $resolvedConfigPath,
            "--agent-route-state-path",
            $resolvedAgentRouteStatePath,
            "--event-log",
            (Join-Path $logDir "mara_events.jsonl"),
            "--tts-health-url",
            $ttsHealthUrl
        )

        $guiProcess = Start-Process -FilePath $venvPython -ArgumentList (Join-ProcessArguments -Arguments $guiArguments) -WorkingDirectory $repoRoot -RedirectStandardOutput $guiStdoutLog -RedirectStandardError $guiStderrLog -PassThru -WindowStyle Hidden
        [Environment]::SetEnvironmentVariable('MARA_GUI_PID', [string]$guiProcess.Id)
        $guiHealth = Wait-ForGuiHealth -Process $guiProcess
        if ($null -eq $guiHealth -or $guiHealth.status -ne "ok") {
            Show-LogTail -Path $guiStdoutLog
            Show-LogTail -Path $guiStderrLog
            Stop-MaraProcesses
            throw "GUI server failed to become healthy on $guiUrl"
        }
    }

    Write-Step "Mara GUI available at $guiUrl"
    try {
        Start-Process $guiUrl | Out-Null
    }
    catch {
        Write-Step "Could not open the GUI automatically: $($_.Exception.Message)"
        Write-Step "Open this URL manually: $guiUrl"
    }
}

$listenerArguments = @(
    $listenerScript,
    "--log-level",
    $LogLevel,
    "--tts-url",
    $ttsUrl,
    "--tts-stream-url",
    $resolvedTtsStreamUrl,
    "--ssh-timeout",
    ([string]$SshTimeoutSeconds),
    "--ssh-connect-timeout",
    ([string]$SshConnectTimeoutSeconds),
    "--tts-timeout",
    ([string]$TtsTimeoutSeconds),
    "--status-interval",
    ([string]$StatusIntervalSeconds),
    "--spoken-reply-char-limit",
    ([string]$SpokenReplyCharLimit),
    "--tts-chunk-char-limit",
    ([string]$TtsChunkCharLimit),
    "--tts-stream-chunk-char-limit",
    ([string]$TtsStreamChunkCharLimit),
    "--active-agent",
    $ActiveAgent,
    "--agent-route-state-path",
    $resolvedAgentRouteStatePath,
    "--openclaw-base-url",
    $OpenClawBaseUrl,
    "--openclaw-model",
    $OpenClawModel,
    "--openclaw-timeout",
    ([string]$OpenClawTimeoutSeconds)
)

if ($TtsStreaming) {
    $listenerArguments += "--tts-streaming"
}
else {
    $listenerArguments += "--no-tts-streaming"
}

if ($HermesCommand) {
    $listenerArguments += @("--hermes-command", $HermesCommand)
}

if ($AudioDevice) {
    $listenerArguments += @("--audio-device", $AudioDevice)
}

if ($CaptureDir) {
    $listenerArguments += @("--capture-dir", $CaptureDir)
}

if ($ForcePolling) {
    $listenerArguments += "--force-polling"
}

$listenerExitCode = 0

try {
    Write-Step "Starting Voicebox listener in this window"
    if ($null -ne $ttsProcess) {
        [Environment]::SetEnvironmentVariable('MARA_TTS_PID', [string]$ttsProcess.Id)
    }
    & $venvPython @listenerArguments
    $listenerExitCode = $LASTEXITCODE
}
catch {
    Stop-MaraProcesses
    throw
}
finally {
    Stop-MaraProcesses
    [Environment]::SetEnvironmentVariable('MARA_TTS_PID', $null)
    [Environment]::SetEnvironmentVariable('MARA_GUI_PID', $null)
}

exit $listenerExitCode
