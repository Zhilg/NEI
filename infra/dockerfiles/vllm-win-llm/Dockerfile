FROM vllm/vllm-openai:v0.10.2

ARG MODEL_NAME=Qwen/Qwen2.5-1.5B-Instruct

RUN pip install huggingface-hub

RUN python3 -c "from huggingface_hub import snapshot_download; snapshot_download('${MODEL_NAME}', local_dir='/models', local_dir_use_symlinks=False)"

CMD ["--model", "/models", "--served-model-name", "Qwen2.5-1.5B-Instruct", "--host", "0.0.0.0", "--port", "8000", "--gpu-memory-utilization", "0.95", "--max-model-len", "8192"]
