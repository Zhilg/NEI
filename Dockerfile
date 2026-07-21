ARG PYTHON_IMAGE=python:3.12-slim
FROM ${PYTHON_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY alembic.ini ./
COPY alembic ./alembic
COPY tests ./tests
COPY wheels ./wheels
RUN pip install --no-index --find-links=/app/wheels hatchling \
    && pip install --no-index --find-links=/app/wheels --no-build-isolation ".[dev]" \
    && rm -rf /app/wheels

COPY tools_wheels ./tools_wheels
RUN pip install --no-index --find-links=/app/tools_wheels \
    paddlepaddle paddleocr magic-pdf \
    && rm -rf /app/tools_wheels

RUN mkdir -p /tools/mineru /tools/ocr

CMD ["idp"]
