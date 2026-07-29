<#
.SYNOPSIS
Export script: builds worker image, downloads models, builds vLLM images, saves to tar.
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

# Model configurations
$winVlModel = "Qwen/Qwen3-VL-2B-Instruct"
$winLlmModel = "Qwen/Qwen3.5-0.8B"
$linuxVlModel = "Qwen/Qwen2.5-VL-32B-Instruct"
$linuxLlmModel = "Qwen/Qwen3-14B-Instruct"

function Invoke-Docker {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)
    & docker @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "docker $($Arguments -join ' ') failed with exit code $LASTEXITCODE."
    }
}

function Invoke-Python {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)
    & python @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "python $($Arguments -join ' ') failed with exit code $LASTEXITCODE."
    }
}

New-Item -ItemType Directory -Force -Path $transferDirectory | Out-Null
Remove-Item -Force $completionPath -ErrorAction SilentlyContinue

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "Docker Desktop CLI was not found in PATH."
}

try {
    Write-Host "[1/7] Pulling base images..."
    Invoke-Docker pull $pythonImage
    Invoke-Docker pull $vllmImage

    Write-Host "[2/7] Building worker image..."
    Invoke-Docker build --pull=false --tag $appImage $projectRoot

    Write-Host "[3/7] Preparing Linux vLLM images..."
    Invoke-Docker tag $vllmImage $vllmVlImage
    Invoke-Docker tag $vllmImage $vllmLlmImage

    Write-Host "[4/7] Downloading Linux models to transfer/models/..."
    $linuxVlDir = Join-Path $transferDirectory "models\vl"
    $linuxLlmDir = Join-Path $transferDirectory "models\llm"
    New-Item -ItemType Directory -Force -Path $linuxVlDir | Out-Null
    New-Item -ItemType Directory -Force -Path $linuxLlmDir | Out-Null
    
    $hfTokenArgs = @()
    if ($env:HF_TOKEN) {
        $hfTokenArgs = @("--token", $env:HF_TOKEN)
    }
    
    $downloadScript = Join-Path $transferDirectory "download_models.py"
    $downloadContent = @"
from huggingface_hub import snapshot_download
import sys
model = sys.argv[1]
out = sys.argv[2]
token = sys.argv[3] if len(sys.argv) > 3 else None
snapshot_download(model, local_dir=out, local_dir_use_symlinks=False, token=token)
print(f"Downloaded {model} to {out}")
"@
    Set-Content -Path $downloadScript -Value $downloadContent -Encoding ascii
    
    $tokenArg = if ($env:HF_TOKEN) { $env:HF_TOKEN } else { "" }
    Invoke-Python $downloadScript $linuxVlModel $linuxVlDir $tokenArg
    Invoke-Python $downloadScript $linuxLlmModel $linuxLlmDir $tokenArg
    Remove-Item $downloadScript -ErrorAction SilentlyContinue

    Write-Host "[5/7] Building Windows E2E images with baked-in small models..."
    $winVlDir = Join-Path $projectRoot "infra\dockerfiles\vllm-win-vl"
    $winLlmDir = Join-Path $projectRoot "infra\dockerfiles\vllm-win-llm"
    $hfTokenBuildArgs = @()
    if ($env:HF_TOKEN) {
        $hfTokenBuildArgs = @("--build-arg", "HF_TOKEN=$env:HF_TOKEN")
    }
    Invoke-Docker build --pull=false @hfTokenBuildArgs --tag $vllmWinVlImage $winVlDir
    Invoke-Docker build --pull=false @hfTokenBuildArgs --tag $vllmWinLlmImage $winLlmDir

    Write-Host "[6/7] Saving images..."
    $images = @($appImage, $vllmVlImage, $vllmLlmImage, $vllmWinVlImage, $vllmWinLlmImage)
    Invoke-Docker save "--output=$absoluteOutput" @images

    Write-Host "[7/7] Writing metadata..."
    $metadata = @{
        app_image = $appImage
        vllm_vl_image = $vllmVlImage
        vllm_llm_image = $vllmLlmImage
        vllm_win_vl_image = $vllmWinVlImage
        vllm_win_llm_image = $vllmWinLlmImage
        win_vl_model = $winVlModel
        win_llm_model = $winLlmModel
        linux_vl_model = $linuxVlModel
        linux_llm_model = $linuxLlmModel
    } | ConvertTo-Json
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($metadataPath, $metadata, $utf8NoBom)
    "Completed: $(Get-Date -Format o)" | Set-Content -Encoding ascii -Path $completionPath
    Write-Host ""
    Write-Host "=== EXPORT COMPLETE ===" -ForegroundColor Green
    Write-Host "Archive: $absoluteOutput"
    Write-Host "Metadata: $metadataPath"
    Write-Host "Completion marker: $completionPath"
    Write-Host "Linux models downloaded to: $transferDirectory\models"
} finally {}
