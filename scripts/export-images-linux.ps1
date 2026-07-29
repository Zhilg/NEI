<#
.SYNOPSIS
Build and export Linux Docker images to tar archive for air-gapped deployment.
PowerShell 7+ compatible.
#>

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot ".."))
$transferDirectory = Join-Path $projectRoot "transfer"
$absoluteOutput = Join-Path $transferDirectory "idp-images-linux.tar"
$metadataPath = "$absoluteOutput.json"
$completionPath = Join-Path $transferDirectory "EXPORT-LINUX-COMPLETE.txt"
$appImage = "local/idp-app:latest"
$vllmVlImage = "local/vllm-vl:latest"
$vllmLlmImage = "local/vllm-llm:latest"
$pythonImage = "python:3.12-slim"
$vllmImage = "vllm/vllm-openai:v0.26.0"

function Invoke-Docker {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)
    & docker @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "docker $($Arguments -join ' ') failed with exit code $LASTEXITCODE."
    }
}

New-Item -ItemType Directory -Force -Path $transferDirectory | Out-Null
Remove-Item -Force $completionPath -ErrorAction SilentlyContinue

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "Docker Desktop CLI was not found in PATH."
}

try {
    Write-Host "[1/5] Pulling base images..."
    Invoke-Docker pull $pythonImage
    Invoke-Docker pull $vllmImage

    Write-Host "[2/5] Building worker image..."
    Invoke-Docker build --pull=false --tag $appImage $projectRoot

    Write-Host "[3/5] Building Linux vLLM images with latest transformers..."
    Invoke-Docker build --pull=false --tag $vllmVlImage "$projectRoot/infra/dockerfiles/vllm-vl"
    Invoke-Docker build --pull=false --tag $vllmLlmImage "$projectRoot/infra/dockerfiles/vllm-llm"

    Write-Host "[4/5] Saving images..."
    $images = @($appImage, $vllmVlImage, $vllmLlmImage)
    Invoke-Docker save "--output=$absoluteOutput" @images

    Write-Host "[5/5] Writing metadata..."
    $metadata = @{
        app_image = $appImage
        vllm_vl_image = $vllmVlImage
        vllm_llm_image = $vllmLlmImage
        base_vllm_image = $vllmImage
    } | ConvertTo-Json
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($metadataPath, $metadata, $utf8NoBom)
    "Completed: $(Get-Date -Format o)" | Set-Content -Encoding ascii -Path $completionPath
    Write-Host ""
    Write-Host "=== EXPORT COMPLETE ===" -ForegroundColor Green
    Write-Host "Archive: $absoluteOutput"
    Write-Host "Metadata: $metadataPath"
    Write-Host "Completion marker: $completionPath"
    Write-Host ""
    Write-Host "Transfer the following to Linux:"
    Write-Host "  - $absoluteOutput"
    Write-Host "  - $metadataPath"
    Write-Host "  - $completionPath"
    Write-Host "  - transfer/models/vl/ (model files)"
    Write-Host "  - transfer/models/llm/ (model files)"
} finally {}
