# 仅用于隔离 Linux race / 多进程 Gate，不是生产镜像。
FROM golang:1.26-bookworm
COPY --from=python:3.12-bookworm /usr/local /usr/local
# Python 只执行现有离线 Alembic ledger；版本与本轮 host migration 工具一致。
RUN python -m pip install --no-cache-dir alembic==1.18.4 sqlalchemy==2.0.46 psycopg2-binary==2.9.11 pydantic-settings==2.14.1 pydantic==2.12.5 httpx==0.28.1 structlog==25.5.0
ENV GOPROXY=https://goproxy.cn,direct G1_TEST_PYTHON=/usr/local/bin/python G3_RACE_WORKER=1 GORACE=halt_on_error=1
WORKDIR /workspace/go-backend
