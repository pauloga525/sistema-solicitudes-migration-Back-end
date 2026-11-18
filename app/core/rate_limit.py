"""Limitador de peticiones equivalente a @nestjs/throttler v6.

- Clave = ruta (controlador+handler) + IP del cliente, como ThrottlerGuard.
- Ventana fija que empieza con la primera petición (ttl 60 s).
- Cabeceras X-RateLimit-Limit/Remaining/Reset y 429 con Retry-After.
- Se evalúa ANTES de la autenticación (en NestJS es un guard global).

Almacenamiento en memoria del proceso: con varios workers/instancias hay que
usar un almacén compartido (ver docs/DESPLIEGUE.md).
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass
from threading import Lock

from fastapi import Request

from app.core.config import get_settings
from app.core.exceptions import TooManyRequests

DEFAULT_LIMIT = 60
DEFAULT_TTL_SECONDS = 60


@dataclass
class _Bucket:
    hits: int
    expires_at: float


class InMemoryThrottlerStorage:
    def __init__(self) -> None:
        self._buckets: dict[str, _Bucket] = {}
        self._lock = Lock()

    def increment(self, key: str, ttl: float) -> tuple[int, float]:
        now = time.monotonic()
        with self._lock:
            if len(self._buckets) > 50_000:
                self._buckets = {k: b for k, b in self._buckets.items() if b.expires_at > now}
            bucket = self._buckets.get(key)
            if bucket is None or bucket.expires_at <= now:
                bucket = _Bucket(0, now + ttl)
                self._buckets[key] = bucket
            bucket.hits += 1
            return bucket.hits, bucket.expires_at - now

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()


storage = InMemoryThrottlerStorage()


def client_ip(request: Request) -> str:
    if get_settings().trust_proxy:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


class Throttle:
    """Dependencia FastAPI: `dependencies=[Depends(Throttle("RequestsController.create", 10))]`."""

    def __init__(self, route_key: str, limit: int = DEFAULT_LIMIT, ttl_seconds: int = DEFAULT_TTL_SECONDS):
        self.route_key = route_key
        self.limit = limit
        self.ttl = ttl_seconds

    async def __call__(self, request: Request) -> None:
        hits, remaining_ttl = storage.increment(f"{self.route_key}-{client_ip(request)}", self.ttl)
        reset = max(1, math.ceil(remaining_ttl))
        if hits > self.limit:
            raise TooManyRequests(retry_after=reset)
        request.state.rate_limit_headers = {
            "X-RateLimit-Limit": str(self.limit),
            "X-RateLimit-Remaining": str(max(0, self.limit - hits)),
            "X-RateLimit-Reset": str(reset),
        }
