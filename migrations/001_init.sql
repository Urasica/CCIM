-- V1 Foundation: 모든 요청을 한 줄에 추적할 수 있는 단일 테이블.
-- V2에서 평가 결과를 위한 별도 테이블 추가, V1은 feature_flags 컬럼으로 확장 여지만 둠.

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

CREATE TABLE IF NOT EXISTS requests (
    id                          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    session_id                  TEXT NOT NULL,
    agent_name                  TEXT,
    endpoint                    TEXT,

    -- PCFI 결과
    pcfi_action                 TEXT NOT NULL,            -- 'allow' | 'sanitize' | 'block'
    pcfi_reason                 TEXT,

    -- 토큰
    tokens_input_original       INTEGER,
    tokens_input_compressed     INTEGER,
    tokens_output               INTEGER,

    -- 지연
    latency_ms                  INTEGER,
    pcfi_latency_ms             INTEGER,
    compress_latency_ms         INTEGER,
    upstream_latency_ms         INTEGER,

    -- 가역성
    retrieve_original_calls     INTEGER NOT NULL DEFAULT 0,
    write_remaps                INTEGER NOT NULL DEFAULT 0,

    -- 메타
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    feature_flags               JSONB NOT NULL DEFAULT '{}'::jsonb,
    version                     TEXT NOT NULL DEFAULT 'v1.0'
);

CREATE INDEX IF NOT EXISTS idx_requests_session
    ON requests (session_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_requests_pcfi_action
    ON requests (pcfi_action, created_at DESC);

-- V2 hook: 평가 결과를 별도 테이블로 받기 위한 뷰는 V2 마이그레이션에서.
