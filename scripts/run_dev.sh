#!/usr/bin/env bash
# 개발 모드 원클릭 부팅: 인프라 띄우고 게이트웨이를 reload 모드로 실행.
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ ! -f .env ]]; then
    cp .env.example .env
    echo "[run_dev] .env 생성됨 — ANTHROPIC_API_KEY 등 채워주세요."
fi

echo "[run_dev] Redis + PostgreSQL 기동 중..."
docker compose up -d redis postgres

echo "[run_dev] DB ready 대기..."
until docker compose exec -T postgres pg_isready -U ccim >/dev/null 2>&1; do
    sleep 1
done

echo "[run_dev] gateway 실행 (Ctrl+C로 종료)"
exec uv run uvicorn ccim.main:app --reload --host 0.0.0.0 --port 8080
