from __future__ import annotations

from abc import ABC, abstractmethod

from .models import Action, RuntimeSnapshot


class RuntimeAdapter(ABC):
    """Universal boundary implemented by every compute runtime."""

    @abstractmethod
    def snapshot(self) -> RuntimeSnapshot:
        raise NotImplementedError

    @abstractmethod
    def apply(self, action: Action) -> None:
        raise NotImplementedError

    @abstractmethod
    def tick(self, minutes: float) -> None:
        raise NotImplementedError

    @abstractmethod
    def is_done(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    def reset(self) -> None:
        raise NotImplementedError
