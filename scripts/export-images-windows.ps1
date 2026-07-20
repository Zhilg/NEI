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
$appImage = "local/idp-app:latest"
$pythonImage = "python:3.12-slim"
$qwenVlImage = "local/qwen-vl:latest"
$qwen3Image = "local/qwen3:latest"
$postgresImage = "postgres:16.9-alpine"
$minioImage = "minio/minio:RELEASE.2025-04-22T22-12-26Z"
$minioMcImage = "minio/mc:RELEASE.2025-05-21T01-59-54Z"
$pipelineProfileVersion = Get-Date -Format "yyyyMMdd.HHmmss"
$composeFile = Join-Path $projectRoot "infra\compose\local.yml"
$smokeData = Join-Path $projectRoot ".smoke-runtime"
$smokeInput = Join-Path $smokeData "input"
$smokeTools = Join-Path $smokeData "tools"
$wheelsDirectory = Join-Path $projectRoot "wheels"
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

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "Docker Desktop CLI was not found in PATH."
}

try {
    Invoke-Docker version
    Invoke-Docker pull $pythonImage
    Invoke-Docker pull $postgresImage
    Invoke-Docker pull $minioImage
    Invoke-Docker pull $minioMcImage
    New-Item -ItemType Directory -Force -Path $wheelsDirectory | Out-Null
    Invoke-Docker run --rm --volume "${projectRoot}:/workspace" --workdir /workspace $pythonImage /bin/sh -ec "python -m pip download --dest /workspace/wheels --only-binary=:all: '.[dev]' hatchling"
    Invoke-Docker build --pull=false --tag $appImage $projectRoot
    Invoke-Docker run --rm --entrypoint pytest $appImage tests/unit

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

    Invoke-Docker compose --project-name $smokeProject -f $composeFile up -d postgres minio minio-init migrate profiles controller
    $smokeStarted = $true
    Invoke-Docker compose --project-name $smokeProject -f $composeFile run --rm operator
    Invoke-Docker compose --project-name $smokeProject -f $composeFile run --rm -e "IDP_TEST_POSTGRES_URL=postgresql+psycopg://idp:idp@postgres:5432/idp" operator pytest
    Invoke-Docker compose --project-name $smokeProject -f $composeFile down --remove-orphans
    $smokeStarted = $false

    $images = @($appImage, $postgresImage, $minioImage, $minioMcImage, $qwenVlImage, $qwen3Image)
    foreach ($image in $images) {
        Invoke-Docker image inspect $image
    }

    New-Item -ItemType Directory -Force -Path $transferDirectory | Out-Null
    Invoke-Docker save --output $absoluteOutput $images

    $hash = Get-FileHash -Algorithm SHA256 -Path $absoluteOutput
    $hashLine = "{0}  {1}" -f $hash.Hash.ToLowerInvariant(), [System.IO.Path]::GetFileName($absoluteOutput)
    $hashLine | Set-Content -NoNewline -Encoding ascii -Path "$absoluteOutput.sha256"
    @{
        app_image = $appImage
        postgres_image = $postgresImage
        minio_image = $minioImage
        minio_mc_image = $minioMcImage
        qwen_vl_image = $qwenVlImage
        qwen3_image = $qwen3Image
        pipeline_profile_version = $pipelineProfileVersion
    } | ConvertTo-Json | Set-Content -Encoding utf8 -Path $metadataPath
    Write-Host "Build, tests, smoke check, and image export completed."
    Write-Host "Archive: $absoluteOutput"
    Write-Host "Checksum: $absoluteOutput.sha256"
    Write-Host "Metadata: $metadataPath"
}
finally {
    if ($smokeStarted -and (Test-Path $composeFile)) {
        docker compose --project-name $smokeProject -f $composeFile down --remove-orphans 2>$null | Out-Null
    }
    foreach ($key in $savedEnvironment.Keys) {
        [Environment]::SetEnvironmentVariable($key, $savedEnvironment[$key], "Process")
    }
    Remove-Item -Recurse -Force $smokeData -ErrorAction SilentlyContinue
}
