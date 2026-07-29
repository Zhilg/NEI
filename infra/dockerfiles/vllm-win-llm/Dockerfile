FROM vllm/vllm-openai:v0.10.2

ARG MODEL_NAME=Qwen/Qwen3.5-0.8B
ARG HF_TOKEN=""

ENV HF_TOKEN=${HF_TOKEN}

RUN pip install huggingface-hub

RUN python3 -c "from huggingface_hub import snapshot_download; snapshot_download('${MODEL_NAME}', local_dir='/models', local_dir_use_symlinks=False, token='${HF_TOKEN}')"

CMD ["--model", "/models", "--served-model-name", "Qwen3.5-0.8B", "--host", "0.0.0.0", "--port", "8000", "--gpu-memory-utilization", "0.95", "--max-model-len", "8192"]
