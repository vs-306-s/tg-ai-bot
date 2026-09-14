# -*- coding: utf-8 -*-
"""
Поиск в интернете без API-ключей.

Основной источник — DuckDuckGo (html-версия), запасные — lite-версия DDG
и Википедия. Всё аккуратно обёрнуто: если источник не ответил, идём к следующему.
"""
from __future__ import annotations

import logging
import re
from urllib.parse import parse_qs, unquote, urlparse

import httpx
from bs4 import BeautifulSoup

from bot.net import build_client

log = logging.getLogger("search")

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml",
}
TIMEOUT = httpx.Timeout(25.0, connect=15.0)


def _clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def _real_url(href: str) -> str:
    """DDG прячет ссылки в редирект /l/?uddg=... — достаём настоящий адрес."""
    if not href:
        return ""
    if href.startswith("//"):
        href = "https:" + href
    if "uddg=" in href:
        query = parse_qs(urlparse(href).query)
        if "uddg" in query:
            return unquote(query["uddg"][0])
    return href


async def _get(client: httpx.AsyncClient, url: str, **kwargs) -> str | None:
    try:
        response = await client.get(url, headers=HEADERS, timeout=TIMEOUT, **kwargs)
        if response.status_code == 200:
            return response.text
        log.info("поиск: %s ответил кодом %s", url, response.status_code)
    except httpx.HTTPError as e:
        log.info("поиск: %s не ответил (%s)", url, type(e).__name__)
    return None


# ------------------------------------------------------------------ источники
async def duckduckgo_html(client: httpx.AsyncClient, query: str, count: int) -> list[dict]:
    html = await _get(client, "https://html.duckduckgo.com/html/", params={"q": query, "kl": "ru-ru"})
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    results = []
    for block in soup.select("div.result, div.web-result"):
        link = block.select_one("a.result__a")
        if not link:
            continue
        snippet = block.select_one(".result__snippet")
        url = _real_url(link.get("href", ""))
        if not url or not link.get_text(strip=True):
            continue
        results.append({
            "title": _clean(link.get_text(" ")),
            "url": url,
            "snippet": _clean(snippet.get_text(" ") if snippet else ""),
        })
        if len(results) >= count:
            break
    return results


async def duckduckgo_lite(client: httpx.AsyncClient, query: str, count: int) -> list[dict]:
    html = await _get(client, "https://lite.duckduckgo.com/lite/", params={"q": query})
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    results = []
    for link in soup.select("a.result-link"):
        url = _real_url(link.get("href", ""))
        row = link.find_parent("tr")
        snippet_cell = row.find_next_sibling("tr") if row else None
        snippet = snippet_cell.select_one(".result-snippet") if snippet_cell else None
        if not url:
            continue
        results.append({
            "title": _clean(link.get_text(" ")),
            "url": url,
            "snippet": _clean(snippet.get_text(" ") if snippet else ""),
        })
        if len(results) >= count:
            break
    return results


async def wikipedia(client: httpx.AsyncClient, query: str, count: int = 2) -> list[dict]:
    """Краткие справки из Википедии — хорошо для «кто это / что это»."""
    data = None
    try:
        response = await client.get(
            "https://ru.wikipedia.org/w/api.php",
            params={"action": "query", "list": "search", "srsearch": query,
                    "format": "json", "utf8": 1, "srlimit": count},
            headers=HEADERS, timeout=TIMEOUT,
        )
        if response.status_code == 200:
            data = response.json()
    except (httpx.HTTPError, ValueError) as e:
        log.info("википедия не ответила (%s)", type(e).__name__)
    if not data:
        return []

    results = []
    for item in (data.get("query", {}).get("search") or [])[:count]:
        title = item.get("title", "")
        if not title:
            continue
        results.append({
            "title": f"Википедия: {title}",
            "url": "https://ru.wikipedia.org/wiki/" + title.replace(" ", "_"),
            "snippet": _clean(re.sub(r"<[^>]+>", "", item.get("snippet", ""))),
        })
    return results


# ---------------------------------------------------------------------- API
async def web_search(query: str, count: int = 5) -> list[dict]:
    """Ищет в интернете. Возвращает список {title, url, snippet}."""
    query = (query or "").strip()
    if not query:
        return []
    async with build_client(timeout=TIMEOUT) as client:
        for source in (duckduckgo_html, duckduckgo_lite):
            results = await source(client, query, count)
            if results:
                return results
        return await wikipedia(client, query, 2)


async def open_page(url: str, limit: int = 4000) -> str:
    """Скачивает страницу и возвращает читаемый текст (без тегов)."""
    url = (url or "").strip()
    if not url.startswith(("http://", "https://")):
        return "Некорректная ссылка."
    try:
        async with build_client(timeout=TIMEOUT) as client:
            response = await client.get(url, headers=HEADERS, timeout=TIMEOUT)
    except httpx.HTTPError as e:
        return f"Не смог открыть страницу: {type(e).__name__}"

    if response.status_code != 200:
        return f"Страница ответила кодом {response.status_code}."

    content_type = response.headers.get("content-type", "")
    if "html" not in content_type and "text" not in content_type:
        return f"Это не текст, а файл ({content_type})."

    soup = BeautifulSoup(response.text, "html.parser")
    for tag in soup(["script", "style", "noscript", "nav", "footer", "header", "form"]):
        tag.decompose()
    text = _clean(soup.get_text(" "))
    return text[:limit] if text else "На странице нет текста."
