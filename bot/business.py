# -*- coding: utf-8 -*-
"""
Telegram Business: читаем переписку владельца и отвечаем от его лица.

Логика ответа:
    auto    — бот сам отправляет ответ в чат (от имени владельца);
    suggest — бот присылает владельцу черновик с кнопками «отправить / переписать / не надо»;
    off     — бот только читает и записывает сообщения в базу.

Перед ответом бот выжидает короткую паузу («человек набирает текст»), а если
собеседник пишет несколько сообщений подряд — отвечает на них одним ответом.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
from datetime import datetime

from aiogram import Bot, F, Router
from aiogram.enums import ChatAction
from aiogram.types import (BusinessConnection, BusinessMessagesDeleted, CallbackQuery,
                           InlineKeyboardButton, InlineKeyboardMarkup, Message)

from bot import config, db, persona
from bot.ai import AIError, DeepSeek, parse_tool_args
from bot.tools import ToolContext, run_tool, tools_schema

log = logging.getLogger("business")
router = Router()

_pending: dict[int, asyncio.Task] = {}

MEDIA_KINDS = (
    ("photo", "фото"), ("video", "видео"), ("voice", "голосовое"), ("audio", "аудио"),
    ("document", "файл"), ("sticker", "стикер"), ("animation", "гифка"),
    ("video_note", "видеокружок"), ("location", "геолокация"), ("contact", "контакт"),
    ("poll", "опрос"),
)


# ----------------------------------------------------------------- помощники
def person_name(user) -> str:
    if not user:
        return "собеседник"
    parts = [user.first_name or "", user.last_name or ""]
    name = " ".join(p for p in parts if p).strip() or (user.username or "собеседник")
    if user.username:
        name += f" (@{user.username})"
    return name


def message_kind(message: Message) -> str:
    if message.text:
        return "text"
    for attr, label in MEDIA_KINDS:
        if getattr(message, attr, None):
            return label
    return "служебное"


def split_text(text: str, limit: int) -> list[str]:
    """Режем длинный ответ на несколько сообщений по границам абзацев/слов."""
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    parts: list[str] = []
    chunk = ""
    for paragraph in text.split("\n"):
        if len(chunk) + len(paragraph) + 1 <= limit:
            chunk = f"{chunk}\n{paragraph}" if chunk else paragraph
            continue
        if chunk:
            parts.append(chunk.strip())
        while len(paragraph) > limit:
            cut = paragraph.rfind(" ", 0, limit)
            if cut <= 0:
                cut = limit
            parts.append(paragraph[:cut].strip())
            paragraph = paragraph[cut:].lstrip()
        chunk = paragraph
    if chunk:
        parts.append(chunk.strip())
    return [p for p in parts if p]


def in_quiet_hours(cfg) -> bool:
    spec = str(cfg.get("quiet_hours") or "").strip()
    if "-" not in spec:
        return False
    try:
        start_s, end_s = spec.split("-", 1)
        start = datetime.strptime(start_s.strip(), "%H:%M").time()
        end = datetime.strptime(end_s.strip(), "%H:%M").time()
    except ValueError:
        return False
    now = datetime.now().time()
    if start <= end:
        return start <= now < end
    return now >= start or now < end


def chat_mode(chat_id: int) -> str:
    """Режим для конкретного чата: сначала личная настройка, потом общая."""
    chat = db.get_chat(chat_id) or {}
    return (chat.get("mode") or config.cfg.get("mode") or "suggest").lower()


def human_delay(text: str) -> float:
    cfg = config.cfg
    low = float(cfg.get("reply_delay_min") or 2)
    high = max(low, float(cfg.get("reply_delay_max") or 7))
    base = min(len(text or "") * 0.02, high)
    return max(low, base) + random.uniform(0, 1.2)


async def typing(bot: Bot, chat_id: int, conn_id: str | None, seconds: float = 0) -> None:
    kwargs = {"business_connection_id": conn_id} if conn_id else {}
    try:
        await bot.send_chat_action(chat_id, ChatAction.TYPING, **kwargs)
    except Exception as e:  # noqa: BLE001 — «печатает» не критично
        log.debug("не смог показать «печатает»: %s", e)
    if seconds > 0:
        await asyncio.sleep(seconds)


async def send_as_owner(bot: Bot, chat_id: int, conn_id: str | None, text: str,
                        reply_to: int | None = None, by_bot: bool = True) -> bool:
    """Отправляет сообщение в бизнес-чат от имени владельца.

    by_bot=True — текст написал бот (ответ, черновик, напоминание): такое сообщение
    помечается, чтобы бот не учился на своих же фразах.
    by_bot=False — текст написан владельцем (команда /reply).
    """
    if not conn_id:
        log.warning("нет business_connection_id для чата %s — отправить нельзя", chat_id)
        return False
    limit = int(config.cfg.get("max_reply_chars") or 900)
    parts = split_text(text, limit)
    sent_any = False
    for index, part in enumerate(parts):
        if index:
            await asyncio.sleep(random.uniform(0.8, 1.7))
        try:
            await bot.send_message(
                chat_id, part,
                business_connection_id=conn_id,
                reply_to_message_id=reply_to if index == 0 else None,
            )
            sent_any = True
            if by_bot:
                _note_bot_send(chat_id, part)
        except Exception as e:  # noqa: BLE001
            log.warning("не смог отправить в чат %s: %s", chat_id, e)
            break
    return sent_any


# ------------------------------------------------- обучение манере владельца
# Наши собственные отправки: чтобы отличить «написал бот» от «написал владелец»
_recent_sends: dict[int, tuple[str, float]] = {}

STYLE_TRAIN_EVERY = 25          # столько новых пар — и сами обновляем профиль стиля
_style_task: asyncio.Task | None = None


def _note_bot_send(chat_id: int, text: str) -> None:
    _recent_sends[chat_id] = (text.strip(), time.time())


def _was_sent_by_bot(chat_id: int, text: str | None) -> bool:
    if not text:
        return False
    stored = _recent_sends.get(chat_id)
    if not stored:
        return False
    sent, when = stored
    return sent == text.strip() and (time.time() - when) < 120


def remember_style_pair(chat_id: int, reply: str | None) -> None:
    """Запоминаем «что написали → как владелец ответил сам» — материал для обучения."""
    if not reply or not reply.strip():
        return
    db.add_style_pair(chat_id, db.last_incoming(chat_id), reply.strip())


def maybe_schedule_training(bot: Bot) -> None:
    """Каждые N новых пар сами обновляем профиль стиля («тренировка» в фоне)."""
    global _style_task
    if not config.cfg.has_ai:
        return
    try:
        seen = int(db.get_setting("style_pairs_seen", 0) or 0)
    except (TypeError, ValueError):
        seen = 0
    if db.pair_count() - seen < STYLE_TRAIN_EVERY:
        return
    if _style_task and not _style_task.done():
        return
    _style_task = asyncio.create_task(_train_in_background(bot))


async def _train_in_background(bot: Bot) -> None:
    try:
        from bot.tools import train_style

        result = await train_style(config.cfg, count=80)
        await notify_owner(bot, f"🎓 Обучился на твоих ответах.\n\n{result}")
    except Exception:  # noqa: BLE001
        log.exception("фоновая тренировка стиля не удалась")


async def notify_owner(bot: Bot, text: str, keyboard: InlineKeyboardMarkup | None = None) -> None:
    owner = config.cfg.owner_chat_id
    if not owner:
        log.info("владелец не задан, уведомление потеряно: %s", text[:120])
        return
    try:
        await bot.send_message(int(owner), text, reply_markup=keyboard)
    except Exception as e:  # noqa: BLE001
        log.warning("уведомление владельцу не ушло: %s", e)


# ------------------------------------------------------------- лимиты и паузы
def check_limits(chat_id: int) -> tuple[bool, str]:
    cfg = config.cfg
    chat = db.get_chat(chat_id) or {}
    now = time.time()

    if float(chat.get("paused_until") or 0) > now:
        return False, "чат на паузе"

    if now - float(chat.get("last_reply") or 0) < float(cfg.get("min_reply_interval") or 3):
        return False, "слишком часто отвечали"

    minute_start = float(chat.get("minute_start") or 0)
    replies = int(chat.get("replies_minute") or 0)
    if now - minute_start > 60:
        minute_start, replies = now, 0
        db.set_chat(chat_id, minute_start=minute_start, replies_minute=replies)

    if replies >= int(cfg.get("max_replies_per_minute") or 6):
        return False, "лимит ответов в минуту"

    return True, ""


def note_reply(chat_id: int) -> None:
    chat = db.get_chat(chat_id) or {}
    db.set_chat(chat_id, last_reply=time.time(), replies_minute=int(chat.get("replies_minute") or 0) + 1)


# ----------------------------------------------------------------- ИИ-движок
def _tool_set(cfg) -> set[str]:
    if not cfg.get("tools_enabled"):
        return {"__none__"}
    enabled = {"chat_history", "save_fact", "list_facts", "add_note", "list_notes",
               "calc", "set_reminder"}
    if cfg.get("web_search"):
        enabled |= {"web_search", "open_page"}
    return enabled


_TAG_OPEN = chr(60)      # символ «меньше»
_TAG_CLOSE = chr(62)     # символ «больше»

TOOL_MARKUP_RE = re.compile(
    _TAG_OPEN + r"\s*/?\s*(tool_calls?|invoke|function_calls?|parameter|antml)", re.I)
TAG_RE = re.compile(_TAG_OPEN + r"[^" + _TAG_CLOSE + r"]{0,120}" + _TAG_CLOSE)


def looks_like_tool_markup(text: str) -> bool:
    """Модель иногда «рисует» вызов инструмента текстом вместо настоящего вызова."""
    return bool(text) and bool(TOOL_MARKUP_RE.search(text))


def clean_answer(text: str) -> str:
    """Убираем служебную разметку из ответа — такое не должно уходить людям."""
    text = (text or "").strip()
    if not looks_like_tool_markup(text):
        return text
    log.warning("в ответе была служебная разметка, вырезаю: %s", text[:120])
    return re.sub(r"\s{2,}", " ", TAG_RE.sub(" ", text)).strip()


async def run_agent(cfg, messages: list[dict], ctx: ToolContext, rounds: int | None = None,
                    tools: set[str] | None = None) -> str:
    """Прогон модели с инструментами: она сама решает, что вызвать.

    tools можно задать явно — например, в чате владельца нужен доступ к переписке
    и возможность менять настройки бота.
    """
    ai = DeepSeek(cfg)
    enabled = set(tools) if tools else _tool_set(cfg)
    schema = tools_schema(enabled) if enabled != {"__none__"} else None
    if schema and not ai.supports_tools:
        # reasoning-модель не умеет инструменты — для действий берём обычную,
        # иначе бот теряет все способности и просто «рисует» вызовы текстом
        log.info("модель %s без инструментов — для этого запроса беру deepseek-chat", ai.model)
        ai = DeepSeek(cfg, model_override="deepseek-chat")
    ctx.enabled = enabled
    limit = int(rounds if rounds is not None else (cfg.get("max_tool_rounds") or 3))
    nudged = False

    for _ in range(limit + 1):
        message = await ai.chat(messages, tools=schema)
        calls = message.get("tool_calls") or []
        if not calls:
            text = (message.get("content") or "").strip()
            if looks_like_tool_markup(text) and not nudged:
                nudged = True
                log.info("модель ответила разметкой вызова — прошу повторить нормально")
                messages.append({"role": "assistant", "content": text})
                messages.append({
                    "role": "user",
                    "content": "Не пиши служебную разметку в ответе. Либо вызови инструмент "
                               "по-настоящему, либо ответь обычным текстом.",
                })
                continue
            return clean_answer(text)

        messages.append({
            "role": "assistant",
            "content": message.get("content") or "",
            "tool_calls": calls,
        })
        for call in calls:
            name = (call.get("function") or {}).get("name", "")
            args = parse_tool_args((call.get("function") or {}).get("arguments"))
            result = await run_tool(name, args, ctx)
            messages.append({
                "role": "tool",
                "tool_call_id": call.get("id") or name,
                "content": result[:6000],
            })

    final = await ai.chat(messages, tools=None)
    return clean_answer((final.get("content") or "").strip())


async def make_reply(cfg, bot: Bot, *, chat_id: int, conn_id: str | None,
                     chat_title: str | None, contact_name: str | None,
                     extra: str = "") -> str:
    messages = persona.build_messages(
        cfg, chat_id=chat_id, chat_title=chat_title, contact_name=contact_name, extra=extra,
    )
    ctx = ToolContext(chat_id=chat_id, conn_id=conn_id, bot=bot,
                      chat_title=chat_title, contact_name=contact_name,
                      owner_chat_id=cfg.owner_chat_id)
    return await run_agent(cfg, messages, ctx)


# ------------------------------------------------------- обработчики Telegram
@router.business_connection()
async def on_business_connection(connection: BusinessConnection, bot: Bot) -> None:
    user = getattr(connection, "user", None)
    rights = ""
    try:
        rights = connection.rights.model_dump_json() if connection.rights else ""
    except Exception:  # noqa: BLE001
        rights = str(getattr(connection, "rights", ""))

    db.save_connection(
        connection.id,
        getattr(user, "id", None),
        getattr(user, "username", None),
        person_name(user),
        bool(connection.is_enabled),
        rights,
    )

    name = person_name(user)
    if connection.is_enabled:
        await notify_owner(
            bot,
            f"✅ Бот подключён к аккаунту {name}.\n"
            "Теперь я вижу переписку и могу отвечать от его лица.\n"
            f"Текущий режим: {config.cfg.get('mode')}\n"
            "Напиши /panel, чтобы настроить.",
        )
    else:
        await notify_owner(bot, f"⚠️ Бот отключён от аккаунта {name}. Отвечать больше не смогу.")


@router.business_message()
async def on_business_message(message: Message, bot: Bot) -> None:
    conn_id = message.business_connection_id
    chat = message.chat
    cfg = config.cfg

    owner_id = db.connection_owner(conn_id)
    if owner_id is None and cfg.owner_chat_id:
        owner_id = int(cfg.owner_chat_id)
    is_out = bool(message.from_user and owner_id and message.from_user.id == owner_id)

    text = message.text or message.caption
    kind = message_kind(message)
    title = chat.title or person_name(message.from_user)
    chat_existed = bool(db.get_chat(chat.id))

    db.ensure_chat(chat.id, title, chat.username, conn_id)
    by_bot = is_out and _was_sent_by_bot(chat.id, text)
    if by_bot:
        return                      # это наше же сообщение — оно уже записано при отправке

    db.log_message(
        chat.id, text, chat_title=title,
        user_id=getattr(message.from_user, "id", None),
        user_name=person_name(message.from_user),
        username=getattr(message.from_user, "username", None),
        is_out=is_out, kind=kind, tg_id=message.message_id, conn_id=conn_id,
    )

    # Владелец сам написал в чат: закрываем черновик и запоминаем его манеру.
    if is_out:
        remember_style_pair(chat.id, text)
        maybe_schedule_training(bot)
        if db.close_drafts(chat.id, "manual"):
            await notify_owner(bot, f"✍️ Ты сам ответил в «{title}» — черновик убрал.")
        return

    if message.from_user and message.from_user.is_bot:
        return

    # Интересные слова — сразу уведомляем владельца.
    keywords = cfg.get("notify_keywords") or []
    if text and keywords and any(word.lower() in text.lower() for word in keywords):
        await notify_owner(
            bot,
            f"🔔 Ключевое слово в «{title}»:\n{text}\n\nОтветить: /reply {chat.id} <текст>",
        )

    if not chat_existed:
        await notify_owner(bot, f"🆕 Новый чат в переписке: «{title}» (id {chat.id})")

    if not text:
        await notify_owner(bot, f"📎 В «{title}» пришло вложение ({kind}) без текста — посмотри сам.")
        return

    blocked = {str(x) for x in (cfg.get("blocked_chats") or [])}
    if str(chat.id) in blocked:
        return
    allowed = {str(x) for x in (cfg.get("allowed_chats") or [])}
    if allowed and str(chat.id) not in allowed:
        return

    if chat_mode(chat.id) == "off":
        return

    _schedule_reply(bot, chat.id, conn_id, title)


def _schedule_reply(bot: Bot, chat_id: int, conn_id: str, chat_title: str) -> None:
    """Ждём паузу: вдруг собеседник допишет ещё сообщение — ответим один раз."""
    previous = _pending.pop(chat_id, None)
    if previous and not previous.done():
        previous.cancel()

    task = asyncio.create_task(_reply_later(bot, chat_id, conn_id, chat_title))
    _pending[chat_id] = task

    def _cleanup(_finished: asyncio.Task) -> None:
        if _pending.get(chat_id) is _finished:
            _pending.pop(chat_id, None)

    task.add_done_callback(_cleanup)


async def _reply_later(bot: Bot, chat_id: int, conn_id: str, chat_title: str) -> None:
    delay = float(config.cfg.get("debounce_seconds") or 1.6)
    try:
        await asyncio.sleep(delay)
        await compose_and_answer(bot, chat_id, conn_id, chat_title)
    except asyncio.CancelledError:
        raise
    except AIError as e:
        await notify_owner(bot, f"❌ ИИ не ответил в «{chat_title}»: {e}")
    except Exception:  # noqa: BLE001
        log.exception("ошибка при подготовке ответа (чат %s)", chat_id)


async def compose_and_answer(bot: Bot, chat_id: int, conn_id: str, chat_title: str) -> None:
    cfg = config.cfg
    if not cfg.has_ai:
        await notify_owner(
            bot,
            "❌ Не задан ключ DeepSeek — отвечать не могу.\n"
            "Впиши «deepseek_api_key» (ключ вида sk-...) в config.json и напиши /reload.",
        )
        return

    history = db.recent(chat_id, 4)
    contact_name = None
    for row in reversed(history):
        if not row.get("is_out") and row.get("user_name"):
            contact_name = row["user_name"]
            break

    mode = chat_mode(chat_id)
    ok, reason = check_limits(chat_id)
    if mode == "auto" and in_quiet_hours(cfg):
        mode, reason = "suggest", "тихие часы"
    elif mode == "auto" and not ok:
        mode, reason = "suggest", reason

    await typing(bot, chat_id, conn_id, 0.4)
    reply = await make_reply(cfg, bot, chat_id=chat_id, conn_id=conn_id,
                             chat_title=chat_title, contact_name=contact_name)
    if not reply:
        log.info("пустой ответ для чата %s", chat_id)
        return

    if mode == "auto":
        await typing(bot, chat_id, conn_id, human_delay(reply))
        if await send_as_owner(bot, chat_id, conn_id, reply):
            note_reply(chat_id)
            db.log_message(chat_id, reply, chat_title=chat_title, is_out=True,
                           kind="text", conn_id=conn_id, by_bot=True)
        return

    await send_draft(bot, chat_id, conn_id, chat_title, reply, reason)


async def send_draft(bot: Bot, chat_id: int, conn_id: str, chat_title: str,
                     reply: str, reason: str = "") -> None:
    last_in = [r for r in db.recent(chat_id, 6) if not r.get("is_out")]
    incoming = last_in[-1]["text"] if last_in else "—"

    draft_id = db.add_draft(chat_id, conn_id, incoming or "", reply)
    buttons = [
        [InlineKeyboardButton(text="✅ Отправить", callback_data=f"draft:send:{draft_id}")],
        [
            InlineKeyboardButton(text="🔄 Переписать", callback_data=f"draft:regen:{draft_id}"),
            InlineKeyboardButton(text="✂️ Короче", callback_data=f"draft:short:{draft_id}"),
        ],
        [InlineKeyboardButton(text="🚫 Не отвечать", callback_data=f"draft:drop:{draft_id}")],
    ]
    why = f"\n\n<i>Почему черновик: {reason}</i>" if reason else ""
    await notify_owner(
        bot,
        f"💡 Черновик для «{chat_title}»\n\n"
        f"<b>Собеседник:</b> {incoming}\n\n"
        f"<b>Предлагаю ответить:</b>\n{reply}{why}",
        InlineKeyboardMarkup(inline_keyboard=buttons),
    )


# ------------------------------------------------------- кнопки под черновиком
@router.callback_query(F.data.startswith("draft:"))
async def on_draft_button(query: CallbackQuery, bot: Bot) -> None:
    owner = config.cfg.owner_chat_id
    if owner and str(query.message.chat.id) != str(owner):
        await query.answer("Эта кнопка не для тебя.", show_alert=True)
        return

    try:
        _, action, raw_id = query.data.split(":", 2)
        draft_id = int(raw_id)
    except ValueError:
        await query.answer("Кнопка устарела.")
        return

    draft = db.get_draft(draft_id)
    if not draft:
        await query.answer("Черновик не найден.")
        return
    if draft["status"] != "new":
        await query.answer("Уже обработано.")
        return

    chat_id = int(draft["chat_id"])
    conn_id = draft["conn_id"]

    if action == "send":
        await query.answer("Отправляю…")
        ok = await send_as_owner(bot, chat_id, conn_id, draft["draft"])
        if ok:
            db.update_draft(draft_id, status="sent")
            db.log_message(chat_id, draft["draft"], is_out=True, kind="text",
                           conn_id=conn_id, by_bot=True)
            note_reply(chat_id)
            await _edit_owner_message(query, f"✅ Отправил:\n\n{draft['draft']}")
        else:
            await _edit_owner_message(query, "❌ Отправить не удалось — связка с аккаунтом потеряна.")
        return

    if action == "drop":
        db.update_draft(draft_id, status="dropped")
        await query.answer("Не отвечаю.")
        await _edit_owner_message(query, "🚫 Хорошо, не отвечаю.")
        return

    if action in ("regen", "short"):
        await query.answer("Переписываю…")
        extra = ("Перепиши этот ответ иначе: короче и проще, смысл сохрани."
                 if action == "regen" else
                 "Перепиши ответ заметно короче — одним предложением.")
        try:
            new_text = await make_reply(
                config.cfg, bot, chat_id=chat_id, conn_id=conn_id,
                chat_title=None, contact_name=None,
                extra=f"{extra} Предыдущий вариант: «{draft['draft']}»",
            )
        except AIError as e:
            await _edit_owner_message(query, f"❌ {e}")
            return
        if not new_text:
            await _edit_owner_message(query, "❌ ИИ вернул пустой ответ, попробуй ещё раз.")
            return
        db.update_draft(draft_id, draft=new_text)
        await _edit_owner_message(query, f"🔄 Новый вариант:\n\n{new_text}")
        return

    await query.answer()


async def _edit_owner_message(query: CallbackQuery, text: str) -> None:
    try:
        await query.message.edit_text(text, reply_markup=None)
    except Exception as e:  # noqa: BLE001 — сообщение могло устареть
        log.debug("не смог отредактировать сообщение: %s", e)


@router.edited_business_message()
async def on_edited_business_message(message: Message) -> None:
    text = message.text or message.caption
    if text:
        db.log_message(message.chat.id, text, chat_title=message.chat.title,
                       user_name=person_name(message.from_user), kind="правка",
                       tg_id=message.message_id, conn_id=message.business_connection_id)


@router.deleted_business_messages()
async def on_deleted_business_messages(event: BusinessMessagesDeleted) -> None:
    ids = ", ".join(str(i) for i in (event.message_ids or []))
    log.info("удалены сообщения в чате %s: %s", event.chat.id, ids)
