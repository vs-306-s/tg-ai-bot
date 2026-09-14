# -*- coding: utf-8 -*-
"""
Точка входа. Запуск:  python main.py

Что делает:
  1) читает config.json (или переменные окружения);
  2) запускает бота в режиме long polling (сервер и белый IP не нужны);
  3) слушает Telegram Business — сообщения из переписки владельца;
  4) отвечает через DeepSeek от лица владельца (режим auto) или присылает черновики (suggest).
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.fsm.storage.memory import MemoryStorage

from bot import config, db
from bot.business import router as business_router
from bot.owner import router as owner_router

# Русская Windows-консоль часто в cp1251 — без этого print с эмодзи роняет бота
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

# Логи пишем и в консоль, и в файл bot.log — так видно проблемы даже без окна
LOG_FILE = os.path.join(config.BASE_DIR, "bot.log")
_handlers: list = [logging.StreamHandler()]
try:
    _handlers.append(logging.FileHandler(LOG_FILE, encoding="utf-8"))
except OSError:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=_handlers,
)
log = logging.getLogger("main")


def say(text: str = "") -> None:
    """Сообщение и в консоль, и в bot.log."""
    print(text, flush=True)
    log.info(text)


def banner(me=None) -> None:
    cfg = config.cfg
    lines = [
        "",
        "=" * 58,
        "  Telegram AI-бот (DeepSeek) — личный помощник",
        "=" * 58,
        f"  Бот:            {('@' + me.username) if me and me.username else '—'}",
        f"  id бота:        {me.id if me else '—'}",
        f"  Владелец:       {cfg.owner_chat_id or 'ещё не определён — напиши боту /start'}",
        f"  Режим:          {cfg.get('mode')}  (auto | suggest | off)",
        f"  DeepSeek:       {'ключ есть ✅' if cfg.has_ai else 'КЛЮЧА НЕТ ❌ — впиши deepseek_api_key в config.json'}",
        f"  Прокси:         {cfg.proxy or 'не используется'}",
        f"  База:           {db.DB_PATH}",
        f"  Лог:            {LOG_FILE}",
        "=" * 58,
        "",
    ]
    say("\n".join(lines))


async def main() -> None:
    cfg = config.cfg
    if not cfg.token:
        say("❌ В config.json не заполнен «bot_token».\n"
            "   Возьми токен у @BotFather (или вставь его в config.json).")
        return

    db.init()

    session = AiohttpSession(proxy=cfg.proxy) if cfg.proxy else None
    bot = Bot(
        token=cfg.token,
        session=session,
        default=DefaultBotProperties(link_preview_is_disabled=True),
    )

    dispatcher = Dispatcher(storage=MemoryStorage())
    # Сначала команды владельца, потом обработчики бизнес-чатов.
    dispatcher.include_router(owner_router)
    dispatcher.include_router(business_router)

    try:
        me = await asyncio.wait_for(bot.get_me(), timeout=25)
    except asyncio.TimeoutError:
        say("❌ Telegram не отвечает (таймаут).\n"
            "   Скорее всего api.telegram.org заблокирован: включи VPN\n"
            "   или укажи «proxy» в config.json, например \"socks5://127.0.0.1:1080\".")
        await bot.session.close()
        return
    except Exception as e:  # noqa: BLE001
        say(f"❌ Не смог подключиться к Telegram: {type(e).__name__}: {e}\n"
            "   Проверь токен и интернет (или укажи proxy в config.json).")
        await bot.session.close()
        return

    banner(me)

    if not cfg.has_ai:
        say("⚠️  ИИ не подключён: впиши «deepseek_api_key» (ключ вида sk-...) в config.json.\n")

    try:
        await bot.delete_webhook(drop_pending_updates=False)
    except Exception as e:  # noqa: BLE001
        log.debug("delete_webhook: %s", e)

    say("▶️  Бот запущен. Подключи его в Telegram: Настройки → Telegram Business → Чат-боты.\n"
        "   Остановить — Ctrl+C.\n")

    try:
        await dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types())
    finally:
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        say("\n⏹  Остановлено пользователем.")
    except Exception:  # noqa: BLE001
        logging.exception("непредвиденная ошибка")
        sys.exit(1)
