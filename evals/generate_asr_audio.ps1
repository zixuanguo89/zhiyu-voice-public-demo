param([Parameter(Mandatory)][string]$Jobs)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
$synth = [System.Speech.Synthesis.SpeechSynthesizer]::new()
$format = [System.Speech.AudioFormat.SpeechAudioFormatInfo]::new(16000, 'Sixteen', 'Mono')
try {
    foreach ($job in (Get-Content -LiteralPath $Jobs -Raw -Encoding UTF8 | ConvertFrom-Json)) {
        $synth.SelectVoice($job.voice)
        $synth.Rate = [int]$job.rate
        $synth.SetOutputToWaveFile($job.output, $format)
        $synth.Speak([string]$job.text)
        $synth.SetOutputToNull()
    }
} finally {
    $synth.Dispose()
}
