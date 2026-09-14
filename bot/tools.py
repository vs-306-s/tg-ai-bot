# -*- coding: utf-8 -*-
"""
Инструменты («функции»), которые ИИ может вызывать сам.

Именно они дают боту «всякие возможности»:
  * web_search    — найти свежую информацию в интернете (с источниками);
  * open_page     — прочитать конкретную страницу;
  * chat_history  — посмотреть последние сообщения переписки;
  * save_fact     — запомнить факт о собеседнике;
  * list_facts    — вспомнить, что уже известно о собеседнике;
  * add_note      — добавить заметку владельцу;
  * list_notes    — посмотреть заметки владельца;
  * calc          — посчитать выражение;
  * set_reminder  — напомнить через N минут.
"""
from __future__ import annotations

import ast
import asyncio
import logging
import math
import operator
import re
from dataclasses import dataclass, field

from bot import db
from bot.search import open_page, web_search

log = logging.getLogger("tools")


@dataclass
class ToolContext:
    chat_id: int
    conn_id: str | None = None
    bot: object = None
    chat_title: str | None = None
    contact_name: str | None = None
    owner_chat_id: str | None = None
    enabled: set[str] = field(default_factory=set)

    def allowed(self, name: str) -> bool:
        return not self.enabled or name in self.enabled


# ------------------------------------------------------------------ описание
SCHEMA: dict[str, dict] = {
    "web_search": {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Найти свежую информацию в интернете. Используй, когда нужны факты, "
                           "новости, цены, погода, расписания, свежие события или проверка слуха.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Поисковый запрос"},
                    "count": {"type": "integer", "description": "Сколько результатов (1-8)"},
                },
                "required": ["query"],
            },
        },
    },
    "open_page": {
        "type": "function",
        "function": {
            "name": "open_page",
            "description": "Прочитать текст страницы по ссылке (если в выдаче поиска мало деталей).",
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string", "description": "Адрес страницы"}},
                "required": ["url"],
            },
        },
    },
    "chat_history": {
        "type": "function",
        "function": {
            "name": "chat_history",
            "description": "Показать последние сообщения текущей переписки (например, чтобы вспомнить контекст).",
            "parameters": {
                "type": "object",
                "properties": {"count": {"type": "integer", "description": "Сколько сообщений (1-50)"}},
            },
        },
    },
    "save_fact": {
        "type": "function",
        "function": {
            "name": "save_fact",
            "description": "Запомнить важный факт о собеседнике или договорённость (имя, работа, "
                           "предпочтения, обещания, даты).",
            "parameters": {
                "type": "object",
                "properties": {"fact": {"type": "string", "description": "Что запомнить, одной фразой"}},
                "required": ["fact"],
            },
        },
    },
    "list_facts": {
        "type": "function",
        "function": {
            "name": "list_facts",
            "description": "Посмотреть, что уже известно о собеседнике.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    "add_note": {
        "type": "function",
        "function": {
            "name": "add_note",
            "description": "Добавить заметку владельцу (задача, напоминание о деле, важное сообщение).",
            "parameters": {
                "type": "object",
                "properties": {"note": {"type": "string", "description": "Текст заметки"}},
                "required": ["note"],
            },
        },
    },
    "list_notes": {
        "type": "function",
        "function": {
            "name": "list_notes",
            "description": "Показать открытые заметки владельца.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    "calc": {
        "type": "function",
        "function": {
            "name": "calc",
            "description": "Посчитать математическое выражение точно (например «(1250*3)/7»).",
            "parameters": {
                "type": "object",
                "properties": {"expression": {"type": "string", "description": "Выражение с числами"}},
                "required": ["expression"],
            },
        },
    },
    "list_chats": {
        "type": "function",
        "function": {
            "name": "list_chats",
            "description": "Показать переписки владельца, которые видит бот: название, id, число сообщений, "
                           "режим и время последнего сообщения. Вызывай, когда спрашивают про чаты.",
            "parameters": {
                "type": "object",
                "properties": {"limit": {"type": "integer", "description": "Сколько чатов (1-40)"}},
            },
        },
    },
    "search_messages": {
        "type": "function",
        "function": {
            "name": "search_messages",
            "description": "Найти сообщения во всей переписке владельца по слову или фразе "
                           "(«что писали про встречу», «где упоминали счёт»).",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Что искать"},
                    "limit": {"type": "integer", "description": "Сколько результатов (1-50)"},
                },
                "required": ["query"],
            },
        },
    },
    "chat_digest": {
        "type": "function",
        "function": {
            "name": "chat_digest",
            "description": "Последние сообщения конкретного чата. Нужен chat_id — возьми его из "
                           "list_chats или search_messages. Вызывай перед пересказом переписки.",
            "parameters": {
                "type": "object",
                "properties": {
                    "chat_id": {"type": "integer", "description": "id чата"},
                    "count": {"type": "integer", "description": "Сколько сообщений (1-50)"},
                },
                "required": ["chat_id"],
            },
        },
    },
    "set_reminder": {
        "type": "function",
        "function": {
            "name": "set_reminder",
            "description": "Напомнить в этом чате через N минут (например «напомни про созвон»).",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "О чём напомнить"},
                    "minutes": {"type": "integer", "description": "Через сколько минут (1-1440)"},
                },
                "required": ["text", "minutes"],
            },
        },
    },
    "list_chats": {
        "type": "function",
        "function": {
            "name": "list_chats",
            "description": "Показать переписки владельца, которые видит бот: название, id, число сообщений, "
                           "режим и время последнего сообщения. Вызывай, когда спрашивают про чаты.",
            "parameters": {
                "type": "object",
                "properties": {"limit": {"type": "integer", "description": "Сколько чатов (1-40)"}},
            },
        },
    },
    "search_messages": {
        "type": "function",
        "function": {
            "name": "search_messages",
            "description": "Найти сообщения во всей переписке владельца по слову или фразе "
                           "(«что писали про встречу», «где упоминали счёт»).",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Что искать"},
                    "limit": {"type": "integer", "description": "Сколько результатов (1-50)"},
                },
                "required": ["query"],
            },
        },
    },
    "chat_digest": {
        "type": "function",
        "function": {
            "name": "chat_digest",
            "description": "Последние сообщения конкретного чата. Нужен chat_id — возьми его из "
                           "list_chats или search_messages. Вызывай перед пересказом переписки.",
            "parameters": {
                "type": "object",
                "properties": {
                    "chat_id": {"type": "integer", "description": "id чата"},
                    "count": {"type": "integer", "description": "Сколько сообщений (1-50)"},
                },
                "required": ["chat_id"],
            },
        },
    },
}


def tools_schema(enabled: set[str] | None = None) -> list[dict]:
    names = enabled or set(SCHEMA)
    return [SCHEMA[name] for name in SCHEMA if name in names]


# ------------------------------------------------------------------ калькулятор
_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod, ast.Pow: operator.pow,
}
_FUNCS = {
    "abs": abs, "round": round, "min": min, "max": max,
    "sqrt": math.sqrt, "sin": math.sin, "cos": math.cos, "tan": math.tan,
    "log": math.log, "log10": math.log10, "exp": math.exp,
    "floor": math.floor, "ceil": math.ceil, "factorial": math.factorial,
}
_CONSTS = {"pi": math.pi, "e": math.e, "tau": math.tau}


def _eval_node(node):
    if isinstance(node, ast.Expression):
        return _eval_node(node.body)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)):
            return node.value
        raise ValueError("можно использовать только числа")
    if isinstance(node, ast.BinOp):
        func = _OPS.get(type(node.op))
        if not func:
            raise ValueError("такая операция запрещена")
        left, right = _eval_node(node.left), _eval_node(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > 100:
            raise ValueError("слишком большая степень")
        return func(left, right)
    if isinstance(node, ast.UnaryOp):
        if isinstance(node.op, ast.USub):
            return -_eval_node(node.operand)
        if isinstance(node.op, ast.UAdd):
            return +_eval_node(node.operand)
        raise ValueError("такая операция запрещена")
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _FUNCS:
            raise ValueError("неизвестная функция")
        if node.keywords:
            raise ValueError("именованные аргументы не поддерживаются")
        return _FUNCS[node.func.id](*[_eval_node(a) for a in node.args])
    if isinstance(node, ast.Name):
        if node.id in _CONSTS:
            return _CONSTS[node.id]
        raise ValueError(f"неизвестное имя «{node.id}»")
    raise ValueError("непонятное выражение")


def calculate(expression: str) -> str:
    expression = (expression or "").replace(",", ".").replace("^", "**")
    expression = re.sub(r"[^0-9+\-*/().%\sA-Za-z_]", "", expression)
    if not expression.strip():
        return "Пустое выражение."
    try:
        value = _eval_node(ast.parse(expression, mode="eval"))
    except (ValueError, SyntaxError, ZeroDivisionError, OverflowError) as e:
        return f"Не смог посчитать: {e}"
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return f"{expression.strip()} = {value}"


# ------------------------------------------------------------------ напоминания
_reminder_tasks: set[asyncio.Task] = set()


async def _fire_reminder(ctx: ToolContext, text: str, minutes: int) -> None:
    await asyncio.sleep(max(1, minutes) * 60)
    if not ctx.bot or not ctx.conn_id:
        return
    from bot.business import send_as_owner  # импорт здесь, чтобы не было цикла
    try:
        await send_as_owner(ctx.bot, ctx.chat_id, ctx.conn_id, f"⏰ Напоминание: {text}")
    except Exception as e:  # noqa: BLE001 — напоминание не должно ронять бота
        log.warning("напоминание не отправилось: %s", e)


def _schedule_reminder(ctx: ToolContext, text: str, minutes: int) -> None:
    task = asyncio.create_task(_fire_reminder(ctx, text, minutes))
    _reminder_tasks.add(task)
    task.add_done_callback(_reminder_tasks.discard)


# ---------------------------------------------------------------- исполнитель
async def run_tool(name: str, args: dict, ctx: ToolContext) -> str:
    """Выполняет инструмент и возвращает текст-результат для модели."""
    if not ctx.allowed(name):
        return "Инструмент отключён."

    try:
        if name == "web_search":
            count = max(1, min(int(args.get("count") or 5), 8))
            results = await web_search(str(args.get("query") or ""), count)
            if not results:
                return "Ничего не нашлось. Попробуй переформулировать запрос."
            lines = []
            for i, item in enumerate(results, 1):
                lines.append(f"{i}. {item['title']}\n   {item['url']}\n   {item['snippet']}")
            return "\n".join(lines)

        if name == "open_page":
            return await open_page(str(args.get("url") or ""))

        if name == "chat_history":
            count = max(1, min(int(args.get("count") or 20), 50))
            rows = db.recent(ctx.chat_id, count)
            if not rows:
                return "История пуста."
            return "\n".join(
                f"[{r['created']}] {'Я' if r['is_out'] else (r['user_name'] or 'собеседник')}: {r['text']}"
                for r in rows if r.get("text")
            )

        if name == "save_fact":
            fact = str(args.get("fact") or "").strip()
            if not fact:
                return "Пустой факт, ничего не сохранил."
            db.add_fact(ctx.chat_id, fact)
            return f"Запомнил: {fact}"

        if name == "list_facts":
            known = db.facts(ctx.chat_id)
            return "\n".join(f"— {f}" for f in known) if known else "Пока ничего не известно."

        if name == "add_note":
            note = str(args.get("note") or "").strip()
            if not note:
                return "Пустая заметка."
            note_id = db.add_note(note)
            if ctx.bot and ctx.owner_chat_id:
                try:
                    await ctx.bot.send_message(
                        int(ctx.owner_chat_id),
                        f"📝 Заметка №{note_id} из чата «{ctx.chat_title or ctx.chat_id}»:\n{note}",
                    )
                except Exception as e:  # noqa: BLE001
                    log.info("заметку не удалось переслать владельцу: %s", e)
            return f"Заметка №{note_id} добавлена владельцу."

        if name == "list_notes":
            rows = db.notes()
            if not rows:
                return "Открытых заметок нет."
            return "\n".join(f"№{r['id']}: {r['text']}" for r in rows)

        if name == "calc":
            return calculate(str(args.get("expression") or ""))

        if name == "set_reminder":
            text = str(args.get("text") or "").strip()
            minutes = max(1, min(int(args.get("minutes") or 5), 1440))
            if not text:
                return "Не понял, о чём напомнить."
            if not (ctx.bot and ctx.conn_id):
                return "Напоминания сейчас недоступны."
            _schedule_reminder(ctx, text, minutes)
            return f"Напомню через {minutes} мин: {text}"

        if name == "list_chats":
            limit = max(1, min(int(args.get("limit") or 20), 40))
            rows = db.all_chats(limit)
            if not rows:
                return ("В базе пока нет ни одной переписки. Бот видит только сообщения, пришедшие "
                        "ПОСЛЕ подключения к Telegram Business (Настройки → Telegram Business → "
                        "Чат-боты); историю до подключения Telegram не отдаёт. Если подключение "
                        "есть — нужно, чтобы кто-то написал владельцу первым сообщением.")
            return "\n".join(
                f"{r.get('title') or r['chat_id']} | id={r['chat_id']} | "
                f"сообщений={r.get('msg_count')} | последнее={r.get('last_msg')} | "
                f"режим={r.get('mode') or 'общий'}"
                for r in rows
            )

        if name == "search_messages":
            query = str(args.get("query") or "").strip()
            if not query:
                return "Пустой запрос."
            limit = max(1, min(int(args.get("limit") or 20), 50))
            rows = db.search(query, limit)
            if not rows:
                return f"По запросу «{query}» в переписке ничего не найдено."
            return "\n".join(
                f"[{r['created']}] {r.get('chat_title') or r['chat_id']} | "
                f"{'Я' if r.get('is_out') else (r.get('user_name') or 'собеседник')}: {r.get('text')}"
                for r in rows
            )

        if name == "chat_digest":
            chat_id = int(args.get("chat_id") or 0)
            count = max(1, min(int(args.get("count") or 20), 50))
            rows = db.recent(chat_id, count)
            if not rows:
                return f"В чате {chat_id} нет сохранённых сообщений."
            return "\n".join(
                f"[{r['created']}] {'Я' if r.get('is_out') else (r.get('user_name') or 'собеседник')}: "
                f"{r.get('text') or '[' + (r.get('kind') or 'вложение') + ']'}"
                for r in rows
            )

    except Exception as e:  # noqa: BLE001 — инструмент не должен ронять ответ
        log.exception("ошибка инструмента %s", name)
        return f"Инструмент «{name}» упал: {type(e).__name__}"

    return f"Неизвестный инструмент «{name}»."
