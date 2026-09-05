FROM python:3.11.16-slim-bookworm@sha256:528257d48c1da0dcecc2e725d1ae34498d60c965f1241e39cd6a85a8859bdf84 AS builder

WORKDIR /app
ENV UV_PROJECT_ENVIRONMENT=/opt/xiaowei-venv

RUN python -m pip install --no-cache-dir uv==0.12.8
COPY pyproject.toml uv.lock ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.11.16-slim-bookworm@sha256:528257d48c1da0dcecc2e725d1ae34498d60c965f1241e39cd6a85a8859bdf84

RUN groupadd --system xiaowei && useradd --system --gid xiaowei --home /app xiaowei
WORKDIR /app
ENV PATH=/opt/xiaowei-venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
COPY --from=builder /opt/xiaowei-venv /opt/xiaowei-venv
COPY alembic.ini ./alembic.ini
USER xiaowei
CMD ["python", "-m", "xiaowei_agent.interfaces.api"]
