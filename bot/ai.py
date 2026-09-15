# -*- coding: utf-8 -*-
"""Клиент DeepSeek (API совместим с OpenAI, поэтому ходим обычным POST через httpx)."""
from __future__ import annotations

import json
import logging

import httpx

from bot.net import build_client

log = logging.getLogger("ai")

FRIENDLY_ERRORS = {
    400: "DeepSeek отклонил запрос (400). Проверьте модель в настройках.",
    401: "DeepSeek не принял ключ (401). Проверьте «deepseek_api_key» в config.json.",
    402: "На балансе DeepSeek закончились деньги (402). Пополните счёт на platform.deepseek.com.",
    403: "Доступ к DeepSeek запрещён (403). Возможно, нужен VPN/прокси.",
    422: "DeepSeek не понял параметры запроса (422).",
    429: "Слишком много запросов к DeepSeek (429). Подождите немного.",
    500: "На стороне DeepSeek ошибка (500). Попробуйте ещё раз.",
    503: "DeepSeek перегружен (503). Попробуйте ещё раз.",
}


class AIError(RuntimeError):
    """Понятная человеку ошибка ИИ."""


class DeepSeek:
    def __init__(self, cfg, model_override: str | None = None) -> None:
        self._cfg = cfg
        self._model_override = model_override

    # ------------------------------------------------------------------ данные
    @property
    def key(self) -> str:
        return str(self._cfg.get("deepseek_api_key") or "").strip()

    @property
    def model(self) -> str:
        return self._model_override or str(self._cfg.get("deepseek_model") or "deepseek-chat")

    @property
    def url(self) -> str:
        base = str(self._cfg.get("deepseek_base_url") or "https://api.deepseek.com").rstrip("/")
        return f"{base}/chat/completions"

    @property
    def supports_tools(self) -> bool:
        # у reasoning-модели инструменты не поддерживаются
        return "reasoner" not in self.model.lower()

    # ------------------------------------------------------------------- запрос
    async def chat(self, messages: list[dict], tools: list[dict] | None = None,
                   temperature: float | None = None, max_tokens: int | None = None) -> dict:
        """Возвращает message из ответа DeepSeek: {"role","content","tool_calls"?}."""
        if not self.key:
            raise AIError("Не задан ключ DeepSeek. Откройте config.json и впишите "
                          "«deepseek_api_key» (ключ вида sk-... с platform.deepseek.com).")

        payload: dict = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "temperature": float(self._cfg.get("temperature") or 0.8),
            "max_tokens": int(max_tokens or self._cfg.get("max_tokens") or 900),
        }
        if temperature is not None:
            payload["temperature"] = float(temperature)
        if tools and self.supports_tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        headers = {"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"}

        proxy = self._cfg.proxy
        try:
            client = build_client(proxy, timeout=httpx.Timeout(120.0, connect=20.0))
        except Exception as e:  # noqa: BLE001 — не должны падать на настройках сети
            raise AIError(f"Не смог настроить соединение: {type(e).__name__}") from e

        try:
            async with client:
                response = await client.post(self.url, json=payload, headers=headers)
        except httpx.HTTPError as e:
            raise AIError(f"Нет связи с DeepSeek: {type(e).__name__}. "
                          "Проверьте интернет или укажите «proxy» в config.json.") from e

        if response.status_code != 200:
            hint = FRIENDLY_ERRORS.get(response.status_code, f"DeepSeek вернул код {response.status_code}.")
            log.warning("DeepSeek %s: %s", response.status_code, response.text[:500])
            raise AIError(hint)

        try:
            data = response.json()
            message = data["choices"][0]["message"]
        except (ValueError, KeyError, IndexError) as e:
            raise AIError("DeepSeek вернул неожиданный ответ.") from e

        # reasoner иногда присылает пустой content и отдельное поле reasoning_content
        if not message.get("content") and message.get("reasoning_content") and not message.get("tool_calls"):
            message["content"] = message["reasoning_content"]
        return message

    async def ask(self, system: str, user: str, tools: list[dict] | None = None) -> str:
        """Короткий вопрос-ответ без инструментов (для служебных задач владельца)."""
        message = await self.chat(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            tools=None,
        )
        return (message.get("content") or "").strip()


def parse_tool_args(raw: str | None) -> dict:
    try:
        return json.loads(raw or "{}")
    except (ValueError, TypeError):
        return {}
