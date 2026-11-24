"""Лаб. 7, вариант 6. Предохранитель (circuit breaker) на уровне ДОМЕНА.

Состояния: `closed` → `open` → `half-open` → `closed`.
Пока домен в `open`, запросы к нему не отправляются вовсе — сайт не получает шторм
повторов, а сборщик продолжает работать с остальными доменами (изоляция отказа).
После паузы отправляется один пробный запрос: успех закрывает предохранитель,
неудача снова открывает его.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class CircuitBreaker:
    """Порог `threshold` последовательных отказов открывает домен на `reset_after` секунд."""

    host: str
    threshold: int = 3
    reset_after: float = 20.0
    clock = time.monotonic
    state: str = "closed"
    failures: int = 0
    opened_at: float = 0.0
    activations: int = 0  # сколько раз домен был изолирован — идёт в метрики прогона
    transitions: list = field(default_factory=list)
    _probing: bool = False

    def allow(self) -> bool:
        """Можно ли отправлять запрос к домену прямо сейчас."""
        if self.state == "closed":
            return True
        if self.state == "open":
            if self.clock() - self.opened_at >= self.reset_after:
                self._move("half-open")
                self._probing = True
                return True
            return False
        if self.state == "half-open":  # пробный запрос уже отправлен
            return not self._probing
        return True

    def record(self, ok: bool) -> str:
        """Регистрирует результат попытки и возвращает новое состояние."""
        if ok:
            if self.state != "closed":
                self._move("closed")
            self.failures = 0
            self._probing = False
            return self.state
        self.failures += 1
        self._probing = False
        if self.state == "half-open" or self.failures >= self.threshold:
            self.state = "closed"  # чтобы _move зафиксировал переход
            self._move("open")
            self.opened_at = self.clock()
            self.activations += 1
        return self.state

    def _move(self, new_state: str):
        if new_state != self.state:
            self.transitions.append(
                {
                    "at_s": round(self.clock(), 3),
                    "from": self.state,
                    "to": new_state,
                    "failures": self.failures,
                }
            )
        self.state = new_state


@dataclass
class BreakerRegistry:
    """Предохранители по доменам: отказ одного домена не останавливает остальные."""

    threshold: int = 3
    reset_after: float = 20.0
    breakers: dict = field(default_factory=dict)

    def for_host(self, host: str) -> CircuitBreaker:
        if host not in self.breakers:
            self.breakers[host] = CircuitBreaker(
                host=host, threshold=self.threshold, reset_after=self.reset_after
            )
        return self.breakers[host]

    def allow(self, host: str) -> bool:
        return self.for_host(host).allow()

    def record(self, host: str, ok: bool) -> str:
        return self.for_host(host).record(ok)

    @property
    def activations(self) -> int:
        return sum(b.activations for b in self.breakers.values())

    def states(self) -> dict:
        return {h: b.state for h, b in self.breakers.items()}

    def table(self) -> list[dict]:
        return [
            {
                "домен": host,
                "состояние": b.state,
                "отказов подряд": b.failures,
                "активаций": b.activations,
                "порог": b.threshold,
                "пауза, с": b.reset_after,
            }
            for host, b in self.breakers.items()
        ]
