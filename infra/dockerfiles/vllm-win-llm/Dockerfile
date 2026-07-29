FROM vllm/vllm-openai:v0.26.0

ARG MODEL_NAME=Qwen/Qwen2.5-0.5B-Instruct
ARG HF_TOKEN=""

ENV HF_TOKEN=${HF_TOKEN}
ENV TRANSFORMERS_VERBOSITY=info

RUN pip install --upgrade pip && \
    pip install --upgrade huggingface_hub transformers tokenizers accelerate safetensors && \
    python3 -c "
import os
from huggingface_hub import snapshot_download
model_name = '${MODEL_NAME}'
local_dir = '/models'
token = '${HF_TOKEN}' or None
print(f'Downloading {model_name} to {local_dir} ...')
snapshot_download(model_name, local_dir=local_dir, local_dir_use_symlinks=False, token=token)
print('Download complete. Verifying files...')
required = ['config.json', 'model.safetensors.index.json']
for f in required:
    path = os.path.join(local_dir, f)
    exists = os.path.exists(path)
    print(f'  {f}: {\"OK\" if exists else \"MISSING\"} ')
    if not exists:
        raise SystemExit(f'Required file missing: {path}')
print('All required files present.')
" && \
    ls -la /models/
