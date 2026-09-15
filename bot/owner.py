# -*- coding: utf-8 -*-
"""
Общение владельца с ботом.

Философия: команд почти нет. Владелец пишет обычным текстом, что ему нужно, а бот сам
вызывает нужные инструменты — меняет режим, запоминает, ищет в переписке, пишет людям
от его лица, учится его манере, показывает состояние.

Остались всего четыре команды:
    /start  — привязать владельца к боту
    /panel  — панель с кнопками
    /key    — вписать ключ DeepSeek (секрет не должен проходить через ИИ)
    /reload — перечитать config.json, если правил его вручную
"""
from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.filters import BaseFilter, Command, CommandObject, CommandStart
from aiogram.types import (CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
                           Message)

from bot import config, db
from bot.ai import AIError
from bot.business import chat_mode, run_agent
from bot.tools import ToolContext

log = logging.getLogger("owner")
router = Router()

CHUNK = 3500
MODES = ("auto", "suggest", "off")
MODE_LABEL = {"auto": "🤖 отвечаю сам", "suggest": "💡 черновики мне", "off": "🔇 только читаю"}

# Что доступно помощнику в личном чате владельца.
# Второй набор — управление самим ботом: владелец просит словами, ИИ вызывает инструмент.
OWNER_TOOLS = {
    "list_chats", "search_messages", "chat_digest", "calc", "add_note", "list_notes", "close_note",
    "set_mode", "set_style", "set_quiet_hours", "set_model", "chat_action", "write_to_chat",
    "remember", "list_memory", "forget", "learn_style", "show_settings",
}
OWNER_WEB_TOOLS = {"web_search", "open_page"}

OWNER_SYSTEM = (
    "Ты личный помощник владельца Telegram-аккаунта и управляешь ботом за него. "
    "Отвечай по-русски, коротко (до 10 строк), по делу.\n"
    "Владелец НЕ ЗНАЕТ команд и не должен их знать: он пишет словами, что хочет, "
    "а ты ВЫПОЛНЯЕШЬ это инструментами. Никогда не отвечай «введи такую-то команду».\n"
    "Твои возможности:\n"
    "— переписка: list_chats (чаты), search_messages (поиск по всей переписке), chat_digest (сводка чата);\n"
    "— настройки: set_mode (auto/suggest/off), set_style (манера письма), set_quiet_hours, set_model;\n"
    "— один чат: chat_action (pause, resume, block, unblock, only, reset_only, note, "
    "facts, add_fact, clear_facts);\n"
    "— написать человеку от лица владельца: write_to_chat (сначала посмотри chat_digest "
    "и пиши его манерой);\n"
    "— память: remember, list_memory, forget; заметки: add_note, list_notes, close_note;\n"
    "— обучение манере письма: learn_style; состояние бота: show_settings;\n"
    "— интернет и счёт: web_search, open_page, calc.\n"
    "Сделал — отчитайся одной строкой, что именно сделал.\n"
    "Никогда не говори «у меня нет доступа», не проверив инструментом. Telegram отдаёт боту только "
    "сообщения, пришедшие ПОСЛЕ подключения к Telegram Business — если данных нет, объясни именно это.\n"
    "Не выдумывай факты, цифры и цитаты из переписки. Перед отправкой сообщения от имени владельца "
    "уточняй только действительно важное (деньги, обязательства, серьёзные договорённости). "
    "Обычные бытовые сообщения («опоздаю», «наберу», «ок, договорились») отправляй сразу, не переспрашивая."
)

HELP = """🤖 <b>Просто напиши мне, что нужно — я сам всё сделаю.</b>

Например:
• «покажи мои чаты»
• «включи авто-ответы» / «пусть только черновики»
• «пиши короче и без эмодзи»
• «не отвечай вот этому чату 30 минут»
• «ночью с 23 до 8 не отвечай сам»
• «запомни: по работе я всегда перезваниваю»
• «выучи мой стиль» — стану писать как ты
• «напиши Олегу, что я опоздаю на 15 минут»
• «найди, где обсуждали встречу» / «перескажи чат с Олегом»
• «что у тебя настроено»

Кнопки: /panel
Ключ ИИ, если понадобится поменять: /key sk-...
"""


# ------------------------------------------------------------------- доступ
class OwnerOnly(BaseFilter):
    async def __call__(self, message: Message) -> bool:
        owner = config.cfg.owner_chat_id
        return bool(owner) and str(message.chat.id) == str(owner)


def owner_ok(query: CallbackQuery) -> bool:
    owner = config.cfg.owner_chat_id
    return bool(owner) and str(query.message.chat.id) == str(owner)


async def say(message: Message, text: str, keyboard: InlineKeyboardMarkup | None = None) -> None:
    """Отправляет длинный текст несколькими сообщениями."""
    text = text or "—"
    for index in range(0, len(text), CHUNK):
        await message.answer(text[index:index + CHUNK],
                             reply_markup=keyboard if index == 0 else None)


# ---------------------------------------------------------------------- /start
@router.message(CommandStart(), F.chat.type == "private")
async def cmd_start(message: Message, bot: Bot) -> None:
    cfg = config.cfg
    owner = cfg.owner_chat_id

    if owner is None:
        connected = {c["user_id"] for c in db.connections() if c.get("user_id")}
        if connected and message.from_user.id not in connected:
            await message.answer("Этот бот уже привязан к другому аккаунту.")
            return
        cfg.set("owner_chat_id", message.chat.id)
        db.set_setting("owner_chat_id", str(message.chat.id))
        await message.answer(
            "🔐 <b>Готово — ты владелец этого бота.</b>\n"
            f"Твой id: <code>{message.chat.id}</code>\n\n"
            "Теперь просто пиши мне словами, что нужно — команды учить не надо.\n\n" + HELP,
        )
        return

    if str(message.chat.id) != str(owner):
        await message.answer("Привет! Это личный бот-помощник, он работает только для владельца.")
        return

    await message.answer(HELP)


# --------------------------------------------------------------------- /panel
def panel_keyboard() -> InlineKeyboardMarkup:
    mode = str(config.cfg.get("mode") or "suggest")

    def mark(value: str, label: str) -> str:
        return ("✅ " if mode == value else "") + label

    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=mark("auto", "🤖 Сам отвечаю"), callback_data="own:mode:auto"),
         InlineKeyboardButton(text=mark("suggest", "💡 Черновики"), callback_data="own:mode:suggest"),
         InlineKeyboardButton(text=mark("off", "🔇 Выкл"), callback_data="own:mode:off")],
        [InlineKeyboardButton(text="🎓 Выучить мой стиль", callback_data="own:learn")],
        [InlineKeyboardButton(text="💬 Чаты", callback_data="own:chats"),
         InlineKeyboardButton(text="🧠 Память", callback_data="own:memory")],
        [InlineKeyboardButton(text="📊 Состояние", callback_data="own:status"),
         InlineKeyboardButton(text="❓ Помощь", callback_data="own:help")],
    ])


def status_text() -> str:
    cfg = config.cfg
    stat = db.stats()
    conns = [c for c in db.connections() if c.get("is_enabled")]
    return (
        "📊 <b>Состояние</b>\n\n"
        f"Режим: {MODE_LABEL.get(str(cfg.get('mode')), cfg.get('mode'))}\n"
        f"ИИ: {'работает ✅' if cfg.has_ai else 'нет ключа ❌'}\n"
        f"Мой стиль: {'выучен ✅' if db.get_setting('style_profile') else 'ещё не учился'}\n"
        f"Память: {len(db.memory_items())} записей\n"
        f"Аккаунтов подключено: {len(conns)}\n"
        f"Переписок: {stat['chats']} · сообщений: {stat['total']} (сегодня {stat['today']})\n"
        f"Примеров для обучения: {db.pair_count()}\n"
        f"Тихие часы: {cfg.get('quiet_hours') or 'выключены'}"
    )


def chats_text() -> str:
    rows = db.all_chats(40)
    if not rows:
        return ("Пока нет ни одной переписки.\n\nTelegram отдаёт боту только сообщения, пришедшие "
                "ПОСЛЕ подключения в Настройки → Telegram Business → Чат-боты. "
                "Всю историю до подключения он не показывает.")
    lines = ["💬 <b>Переписки</b>", ""]
    for row in rows:
        mark = {"auto": "🤖", "suggest": "💡", "off": "🔇"}.get(chat_mode(int(row["chat_id"])), "💡")
        lines.append(f"{mark} <b>{row.get('title') or row['chat_id']}</b> — {row.get('msg_count', 0)} сообщ.")
        lines.append(f"    <code>{row['chat_id']}</code> · {row.get('last_msg') or '—'}")
    lines.append("")
    lines.append("Написать в чат можно словами: «напиши в чат 123456789, что я опоздаю»")
    return "\n".join(lines)


@router.message(Command("panel"), OwnerOnly())
async def cmd_panel(message: Message) -> None:
    await message.answer(status_text() + "\n\nЛюбую настройку можно менять словами.",
                         reply_markup=panel_keyboard())


@router.callback_query(F.data.startswith("own:"))
async def on_panel_button(query: CallbackQuery, bot: Bot) -> None:
    if not owner_ok(query):
        await query.answer("Не для тебя.", show_alert=True)
        return

    parts = query.data.split(":")
    action = parts[1] if len(parts) > 1 else ""

    if action == "mode" and len(parts) > 2 and parts[2] in MODES:
        config.cfg.set("mode", parts[2])
        await query.answer(f"Режим: {parts[2]}")
        await query.message.edit_text(status_text() + "\n\nЛюбую настройку можно менять словами.",
                                     reply_markup=panel_keyboard())
        return

    if action == "learn":
        if not config.cfg.has_ai:
            await query.answer("Нужен ключ DeepSeek", show_alert=True)
            return
        await query.answer("Учусь…")
        from bot.tools import train_style

        await say(query.message, await train_style(config.cfg))
        return

    if action == "memory":
        await query.answer()
        items = db.memory_items()
        if not items:
            await query.message.answer("🧠 Память пуста. Скажи «запомни: …» — сохраню навсегда.")
            return
        await say(query.message, "🧠 <b>Помню:</b>\n"
                  + "\n".join(f"№{i['id']}: {i['text']}" for i in items)
                  + "\n\nУбрать всё — «забудь всё», одну запись — «забудь №3».")
        return

    if action == "chats":
        await query.answer()
        await say(query.message, chats_text())
        return

    if action == "status":
        await query.answer()
        await say(query.message, status_text())
        return

    if action == "help":
        await query.answer()
        await query.message.answer(HELP)
        return

    await query.answer()


# ------------------------------------------------------- служебные (не в меню)
@router.message(Command("key"), OwnerOnly())
async def cmd_key(message: Message, command: CommandObject) -> None:
    """Ключ ИИ вписываем вручную: секрет не должен проходить через модель."""
    key = (command.args or "").strip()
    if not key:
        await message.answer("Пришли ключ в таком виде: /key sk-xxxxxxxx")
        return
    if not key.startswith("sk-"):
        await message.answer(
            "⚠️ Ключ DeepSeek начинается с <code>sk-</code>.\n"
            "Взять его можно на platform.deepseek.com → API keys.",
        )
        return
    config.cfg.set("deepseek_api_key", key)
    await message.answer("🔑 Ключ сохранён. Можешь просто написать мне «проверь себя».")


@router.message(Command("reload"), OwnerOnly())
async def cmd_reload(message: Message) -> None:
    config.cfg.reload()
    await message.answer("♻️ Перечитал config.json.")


# ------------------------------------------------------ слова вместо команд
async def ask_agent(message: Message, bot: Bot, question: str) -> None:
    """Отправляем просьбу владельца ИИ — он сам решает, какие инструменты вызвать."""
    if not config.cfg.has_ai:
        await message.answer("Мне нужен ключ DeepSeek, чтобы работать: /key sk-...")
        return

    status = await message.answer("⏳ Секунду…")
    messages = [{"role": "system", "content": OWNER_SYSTEM},
                {"role": "user", "content": question}]
    tools = set(OWNER_TOOLS)
    if config.cfg.get("web_search"):
        tools |= OWNER_WEB_TOOLS

    ctx = ToolContext(chat_id=0, bot=bot, owner_chat_id=config.cfg.owner_chat_id, admin=True)
    try:
        answer = await run_agent(config.cfg, messages, ctx, tools=tools)
    except AIError as e:
        answer = f"❌ {e}"
    try:
        await status.delete()
    except Exception:  # noqa: BLE001
        pass
    await say(message, answer or "Не понял, повтори иначе.")


@router.message(OwnerOnly(), F.text)
async def cmd_free_text(message: Message, bot: Bot) -> None:
    """Любой текст владельца — просьба к боту. Старые команды тоже сюда попадают."""
    question = (message.text or "").strip()
    if question.startswith("/"):
        question = question[1:].strip()   # «/mode auto» = просто просьба «mode auto»
    if not question:
        return
    await ask_agent(message, bot, question)


@router.message(F.chat.type == "private", F.text)
async def unknown_guest(message: Message) -> None:
    """Написали не владельцу-боту — вежливо объясняем."""
    if config.cfg.owner_chat_id is None:
        await message.answer("Привет! Если ты владелец этого бота — отправь /start, и я привяжу тебя.")
    else:
        await message.answer("Это личный бот-помощник, он работает только для своего владельца.")
