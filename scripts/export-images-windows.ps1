<#
.SYNOPSIS
Builds, tests, and exports every Docker image needed on Linux.

.DESCRIPTION
Run from Windows 11 with Docker Desktop configured for Linux containers. The script:
  1. Builds local/idp-app from this repository using the local wheelhouse.
  2. Runs unit tests inside that image.
  3. Starts PostgreSQL and MinIO through Compose and runs an integration health check.
  4. Saves the application, database, object storage, and model images to one .tar archive.

The archive deliberately does not include source code, PDF documents, models, or local
MinerU/OCR tools. Those are mounted from ordinary directories on Linux.
#>

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot ".."))
$transferDirectory = Join-Path $projectRoot "transfer"
$absoluteOutput = Join-Path $transferDirectory "idp-images.tar"
$metadataPath = "$absoluteOutput.json"
$completionPath = Join-Path $transferDirectory "EXPORT-COMPLETE.txt"
$appImage = "local/idp-app:latest"
$pythonImage = "python:3.12-slim"
$qwenVlImage = "local/qwen-vl:latest"
$qwen3Image = "local/qwen3:latest"
$postgresImage = "postgres:16.9-alpine"
$minioImage = "minio/minio:RELEASE.2025-04-22T22-12-26Z"
$minioMcImage = "minio/mc:RELEASE.2025-05-21T01-59-54Z"
$vllmImage = "vllm/vllm-openai:v0.10.2"
$pipelineProfileVersion = Get-Date -Format "yyyyMMdd.HHmmss"
$composeFile = Join-Path $projectRoot "infra\compose\local.yml"
$smokeData = Join-Path $projectRoot ".smoke-runtime"
$smokeInput = Join-Path $smokeData "input"
$smokeTools = Join-Path $smokeData "tools"
$wheelsDirectory = Join-Path $projectRoot "wheels"
$modelsDirectory = Join-Path $transferDirectory "models"
$smokeProject = "idp-smoke-$PID"
$savedEnvironment = @{}
$smokeStarted = $false

function Invoke-Docker {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)
    & docker @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "docker $($Arguments -join ' ') failed with exit code $LASTEXITCODE."
    }
}

function Test-ModelSnapshot {
    param([string]$Target)
    if (-not (Test-Path (Join-Path $Target "config.json"))) { return $false }
    $index = Join-Path $Target "model.safetensors.index.json"
    if (Test-Path $index) {
        try {
            $payload = Get-Content -Raw -Path $index | ConvertFrom-Json
            $weightFiles = @($payload.weight_map.PSObject.Properties.Value | Sort-Object -Unique)
        } catch { return $false }
    } else {
        $weightFiles = @(Get-ChildItem -Path $Target -Filter "*.safetensors" -File | ForEach-Object Name)
    }
    if ($weightFiles.Count -eq 0) { return $false }
    foreach ($weightFile in $weightFiles) {
        $file = Join-Path $Target $weightFile
        if (-not (Test-Path $file) -or (Get-Item $file).Length -eq 0) { return $false }
    }
    return $true
}

function Install-ModelSnapshot {
    param([string]$Repository, [string]$DirectoryName)
    $target = Join-Path $modelsDirectory $DirectoryName
    $marker = Join-Path $target ".download-complete"
    $repositoryMarker = Join-Path $target ".repository"
    $storedRepository = if (Test-Path $repositoryMarker) { (Get-Content -Raw $repositoryMarker).Trim() } else { "" }
    if ($storedRepository -and $storedRepository -ne $Repository) { Remove-Item -Recurse -Force $target }
    if ((Test-Path $marker) -and $storedRepository -eq $Repository -and (Test-ModelSnapshot $target)) {
        Write-Host "Model already downloaded: $Repository"
        return
    }
    if (-not $storedRepository -and (Test-Path $target)) { Remove-Item -Recurse -Force $target }
    New-Item -ItemType Directory -Force -Path $target | Out-Null
    $Repository | Set-Content -NoNewline -Encoding ascii -Path $repositoryMarker
    Remove-Item -Force $marker -ErrorAction SilentlyContinue
    $lockDirectory = Join-Path $target ".cache\huggingface\download"
    if (Test-Path $lockDirectory) {
        Get-ChildItem -Path $lockDirectory -Filter "*.lock" -File -Recurse | Remove-Item -Force
    }
    Invoke-Docker run --rm --env HF_HUB_OFFLINE=0 --env TRANSFORMERS_OFFLINE=0 --env HF_HUB_DISABLE_XET=1 --env HF_HUB_DOWNLOAD_TIMEOUT=120 --env HF_HUB_ETAG_TIMEOUT=30 --mount "type=bind,source=$modelsDirectory,target=/models" --entrypoint hf $appImage download $Repository --local-dir "/models/$DirectoryName" --max-workers 1
    if (-not (Test-ModelSnapshot $target)) { throw "Downloaded model is incomplete: $Repository" }
    New-Item -ItemType File -Force -Path $marker | Out-Null
}

function Test-ModelChecksums {
    $checksumPath = Join-Path $modelsDirectory "SHA256SUMS"
    if (-not (Test-Path $checksumPath)) { return $false }
    docker run --rm --mount "type=bind,source=$modelsDirectory,target=/models,readonly" --workdir /models --entrypoint sha256sum $pythonImage --check SHA256SUMS *> $null
    return $LASTEXITCODE -eq 0
}

function Write-ModelChecksums {
    $checksumPath = Join-Path $modelsDirectory "SHA256SUMS"
    $files = Get-ChildItem -Path $modelsDirectory -File -Recurse | Where-Object FullName -ne $checksumPath
    $basePath = $modelsDirectory.TrimEnd("\", "/") + [System.IO.Path]::DirectorySeparatorChar
    $lines = foreach ($file in $files) {
        $relative = $file.FullName.Substring($basePath.Length).Replace("\", "/")
        $hash = (Get-FileHash -Algorithm SHA256 -Path $file.FullName).Hash.ToLowerInvariant()
        "$hash  $relative"
    }
    [System.IO.File]::WriteAllText($checksumPath, (($lines -join "`n") + "`n"), [System.Text.Encoding]::ASCII)
}

New-Item -ItemType Directory -Force -Path $transferDirectory | Out-Null
Remove-Item -Force $completionPath -ErrorAction SilentlyContinue

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "Docker Desktop CLI was not found in PATH."
}

try {
    Write-Host "[1/8] Checking Docker Desktop and downloading runtime images..."
    Invoke-Docker version
    Invoke-Docker pull $pythonImage
    Invoke-Docker pull $postgresImage
    Invoke-Docker pull $minioImage
    Invoke-Docker pull $minioMcImage
    Invoke-Docker pull $vllmImage
    Invoke-Docker tag $vllmImage $qwenVlImage
    Invoke-Docker tag $vllmImage $qwen3Image
    Write-Host "[2/8] Preparing Python wheels and building the application image..."
    New-Item -ItemType Directory -Force -Path $wheelsDirectory | Out-Null
    Invoke-Docker run --rm --mount "type=bind,source=$projectRoot,target=/workspace" --workdir /workspace --entrypoint python $pythonImage -m pip download --dest /workspace/wheels --only-binary=:all: ".[dev]" hatchling
    Invoke-Docker build --pull=false --tag $appImage $projectRoot
    Write-Host "[3/8] Running unit tests..."
    Invoke-Docker run --rm --entrypoint pytest $appImage tests/unit

    Write-Host "[4/8] Checking and downloading mounted AWQ model snapshots..."
    New-Item -ItemType Directory -Force -Path $modelsDirectory | Out-Null
    if ((Test-Path (Join-Path $modelsDirectory "SHA256SUMS")) -and -not (Test-ModelChecksums)) {
        Write-Warning "Stored model checksums failed; model snapshots will be downloaded again."
        Remove-Item -Force (Join-Path $modelsDirectory "SHA256SUMS") -ErrorAction SilentlyContinue
        Remove-Item -Recurse -Force (Join-Path $modelsDirectory "qwen-vl") -ErrorAction SilentlyContinue
        Remove-Item -Recurse -Force (Join-Path $modelsDirectory "qwen3") -ErrorAction SilentlyContinue
    }
    Install-ModelSnapshot "Qwen/Qwen2.5-VL-32B-Instruct-AWQ" "qwen-vl"
    Install-ModelSnapshot "Qwen/Qwen3-14B-AWQ" "qwen3"
    Write-Host "Computing SHA-256 checksums for mounted model files..."
    Write-ModelChecksums

    New-Item -ItemType Directory -Force -Path $smokeInput, $smokeTools | Out-Null
    $smokeEnvironment = @{
        IDP_SOURCE_ROOT = $projectRoot
        IDP_INPUT_ROOT = $smokeInput
        IDP_DATA_ROOT = $smokeData
        IDP_TOOLS_ROOT = $smokeTools
        IDP_APP_IMAGE = $appImage
        IDP_PIPELINE_PROFILE_VERSION = "windows-smoke"
        IDP_MINIO_PORT = "19000"
        IDP_MINIO_CONSOLE_PORT = "19001"
        IDP_CONTROLLER_METRICS_PORT = "19100"
        IDP_WORKER_METRICS_PORT = "19101"
    }
    foreach ($key in $smokeEnvironment.Keys) {
        $savedEnvironment[$key] = [Environment]::GetEnvironmentVariable($key, "Process")
        [Environment]::SetEnvironmentVariable($key, $smokeEnvironment[$key], "Process")
    }

    $smokeStarted = $true
    Write-Host "[5/8] Starting the isolated smoke stack..."
    Invoke-Docker compose --project-name $smokeProject -f $composeFile up -d postgres minio minio-init migrate profiles controller
    Write-Host "[6/8] Running health checks and PostgreSQL integration tests..."
    Invoke-Docker compose --project-name $smokeProject -f $composeFile run --rm operator
    Invoke-Docker compose --project-name $smokeProject -f $composeFile run --rm -e "IDP_TEST_POSTGRES_URL=postgresql+psycopg://idp:idp@postgres:5432/idp" operator pytest
    Write-Host "[7/8] Stopping and removing the smoke stack..."
    Invoke-Docker compose --project-name $smokeProject -f $composeFile down --remove-orphans --timeout 30
    $smokeStarted = $false

    $images = @($appImage, $postgresImage, $minioImage, $minioMcImage, $qwenVlImage, $qwen3Image)
    foreach ($image in $images) {
        Invoke-Docker image inspect $image
    }

    Write-Host "[8/8] Exporting Docker images and writing checksums..."
    Invoke-Docker save --output $absoluteOutput $images

    $hash = Get-FileHash -Algorithm SHA256 -Path $absoluteOutput
    $hashLine = "{0}  {1}" -f $hash.Hash.ToLowerInvariant(), [System.IO.Path]::GetFileName($absoluteOutput)
    $hashLine | Set-Content -NoNewline -Encoding ascii -Path "$absoluteOutput.sha256"
    $metadata = @{
        app_image = $appImage
        postgres_image = $postgresImage
        minio_image = $minioImage
        minio_mc_image = $minioMcImage
        qwen_vl_image = $qwenVlImage
        qwen3_image = $qwen3Image
        pipeline_profile_version = $pipelineProfileVersion
    } | ConvertTo-Json
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($metadataPath, $metadata, $utf8NoBom)
    "Completed: $(Get-Date -Format o)" | Set-Content -Encoding ascii -Path $completionPath
    Write-Host ""
    Write-Host "=== IDP EXPORT COMPLETE ===" -ForegroundColor Green
    Write-Host "Archive: $absoluteOutput"
    Write-Host "Checksum: $absoluteOutput.sha256"
    Write-Host "Metadata: $metadataPath"
    Write-Host "Models: $modelsDirectory"
    Write-Host "Completion marker: $completionPath"
}
finally {
    if ($smokeStarted -and (Test-Path $composeFile)) {
        docker compose --project-name $smokeProject -f $composeFile down --remove-orphans --timeout 30 2>$null | Out-Null
    }
    foreach ($key in $savedEnvironment.Keys) {
        [Environment]::SetEnvironmentVariable($key, $savedEnvironment[$key], "Process")
    }
    Remove-Item -Recurse -Force $smokeData -ErrorAction SilentlyContinue
}
