param(
    [string]$AudioDevice,
    [string]$CaptureDir,
    [string]$LogLevel = "INFO",
    [string]$TtsDevice = "cuda",
    [int]$HealthTimeoutSeconds = 120,
    [double]$SshTimeoutSeconds = 0,
    [double]$SshConnectTimeoutSeconds = 5,
    [double]$TtsTimeoutSeconds = 300,
    [int]$TtsInferenceTimesteps = 4,
    [double]$StatusIntervalSeconds = 10,
    [int]$SpokenReplyCharLimit = 900,
    [int]$TtsChunkCharLimit = 450,
    [int]$TtsStreamChunkCharLimit = 1200,
    [double]$TtsStreamPrebufferSeconds = 0.5,
    [double]$TtsStreamPrebufferMaxSeconds = 4,
    [switch]$TtsStreamPrebufferDynamic = $true,
    [switch]$NoTtsStreamPrebufferDynamic,
    [string]$TtsStreamUrl,
    [switch]$AsyncAgentReplies = $true,
    [switch]$NoAsyncAgentReplies,
    [switch]$AsyncAckAgent,
    [switch]$NoAsyncAckAgent,
    [double]$AsyncAckGraceSeconds = 5,
    [string]$AsyncAckText,
    [string]$AsyncAckPhrases,
    [switch]$AsyncFollowup = $true,
    [switch]$NoAsyncFollowup,
    [double]$AsyncFollowupInitialDelaySeconds = 3,
    [double]$AsyncFollowupPollSeconds = 8,
    [int]$AsyncFollowupMaxAttempts = 60,
    [string]$VoiceInboxPath,
    [double]$VoiceInboxPollSeconds = 5,
    [string]$AgentSessionId = "voice-session",
    [string]$HermesSessionId,
    [string]$OpenClawSessionId,
    [string]$AgentSessionHistoryPath,
    [int]$AgentSessionHistoryMessages = 20,
    [switch]$AgentSessionPersistence = $true,
    [switch]$NoAgentSessionPersistence,
    [string]$ConfigPath,
    [string]$HermesCommand,
    [string]$ActiveAgent = "hermes",
    [string]$AgentRouteStatePath,
    [string]$OpenClawBaseUrl = "http://192.168.0.65:8645/v1",
    [string]$OpenClawModel = "gemini 3.1 pro",
    [double]$OpenClawTimeoutSeconds = 600,
    [string]$VoiceReferencePath,
    [string]$VoiceReferenceText,
    [string]$HermesVoiceStyle,
    [string]$OpenClawVoiceStyle,
    [switch]$DisableVoiceReference,
    [switch]$RegenerateVoiceReference,
    [switch]$ForcePolling,
    [switch]$TtsStreaming = $true,
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
$ttsScript = Join-Path $repoRoot "mara\tts_server.py"
$listenerScript = Join-Path $repoRoot "jarvis_listener.py"
$guiScript = Join-Path $repoRoot "mara\gui_server.py"
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
$guiOpenUrl = "$guiUrl/?v=$((Get-Date).ToUniversalTime().ToString('yyyyMMddHHmmss'))"
$requiredVoiceGenerationMode = "persistent_reference_only_v2"
$requiredGuiOptionKeys = @(
    "hermes_voice_profile",
    "hermes_voice_style",
    "openclaw_voice_profile",
    "openclaw_voice_style"
)

$requiredGuiRouteProbes = @(
    "/api/chat"
)

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
        if ([string]::IsNullOrWhiteSpace($value)) {
            continue
        }
        if ([string]::IsNullOrEmpty([Environment]::GetEnvironmentVariable($name))) {
            [Environment]::SetEnvironmentVariable($name, $value, "Process")
        }
    }
}

if ($PSBoundParameters.ContainsKey('TtsStreaming') -and $TtsStreaming -and $NoTtsStreaming) {
    throw "Use either -TtsStreaming or -NoTtsStreaming, not both."
}

if ($PSBoundParameters.ContainsKey('TtsStreamPrebufferDynamic') -and $TtsStreamPrebufferDynamic -and $NoTtsStreamPrebufferDynamic) {
    throw "Use either -TtsStreamPrebufferDynamic or -NoTtsStreamPrebufferDynamic, not both."
}

if ($PSBoundParameters.ContainsKey('AsyncAgentReplies') -and $AsyncAgentReplies -and $NoAsyncAgentReplies) {
    throw "Use either -AsyncAgentReplies or -NoAsyncAgentReplies, not both."
}

if ($PSBoundParameters.ContainsKey('AsyncAckAgent') -and $AsyncAckAgent -and $NoAsyncAckAgent) {
    throw "Use either -AsyncAckAgent or -NoAsyncAckAgent, not both."
}

if ($PSBoundParameters.ContainsKey('AsyncFollowup') -and $AsyncFollowup -and $NoAsyncFollowup) {
    throw "Use either -AsyncFollowup or -NoAsyncFollowup, not both."
}

if ($PSBoundParameters.ContainsKey('AgentSessionPersistence') -and $AgentSessionPersistence -and $NoAgentSessionPersistence) {
    throw "Use either -AgentSessionPersistence or -NoAgentSessionPersistence, not both."
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

# Read tts_backend from saved config to determine which health checks apply.
# Must be after $savedConfig is loaded above.
$isTtsBackendVoicebox = $false
$savedVoiceboxUrl = ""
$savedHermesVoiceboxProfileId = ""
$savedOpenClawVoiceboxProfileId = ""
if ($null -ne $savedConfig) {
    if ($savedConfig.PSObject.Properties.Name -contains "tts_backend" -and [string]$savedConfig.tts_backend -eq "voicebox") {
        $isTtsBackendVoicebox = $true
    }
    if ($savedConfig.PSObject.Properties.Name -contains "voicebox_url" -and -not [string]::IsNullOrWhiteSpace([string]$savedConfig.voicebox_url)) {
        $savedVoiceboxUrl = [string]$savedConfig.voicebox_url
    }
    if ($savedConfig.PSObject.Properties.Name -contains "hermes_voicebox_profile_id" -and -not [string]::IsNullOrWhiteSpace([string]$savedConfig.hermes_voicebox_profile_id)) {
        $savedHermesVoiceboxProfileId = [string]$savedConfig.hermes_voicebox_profile_id
    }
    if ($savedConfig.PSObject.Properties.Name -contains "openclaw_voicebox_profile_id" -and -not [string]::IsNullOrWhiteSpace([string]$savedConfig.openclaw_voicebox_profile_id)) {
        $savedOpenClawVoiceboxProfileId = [string]$savedConfig.openclaw_voicebox_profile_id
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

function Resolve-Option {
    param(
        [hashtable]$Bound,
        [string]$Name,
        $Current,
        [string]$EnvName = "",
        [string]$SavedName = "",
        [ValidateSet('String', 'Int', 'Double')]
        [string]$Type = 'String',
        [switch]$SavedKeepBlank
    )

    # Precedence (highest first): explicit -Parameter, saved GUI option, environment
    # variable, then the parameter default. This matches the original two-pass logic
    # where the environment pass ran first and the saved-config pass ran second.
    if ($Bound.ContainsKey($Name)) {
        return $Current
    }

    if ($SavedName -and (Test-SavedOption $SavedName)) {
        $savedValue = $savedConfig.$SavedName
        switch ($Type) {
            'Int' { return [int]$savedValue }
            'Double' { return [double]$savedValue }
            default {
                $savedText = [string]$savedValue
                if ($SavedKeepBlank -or -not [string]::IsNullOrWhiteSpace($savedText)) {
                    return $savedText
                }
                # Whitespace-only saved string: fall through to environment/default.
            }
        }
    }

    if ($EnvName -and (Test-EnvOption $EnvName)) {
        switch ($Type) {
            'Int' { return [int](Get-EnvOption $EnvName) }
            'Double' { return [double](Get-EnvOption $EnvName) }
            default { return (Get-EnvOption $EnvName) }
        }
    }

    return $Current
}

$boundParams = $PSBoundParameters

# --- Scalar options: explicit param > saved GUI option > environment > default ---
$AudioDevice = Resolve-Option $boundParams 'AudioDevice' $AudioDevice -EnvName 'MARA_AUDIO_DEVICE' -SavedName 'audio_device' -SavedKeepBlank
$CaptureDir = Resolve-Option $boundParams 'CaptureDir' $CaptureDir -EnvName 'MARA_CAPTURE_DIR' -SavedName 'capture_dir' -SavedKeepBlank
$LogLevel = Resolve-Option $boundParams 'LogLevel' $LogLevel -EnvName 'MARA_LOG_LEVEL'
$TtsDevice = Resolve-Option $boundParams 'TtsDevice' $TtsDevice -EnvName 'MARA_TTS_DEVICE' -SavedName 'tts_device'
$SshTimeoutSeconds = Resolve-Option $boundParams 'SshTimeoutSeconds' $SshTimeoutSeconds -EnvName 'MARA_SSH_TIMEOUT' -SavedName 'ssh_timeout_seconds' -Type 'Double'
$SshConnectTimeoutSeconds = Resolve-Option $boundParams 'SshConnectTimeoutSeconds' $SshConnectTimeoutSeconds -EnvName 'MARA_SSH_CONNECT_TIMEOUT' -SavedName 'ssh_connect_timeout_seconds' -Type 'Double'
$TtsTimeoutSeconds = Resolve-Option $boundParams 'TtsTimeoutSeconds' $TtsTimeoutSeconds -EnvName 'MARA_TTS_TIMEOUT' -SavedName 'tts_timeout_seconds' -Type 'Double'
$TtsInferenceTimesteps = Resolve-Option $boundParams 'TtsInferenceTimesteps' $TtsInferenceTimesteps -EnvName 'MARA_TTS_INFERENCE_TIMESTEPS' -SavedName 'tts_inference_timesteps' -Type 'Int'
$StatusIntervalSeconds = Resolve-Option $boundParams 'StatusIntervalSeconds' $StatusIntervalSeconds -EnvName 'MARA_STATUS_INTERVAL' -SavedName 'status_interval_seconds' -Type 'Double'
$SpokenReplyCharLimit = Resolve-Option $boundParams 'SpokenReplyCharLimit' $SpokenReplyCharLimit -EnvName 'MARA_SPOKEN_REPLY_CHAR_LIMIT' -SavedName 'spoken_reply_char_limit' -Type 'Int'
$TtsChunkCharLimit = Resolve-Option $boundParams 'TtsChunkCharLimit' $TtsChunkCharLimit -EnvName 'MARA_TTS_CHUNK_CHAR_LIMIT' -SavedName 'tts_chunk_char_limit' -Type 'Int'
$TtsStreamChunkCharLimit = Resolve-Option $boundParams 'TtsStreamChunkCharLimit' $TtsStreamChunkCharLimit -EnvName 'MARA_TTS_STREAM_CHUNK_CHAR_LIMIT' -SavedName 'tts_stream_chunk_char_limit' -Type 'Int'
$TtsStreamPrebufferSeconds = Resolve-Option $boundParams 'TtsStreamPrebufferSeconds' $TtsStreamPrebufferSeconds -EnvName 'MARA_TTS_STREAM_PREBUFFER_SECONDS' -SavedName 'tts_stream_prebuffer_seconds' -Type 'Double'
$TtsStreamPrebufferMaxSeconds = Resolve-Option $boundParams 'TtsStreamPrebufferMaxSeconds' $TtsStreamPrebufferMaxSeconds -EnvName 'MARA_TTS_STREAM_PREBUFFER_MAX_SECONDS' -SavedName 'tts_stream_prebuffer_max_seconds' -Type 'Double'
$TtsStreamUrl = Resolve-Option $boundParams 'TtsStreamUrl' $TtsStreamUrl -EnvName 'MARA_TTS_STREAM_URL'
$AsyncAckGraceSeconds = Resolve-Option $boundParams 'AsyncAckGraceSeconds' $AsyncAckGraceSeconds -EnvName 'MARA_ASYNC_ACK_GRACE_SECONDS' -SavedName 'async_ack_grace_seconds' -Type 'Double'
$AsyncAckText = Resolve-Option $boundParams 'AsyncAckText' $AsyncAckText -EnvName 'MARA_ASYNC_ACK_TEXT' -SavedName 'async_ack_text'
$AsyncAckPhrases = Resolve-Option $boundParams 'AsyncAckPhrases' $AsyncAckPhrases -EnvName 'MARA_ASYNC_ACK_PHRASES' -SavedName 'async_ack_phrases' -SavedKeepBlank
$AsyncFollowupInitialDelaySeconds = Resolve-Option $boundParams 'AsyncFollowupInitialDelaySeconds' $AsyncFollowupInitialDelaySeconds -EnvName 'MARA_ASYNC_FOLLOWUP_INITIAL_DELAY_SECONDS' -SavedName 'async_followup_initial_delay_seconds' -Type 'Double'
$AsyncFollowupPollSeconds = Resolve-Option $boundParams 'AsyncFollowupPollSeconds' $AsyncFollowupPollSeconds -EnvName 'MARA_ASYNC_FOLLOWUP_POLL_SECONDS' -SavedName 'async_followup_poll_seconds' -Type 'Double'
$AsyncFollowupMaxAttempts = Resolve-Option $boundParams 'AsyncFollowupMaxAttempts' $AsyncFollowupMaxAttempts -EnvName 'MARA_ASYNC_FOLLOWUP_MAX_ATTEMPTS' -SavedName 'async_followup_max_attempts' -Type 'Int'
$VoiceInboxPath = Resolve-Option $boundParams 'VoiceInboxPath' $VoiceInboxPath -EnvName 'MARA_VOICE_INBOX_PATH' -SavedName 'voice_inbox_path'
$VoiceInboxPollSeconds = Resolve-Option $boundParams 'VoiceInboxPollSeconds' $VoiceInboxPollSeconds -EnvName 'MARA_VOICE_INBOX_POLL_SECONDS' -SavedName 'voice_inbox_poll_seconds' -Type 'Double'
$AgentSessionId = Resolve-Option $boundParams 'AgentSessionId' $AgentSessionId -EnvName 'MARA_AGENT_SESSION_ID' -SavedName 'agent_session_id'
$HermesSessionId = Resolve-Option $boundParams 'HermesSessionId' $HermesSessionId -EnvName 'MARA_HERMES_SESSION_ID' -SavedName 'hermes_session_id'
$OpenClawSessionId = Resolve-Option $boundParams 'OpenClawSessionId' $OpenClawSessionId -EnvName 'MARA_OPENCLAW_SESSION_ID' -SavedName 'openclaw_session_id'
$AgentSessionHistoryPath = Resolve-Option $boundParams 'AgentSessionHistoryPath' $AgentSessionHistoryPath -EnvName 'MARA_AGENT_SESSION_HISTORY_PATH' -SavedName 'agent_session_history_path'
$AgentSessionHistoryMessages = Resolve-Option $boundParams 'AgentSessionHistoryMessages' $AgentSessionHistoryMessages -EnvName 'MARA_AGENT_SESSION_HISTORY_MESSAGES' -SavedName 'agent_session_history_messages' -Type 'Int'
$HermesCommand = Resolve-Option $boundParams 'HermesCommand' $HermesCommand -EnvName 'MARA_HERMES_COMMAND' -SavedName 'hermes_command'
$ActiveAgent = Resolve-Option $boundParams 'ActiveAgent' $ActiveAgent -EnvName 'MARA_ACTIVE_AGENT' -SavedName 'active_agent'
$OpenClawBaseUrl = Resolve-Option $boundParams 'OpenClawBaseUrl' $OpenClawBaseUrl -EnvName 'MARA_OPENCLAW_BASE_URL' -SavedName 'openclaw_base_url'
$OpenClawModel = Resolve-Option $boundParams 'OpenClawModel' $OpenClawModel -EnvName 'MARA_OPENCLAW_MODEL' -SavedName 'openclaw_model'
$OpenClawTimeoutSeconds = Resolve-Option $boundParams 'OpenClawTimeoutSeconds' $OpenClawTimeoutSeconds -EnvName 'MARA_OPENCLAW_TIMEOUT' -SavedName 'openclaw_timeout_seconds' -Type 'Double'
$VoiceReferencePath = Resolve-Option $boundParams 'VoiceReferencePath' $VoiceReferencePath -EnvName 'MARA_VOICE_REFERENCE_PATH' -SavedName 'voice_reference_path'
$VoiceReferenceText = Resolve-Option $boundParams 'VoiceReferenceText' $VoiceReferenceText -EnvName 'MARA_VOICE_REFERENCE_TEXT' -SavedName 'voice_reference_text'
$HermesVoiceStyle = Resolve-Option $boundParams 'HermesVoiceStyle' $HermesVoiceStyle -EnvName 'MARA_VOICE_STYLE' -SavedName 'hermes_voice_style'
$OpenClawVoiceStyle = Resolve-Option $boundParams 'OpenClawVoiceStyle' $OpenClawVoiceStyle -EnvName 'MARA_OPENCLAW_VOICE_STYLE' -SavedName 'openclaw_voice_style'

# --- Boolean switches need bespoke handling for their -No* counterparts ---
if (-not $boundParams.ContainsKey('TtsStreaming') -and -not $NoTtsStreaming -and (Test-EnvOption "MARA_TTS_STREAMING")) {
    $TtsStreaming = Get-EnvBool "MARA_TTS_STREAMING"
}
if ($NoTtsStreaming) {
    $TtsStreaming = $false
}
elseif (-not $boundParams.ContainsKey('TtsStreaming') -and (Test-SavedOption "tts_streaming")) {
    $TtsStreaming = [bool]$savedConfig.tts_streaming
}

if (-not $boundParams.ContainsKey('TtsStreamPrebufferDynamic') -and -not $NoTtsStreamPrebufferDynamic -and (Test-EnvOption "MARA_TTS_STREAM_PREBUFFER_DYNAMIC")) {
    $TtsStreamPrebufferDynamic = Get-EnvBool "MARA_TTS_STREAM_PREBUFFER_DYNAMIC"
}
if ($NoTtsStreamPrebufferDynamic) {
    $TtsStreamPrebufferDynamic = $false
}
elseif (-not $boundParams.ContainsKey('TtsStreamPrebufferDynamic') -and (Test-SavedOption "tts_stream_prebuffer_dynamic")) {
    $TtsStreamPrebufferDynamic = [bool]$savedConfig.tts_stream_prebuffer_dynamic
}

if (-not $boundParams.ContainsKey('AsyncAgentReplies') -and -not $NoAsyncAgentReplies -and (Test-EnvOption "MARA_ASYNC_AGENT_REPLIES")) {
    $AsyncAgentReplies = Get-EnvBool "MARA_ASYNC_AGENT_REPLIES"
}
if ($NoAsyncAgentReplies) {
    $AsyncAgentReplies = $false
}
elseif (-not $boundParams.ContainsKey('AsyncAgentReplies') -and (Test-SavedOption "async_agent_replies")) {
    $AsyncAgentReplies = [bool]$savedConfig.async_agent_replies
}

if (-not $boundParams.ContainsKey('AsyncAckAgent') -and -not $NoAsyncAckAgent -and (Test-EnvOption "MARA_ASYNC_ACK_AGENT")) {
    $AsyncAckAgent = Get-EnvBool "MARA_ASYNC_ACK_AGENT"
}
if ($NoAsyncAckAgent) {
    $AsyncAckAgent = $false
}
elseif (-not $boundParams.ContainsKey('AsyncAckAgent') -and (Test-SavedOption "async_ack_agent")) {
    $AsyncAckAgent = [bool]$savedConfig.async_ack_agent
}

if (-not $boundParams.ContainsKey('AsyncFollowup') -and -not $NoAsyncFollowup -and (Test-EnvOption "MARA_ASYNC_FOLLOWUP_ENABLED")) {
    $AsyncFollowup = Get-EnvBool "MARA_ASYNC_FOLLOWUP_ENABLED"
}
if ($NoAsyncFollowup) {
    $AsyncFollowup = $false
}
elseif (-not $boundParams.ContainsKey('AsyncFollowup') -and (Test-SavedOption "async_followup_enabled")) {
    $AsyncFollowup = [bool]$savedConfig.async_followup_enabled
}

if (-not $boundParams.ContainsKey('AgentSessionPersistence') -and -not $NoAgentSessionPersistence -and (Test-EnvOption "MARA_AGENT_SESSION_PERSISTENCE")) {
    $AgentSessionPersistence = Get-EnvBool "MARA_AGENT_SESSION_PERSISTENCE"
}
if ($NoAgentSessionPersistence) {
    $AgentSessionPersistence = $false
}
elseif (-not $boundParams.ContainsKey('AgentSessionPersistence') -and (Test-SavedOption "agent_session_persistence")) {
    $AgentSessionPersistence = [bool]$savedConfig.agent_session_persistence
}

$ActiveAgent = $ActiveAgent.Trim().ToLowerInvariant()
if ($ActiveAgent -notin @("hermes", "openclaw")) {
    throw "ActiveAgent must be 'hermes' or 'openclaw'."
}

if (-not $boundParams.ContainsKey('RegenerateVoiceReference') -and (Test-SavedOption "regenerate_voice_reference")) {
    $RegenerateVoiceReference = [bool]$savedConfig.regenerate_voice_reference
    $regenerateVoiceReferenceFromSavedConfig = [bool]$savedConfig.regenerate_voice_reference
}

$resolvedTtsStreamUrl = if ($TtsStreamUrl) { $TtsStreamUrl } else { "http://127.0.0.1:8000/tts/stream" }
$resolvedAgentSessionHistoryPath = if ($AgentSessionHistoryPath) { $AgentSessionHistoryPath } else { Join-Path $repoRoot "config\mara_agent_sessions.json" }
$resolvedHermesSessionId = if ($HermesSessionId) { $HermesSessionId } else { "$AgentSessionId-hermes" }
$resolvedOpenClawSessionId = if ($OpenClawSessionId) { $OpenClawSessionId } else { "$AgentSessionId-openclaw" }

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

function Get-VoiceProfile {
    param(
        $Health,
        [string]$VoiceId
    )

    if ($null -eq $Health -or -not ($Health.PSObject.Properties.Name -contains "voice_profiles")) {
        return $null
    }

    $profileProperty = $Health.voice_profiles.PSObject.Properties[$VoiceId]
    if ($null -eq $profileProperty) {
        return $null
    }

    return $profileProperty.Value
}

function Test-VoiceProfileStyle {
    param(
        $Health,
        [string]$VoiceId,
        [string]$ExpectedStyle
    )

    if ([string]::IsNullOrWhiteSpace($ExpectedStyle)) {
        return $true
    }

    $profile = Get-VoiceProfile -Health $Health -VoiceId $VoiceId
    if ($null -eq $profile -or -not ($profile.PSObject.Properties.Name -contains "voice_style")) {
        return $false
    }

    return (Normalize-Whitespace -Text ([string]$profile.voice_style)) -eq (Normalize-Whitespace -Text $ExpectedStyle)
}

function Test-TtsHealthUsable {
    param($Health)

    if ($null -eq $Health -or $Health.status -ne "ok") {
        return $false
    }

    # VoiceBox backend: check that the running server is also VoiceBox, then done.
    if ($isTtsBackendVoicebox) {
        if (-not ($Health.PSObject.Properties.Name -contains "backend") -or [string]$Health.backend -ne "voicebox") {
            return $false
        }
        return $true
    }

    # VoxCPM2 backend: reject if a VoiceBox server is running here.
    if ($Health.PSObject.Properties.Name -contains "backend" -and [string]$Health.backend -eq "voicebox") {
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

        if (-not ($Health.PSObject.Properties.Name -contains "stream_pcm_sanitized")) {
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

    if (-not ($Health.voice_profiles.PSObject.Properties.Name -contains "hermes")) {
        return $false
    }

    if (-not ($Health.voice_profiles.PSObject.Properties.Name -contains "openclaw")) {
        return $false
    }

    if ($Health.voice_generation_mode -ne $requiredVoiceGenerationMode) {
        return $false
    }

    if (-not (Test-VoiceProfileStyle -Health $Health -VoiceId "hermes" -ExpectedStyle $HermesVoiceStyle)) {
        return $false
    }

    if (-not (Test-VoiceProfileStyle -Health $Health -VoiceId "openclaw" -ExpectedStyle $OpenClawVoiceStyle)) {
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

    if ($isTtsBackendVoicebox) {
        if (-not ($Health.PSObject.Properties.Name -contains "backend") -or [string]$Health.backend -ne "voicebox") {
            return "Expected VoiceBox backend but TTS server is running a different backend. Stop the old TTS server and start again."
        }
        return "VoiceBox TTS health returned status '$($Health.status)'."
    }

    if ($Health.PSObject.Properties.Name -contains "backend" -and [string]$Health.backend -eq "voicebox") {
        return "TTS server is running VoiceBox backend but config requests VoxCPM2. Stop the old TTS server and start again."
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

        if (-not ($Health.voice_profiles.PSObject.Properties.Name -contains "hermes")) {
            return "The server does not report the Hermes voice profile. Stop the old TTS server and start again."
        }

        if (-not ($Health.voice_profiles.PSObject.Properties.Name -contains "openclaw")) {
            return "The server does not report the OpenClaw voice profile. Stop the old TTS server and start again."
        }

        if ($Health.voice_generation_mode -ne $requiredVoiceGenerationMode) {
            return "The server uses voice_generation_mode='$($Health.voice_generation_mode)', but this launch requires '$requiredVoiceGenerationMode'. Stop the old TTS server and start again."
        }

        if (-not (Test-VoiceProfileStyle -Health $Health -VoiceId "hermes" -ExpectedStyle $HermesVoiceStyle)) {
            return "The server uses a different Hermes voice style than this launch. Stop the old TTS server and start again."
        }

        if (-not (Test-VoiceProfileStyle -Health $Health -VoiceId "openclaw" -ExpectedStyle $OpenClawVoiceStyle)) {
            return "The server uses a different OpenClaw voice style than this launch. Stop the old TTS server and start again."
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

        if (-not ($Health.PSObject.Properties.Name -contains "stream_pcm_sanitized")) {
            return "The server does not report sanitized streaming PCM. Stop the old TTS server and start again."
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

function Test-GuiRouteProbeUsable {
    param([string]$Route)

    try {
        Invoke-WebRequest -Uri "$guiUrl$Route" -Method Get -TimeoutSec 2 | Out-Null
        return $true
    }
    catch {
        $response = $_.Exception.Response
        if ($null -eq $response) {
            return $false
        }

        $statusCode = [int]$response.StatusCode
        return $statusCode -eq 405
    }
}

function Test-GuiRouteProbesUsable {
    foreach ($route in $requiredGuiRouteProbes) {
        if (-not (Test-GuiRouteProbeUsable -Route $route)) {
            return $false
        }
    }

    return $true
}

function Test-GuiHealthUsable {
    param($Health)

    if ($null -eq $Health -or $Health.status -ne "ok") {
        return $false
    }

    if (-not ($Health.PSObject.Properties.Name -contains "option_schema_keys")) {
        return $false
    }

    $schemaKeys = @($Health.option_schema_keys)
    foreach ($key in $requiredGuiOptionKeys) {
        if ($schemaKeys -notcontains $key) {
            return $false
        }
    }

    if (-not (Test-GuiRouteProbesUsable)) {
        return $false
    }

    return $true
}

function Format-GuiHealthProblem {
    param($Health)

    if ($null -eq $Health) {
        return "No health response received."
    }

    if ($Health.status -ne "ok") {
        return "Health endpoint returned status '$($Health.status)'."
    }

    if (-not ($Health.PSObject.Properties.Name -contains "option_schema_keys")) {
        return "The server does not report option schema keys. Stop the old GUI server and start again."
    }

    $schemaKeys = @($Health.option_schema_keys)
    foreach ($key in $requiredGuiOptionKeys) {
        if ($schemaKeys -notcontains $key) {
            return "The server option schema is missing '$key'. Stop the old GUI server and start again."
        }
    }

    foreach ($route in $requiredGuiRouteProbes) {
        if (-not (Test-GuiRouteProbeUsable -Route $route)) {
            return "The GUI server is missing required route '$route'. Stop the old GUI server and start again."
        }
    }

    return "The GUI health endpoint is not compatible with this launcher."
}

function Wait-ForGuiHealth {
    param(
        [System.Diagnostics.Process]$Process,
        [int]$TimeoutSeconds = 20
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        $health = Get-GuiHealth
        if (Test-GuiHealthUsable -Health $health) {
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
            if ($commandLine -notlike "*mara\\tts_server.py*" -and $commandLine -notlike "*mara/tts_server.py*") {
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

function Stop-ExistingLocalGuiServer {
    $stoppedAny = $false
    try {
        $connections = Get-NetTCPConnection -LocalPort $GuiPort -State Listen -ErrorAction SilentlyContinue
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
            if ($commandLine -notlike "*mara\\gui_server.py*" -and $commandLine -notlike "*mara/gui_server.py*") {
                continue
            }

            Write-Step "Stopping stale Mara GUI server process $processId"
            Stop-Process -Id ([int]$processId) -Force -ErrorAction Stop
            $stoppedAny = $true
        }
        catch {
            Write-Step "Could not stop GUI process $processId`: $($_.Exception.Message)"
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
        $LogLevel
    )

    if ($isTtsBackendVoicebox) {
        $ttsArguments += @("--tts-backend", "voicebox")
        if ($savedVoiceboxUrl) {
            $ttsArguments += @("--voicebox-url", $savedVoiceboxUrl)
        }
        if ($savedHermesVoiceboxProfileId) {
            $ttsArguments += @("--hermes-voicebox-profile-id", $savedHermesVoiceboxProfileId)
        }
        if ($savedOpenClawVoiceboxProfileId) {
            $ttsArguments += @("--openclaw-voicebox-profile-id", $savedOpenClawVoiceboxProfileId)
        }
    }
    else {
        $ttsArguments += @("--device", $TtsDevice, "--inference-timesteps", ([string]$TtsInferenceTimesteps))

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

        if ($HermesVoiceStyle) {
            $ttsArguments += @("--hermes-voice-style", $HermesVoiceStyle)
        }

        if ($OpenClawVoiceStyle) {
            $ttsArguments += @("--openclaw-voice-style", $OpenClawVoiceStyle)
        }
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
    if ($null -ne $existingGuiHealth -and -not (Test-GuiHealthUsable -Health $existingGuiHealth)) {
        $guiHealthProblem = Format-GuiHealthProblem -Health $existingGuiHealth
        Write-Step "Existing GUI server is not usable: $guiHealthProblem"
        if (Stop-ExistingLocalGuiServer) {
            Start-Sleep -Seconds 1
            $existingGuiHealth = Get-GuiHealth
        }
        else {
            throw "A GUI server is already responding on $guiUrl but could not be safely restarted: $guiHealthProblem"
        }
    }

    if ($null -ne $existingGuiHealth -and (Test-GuiHealthUsable -Health $existingGuiHealth)) {
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
        if (-not (Test-GuiHealthUsable -Health $guiHealth)) {
            Show-LogTail -Path $guiStdoutLog
            Show-LogTail -Path $guiStderrLog
            $guiHealthProblem = Format-GuiHealthProblem -Health $guiHealth
            Stop-MaraProcesses
            throw "GUI server failed to become healthy on $guiUrl. $guiHealthProblem"
        }
    }

    Write-Step "Mara GUI available at $guiUrl"
    try {
        Start-Process $guiOpenUrl | Out-Null
    }
    catch {
        Write-Step "Could not open the GUI automatically: $($_.Exception.Message)"
        Write-Step "Open this URL manually: $guiOpenUrl"
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
    "--tts-stream-prebuffer-seconds",
    ([string]$TtsStreamPrebufferSeconds),
    "--tts-stream-prebuffer-max-seconds",
    ([string]$TtsStreamPrebufferMaxSeconds),
    "--async-ack-grace-seconds",
    ([string]$AsyncAckGraceSeconds),
    "--async-followup-initial-delay-seconds",
    ([string]$AsyncFollowupInitialDelaySeconds),
    "--async-followup-poll-seconds",
    ([string]$AsyncFollowupPollSeconds),
    "--async-followup-max-attempts",
    ([string]$AsyncFollowupMaxAttempts),
    "--voice-inbox-poll-seconds",
    ([string]$VoiceInboxPollSeconds),
    "--active-agent",
    $ActiveAgent,
    "--agent-route-state-path",
    $resolvedAgentRouteStatePath,
    "--agent-session-id",
    $AgentSessionId,
    "--hermes-session-id",
    $resolvedHermesSessionId,
    "--openclaw-session-id",
    $resolvedOpenClawSessionId,
    "--agent-session-history-path",
    $resolvedAgentSessionHistoryPath,
    "--agent-session-history-messages",
    ([string]$AgentSessionHistoryMessages),
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

if ($TtsStreamPrebufferDynamic) {
    $listenerArguments += "--tts-stream-prebuffer-dynamic"
}
else {
    $listenerArguments += "--no-tts-stream-prebuffer-dynamic"
}

if ($AsyncAgentReplies) {
    $listenerArguments += "--async-agent-replies"
}
else {
    $listenerArguments += "--no-async-agent-replies"
}

if ($AsyncFollowup) {
    $listenerArguments += "--async-followup"
}
else {
    $listenerArguments += "--no-async-followup"
}

if ($AgentSessionPersistence) {
    $listenerArguments += "--agent-session-persistence"
}
else {
    $listenerArguments += "--no-agent-session-persistence"
}

if ($AsyncAckAgent) {
    $listenerArguments += "--async-ack-agent"
}
else {
    $listenerArguments += "--no-async-ack-agent"
}

if ($AsyncAckText) {
    $listenerArguments += @("--async-ack-text", $AsyncAckText)
}

if ($AsyncAckPhrases) {
    $listenerArguments += @("--async-ack-phrases", $AsyncAckPhrases)
}

if ($VoiceInboxPath) {
    $listenerArguments += @("--voice-inbox-path", $VoiceInboxPath)
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
