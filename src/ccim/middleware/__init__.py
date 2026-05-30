"""미들웨어 체인 — V1부터 플러그인 패턴으로 V2/V3 기능을 끼워 넣기 위한 자리.

설계 §5 원칙 5: 기능은 플러그인.
"""

from ccim.middleware.chain import Middleware, MiddlewareChain, RequestContext

__all__ = ["Middleware", "MiddlewareChain", "RequestContext"]
