# -*- coding: utf-8 -*-
"""
Настройки бота.

Порядок приоритета:
    1) переменная окружения (удобно на хостинге),
    2) файл config.json (удобно на своём компьютере),
    3) значение по умолчанию.

Часть настроек можно менять прямо из Telegram (команды владельца) —
тогда они сразу сохраняются в config.json.
"""
from __future__ import annotations

import json
import os
import threading

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
DATA_DIR = os.environ.get("DATA_DIR") or os.path.join(BASE_DIR, "data")

DEFAULTS: dict = {
    # --- подключение ---
    "bot_token": "",
    "deepseek_api_key": "",
    "deepseek_model": "deepseek-chat",
    "deepseek_base_url": "https://api.deepseek.com",
    "owner_chat_id": None,
    "proxy": None,

    # --- поведение ---
    "mode": "suggest",              # auto = отвечать сам | suggest = черновики мне | off = только читать
    "persona": "",
    "extra_instructions": "",
    "disclose_ai": False,           # True = бот честно скажет, что помогает ИИ-помощник
    "allowed_chats": [],            # пусто = все чаты; иначе только эти chat_id
    "blocked_chats": [],            # эти чаты игнорируем
    "quiet_hours": None,            # "23:00-08:00" — в это время не отвечаем сами, шлём черновик
    "notify_keywords": [],          # слова, при которых владельцу сразу летит уведомление

    # --- «человечность» и защита от спама ---
    "reply_delay_min": 2.0,
    "reply_delay_max": 7.0,
    "min_reply_interval": 3.0,
    "max_replies_per_minute": 6,
    "debounce_seconds": 1.6,        # ждём паузу, чтобы ответить на серию сообщений одним ответом
    "max_reply_chars": 900,
    "history_limit": 20,
    "temperature": 0.8,
    "max_tokens": 900,
    "max_tool_rounds": 3,

    # --- возможности ---
    "web_search": True,
    "tools_enabled": True,
    "memory_enabled": True,
}

ENV_MAP = {
    "bot_token": "BOT_TOKEN",
    "deepseek_api_key": "DEEPSEEK_API_KEY",
    "deepseek_model": "DEEPSEEK_MODEL",
    "owner_chat_id": "OWNER_CHAT_ID",
    "proxy": "PROXY",
    "mode": "MODE",
}


def _cast(value: str, like):
    """Приводит строку из переменной окружения к типу значения по умолчанию."""
    try:
        if isinstance(like, bool):
            return value.strip().lower() in ("1", "true", "yes", "on", "да")
        if isinstance(like, int):
            return int(value)
        if isinstance(like, float):
            return float(value)
        if isinstance(like, list):
            return [x.strip() for x in value.split(",") if x.strip()]
    except (TypeError, ValueError):
        return value
    return value


class _Config:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.data: dict = dict(DEFAULTS)
        self.reload()

    # ---------------------------------------------------------------- чтение
    def reload(self) -> dict:
        raw: dict = {}
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                raw = json.load(f)
        except FileNotFoundError:
            print("[config] config.json не найден — беру config.example.json как образец настроек")
        except (OSError, ValueError) as e:
            print(f"[config] не смог прочитать config.json: {e}")

        merged = dict(DEFAULTS)
        for key, value in raw.items():
            if value is None and key in DEFAULTS and DEFAULTS[key] is not None:
                continue  # null в файле = «оставь как по умолчанию»
            merged[key] = value

        for key, env_name in ENV_MAP.items():
            value = os.environ.get(env_name)
            if value:
                merged[key] = _cast(value, DEFAULTS.get(key))

        with self._lock:
            self.data = merged
        return merged

    # ---------------------------------------------------------------- запись
    def save(self) -> None:
        with self._lock:
            snapshot = dict(self.data)
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(snapshot, f, ensure_ascii=False, indent=2)
        except OSError as e:
            print(f"[config] не смог сохранить config.json: {e}")

    # ---------------------------------------------------------------- доступ
    def get(self, key: str, default=None):
        with self._lock:
            if key in self.data:
                return self.data[key]
        return DEFAULTS.get(key, default)

    def set(self, key: str, value, save: bool = True) -> None:
        with self._lock:
            self.data[key] = value
        if save:
            self.save()

    # удобные ссылки
    @property
    def token(self) -> str:
        return str(self.get("bot_token") or "").strip()

    @property
    def owner_chat_id(self):
        value = self.get("owner_chat_id")
        return str(value) if value not in (None, "") else None

    @property
    def proxy(self):
        value = self.get("proxy")
        return str(value) if value else None

    @property
    def has_ai(self) -> bool:
        return bool(str(self.get("deepseek_api_key") or "").strip())


cfg = _Config()
