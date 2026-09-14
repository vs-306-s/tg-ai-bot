# -*- coding: utf-8 -*-
"""
Управление ботом из личного чата с ним.

Владелец пишет боту команды — бот показывает статус, переключает режимы,
ищет по переписке, отвечает на вопросы (с поиском в интернете) и умеет
писать в любой чат от лица владельца командой /reply.
"""
from __future__ import annotations

import logging
import time

from aiogram import Bot, F, Router
from aiogram.filters import BaseFilter, Command, CommandObject, CommandStart
from aiogram.types import (BufferedInputFile, CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)

from bot import config, db, persona
from bot.ai import AIError
from bot.business import (chat_mode, make_reply, run_agent, send_as_owner)
from bot.tools import ToolContext

log = logging.getLogger("owner")
router = Router()

CHUNK = 3500
MODES = ("auto", "suggest", "off")
MODE_LABEL = {"auto": "🤖 авто-ответы", "suggest": "💡 черновики мне", "off": "🔇 только читать"}

HELP = """🤖 <b>Твой личный ИИ-помощник в Telegram</b>

Я читаю твою переписку (подключён через Telegram Business) и могу отвечать от твоего лица.

<b>Режимы</b>
/mode auto — отвечаю сам
/mode suggest — присылаю черновик с кнопками
/mode off — только читаю и записываю
/mode suggest 123456789 — режим для конкретного чата

<b>Чаты</b>
/chats — список переписок
/chat 123456789 — карточка чата
/pause 123456789 30 — пауза 30 минут
/resume 123456789 — снять паузу
/block 123456789 — игнорировать чат
/allow 123456789 — только эти чаты (пустой список = все)
/reply 123456789 текст — написать от твоего лица

<b>Поиск и вопросы</b>
/find слово — поиск по всей переписке
/ask вопрос — спросить ИИ (с поиском в интернете)
/search запрос — просто поиск в интернете
/sum 123456789 40 — пересказ последних сообщений

<b>Память о людях</b>
/facts 123456789 — что я знаю о собеседнике
/fact 123456789 текст — запомнить
/forget 123456789 — очистить память о нём

<b>Заметки</b>
/note текст · /notes · /done 3

<b>Характер и настройки</b>
/style текст — как писать от твоего имени
/extra текст — дополнительные правила
/quiet 23:00-08:00 — тихие часы (или /quiet off)
/key sk-... — ключ DeepSeek
/model deepseek-chat | deepseek-reasoner
/status · /stats · /panel · /reload

<b>Прочее</b>
/dump 123456789 — выгрузить переписку файлом
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
        await message.answer(text[index:index + CHUNK], reply_markup=keyboard if index == 0 else None)


def _chat_id(raw: str) -> int | None:
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


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
            "🔐 <b>Готово — ты владелец этого бота.</b>\n\n"
            f"Твой chat_id: <code>{message.chat.id}</code> (сохранён в config.json).\n\n" + HELP,
        )
        return

    if str(message.chat.id) != str(owner):
        await message.answer("Привет! Это личный бот-помощник, он работает только для своего владельца.")
        return

    await message.answer(HELP)


# --------------------------------------------------------------------- /panel
def panel_keyboard() -> InlineKeyboardMarkup:
    cfg = config.cfg
    mode = str(cfg.get("mode") or "suggest")

    def mark(value: str, label: str) -> str:
        return ("✅ " if mode == value else "") + label

    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=mark("auto", "🤖 Авто-ответы"), callback_data="own:mode:auto"),
         InlineKeyboardButton(text=mark("suggest", "💡 Черновики"), callback_data="own:mode:suggest"),
         InlineKeyboardButton(text=mark("off", "🔇 Выкл"), callback_data="own:mode:off")],
        [InlineKeyboardButton(text="📊 Статус", callback_data="own:status"),
         InlineKeyboardButton(text="💬 Чаты", callback_data="own:chats")],
        [InlineKeyboardButton(text="📝 Заметки", callback_data="own:notes"),
         InlineKeyboardButton(text="❓ Помощь", callback_data="own:help")],
    ])


@router.message(Command("panel"), OwnerOnly())
async def cmd_panel(message: Message) -> None:
    cfg = config.cfg
    await message.answer(
        "⚙️ <b>Панель управления</b>\n\n"
        f"Режим: {MODE_LABEL.get(str(cfg.get('mode')), cfg.get('mode'))}\n"
        f"ИИ: {'подключён ✅' if cfg.has_ai else 'нет ключа ❌'}\n"
        f"Тихие часы: {cfg.get('quiet_hours') or 'выключены'}\n"
        f"Подключений к аккаунту: {len([c for c in db.connections() if c.get('is_enabled')])}",
        panel_keyboard(),
    )


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
        await query.message.edit_text(f"⚙️ Режим переключён: {MODE_LABEL[parts[2]]}", reply_markup=panel_keyboard())
        return

    if action == "status":
        await query.answer()
        await query.message.answer(await status_text())
        return

    if action == "chats":
        await query.answer()
        await query.message.answer(await chats_text())
        return

    if action == "notes":
        await query.answer()
        await query.message.answer(await notes_text())
        return

    if action == "help":
        await query.answer()
        await query.message.answer(HELP)
        return

    await query.answer()


# -------------------------------------------------------------------- /status
async def status_text() -> str:
    cfg = config.cfg
    stat = db.stats()
    conns = db.connections()
    conn_lines = []
    for c in conns:
        state = "включено ✅" if c.get("is_enabled") else "выключено ⚠️"
        conn_lines.append(f"• {c.get('name') or c.get('username') or c.get('conn_id')} — {state}")
    reply = (
        "📊 <b>Статус</b>\n\n"
        f"Режим: {MODE_LABEL.get(str(cfg.get('mode')), cfg.get('mode'))}\n"
        f"Модель: {cfg.get('deepseek_model')}\n"
        f"Ключ DeepSeek: {'есть ✅' if cfg.has_ai else 'нет ❌'}\n"
        f"Тихие часы: {cfg.get('quiet_hours') or 'выключены'}\n"
        f"Кому отвечаю: {'всем' if not cfg.get('allowed_chats') else ', '.join(map(str, cfg.get('allowed_chats')))}\n"
        f"Игнорирую: {', '.join(map(str, cfg.get('blocked_chats'))) or '—'}\n"
        f"Владелец: <code>{cfg.owner_chat_id}</code>\n"
        f"Прокси: {cfg.proxy or '—'}\n\n"
        f"Сообщений в базе: {stat['total']} (сегодня {stat['today']})\n"
        f"Входящих: {stat['incoming']}, исходящих: {stat['outgoing']}\n"
        f"Чатов: {stat['chats']}, черновиков ждёт: {stat['drafts']}\n\n"
        "Подключения:\n" + ("\n".join(conn_lines) if conn_lines else "— пока нет —")
    )
    return reply


@router.message(Command("status"), OwnerOnly())
async def cmd_status(message: Message) -> None:
    await say(message, await status_text())


@router.message(Command("stats"), OwnerOnly())
async def cmd_stats(message: Message) -> None:
    stat = db.stats()
    top = db.top_chats()
    lines = [
        "📈 <b>Статистика</b>",
        f"Всего сообщений: {stat['total']} (сегодня {stat['today']})",
        f"Входящих {stat['incoming']} / исходящих {stat['outgoing']}",
        f"Чатов: {stat['chats']}",
        "",
        "<b>Самые активные переписки:</b>",
    ]
    for row in top:
        lines.append(f"• {row['chat_title'] or row['chat_id']} — {row['n']} сообщ. (последнее {row['last_msg']})")
    await say(message, "\n".join(lines))


# ---------------------------------------------------------------------- /mode
@router.message(Command("mode"), OwnerOnly())
async def cmd_mode(message: Message, command: CommandObject) -> None:
    args = (command.args or "").split()
    if not args:
        await message.answer(
            f"Сейчас режим: <b>{config.cfg.get('mode')}</b>\n\n"
            "Использование:\n/mode auto — отвечать сам\n/mode suggest — черновики мне\n"
            "/mode off — только читать\n/mode suggest 123456789 — для конкретного чата",
        )
        return

    mode = args[0].lower()
    if mode not in MODES:
        await message.answer("Можно только: auto, suggest или off.")
        return

    if len(args) > 1:
        chat_id = _chat_id(args[1])
        if chat_id is None:
            await message.answer("Не понял id чата. Пример: /mode auto 123456789")
            return
        db.ensure_chat(chat_id)
        db.set_chat(chat_id, mode=mode)
        await message.answer(f"Для чата <code>{chat_id}</code> режим: {MODE_LABEL[mode]}")
        return

    config.cfg.set("mode", mode)
    await message.answer(f"Общий режим: {MODE_LABEL[mode]}")


# --------------------------------------------------------------------- /chats
async def chats_text() -> str:
    rows = db.all_chats(40)
    if not rows:
        return "Пока нет переписок. Подключи бота к аккаунту в настройках Telegram Business."
    lines = ["💬 <b>Переписки</b> (последние сверху)", ""]
    for row in rows:
        mode = chat_mode(int(row["chat_id"]))
        mark = {"auto": "🤖", "suggest": "💡", "off": "🔇"}.get(mode, "💡")
        paused = " ⏸" if float(row.get("paused_until") or 0) > time.time() else ""
        lines.append(
            f"{mark} <b>{row.get('title') or row['chat_id']}</b>{paused} — {row.get('msg_count', 0)} сообщ.\n"
            f"    <code>{row['chat_id']}</code> · {row.get('last_msg') or '—'}"
        )
    lines.append("")
    lines.append("Режим чата: /mode suggest &lt;id&gt; · пауза: /pause &lt;id&gt; 30 · написать: /reply &lt;id&gt; текст")
    return "\n".join(lines)


@router.message(Command("chats"), OwnerOnly())
async def cmd_chats(message: Message) -> None:
    await say(message, await chats_text())


@router.message(Command("chat"), OwnerOnly())
async def cmd_chat(message: Message, command: CommandObject) -> None:
    chat_id = _chat_id(command.args)
    if chat_id is None:
        await message.answer("Использование: /chat 123456789")
        return
    chat = db.get_chat(chat_id)
    if not chat:
        await message.answer("Такого чата в базе нет. Посмотри /chats")
        return
    recent = db.recent(chat_id, 10)
    tail = "\n".join(
        f"{'Я' if r['is_out'] else (r['user_name'] or 'собеседник')}: {r['text'] or '[' + (r['kind'] or 'вложение') + ']'}"
        for r in recent
    )
    known = db.facts(chat_id)
    text = (
        f"👤 <b>{chat.get('title') or chat_id}</b>\n"
        f"id: <code>{chat_id}</code>\n"
        f"Режим: {chat_mode(chat_id)}\n"
        f"Пауза до: {'—' if not float(chat.get('paused_until') or 0) > time.time() else '⏸ активна'}\n"
        f"Заметка: {chat.get('note') or '—'}\n"
        f"Знаю о собеседнике: {'; '.join(known) if known else '—'}\n\n"
        f"<b>Последние сообщения:</b>\n{tail or '—'}"
    )
    await say(message, text)


# --------------------------------------------------------------- паузы/фильтры
@router.message(Command("pause"), OwnerOnly())
async def cmd_pause(message: Message, command: CommandObject) -> None:
    args = (command.args or "").split()
    chat_id = _chat_id(args[0]) if args else None
    if chat_id is None:
        await message.answer("Использование: /pause 123456789 30 (минут)")
        return
    minutes = 30
    if len(args) > 1:
        try:
            minutes = int(args[1])
        except ValueError:
            minutes = 30
    db.ensure_chat(chat_id)
    db.set_chat(chat_id, paused_until=time.time() + max(1, minutes) * 60)
    await message.answer(f"⏸ Чат <code>{chat_id}</code> на паузе {minutes} мин — отвечать не буду.")


@router.message(Command("resume"), OwnerOnly())
async def cmd_resume(message: Message, command: CommandObject) -> None:
    chat_id = _chat_id(command.args)
    if chat_id is None:
        await message.answer("Использование: /resume 123456789")
        return
    db.ensure_chat(chat_id)
    db.set_chat(chat_id, paused_until=0)
    await message.answer(f"▶️ Пауза снята: <code>{chat_id}</code>")


async def _toggle_list(message: Message, key: str, chat_id: int, add: bool) -> None:
    values = [str(x) for x in (config.cfg.get(key) or [])]
    text_id = str(chat_id)
    if add and text_id not in values:
        values.append(text_id)
    if not add and text_id in values:
        values.remove(text_id)
    config.cfg.set(key, values)
    name = {"blocked_chats": "игнор-список", "allowed_chats": "белый список"}[key]
    await message.answer(f"{name}: {', '.join(values) if values else 'пусто'}")


@router.message(Command("block"), OwnerOnly())
async def cmd_block(message: Message, command: CommandObject) -> None:
    chat_id = _chat_id(command.args)
    if chat_id is None:
        await message.answer("Использование: /block 123456789")
        return
    await _toggle_list(message, "blocked_chats", chat_id, True)


@router.message(Command("unblock"), OwnerOnly())
async def cmd_unblock(message: Message, command: CommandObject) -> None:
    chat_id = _chat_id(command.args)
    if chat_id is None:
        await message.answer("Использование: /unblock 123456789")
        return
    await _toggle_list(message, "blocked_chats", chat_id, False)


@router.message(Command("allow"), OwnerOnly())
async def cmd_allow(message: Message, command: CommandObject) -> None:
    raw = (command.args or "").strip()
    if not raw or raw.lower() == "all":
        config.cfg.set("allowed_chats", [])
        await message.answer("Отвечаю во всех чатах.")
        return
    if raw.lower() == "off":
        await message.answer("Подсказка: /allow all — отвечать везде, /allow 123 — только в этом чате.")
        return
    chat_id = _chat_id(raw)
    if chat_id is None:
        await message.answer("Использование: /allow 123456789 или /allow all")
        return
    await _toggle_list(message, "allowed_chats", chat_id, True)


@router.message(Command("reply"), OwnerOnly())
async def cmd_reply(message: Message, command: CommandObject, bot: Bot) -> None:
    args = (command.args or "").split(maxsplit=1)
    chat_id = _chat_id(args[0]) if args else None
    if chat_id is None or len(args) < 2:
        await message.answer("Использование: /reply 123456789 твой текст")
        return
    conn_id = db.last_conn_id(chat_id)
    if not conn_id:
        await message.answer("Не знаю, через какую связку писать в этот чат. Пусть собеседник напишет первым.")
        return
    if await send_as_owner(bot, chat_id, conn_id, args[1]):
        db.close_drafts(chat_id, "manual")
        db.log_message(chat_id, args[1], is_out=True, kind="text", conn_id=conn_id)
        await message.answer(f"✅ Отправил в <code>{chat_id}</code>: {args[1]}")
    else:
        await message.answer("❌ Не получилось отправить.")


# ----------------------------------------------------------- поиск и вопросы
@router.message(Command("find"), OwnerOnly())
async def cmd_find(message: Message, command: CommandObject) -> None:
    query = (command.args or "").strip()
    if not query:
        await message.answer("Использование: /find что искать")
        return
    rows = db.search(query, 40)
    if not rows:
        await message.answer("Ничего не нашёл в переписке.")
        return
    lines = [f"🔍 <b>Нашёл {len(rows)} сообщений по «{query}»:</b>", ""]
    for row in rows:
        who = "Я" if row["is_out"] else (row["user_name"] or "собеседник")
        snippet = (row["text"] or "").replace("<", "&lt;")[:200]
        lines.append(
            f"<b>{row['chat_title'] or row['chat_id']}</b> · {who} · {row['created']}\n{snippet}\n"
            f"<i>чат</i> <code>{row['chat_id']}</code>"
        )
    await say(message, "\n".join(lines))


@router.message(Command("ask"), OwnerOnly())
async def cmd_ask(message: Message, command: CommandObject, bot: Bot) -> None:
    question = (command.args or "").strip()
    if not question:
        await message.answer("Использование: /ask что найти или о чём узнать")
        return
    await ask_agent(message, bot, question)


async def ask_agent(message: Message, bot: Bot, question: str) -> None:
    """Спрашиваем ИИ (он сам решит, искать ли в интернете) и отвечаем владельцу."""
    if not config.cfg.has_ai:
        await message.answer("Сначала впиши ключ DeepSeek: /key sk-...")
        return
    status = await message.answer("🤔 Ищу и думаю…")
    system = (
        "Ты личный помощник владельца. Отвечай по-русски, по делу, коротко (до 10 строк). "
        "Если нужны свежие факты — вызывай инструменты (поиск в интернете, чтение страницы, калькулятор). "
        "Не выдумывай, ссылайся на источники, если искал."
    )
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": question}]
    ctx = ToolContext(chat_id=0, bot=bot, owner_chat_id=config.cfg.owner_chat_id)
    try:
        answer = await run_agent(config.cfg, messages, ctx)
    except AIError as e:
        answer = f"❌ {e}"
    try:
        await status.delete()
    except Exception:  # noqa: BLE001
        pass
    await say(message, answer or "Пустой ответ.")


@router.message(Command("search"), OwnerOnly())
async def cmd_search(message: Message, command: CommandObject) -> None:
    from bot.search import web_search

    query = (command.args or "").strip()
    if not query:
        await message.answer("Использование: /search запрос")
        return
    results = await web_search(query, 5)
    if not results:
        await message.answer("Ничего не нашлось (или нет интернета).")
        return
    lines = [f"🌐 <b>Поиск: {query}</b>", ""]
    for item in results:
        lines.append(f"• <a href=\"{item['url']}\">{item['title']}</a>\n{item['snippet'][:300]}")
    await say(message, "\n".join(lines))


@router.message(Command("sum"), OwnerOnly())
async def cmd_sum(message: Message, command: CommandObject) -> None:
    args = (command.args or "").split()
    chat_id = _chat_id(args[0]) if args else None
    if chat_id is None:
        await message.answer("Использование: /sum 123456789 40")
        return
    limit = int(args[1]) if len(args) > 1 and args[1].isdigit() else 40
    history = persona.history_text(chat_id, limit)
    if not history:
        await message.answer("В этом чате нет сохранённых сообщений.")
        return
    if not config.cfg.has_ai:
        await message.answer("Нужен ключ DeepSeek: /key sk-...")
        return
    from bot.ai import DeepSeek

    ai = DeepSeek(config.cfg)
    try:
        answer = await ai.ask(
            "Ты помощник. Перескажи переписку по-русски: о чём договорились, что важно, "
            "какие задачи остались, что нужно сделать. Кратко, по пунктам, без воды.",
            history,
        )
    except AIError as e:
        answer = f"❌ {e}"
    await say(message, f"🧾 <b>Пересказ чата {chat_id}</b>\n\n{answer}")


# --------------------------------------------------------------------- память
@router.message(Command("facts"), OwnerOnly())
async def cmd_facts(message: Message, command: CommandObject) -> None:
    chat_id = _chat_id(command.args)
    if chat_id is None:
        await message.answer("Использование: /facts 123456789")
        return
    known = db.facts(chat_id)
    await message.answer("🧠 <b>Что я знаю:</b>\n" + ("\n".join(f"— {f}" for f in known) if known else "— пока ничего —"))


@router.message(Command("fact"), OwnerOnly())
async def cmd_fact(message: Message, command: CommandObject) -> None:
    args = (command.args or "").split(maxsplit=1)
    chat_id = _chat_id(args[0]) if args else None
    if chat_id is None or len(args) < 2:
        await message.answer("Использование: /fact 123456789 у него день рождения 3 мая")
        return
    db.add_fact(chat_id, args[1])
    await message.answer("🧠 Запомнил.")


@router.message(Command("forget"), OwnerOnly())
async def cmd_forget(message: Message, command: CommandObject) -> None:
    chat_id = _chat_id(command.args)
    if chat_id is None:
        await message.answer("Использование: /forget 123456789")
        return
    await message.answer(f"🧽 Убрал {db.clear_facts(chat_id)} записей о собеседнике.")


# -------------------------------------------------------------------- заметки
async def notes_text() -> str:
    rows = db.notes()
    if not rows:
        return "📝 Открытых заметок нет."
    return "📝 <b>Заметки</b>\n" + "\n".join(f"№{r['id']} · {r['created']}\n{r['text']}" for r in rows)


@router.message(Command("note"), OwnerOnly())
async def cmd_note(message: Message, command: CommandObject) -> None:
    text = (command.args or "").strip()
    if not text:
        await message.answer("Использование: /note купить билеты")
        return
    await message.answer(f"📝 Заметка №{db.add_note(text)} сохранена.")


@router.message(Command("notes"), OwnerOnly())
async def cmd_notes(message: Message) -> None:
    await say(message, await notes_text())


@router.message(Command("done"), OwnerOnly())
async def cmd_done(message: Message, command: CommandObject) -> None:
    try:
        note_id = int((command.args or "").strip())
    except ValueError:
        await message.answer("Использование: /done 3")
        return
    await message.answer("✅ Готово." if db.close_note(note_id) else "Такой заметки нет.")


# ------------------------------------------------------------- характер, ключи
@router.message(Command("style"), OwnerOnly())
async def cmd_style(message: Message, command: CommandObject) -> None:
    text = (command.args or "").strip()
    if not text:
        await message.answer(f"Текущий стиль:\n{config.cfg.get('persona') or '—'}\n\nИспользование: /style пиши коротко и по-дружески")
        return
    config.cfg.set("persona", text)
    await message.answer("🎭 Запомнил, как писать от твоего имени.")


@router.message(Command("extra"), OwnerOnly())
async def cmd_extra(message: Message, command: CommandObject) -> None:
    text = (command.args or "").strip()
    if not text or text.lower() == "clear":
        config.cfg.set("extra_instructions", "")
        await message.answer("Дополнительные правила очищены.")
        return
    config.cfg.set("extra_instructions", text)
    await message.answer("📌 Дописал правила.")


@router.message(Command("quiet"), OwnerOnly())
async def cmd_quiet(message: Message, command: CommandObject) -> None:
    value = (command.args or "").strip()
    if not value:
        await message.answer(f"Тихие часы: {config.cfg.get('quiet_hours') or 'выключены'}\n\nИспользование: /quiet 23:00-08:00")
        return
    if value.lower() in ("off", "нет", "выкл"):
        config.cfg.set("quiet_hours", None)
        await message.answer("Тихие часы выключены.")
        return
    config.cfg.set("quiet_hours", value)
    await message.answer(f"🌙 Тихие часы: {value}. В это время отвечаю только черновиками.")


@router.message(Command("key"), OwnerOnly())
async def cmd_key(message: Message, command: CommandObject) -> None:
    key = (command.args or "").strip()
    if not key:
        await message.answer("Использование: /key sk-xxxxxxxx (ключ с platform.deepseek.com)")
        return
    if not key.startswith("sk-"):
        await message.answer(
            "⚠️ Ключ DeepSeek начинается с <code>sk-</code>.\n"
            "То, что ты прислал, похоже на токен Telegram-бота — он для ИИ не подойдёт.\n"
            "Ключ берётся на platform.deepseek.com → API keys.",
        )
        return
    config.cfg.set("deepseek_api_key", key)
    await message.answer("🔑 Ключ сохранён в config.json. Проверка: /ask привет")


@router.message(Command("model"), OwnerOnly())
async def cmd_model(message: Message, command: CommandObject) -> None:
    model = (command.args or "").strip()
    if model not in ("deepseek-chat", "deepseek-reasoner"):
        await message.answer(f"Сейчас: {config.cfg.get('deepseek_model')}\n\nМожно: deepseek-chat (с инструментами) "
                             "или deepseek-reasoner (умнее, но без поиска).")
        return
    config.cfg.set("deepseek_model", model)
    await message.answer(f"🧠 Модель: {model}")


@router.message(Command("reload"), OwnerOnly())
async def cmd_reload(message: Message) -> None:
    config.cfg.reload()
    await message.answer("♻️ config.json перечитан.")


# ------------------------------------------------------------------- рассылка
@router.message(Command("dump"), OwnerOnly())
async def cmd_dump(message: Message, command: CommandObject) -> None:
    chat_id = _chat_id(command.args)
    if chat_id is None:
        await message.answer("Использование: /dump 123456789")
        return
    history = persona.history_text(chat_id, 500)
    if not history:
        await message.answer("Пусто.")
        return
    file = BufferedInputFile(history.encode("utf-8"), filename=f"chat_{chat_id}.txt")
    await message.answer_document(file, caption=f"Переписка чата {chat_id}")


@router.message(OwnerOnly(), F.text & ~F.text.startswith("/"))
async def cmd_free_text(message: Message, bot: Bot) -> None:
    """Любой текст владельца в личке = вопрос к ИИ («найди…», «что такое…»)."""
    question = (message.text or "").strip()
    if not question:
        return
    await ask_agent(message, bot, question)
