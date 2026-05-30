"""pytest 공통 fixture.

실제 인프라가 필요한 테스트는 `@pytest.mark.integration` 마커로 분리.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from ccim.config import Settings, get_settings


@pytest.fixture(autouse=True)
def _reset_settings_cache() -> None:
    """매 테스트마다 Settings 캐시 초기화 (env 변경 반영)."""
    get_settings.cache_clear()


@pytest.fixture
def settings() -> Settings:
    return get_settings()


@pytest.fixture
async def app() -> AsyncIterator:
    """FastAPI app 인스턴스(의존성은 mock 권장). lifespan 미실행."""
    from ccim.main import create_app

    yield create_app()
