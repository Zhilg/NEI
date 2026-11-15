FROM vllm/vllm-openai:v0.29.0

RUN pip install --upgrade pip && \
    pip install --upgrade huggingface_hub transformers tokenizers accelerate safetensors

COPY infra/dockerfiles/vllm-vl/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]

# Kept as CMD so the image is runnable standalone; compose `command` overrides
# these at runtime and the entrypoint conditionally omits --quantization.
CMD ["--model", "/models/new_vl", "--served-model-name", "Qwen3.8-27B", "--host", "0.0.0.0", "--port", "8000", "--quantization", "compressed-tensors", "--dtype", "half", "--gpu-memory-utilization", "0.9", "--max-model-len", "32768", "--kv-cache-dtype", "auto", "--max-num-seqs", "256", "--max-num-batched-tokens", "8192", "--limit-mm-per-prompt", "{\"vision\": 3}", "--default-chat-template-kwargs", "{\"enable_thinking\": false}", "--enable-prefix-caching", "--async-scheduling", "--attention-backend", "auto", "--trust-remote-code", "--kv-cache-memory", "15000000000"]
