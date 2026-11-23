FROM python:3.12-slim

WORKDIR /workspace

RUN pip install --no-cache-dir \
    pydantic \
    pydantic-settings \
    httpx \
    tqdm \
    PyMuPDF \
    Pillow \
    numpy \
    mammoth \
    python-docx \
    python-pptx \
    beautifulsoup4 \
    lxml \
    huggingface_hub \
    uvicorn \
    fastapi \
    opencv-python-headless \
    pikepdf

COPY src /workspace/src
COPY scripts /workspace/scripts
COPY tests /workspace/tests
COPY pyproject.toml /workspace/pyproject.toml
COPY README.md /workspace/README.md

ENV PYTHONPATH=/workspace/src

RUN pip install -e /workspace --no-deps --no-cache-dir

CMD ["idp"]
