<#
.SYNOPSIS
Exports the Docker images needed by the Compose stack to one portable archive.

.DESCRIPTION
Run on Windows 11 after the two local model images are present in Docker Desktop.
The archive can be copied to Linux and loaded by import-images-linux.sh. Source code,
models, OCR/MinerU tools, input PDFs, and runtime data are not embedded; they stay as
ordinary mounted directories.
#>

[CmdletBinding()]
param(
    [string]$OutputPath = ".\idp-images.tar",
    [string]$QwenVlImage = "local/qwen-vl:latest",
    [string]$Qwen3Image = "local/qwen3:latest",
    [string]$PostgresImage = "postgres:16.9-alpine",
    [string]$MinioImage = "minio/minio:RELEASE.2025-04-22T22-12-26Z",
    [string]$MinioMcImage = "minio/mc:RELEASE.2025-05-21T01-59-54Z",
    [string]$AppImage = "python:3.12-slim"
)

$ErrorActionPreference = "Stop"

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "Docker Desktop CLI was not found in PATH."
}

$images = @(
    $AppImage,
    $PostgresImage,
    $MinioImage,
    $MinioMcImage,
    $QwenVlImage,
    $Qwen3Image
)

foreach ($image in $images) {
    docker image inspect $image *> $null
    if ($LASTEXITCODE -ne 0) {
        throw "Docker image is missing: $image"
    }
}

$absoluteOutput = [System.IO.Path]::GetFullPath($OutputPath)
$parent = Split-Path -Parent $absoluteOutput
New-Item -ItemType Directory -Force -Path $parent | Out-Null

docker save --output $absoluteOutput $images
if ($LASTEXITCODE -ne 0) {
    throw "docker save failed."
}

$hash = Get-FileHash -Algorithm SHA256 -Path $absoluteOutput
$hashLine = "{0}  {1}" -f $hash.Hash.ToLowerInvariant(), [System.IO.Path]::GetFileName($absoluteOutput)
$hashLine | Set-Content -NoNewline -Encoding ascii -Path "$absoluteOutput.sha256"

Write-Host "Exported $($images.Count) images to $absoluteOutput"
Write-Host "SHA-256 saved to $absoluteOutput.sha256"
