FROM python:3.12-slim

WORKDIR /workspace

RUN pip install --no-cache-dir \
    pydantic \
    pydantic-settings \
    httpx \
    tqdm \
    PyMuPDF \
    Pillow \
    python-docx \
    python-pptx \
    beautifulsoup4 \
    lxml \
    huggingface_hub \
    uvicorn \
    fastapi

COPY src /workspace/src
COPY scripts /workspace/scripts
COPY tests /workspace/tests

ENV PYTHONPATH=/workspace/src

CMD ["idp"]
