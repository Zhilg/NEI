ARG PYTHON_IMAGE=python:3.12-slim
FROM ${PYTHON_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1

WORKDIR /app
RUN apt-get update \
    && apt-get install --no-install-recommends -y libgl1 libglib2.0-0 libxcb1 \
    && rm -rf /var/lib/apt/lists/*

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
    magic-pdf doclayout-yolo==0.0.4 openai ultralytics rapid-table onnxruntime omegaconf shapely pyclipper dill \
    && rm -rf /app/tools_wheels

RUN mkdir -p /tools/mineru

CMD ["idp"]
