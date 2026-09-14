# -*- coding: utf-8 -*-
"""
Создание HTTP-клиентов с защитой от «кривых» прокси в системе.

Зачем это нужно: в Windows часто прописан системный прокси вида
    socks4://127.0.0.1:10808
(его ставят VPN/прокси-клиенты вроде v2rayN, WARP, Clash). httpx такие схемы
не поддерживает и падает с ValueError при создании клиента — то есть падал бы
и DeepSeek, и поиск, хотя интернет работает.

Правила:
    1) прокси указан в config.json -> используем только его;
    2) иначе берём системные настройки, но если они несовместимы — идём напрямую.
"""
from __future__ import annotations

import logging

import httpx

log = logging.getLogger("net")


def build_client(proxy: str | None = None, *, timeout=60.0, **kwargs) -> httpx.AsyncClient:
    """Возвращает httpx.AsyncClient, который точно создастся (или понятно упадёт)."""
    options: dict = {"timeout": timeout, "follow_redirects": True}
    options.update(kwargs)

    if proxy:
        try:
            return httpx.AsyncClient(proxy=proxy, trust_env=False, **options)
        except ValueError as e:
            log.warning("прокси из настроек не подошёл httpx (%s) — иду напрямую", e)

    try:
        return httpx.AsyncClient(trust_env=True, **options)
    except ValueError as e:
        log.info("системный прокси несовместим с httpx (%s) — иду напрямую без прокси", e)
        return httpx.AsyncClient(trust_env=False, **options)


def proxy_hint() -> str:
    return ('Если нужен прокси — укажи в config.json HTTP-адрес, например '
            '"http://127.0.0.1:10809" (httpx не умеет socks4, ему нужен socks5 или http).')
