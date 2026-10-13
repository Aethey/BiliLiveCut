# CPU 运行镜像；构建工具和 uv 只保留在构建阶段。
FROM python:3.12-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258 AS builder
COPY --from=ghcr.io/astral-sh/uv:0.12.18@sha256:3adc3706091ce7c2fe595e669628caedd6d951551b92b258b7e7dbe06d9440bc /uv /bin/uv
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

ENV UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --extra asr-all --extra llm --extra web --no-install-project

# PyTorch 不在项目 extras 中，单独安装配套的官方 CPU wheel 和锁定依赖。
COPY packaging/docker/requirements-cpu.lock ./requirements-cpu.lock
RUN uv pip install --python /app/.venv/bin/python --require-hashes --no-deps \
    --index-url https://pypi.org/simple --extra-index-url https://download.pytorch.org/whl/cpu \
    --index-strategy unsafe-best-match -r requirements-cpu.lock

# 包括 setuptools data-files 与 C/Cython 构建源，不能只复制 app/config。
COPY README.md LICENSE MANIFEST.in setup.py setup_c.py .env.example ./
COPY app ./app
COPY config ./config
COPY docs/live-source-plugins.md docs/douyin-plugin-handoff.md ./docs/
COPY plugin ./plugin
COPY tools/native/c ./tools/native/c
COPY tools/native/cython ./tools/native/cython
RUN uv sync --locked --no-dev --extra asr-all --extra llm --extra web --no-editable \
    --inexact \
    && uv pip check --python /app/.venv/bin/python \
    && /app/.venv/bin/python -I -c "from app.accelerators import _c_speedups, _cython_speedups; import torch, torchaudio; from funasr import AutoModel; assert torch.version.cuda is None"

FROM python:3.12-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258 AS runtime
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg fonts-noto-cjk fontconfig libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 1000 --create-home appuser \
    && mkdir -p /data /app \
    && chown appuser:appuser /data

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    APP_ENV=prod \
    DOCKER_AUTH_ENABLED=true \
    STORAGE_ROOT=/data \
    DATABASE_URL=sqlite:////data/blc.db \
    PLUGIN_DIR=/data/plugins \
    BLC_APP_ROOT=/data \
    HF_HOME=/data/cache/huggingface \
    MODELSCOPE_CACHE=/data/cache/modelscope \
    PLAYWRIGHT_BROWSERS_PATH=/data/cache/playwright

WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY packaging/docker/entrypoint.py packaging/docker/healthcheck.py ./docker/
USER appuser
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD ["python", "/app/docker/healthcheck.py"]
ENTRYPOINT ["python", "/app/docker/entrypoint.py"]
CMD ["blc", "serve", "--host", "0.0.0.0", "--port", "8000"]
