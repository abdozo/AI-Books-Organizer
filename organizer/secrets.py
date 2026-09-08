from __future__ import annotations

from typing import Protocol

from .database import Library


class SecretStore(Protocol):
    def save(self, value: str) -> None: ...
    def get(self) -> str: ...
    def exists(self) -> bool: ...


class DatabaseSecretStore:
    """Store the personal Gemini key in the local SQLite settings table."""

    def __init__(self, library: Library) -> None:
        self.library = library

    def save(self, value: str) -> None:
        self.library.save_api_key(value)

    def get(self) -> str:
        value = self.library.setting("api_key")
        if not value:
            raise KeyError("أدخل مفتاح Gemini API من الإعدادات قبل بدء الفحص")
        return value

    def exists(self) -> bool:
        return bool(self.library.setting("api_key"))


class MemorySecretStore:
    def __init__(self, value: str = "") -> None:
        self.value = value

    def save(self, value: str) -> None:
        self.value = value.strip()

    def get(self) -> str:
        if not self.value:
            raise KeyError("أدخل مفتاح Gemini API من الإعدادات قبل بدء الفحص")
        return self.value

    def exists(self) -> bool:
        return bool(self.value)
