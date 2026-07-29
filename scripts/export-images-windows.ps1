<#
.SYNOPSIS
Export script: builds worker image, builds Windows E2E images, saves to tar.
Linux models are expected to be already present in transfer/models/.
#>

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot ".."))
$transferDirectory = Join-Path $projectRoot "transfer"
$absoluteOutput = Join-Path $transferDirectory "idp-images.tar"
$metadataPath = "$absoluteOutput.json"
$completionPath = Join-Path $transferDirectory "EXPORT-COMPLETE.txt"
$appImage = "local/idp-app:latest"
$vllmVlImage = "local/vllm-vl:latest"
$vllmLlmImage = "local/vllm-llm:latest"
$vllmWinVlImage = "local/vllm-win-vl:latest"
$vllmWinLlmImage = "local/vllm-win-llm:latest"
$pythonImage = "python:3.12-slim"
$vllmImage = "vllm/vllm-openai:v0.10.2"

# Windows E2E model configurations
$winVlModel = "Qwen/Qwen3-VL-2B-Instruct"
$winLlmModel = "Qwen/Qwen3.5-0.8B"

# Linux models are expected to be already in transfer/models/
$linuxVlDir = Join-Path $transferDirectory "models\vl"
$linuxLlmDir = Join-Path $transferDirectory "models\llm"

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
    $linuxVlDir = Join-Path $projectRoot "infra\dockerfiles\vllm-vl"
    $linuxLlmDir = Join-Path $projectRoot "infra\dockerfiles\vllm-llm"
    Invoke-Docker build --pull=false --tag $vllmVlImage $linuxVlDir
    Invoke-Docker build --pull=false --tag $vllmLlmImage $linuxLlmDir

    Write-Host "[4/5] Building Windows E2E images with baked-in small models..."
    $winVlDir = Join-Path $projectRoot "infra\dockerfiles\vllm-win-vl"
    $winLlmDir = Join-Path $projectRoot "infra\dockerfiles\vllm-win-llm"
    $hfTokenBuildArgs = @()
    if ($env:HF_TOKEN) {
        $hfTokenBuildArgs = @("--build-arg", "HF_TOKEN=$env:HF_TOKEN")
    }
    Invoke-Docker build --pull=false @hfTokenBuildArgs --tag $vllmWinVlImage $winVlDir
    Invoke-Docker build --pull=false @hfTokenBuildArgs --tag $vllmWinLlmImage $winLlmDir

    Write-Host "[5/5] Saving images..."
    $images = @($appImage, $vllmVlImage, $vllmLlmImage, $vllmWinVlImage, $vllmWinLlmImage)
    Invoke-Docker save "--output=$absoluteOutput" @images

    Write-Host ""
    Write-Host "=== EXPORT COMPLETE ===" -ForegroundColor Green
    Write-Host "Archive: $absoluteOutput"
    Write-Host "Metadata: $metadataPath"
    Write-Host "Completion marker: $completionPath"
    Write-Host ""
    Write-Host "Linux models should be manually placed in:"
    Write-Host "  VL: $linuxVlDir"
    Write-Host "  LLM: $linuxLlmDir"
} finally {}
