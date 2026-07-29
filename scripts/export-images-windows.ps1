<#
.SYNOPSIS
Export script: builds worker image and Windows E2E images, saves to tar.
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
    Write-Host "[1/6] Pulling base images..."
    Invoke-Docker pull $pythonImage
    Invoke-Docker pull $vllmImage

    Write-Host "[2/6] Building worker image..."
    Invoke-Docker build --pull=false --tag $appImage $projectRoot

    Write-Host "[3/6] Preparing Linux vLLM images..."
    Invoke-Docker tag $vllmImage $vllmVlImage
    Invoke-Docker tag $vllmImage $vllmLlmImage

    Write-Host "[4/6] Building Windows E2E images with baked-in small models..."
    $winVlDir = Join-Path $projectRoot "infra\dockerfiles\vllm-win-vl"
    $winLlmDir = Join-Path $projectRoot "infra\dockerfiles\vllm-win-llm"
    $hfToken = if ($env:HF_TOKEN) { "--build-arg HF_TOKEN=$env:HF_TOKEN" } else { "" }
    Invoke-Docker build --pull=false $hfToken --tag $vllmWinVlImage $winVlDir
    Invoke-Docker build --pull=false $hfToken --tag $vllmWinLlmImage $winLlmDir

    Write-Host "[5/6] Saving images..."
    $images = @($appImage, $vllmVlImage, $vllmLlmImage, $vllmWinVlImage, $vllmWinLlmImage)
    Invoke-Docker save "--output=$absoluteOutput" @images

    Write-Host "[6/6] Writing metadata..."
    $metadata = @{
        app_image = $appImage
        vllm_vl_image = $vllmVlImage
        vllm_llm_image = $vllmLlmImage
        vllm_win_vl_image = $vllmWinVlImage
        vllm_win_llm_image = $vllmWinLlmImage
    } | ConvertTo-Json
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($metadataPath, $metadata, $utf8NoBom)
    "Completed: $(Get-Date -Format o)" | Set-Content -Encoding ascii -Path $completionPath
    Write-Host ""
    Write-Host "=== EXPORT COMPLETE ===" -ForegroundColor Green
    Write-Host "Archive: $absoluteOutput"
    Write-Host "Metadata: $metadataPath"
    Write-Host "Completion marker: $completionPath"
} finally {}
