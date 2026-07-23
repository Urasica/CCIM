"""환경 변수 기반 설정. 모든 모듈은 `get_settings()`로 주입받음."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """게이트웨이 전체 설정. 접두사 `CCIM_` 또는 외부 표준명(ANTHROPIC_, OTEL_)."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ── Gateway
    host: str = Field(default="0.0.0.0", alias="CCIM_HOST")
    port: int = Field(default=8080, alias="CCIM_PORT")
    log_level: str = Field(default="INFO", alias="CCIM_LOG_LEVEL")
    version: str = Field(default="v1.0", alias="CCIM_VERSION")

    # ── Upstream / LLM provider
    # provider: "anthropic" | "openai" | "openai-compatible"
    llm_provider: str = Field(default="anthropic", alias="CCIM_LLM_PROVIDER")
    llm_base_url: str | None = Field(default=None, alias="CCIM_LLM_BASE_URL")
    llm_model: str | None = Field(default=None, alias="CCIM_LLM_MODEL")
    llm_timeout_s: float = Field(default=300.0, alias="CCIM_LLM_TIMEOUT_S")

    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")
    anthropic_base_url: str = Field(
        default="https://api.anthropic.com", alias="ANTHROPIC_BASE_URL"
    )
    openai_api_key: str = Field(default="ollama", alias="OPENAI_API_KEY")

    # ── Redis
    redis_url: str = Field(default="redis://localhost:6379/0", alias="CCIM_REDIS_URL")
    redis_ttl_seconds: int = Field(default=3600, alias="CCIM_REDIS_TTL_SECONDS")
    evidence_store_path: str = Field(default="", alias="CCIM_EVIDENCE_STORE_PATH")

    # ── PostgreSQL (psycopg3 드라이버 사용 — asyncpg는 Windows Docker에서 WinError 10054 발생)
    database_url: str = Field(
        default="postgresql+psycopg://ccim:ccim@localhost:5432/ccim",
        alias="CCIM_DATABASE_URL",
    )
    telemetry_drain_timeout_s: float = Field(
        default=5.0, alias="CCIM_TELEMETRY_DRAIN_TIMEOUT_S"
    )

    # ── PCFI
    llamaguard_url: str = Field(
        default="", alias="CCIM_LLAMAGUARD_URL"
    )  # 빈 문자열 = Llama Guard 비활성화 (regex-only 모드)
    llamaguard_model: str = Field(default="llama-guard3:8b", alias="CCIM_LLAMAGUARD_MODEL")
    pcfi_latency_budget_ms: int = Field(default=50, alias="CCIM_PCFI_LATENCY_BUDGET_MS")
    # 코딩 에이전트 미들웨어 특성상 S14(Code Interpreter Abuse)는 정상 요청을 오탐함.
    # 쉼표 구분 카테고리 문자열 — 해당 카테고리만 탐지되면 block 대신 allow로 처리.
    pcfi_skip_guard_categories: str = Field(
        default="S14", alias="CCIM_PCFI_SKIP_GUARD_CATEGORIES"
    )

    # ── Session tagging (header/launcher token이 없을 때 사용하는 fallback prefix)
    session_prefix: str = Field(default="", alias="CCIM_SESSION_PREFIX")

    # ── Compression
    compression_enabled: bool = Field(default=True, alias="CCIM_COMPRESSION_ENABLED")
    compression_trigger_tokens: int = Field(
        default=20_000, alias="CCIM_COMPRESSION_TRIGGER_TOKENS"
    )
    compression_target_tokens: int = Field(
        default=14_000, alias="CCIM_COMPRESSION_TARGET_TOKENS"
    )
    # retrieve_original 도구 주입 여부.
    # True  -> 압축 시 LLM이 retrieve_original을 호출해 원본 코드를 복원할 수 있음 (완전한 가역성)
    #          단, LLM이 도구를 호출할 때마다 추가 LLM 라운드트립 발생 -> 지연 증가
    # False -> 압축만 수행 (토큰 절감), 원본 복원 불가 -- 기본값
    compression_enable_retrieve: bool = Field(
        default=False, alias="CCIM_COMPRESSION_ENABLE_RETRIEVE"
    )
    current_turn_compression_enabled: bool = Field(
        default=False, alias="CCIM_CURRENT_TURN_COMPRESSION_ENABLED"
    )
    current_turn_compression_trigger_tokens: int = Field(
        default=20_000, alias="CCIM_CURRENT_TURN_COMPRESSION_TRIGGER_TOKENS"
    )
    current_turn_compression_read_tools: str = Field(
        default="Read,Grep,Glob,LS,Search",
        alias="CCIM_CURRENT_TURN_COMPRESSION_READ_TOOLS",
    )
    compression_write_guard_enabled: bool = Field(
        default=True, alias="CCIM_COMPRESSION_WRITE_GUARD_ENABLED"
    )
    compression_write_guard_tools: str = Field(
        default="Edit,MultiEdit,Write",
        alias="CCIM_COMPRESSION_WRITE_GUARD_TOOLS",
    )

    # ── OpenTelemetry
    compression_cluster_summary_enabled: bool = Field(
        default=False, alias="CCIM_COMPRESSION_CLUSTER_SUMMARY_ENABLED"
    )

    otel_service_name: str = Field(default="ccim-gateway", alias="OTEL_SERVICE_NAME")
    otel_exporter_endpoint: str = Field(
        default="", alias="OTEL_EXPORTER_OTLP_ENDPOINT"
    )  # 빈 문자열 = 콘솔 exporter (OTLP collector 없을 때)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """프로세스 수명 동안 1회만 로드. 테스트에선 `get_settings.cache_clear()`."""
    return Settings()
