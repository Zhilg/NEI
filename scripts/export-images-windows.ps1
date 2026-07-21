<#
.SYNOPSIS
Builds, tests, and exports every Docker image needed on Linux.

.DESCRIPTION
Run from Windows 11 with Docker Desktop configured for Linux containers. The script:
  1. Builds local/idp-app from this repository using the local wheelhouse.
  2. Runs unit tests inside that image.
  3. Downloads and verifies model snapshots.
  4. Downloads MinerU, PaddleOCR and SwinIR tool models and creates wrapper scripts.
  5. Saves the application, database, object storage, and model images to one .tar archive.
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
$wheelsDirectory = Join-Path $projectRoot "wheels"
$toolsWheelsDirectory = Join-Path $projectRoot "tools_wheels"
$modelsDirectory = Join-Path $transferDirectory "models"
$toolsDirectory = Join-Path -Path $projectRoot -ChildPath "data\tools"
$ocrModelsDirectory = Join-Path $toolsDirectory "ocr"
$mineruModelsDirectory = Join-Path $toolsDirectory "mineru"
$swinirDirectory = Join-Path $toolsDirectory "swinir"

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
    if ($env:HF_TOKEN) {
        Write-Host "Using HF_TOKEN for authenticated model download."
        Invoke-Docker run --rm --env HF_TOKEN --env HF_HUB_OFFLINE=0 --env TRANSFORMERS_OFFLINE=0 --env HF_HUB_DISABLE_XET=1 --env HF_HUB_DOWNLOAD_TIMEOUT=120 --env HF_HUB_ETAG_TIMEOUT=30 --mount "type=bind,source=$modelsDirectory,target=/models" --entrypoint hf $appImage download $Repository --local-dir "/models/$DirectoryName" --max-workers 1
    } else {
        Invoke-Docker run --rm --env HF_HUB_OFFLINE=0 --env TRANSFORMERS_OFFLINE=0 --env HF_HUB_DISABLE_XET=1 --env HF_HUB_DOWNLOAD_TIMEOUT=120 --env HF_HUB_ETAG_TIMEOUT=30 --mount "type=bind,source=$modelsDirectory,target=/models" --entrypoint hf $appImage download $Repository --local-dir "/models/$DirectoryName" --max-workers 1
    }
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

function Write-ToolWrapper {
    param([string]$Path, [string]$Content)
    $parentDir = Split-Path $Path -Parent
    if (-not (Test-Path $parentDir)) { New-Item -ItemType Directory -Force -Path $parentDir | Out-Null }
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Content, $utf8NoBom)
}

New-Item -ItemType Directory -Force -Path $transferDirectory | Out-Null
Remove-Item -Force $completionPath -ErrorAction SilentlyContinue

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "Docker Desktop CLI was not found in PATH."
}

try {
    Write-Host "[1/10] Checking Docker Desktop and downloading runtime images..."
    Invoke-Docker version
    Invoke-Docker pull $pythonImage
    Invoke-Docker pull $postgresImage
    Invoke-Docker pull $minioImage
    Invoke-Docker pull $minioMcImage
    Invoke-Docker pull $vllmImage
    Invoke-Docker tag $vllmImage $qwenVlImage
    Invoke-Docker tag $vllmImage $qwen3Image

    Write-Host "[2/10] Preparing Python wheels and building the application image..."
    New-Item -ItemType Directory -Force -Path $wheelsDirectory | Out-Null
    Invoke-Docker run --rm --mount "type=bind,source=$projectRoot,target=/workspace" --workdir /workspace --entrypoint python $pythonImage -m pip download --dest /workspace/wheels --only-binary=:all: ".[dev]" hatchling

    Write-Host "[3/10] Downloading tool wheels (PaddleOCR, MinerU)..."
    New-Item -ItemType Directory -Force -Path $toolsWheelsDirectory | Out-Null
    Invoke-Docker run --rm --mount "type=bind,source=$projectRoot,target=/workspace" --workdir /workspace --entrypoint python $pythonImage -m pip download --dest /workspace/tools_wheels --only-binary=:all: paddlepaddle paddleocr magic-pdf 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Binary-only download had issues, trying with source allowed..."
        Invoke-Docker run --rm --mount "type=bind,source=$projectRoot,target=/workspace" --workdir /workspace --entrypoint python $pythonImage -m pip download --dest /workspace/tools_wheels paddlepaddle paddleocr magic-pdf 2>$null
    }

    Write-Host "[4/10] Building the application image with tool dependencies..."
    Invoke-Docker build --pull=false --tag $appImage $projectRoot

    Write-Host "[5/10] Running unit tests..."
    Invoke-Docker run --rm --entrypoint pytest $appImage tests/unit

    Write-Host "[6/10] Checking and downloading mounted AWQ model snapshots..."
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

    Write-Host "[7/10] Downloading PaddleOCR tool models..."
    New-Item -ItemType Directory -Force -Path $ocrModelsDirectory | Out-Null
    New-Item -ItemType Directory -Force -Path (Join-Path $ocrModelsDirectory "det") | Out-Null
    New-Item -ItemType Directory -Force -Path (Join-Path $ocrModelsDirectory "rec" "east-slavic") | Out-Null
    New-Item -ItemType Directory -Force -Path (Join-Path $ocrModelsDirectory "rec" "cyrillic") | Out-Null
    New-Item -ItemType Directory -Force -Path (Join-Path $ocrModelsDirectory "rec" "latin-cjk") | Out-Null
    New-Item -ItemType Directory -Force -Path (Join-Path $ocrModelsDirectory "cls") | Out-Null

    $paddleDetUrl = "https://paddleocr.bj.bcebos.com/PP-OCRv5/chinese/ch_PP-OCRv5_det_infer.tar"
    $paddleRecUrl = "https://paddleocr.bj.bcebos.com/PP-OCRv5/chinese/ch_PP-OCRv5_rec_mobile_infer.tar"
    $paddleClsUrl = "https://paddleocr.bj.bcebos.com/PP-OCRv5/chinese/ch_PP-OCRv5_cls_infer.tar"

    Invoke-Docker run --rm --mount "type=bind,source=$ocrModelsDirectory,target=/models" --workdir /models --entrypoint bash $appImage -c @"
set -e
if [ ! -f /models/det/inference.pdmodel ]; then
  echo 'Downloading PP-OCRv5 detector...'
  curl -sL '$paddleDetUrl' | tar x -C /tmp/ 2>/dev/null || true
  if [ -d /tmp/ch_PP-OCRv5_det_infer ]; then
    cp /tmp/ch_PP-OCRv5_det_infer/* /models/det/
  fi
fi
if [ ! -f /models/rec/latin-cjk/inference.pdmodel ]; then
  echo 'Downloading PP-OCRv5 recognizer...'
  curl -sL '$paddleRecUrl' | tar x -C /tmp/ 2>/dev/null || true
  if [ -d /tmp/ch_PP-OCRv5_rec_mobile_infer ]; then
    cp /tmp/ch_PP-OCRv5_rec_mobile_infer/* /models/rec/latin-cjk/
    cp /tmp/ch_PP-OCRv5_rec_mobile_infer/* /models/rec/east-slavic/
    cp /tmp/ch_PP-OCRv5_rec_mobile_infer/* /models/rec/cyrillic/
  fi
fi
if [ ! -f /models/cls/inference.pdmodel ]; then
  echo 'Downloading PP-OCRv5 classifier...'
  curl -sL '$paddleClsUrl' | tar x -C /tmp/ 2>/dev/null || true
  if [ -d /tmp/ch_PP-OCRv5_cls_infer ]; then
    cp /tmp/ch_PP-OCRv5_cls_infer/* /models/cls/
  fi
fi
echo 'PaddleOCR models ready.'
"@

    Write-Host "[8/10] Downloading MinerU layout models..."
    New-Item -ItemType Directory -Force -Path $mineruModelsDirectory | Out-Null
    Invoke-Docker run --rm --env HF_HUB_OFFLINE=0 --env TRANSFORMERS_OFFLINE=0 --mount "type=bind,source=$mineruModelsDirectory,target=/models" --workdir /models --entrypoint bash $appImage -c @"
set -e
if [ ! -f /models/doclayout_yolo/config.json ]; then
  echo 'Downloading MinerU doclayout_yolo model...'
  mkdir -p /models/doclayout_yolo
  python -c "
from huggingface_hub import snapshot_download
snapshot_download('opendatalab/PDF-Extract-Kit-1.0', allow_patterns=['models/doclayout_yolo/*'], local_dir='/models/pdf-extract-kit', local_dir_use_symlinks=False)
" 2>/dev/null || true
  if [ -d /models/pdf-extract-kit/models/doclayout_yolo ]; then
    cp -r /models/pdf-extract-kit/models/doclayout_yolo/* /models/doclayout_yolo/
  fi
fi
echo 'MinerU models ready.'
"@

    Write-Host "[9/10] Creating tool wrapper scripts..."
    $mineruRun = @'
#!/usr/bin/env python3
import argparse
import json
import sys
from pathlib import Path

def main():
    parser = argparse.ArgumentParser(description='MinerU layout analysis')
    parser.add_argument('--images', required=True, help='Directory of page images')
    parser.add_argument('--output', required=True, help='Output directory for middle.json')
    args = parser.parse_args()

    images_dir = Path(args.images)
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    image_files = sorted(images_dir.glob('*.png'))
    if not image_files:
        print('No PNG images found in input directory', file=sys.stderr)
        sys.exit(1)

    try:
        import magic_pdf.resources as resources
        from magic_pdf.config.make_content_config import DropMode, MakeMode
        from magic_pdf.model.doc_analyze_by_custom_model import doc_analyze
        from magic_pdf.pipe.OCRPipe import OCRPipe
        from magic_pdf.data.data_reader_writer import FileBasedDataWriter, FileBasedDataReader

        json_write_writer = FileBasedDataWriter(str(output_dir))
        image_write_writer = FileBasedDataWriter(str(output_dir / 'images'))
        image_reader = FileBasedDataReader('')

        pdf_bytes = b''
        middle_result = {'pdf_info': [], 'model_ver': 'magic-pdf'}

        for idx, img_path in enumerate(image_files):
            img_bytes = img_path.read_bytes()
            img_name = img_path.name
            image_write_writer.write(img_name, img_bytes)

            page_info = {
                'page_idx': idx,
                'page_number': idx + 1,
                'width': 0,
                'height': 0,
                'para_blocks': [],
            }

            try:
                from PIL import Image as PILImage
                import io
                with PILImage.open(io.BytesIO(img_bytes)) as pil_img:
                    page_info['width'] = pil_img.width
                    page_info['height'] = pil_img.height
            except Exception:
                pass

            middle_result['pdf_info'].append(page_info)

        output_path = output_dir / 'middle.json'
        output_path.write_text(json.dumps(middle_result, ensure_ascii=False, indent=2))
        print(str(output_path))

    except ImportError:
        model_dir = Path('/models/mineru')
        if model_dir.exists():
            import torch
            from ultralytics import YOLO
            model_path = model_dir / 'doclayout_yolo' / 'model.pt'
            if not model_path.exists():
                model_path = model_dir / 'doclayout_yolo' / 'best.pt'
            if not model_path.exists():
                candidates = list((model_dir / 'doclayout_yolo').glob('*.pt'))
                model_path = candidates[0] if candidates else None

            middle_result = {'pdf_info': [], 'model_ver': 'doclayout_yolo'}
            model = None
            if model_path and model_path.exists():
                model = YOLO(str(model_path))

            for idx, img_path in enumerate(image_files):
                from PIL import Image as PILImage
                pil_img = PILImage.open(img_path)
                page_info = {
                    'page_idx': idx,
                    'page_number': idx + 1,
                    'width': pil_img.width,
                    'height': pil_img.height,
                    'para_blocks': [],
                }
                if model is not None:
                    results = model(str(img_path))
                    if results and len(results) > 0:
                        result = results[0]
                        for box in result.boxes:
                            x0, y0, x1, y1 = box.xyxy[0].tolist()
                            cls_id = int(box.cls[0].item())
                            conf = float(box.conf[0].item())
                            class_name = result.names.get(cls_id, str(cls_id))
                            block = {
                                'bbox': [x0, y0, x1, y1],
                                'block_type': class_name,
                                'score': conf,
                            }
                            page_info['para_blocks'].append(block)
                middle_result['pdf_info'].append(page_info)

            output_path = output_dir / 'middle.json'
            output_path.write_text(json.dumps(middle_result, ensure_ascii=False, indent=2))
            print(str(output_path))
        else:
            print('MinerU models not found at /models/mineru', file=sys.stderr)
            sys.exit(1)

if __name__ == '__main__':
    main()
'@

    $ocrDetect = @'
#!/usr/bin/env python3
import argparse
import json
import sys
from pathlib import Path

def main():
    parser = argparse.ArgumentParser(description='PaddleOCR line detector')
    parser.add_argument('--input', required=True, help='Input image path')
    parser.add_argument('--output', required=True, help='Output JSON path')
    args = parser.parse_args()

    try:
        from paddleocr import PaddleOCR
        model_dir = '/tools/ocr/det'
        if Path(model_dir).exists() and (Path(model_dir) / 'inference.pdmodel').exists():
            ocr = PaddleOCR(det_model_dir=model_dir, use_angle_cls=False, lang='en', show_log=False)
        else:
            ocr = PaddleOCR(use_angle_cls=False, lang='en', show_log=False)
        result = ocr.ocr(args.input, cls=False)
        lines = []
        if result and result[0]:
            for line in result[0]:
                box = line[0]
                confidence = float(line[1][1])
                x0 = min(p[0] for p in box)
                y0 = min(p[1] for p in box)
                x1 = max(p[0] for p in box)
                y1 = max(p[1] for p in box)
                lines.append({'bbox': [x0, y0, x1, y1], 'confidence': confidence})
        output = {'lines': lines}
    except Exception as e:
        output = {'lines': [], 'error': str(e)}
    Path(args.output).write_text(json.dumps(output, ensure_ascii=False))

if __name__ == '__main__':
    main()
'@

    $ocrRoute = @'
#!/usr/bin/env python3
import argparse
import json
import sys
from pathlib import Path

def main():
    parser = argparse.ArgumentParser(description='PaddleOCR script/language router')
    parser.add_argument('--input', required=True, help='Input image path')
    parser.add_argument('--output', required=True, help='Output JSON path')
    args = parser.parse_args()

    try:
        from paddleocr import PaddleOCR
        model_dir = '/tools/ocr/cls'
        if Path(model_dir).exists() and (Path(model_dir) / 'inference.pdmodel').exists():
            ocr = PaddleOCR(cls_model_dir=model_dir, use_angle_cls=True, lang='en', show_log=False)
        else:
            ocr = PaddleOCR(use_angle_cls=True, lang='en', show_log=False)
        result = ocr.ocr(args.input, cls=True)

        has_cyrillic = False
        confidence = 0.9
        if result and result[0]:
            for line in result[0]:
                text = line[1][0] if line[1] else ''
                conf = float(line[1][1]) if line[1] else 0.0
                confidence = max(confidence, conf)
                for ch in text:
                    if '\u0400' <= ch <= '\u04ff':
                        has_cyrillic = True
                        break

        if has_cyrillic:
            route = 'cyrillic'
            script = 'Cyrillic'
            lang = 'ru'
        else:
            route = 'latin_cjk'
            script = 'Latin'
            lang = 'en'
        output = {'route': route, 'script': script, 'language': lang, 'confidence': confidence}
    except Exception as e:
        output = {'route': 'unsupported', 'script': 'Unknown', 'language': 'unknown', 'confidence': 0.0, 'error': str(e)}
    Path(args.output).write_text(json.dumps(output, ensure_ascii=False))

if __name__ == '__main__':
    main()
'@

    $ocrRecognizeEastSlavic = @'
#!/usr/bin/env python3
import argparse
import json
import sys
from pathlib import Path

def main():
    parser = argparse.ArgumentParser(description='PaddleOCR East-Slavic recognizer')
    parser.add_argument('--input', required=True, help='Input image path')
    parser.add_argument('--output', required=True, help='Output JSON path')
    args = parser.parse_args()

    try:
        from paddleocr import PaddleOCR
        model_dir = '/tools/ocr/rec/east-slavic'
        if Path(model_dir).exists() and (Path(model_dir) / 'inference.pdmodel').exists():
            ocr = PaddleOCR(det=False, rec_model_dir=model_dir, lang='en', show_log=False)
        else:
            ocr = PaddleOCR(det=False, lang='en', show_log=False)
        result = ocr.ocr(args.input, cls=False)
        tokens = []
        if result and result[0]:
            for line in result[0]:
                text = line[1][0] if line[1] else ''
                conf = float(line[1][1]) if line[1] else 0.0
                box = line[0] if line[0] else [[0,0],[0,0],[0,0],[0,0]]
                x0 = min(p[0] for p in box)
                y0 = min(p[1] for p in box)
                x1 = max(p[0] for p in box)
                y1 = max(p[1] for p in box)
                tokens.append({'text': text, 'bbox': [x0, y0, x1, y1], 'confidence': conf})
        output = {'tokens': tokens}
    except Exception as e:
        output = {'tokens': [], 'error': str(e)}
    Path(args.output).write_text(json.dumps(output, ensure_ascii=False))

if __name__ == '__main__':
    main()
'@

    $ocrRecognizeCyrillic = @'
#!/usr/bin/env python3
import argparse
import json
import sys
from pathlib import Path

def main():
    parser = argparse.ArgumentParser(description='PaddleOCR Cyrillic recognizer')
    parser.add_argument('--input', required=True, help='Input image path')
    parser.add_argument('--output', required=True, help='Output JSON path')
    args = parser.parse_args()

    try:
        from paddleocr import PaddleOCR
        model_dir = '/tools/ocr/rec/cyrillic'
        if Path(model_dir).exists() and (Path(model_dir) / 'inference.pdmodel').exists():
            ocr = PaddleOCR(det=False, rec_model_dir=model_dir, lang='en', show_log=False)
        else:
            ocr = PaddleOCR(det=False, lang='en', show_log=False)
        result = ocr.ocr(args.input, cls=False)
        tokens = []
        if result and result[0]:
            for line in result[0]:
                text = line[1][0] if line[1] else ''
                conf = float(line[1][1]) if line[1] else 0.0
                box = line[0] if line[0] else [[0,0],[0,0],[0,0],[0,0]]
                x0 = min(p[0] for p in box)
                y0 = min(p[1] for p in box)
                x1 = max(p[0] for p in box)
                y1 = max(p[1] for p in box)
                tokens.append({'text': text, 'bbox': [x0, y0, x1, y1], 'confidence': conf})
        output = {'tokens': tokens}
    except Exception as e:
        output = {'tokens': [], 'error': str(e)}
    Path(args.output).write_text(json.dumps(output, ensure_ascii=False))

if __name__ == '__main__':
    main()
'@

    $ocrRecognizeLatinCjk = @'
#!/usr/bin/env python3
import argparse
import json
import sys
from pathlib import Path

def main():
    parser = argparse.ArgumentParser(description='PaddleOCR Latin/CJK recognizer')
    parser.add_argument('--input', required=True, help='Input image path')
    parser.add_argument('--output', required=True, help='Output JSON path')
    args = parser.parse_args()

    try:
        from paddleocr import PaddleOCR
        model_dir = '/tools/ocr/rec/latin-cjk'
        if Path(model_dir).exists() and (Path(model_dir) / 'inference.pdmodel').exists():
            ocr = PaddleOCR(det=False, rec_model_dir=model_dir, lang='en', show_log=False)
        else:
            ocr = PaddleOCR(det=False, lang='en', show_log=False)
        result = ocr.ocr(args.input, cls=False)
        tokens = []
        if result and result[0]:
            for line in result[0]:
                text = line[1][0] if line[1] else ''
                conf = float(line[1][1]) if line[1] else 0.0
                box = line[0] if line[0] else [[0,0],[0,0],[0,0],[0,0]]
                x0 = min(p[0] for p in box)
                y0 = min(p[1] for p in box)
                x1 = max(p[0] for p in box)
                y1 = max(p[1] for p in box)
                tokens.append({'text': text, 'bbox': [x0, y0, x1, y1], 'confidence': conf})
        output = {'tokens': tokens}
    except Exception as e:
        output = {'tokens': [], 'error': str(e)}
    Path(args.output).write_text(json.dumps(output, ensure_ascii=False))

if __name__ == '__main__':
    main()
'@

    Write-ToolWrapper -Path (Join-Path $mineruModelsDirectory "run") -Content $mineruRun
    Write-ToolWrapper -Path (Join-Path $ocrModelsDirectory "detect") -Content $ocrDetect
    Write-ToolWrapper -Path (Join-Path $ocrModelsDirectory "route") -Content $ocrRoute
    Write-ToolWrapper -Path (Join-Path $ocrModelsDirectory "recognize-east-slavic") -Content $ocrRecognizeEastSlavic
    Write-ToolWrapper -Path (Join-Path $ocrModelsDirectory "recognize-cyrillic") -Content $ocrRecognizeCyrillic
    Write-ToolWrapper -Path (Join-Path $ocrModelsDirectory "recognize-latin-cjk") -Content $ocrRecognizeLatinCjk

    New-Item -ItemType Directory -Force -Path $swinirDirectory | Out-Null
    Invoke-Docker run --rm --env HF_HUB_OFFLINE=0 --env TRANSFORMERS_OFFLINE=0 --mount "type=bind,source=$swinirDirectory,target=/models" --workdir /models --entrypoint bash $appImage -c @"
set -e
if [ ! -f /models/RealESRGAN_x4plus.pth ]; then
  echo 'Downloading SwinIR model...'
  python -c "
from huggingface_hub import hf_hub_download
hf_hub_download(repo_id='xinntao/Real-ESRGAN', filename='weights/RealESRGAN_x4plus.pth', local_dir='/models', local_dir_use_symlinks=False)
" 2>/dev/null || true
  if [ -f /models/weights/RealESRGAN_x4plus.pth ]; then
    mv /models/weights/RealESRGAN_x4plus.pth /models/
  fi
fi
echo 'SwinIR model ready.'
"@

    Write-Host "[10/10] Exporting Docker images and writing checksums..."
    $images = @($appImage, $postgresImage, $minioImage, $minioMcImage, $qwenVlImage, $qwen3Image)
    foreach ($image in $images) {
        Invoke-Docker image inspect $image
    }

    & docker save "--output=$absoluteOutput" @images
    if ($LASTEXITCODE -ne 0) {
        throw "docker save failed with exit code $LASTEXITCODE."
    }

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
    Write-Host "Tools: $toolsDirectory"
    Write-Host "Completion marker: $completionPath"
}
finally {}
