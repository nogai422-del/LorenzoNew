import asyncio
import json
import os
import re
import time
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime, time as dtime

from aiogram import Bot, Dispatcher, F, Router, BaseMiddleware
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import (
    Message, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove,
    InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, BufferedInputFile
)

from members_panel import (
    router as members_router,
    setup_members_panel,
    MembersTrackMiddleware,
)

from db import (
    init_db,
    ensure_chat_settings,
    upsert_message_activity,
    set_joined,
    set_left,
    get_chat_settings,
    list_chat_ids_with_settings,
    record_chat,
)
from inactivity import inactivity_watcher
from admin_activity_panel import router as activity_router, setup_activity_panel

# =========================
# CONFIG
# =========================
from dotenv import load_dotenv
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", "7740055931"))
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN not found. Проверь файл .env (строка BOT_TOKEN=...).")

OWNER_ID_RAW = os.getenv("OWNER_ID", "7740055931")
OWNER_ID = int(OWNER_ID_RAW)

ADMINS_FILE = "admins.json"
SETTINGS_FILE = "settings.json"
LOG_FILE = "bot.log"

# (опционально: диагностика токена, не раскрывает полностью)
print("BOT_TOKEN loaded:", BOT_TOKEN[:6] + "..." + BOT_TOKEN[-4:] if BOT_TOKEN else None)

# =========================
# LOGGING
# =========================
logger = logging.getLogger("botlog")
logger.setLevel(logging.INFO)
if not logger.handlers:
    fh = RotatingFileHandler(LOG_FILE, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(fh)

async def botlog(text: str):
    logger.info(text)

# =========================
# DEFAULTS
# =========================
_DEFAULT_TEXTS = {
    "welcome_text": "Привет, {name}! Добро пожаловать к нам. 😊\nПодскажи, сколько тебе лет?",
    "consent_text": "Отлично! {age} — прекрасный возраст.\n\nЧтобы мы могли добавить тебя в списки и дать доступ, готов(а) заполнить небольшую анкету?",
}

_DEFAULT_SETTINGS = {
    "level": 1,
    "reply_delay": 1,
    "work_start": "07:00",
    "work_end": "19:00",
    "is_active": True,
    "allow_admins_edit": True,
    "notify_admins": [OWNER_ID],
    "texts": dict(_DEFAULT_TEXTS),
}

# =========================
# FILES & DATA
# =========================
def _ensure_files():
    if not os.path.exists(ADMINS_FILE):
        with open(ADMINS_FILE, "w", encoding="utf-8") as f:
            json.dump([OWNER_ID], f, ensure_ascii=False, indent=2)
    if not os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(_DEFAULT_SETTINGS, f, ensure_ascii=False, indent=2)

def load_admins() -> list[int]:
    _ensure_files()
    with open(ADMINS_FILE, "r", encoding="utf-8") as f:
        return [int(x) for x in json.load(f)]

def save_admins(admins: list[int]):
    with open(ADMINS_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(set(admins)), f, ensure_ascii=False, indent=2)

def load_settings() -> dict:
    _ensure_files()
    with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_settings(settings: dict):
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)

def normalize_settings(data: dict) -> dict:
    s = dict(_DEFAULT_SETTINGS)
    s.update(data or {})
    s["level"] = int(s.get("level", 1)) if int(s.get("level", 1)) in (1, 2, 3) else 1
    s["reply_delay"] = max(0, min(360, int(s.get("reply_delay", 0))))
    s["is_active"] = bool(s.get("is_active", True))
    s["allow_admins_edit"] = bool(s.get("allow_admins_edit", True))

    if "notify_admins" not in s or not isinstance(s["notify_admins"], list):
        s["notify_admins"] = [OWNER_ID]
    s["notify_admins"] = [int(x) for x in s["notify_admins"]]

    s["work_start"] = str(s.get("work_start", "07:00"))
    s["work_end"] = str(s.get("work_end", "19:00"))

    if "texts" not in s or not isinstance(s["texts"], dict):
        s["texts"] = dict(_DEFAULT_TEXTS)
    return s

# =========================
# INIT
# =========================
_ensure_files()
init_db()

ADMIN_USER_IDS = load_admins()
if OWNER_ID not in ADMIN_USER_IDS:
    ADMIN_USER_IDS.append(OWNER_ID)
    save_admins(ADMIN_USER_IDS)

settings_data = normalize_settings(load_settings())

print("DEBUG BOT_TOKEN is None?:", BOT_TOKEN is None)
print("DEBUG BOT_TOKEN head:", (BOT_TOKEN or "")[:10])
print("DEBUG BOT_TOKEN tail:", (BOT_TOKEN or "")[-5:])

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()
router = Router()

# =========================
# TRACK ACTIVITY MIDDLEWARE
# =========================
class TrackActivityMiddleware(BaseMiddleware):
    """Фиксирует активность: любое Message от пользователя в group/supergroup."""
    async def __call__(self, handler, event, data):
        if isinstance(event, Message) and event.chat and event.chat.type in ("group", "supergroup"):
            if event.from_user and not event.from_user.is_bot:
                chat_id = int(event.chat.id)
                user_id = int(event.from_user.id)
                user_name = event.from_user.full_name or str(user_id)
                now_ts = int(time.time())

                record_chat(
                    chat_id, event.chat.title, getattr(event.chat, "username", None),
                    event.chat.type, now_ts
                )
                ensure_chat_settings(chat_id)
                upsert_message_activity(
                    chat_id, user_id, user_name, now_ts,
                    message_id=event.message_id,
                    username=event.from_user.username,
                )

        return await handler(event, data)

dp.message.outer_middleware(TrackActivityMiddleware())

# =========================
# STATES
# =========================
class Onboarding(StatesGroup):
    waiting_for_age = State()
    waiting_for_consent = State()

class AdminStates(StatesGroup):
    menu = State()
    edit_active = State()
    edit_level = State()
    edit_work = State()
    edit_delay = State()
    add_admin = State()
    remove_admin = State()
    texts_menu = State()
    edit_text_value = State()
    logs_n = State()

# =========================
# HELPERS
# =========================
def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_USER_IDS


setup_members_panel(is_admin)
dp.message.outer_middleware(MembersTrackMiddleware())

def can_edit_settings(user_id: int) -> bool:
    if user_id == OWNER_ID:
        return True
    return settings_data.get("allow_admins_edit", True)

setup_activity_panel(is_admin, can_edit_settings, lambda: list(ADMIN_USER_IDS), lambda: OWNER_ID, botlog)

def get_notify_admins() -> list[int]:
    return settings_data.get("notify_admins", []) or []

def get_texts() -> dict:
    return settings_data.get("texts", {})

def parse_hhmm(s: str) -> dtime:
    s = (s or "").strip()
    m = re.fullmatch(r"(\d{2}):(\d{2})", s)
    if not m: raise ValueError("Неверный формат HH:MM")
    hh, mm = int(m.group(1)), int(m.group(2))
    if not (0 <= hh <= 23 and 0 <= mm <= 59): raise ValueError("Время вне диапазона")
    return dtime(hh, mm)

def is_time_active_now() -> bool:
    if not settings_data.get("is_active", True):
        return False
    try:
        start = parse_hhmm(settings_data["work_start"])
        end = parse_hhmm(settings_data["work_end"])
    except:
        return False
    now = datetime.now().time()
    if start <= end:
        return start <= now <= end
    return now >= start or now <= end

def level_value() -> int:
    return int(settings_data.get("level", 1))

async def typed_delay():
    d = int(settings_data.get("reply_delay", 0))
    if d > 0:
        await asyncio.sleep(d)

def user_profile_link(user_id: int, full_name: str | None) -> str:
    label = full_name or str(user_id)
    return f'<a href="tg://user?id={user_id}">{label}</a>'

async def build_message_link_safe(message: Message) -> str:
    chat = message.chat
    try:
        if getattr(chat, "username", None):
            return f"https://t.me/{chat.username}/{message.message_id}"
        if isinstance(chat.id, int) and str(chat.id).startswith("-100"):
            return f"https://t.me/c/{abs(chat.id) - 1000000000000}/{message.message_id}"
    except:
        pass
    return "Нет ссылки"

def is_refusal(text: str) -> bool:
    if not text: return False
    refusals = ["не хочу", "не буду", "отказываюсь", "нет", "не согласен", "против", "отказ"]
    return any(p in text.lower() for p in refusals)

async def do_ban(message: Message, reason: str):
    await botlog(f"BAN user_id={message.from_user.id} reason={reason}")
    try: await message.delete()
    except: pass
    try:
        await bot.ban_chat_member(chat_id=message.chat.id, user_id=message.from_user.id)
    except:
        return

    link = user_profile_link(message.from_user.id, message.from_user.full_name)
    msg_link = await build_message_link_safe(message)
    for aid in get_notify_admins():
        try:
            await bot.send_message(aid, f"🚫 <b>Бан</b>\nЮзер: {link}\nПричина: {reason}\nСообщение: {msg_link}")
        except:
            pass

async def maybe_ban_on_suspicious_links(message: Message) -> bool:
    t = (message.text or "") + (message.caption or "")
    t = t.lower()
    if "http://" in t or "https://" in t or "t.me/" in t or bool(re.search(r"(^|\s)@[\w_]{5,32}($|\s)", t)):
        await do_ban(message, "Подозрительные ссылки/@")
        return True
    return False

async def handle_user_failure(message: Message, state: FSMContext, reason: str):
    link = user_profile_link(message.from_user.id, message.from_user.full_name)
    msg_link = await build_message_link_safe(message)
    for aid in get_notify_admins():
        try:
            await bot.send_message(aid, f"⚠️ <b>Прервано</b>\nПричина: {reason}\nЮзер: {link}\nСообщение: {msg_link}")
        except:
            pass
    await state.clear()

async def get_user_display_name(user_id: int) -> str:
    try:
        chat = await bot.get_chat(user_id)
        name = chat.first_name or chat.title or "Unknown"
        if getattr(chat, "last_name", None):
            name += f" {chat.last_name}"
        if getattr(chat, "username", None):
            return f"{name} (@{chat.username})"
        return name
    except Exception:
        return f"ID: {user_id}"

# =========================
# USER FLOW (GROUP ACTIONS)
# =========================
@router.message(F.new_chat_members)
async def welcome_new_member(message: Message):
    # Пишем join в БД для каждого нового
    now_ts = int(time.time())
    chat_id = int(message.chat.id)

    for new_member in message.new_chat_members:
        if new_member.id == bot.id:
            continue

        record_chat(
            chat_id, message.chat.title, getattr(message.chat, "username", None),
            message.chat.type, now_ts
        )
        ensure_chat_settings(chat_id)
        set_joined(
            chat_id, int(new_member.id), new_member.full_name, now_ts,
            username=new_member.username,
        )

        if not is_time_active_now() or level_value() not in (1, 2, 3):
            continue

        await typed_delay()

        welcome_text = get_texts()["welcome_text"].format(
            name=f'<a href="tg://user?id={new_member.id}">{new_member.first_name}</a>'
        )
        user_state = FSMContext(
            storage=dp.storage,
            key=StorageKey(bot_id=bot.id, chat_id=message.chat.id, user_id=new_member.id)
        )
        await user_state.set_state(Onboarding.waiting_for_age)
        await message.reply(welcome_text)

@router.message(F.left_chat_member)
async def left_chat_member_handler(message: Message):
    try:
        chat_id = int(message.chat.id)
        now_ts = int(time.time())
        uid = int(message.left_chat_member.id)
        set_left(chat_id, uid, now_ts)

        # Убираем системное сообщение
        try:
            await bot.delete_message(chat_id=message.chat.id, message_id=message.message_id)
        except:
            pass
    except:
        pass

@router.message(Onboarding.waiting_for_age)
async def process_age_any_text(message: Message, state: FSMContext):
    if not is_time_active_now():
        return await state.clear()
    if await maybe_ban_on_suspicious_links(message):
        return await state.clear()
    if is_refusal(message.text):
        return await handle_user_failure(message, state, "Отказ назвать возраст")

    text = (message.text or "").strip()
    m = re.search(r"\b(\d{1,3})\b", text)
    if not m:
        return

    age = int(m.group(1))
    if age < 18 or age >= 70:
        return await handle_user_failure(message, state, f"Возраст вне диапазона: {age}")

    if level_value() == 1:
        return await state.clear()

    await typed_delay()
    await message.reply(get_texts()["consent_text"].format(age=age))
    await state.set_state(Onboarding.waiting_for_consent)

@router.message(Onboarding.waiting_for_consent)
async def process_consent_any_text(message: Message, state: FSMContext):
    if not is_time_active_now():
        return await state.clear()
    if await maybe_ban_on_suspicious_links(message):
        return await state.clear()

    text = (message.text or "").lower().strip()
    positive_words = {"да", "давай", "ок", "окей", "хочу", "конечно", "готов", "+"}

    if not any(w in text for w in positive_words):
        return await handle_user_failure(message, state, "Отказ от анкеты")

    link = user_profile_link(message.from_user.id, message.from_user.full_name)
    msg_link = await build_message_link_safe(message)

    for aid in get_notify_admins():
        try:
            await bot.send_message(aid, f"✅ <b>Согласие на анкету</b>\nЮзер: {link}\nСообщение: {msg_link}")
        except:
            pass

    await state.clear()

# =========================
# HIDDEN OWNER PANEL
# =========================
def owner_kb():
    allow = settings_data.get("allow_admins_edit", True)
    text = "🔴 Запретить админам настройки" if allow else "🟢 Разрешить админам настройки"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=text, callback_data="owner_toggle_edit")]
    ])

@router.message(Command("owner"))
async def owner_cmd(message: Message):
    if message.from_user.id != OWNER_ID:
        return
    await message.reply(
        "👑 **Панель Владельца**\n\nЗдесь можно запретить обычным админам менять настройки и добавлять других.",
        reply_markup=owner_kb()
    )

@router.callback_query(F.data == "owner_toggle_edit")
async def owner_toggle_edit_cb(call: CallbackQuery):
    if call.from_user.id != OWNER_ID:
        return await call.answer("Отказано", show_alert=True)
    settings_data["allow_admins_edit"] = not settings_data.get("allow_admins_edit", True)
    save_settings(settings_data)
    await call.message.edit_reply_markup(reply_markup=owner_kb())
    await call.answer("Сохранено")

# =========================
# ADMIN PANEL UI (ваш)
# =========================
BTN_ADMIN_PANEL = "🛠️ Админ-панель"
BTN_BACK = "◀️ Назад"
CANCEL_KB = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=BTN_BACK)]], resize_keyboard=True)

def admin_main_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="👥 Админы"), KeyboardButton(text="🔔 Оповещения")],
            [KeyboardButton(text="👥 Участники"), KeyboardButton(text="📊 Активность чатов")],
            [KeyboardButton(text="🟢 Бот ON/OFF"), KeyboardButton(text="🏷️ Уровни 1/2/3")],
            [KeyboardButton(text="🕒 Время работы"), KeyboardButton(text="⏱️ Задержка")],
            [KeyboardButton(text="📝 Тексты"), KeyboardButton(text="🧾 Логи")],
            [KeyboardButton(text="❌ Закрыть панель")],
        ],
        resize_keyboard=True
    )

async def check_edit_rights(message: Message) -> bool:
    if not can_edit_settings(message.from_user.id):
        await message.reply("⛔ Создатель бота временно запретил изменение настроек.")
        return False
    return True

async def show_admin_menu(message: Message, state: FSMContext):
    await state.set_state(AdminStates.menu)
    await message.reply(
        "🛠️ <b>Админ-панель</b>\n\n"
        f"Бот: <code>{'ON 🟢' if settings_data.get('is_active', True) else 'OFF 🔴'}</code>\n"
        f"Уровень: <code>{settings_data.get('level', 1)}</code>\n"
        f"Время: <code>{settings_data.get('work_start')} - {settings_data.get('work_end')}</code>\n"
        f"Задержка: <code>{settings_data.get('reply_delay')} сек.</code>\n",
        reply_markup=admin_main_kb()
    )

@router.message(F.text.in_([BTN_BACK, "❌ Закрыть панель"]))
async def global_admin_back(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    if message.text == "❌ Закрыть панель":
        await state.clear()
        await message.reply("Панель закрыта.", reply_markup=ReplyKeyboardRemove())
    else:
        await show_admin_menu(message, state)

@router.message(Command("admin"))
@router.message(Command("activity"))
@router.message(F.text == BTN_ADMIN_PANEL)
async def admin_open_panel(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return await message.reply("⛔ Нет доступа.")
    await show_admin_menu(message, state)

# =========================
# ДАЛЕЕ ВАШИ ОСТАЛЬНЫЕ АДМИН РАЗДЕЛЫ (как было)
# =========================
@router.message(AdminStates.menu, F.text == "🛠️ Админ-панель")
async def dummy_menu(message: Message):
    pass

@router.message(AdminStates.menu, F.text == "👥 Админы")
async def admin_admins_menu(message: Message, state: FSMContext):
    lines = []
    for aid in ADMIN_USER_IDS:
        name = await get_user_display_name(aid)
        role = " <b>(Создатель)</b>" if aid == OWNER_ID else ""
        lines.append(f"• {name} <code>({aid})</code>{role}")

    kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="➕ Добавить админа"), KeyboardButton(text="➖ Удалить админа")],
            [KeyboardButton(text=BTN_BACK)]
        ],
        resize_keyboard=True
    )
    await message.reply(f"<b>👥 Админы</b>\n\n" + "\n".join(lines), reply_markup=kb)

@router.message(F.text == "➕ Добавить админа")
async def admin_add_prepare(message: Message, state: FSMContext):
    if not await check_edit_rights(message):
        return
    await state.set_state(AdminStates.add_admin)
    await message.reply("Введи ID нового админа (помощника):", reply_markup=CANCEL_KB)

@router.message(AdminStates.add_admin, F.text)
async def admin_add_finish(message: Message, state: FSMContext):
    try:
        aid = int(message.text.strip())
    except:
        return await message.reply("Нужно число.")
    if aid not in ADMIN_USER_IDS:
        ADMIN_USER_IDS.append(aid)
        save_admins(ADMIN_USER_IDS)
        await message.reply("✅ Добавлен.")
    await show_admin_menu(message, state)

@router.message(F.text == "➖ Удалить админа")
async def admin_del_prepare(message: Message, state: FSMContext):
    if not await check_edit_rights(message):
        return
    await state.set_state(AdminStates.remove_admin)
    await message.reply("Введи ID админа для удаления:", reply_markup=CANCEL_KB)

@router.message(AdminStates.remove_admin, F.text)
async def admin_remove_finish(message: Message, state: FSMContext):
    try:
        rid = int(message.text.strip())
    except:
        return await message.reply("Нужно число.")
    if rid == OWNER_ID:
        return await message.reply("❌ Нельзя удалить создателя.")
    if rid in ADMIN_USER_IDS:
        ADMIN_USER_IDS.remove(rid)
        save_admins(ADMIN_USER_IDS)
        await message.reply("✅ Удалён.")
    await show_admin_menu(message, state)

async def build_notify_kb() -> InlineKeyboardMarkup:
    notified = set(get_notify_admins())
    buttons = []
    for aid in ADMIN_USER_IDS:
        status = "🔔" if aid in notified else "🔕"
        name = await get_user_display_name(aid)
        btn_text = f"{status} {name}"
        if len(btn_text) > 40:
            btn_text = btn_text[:37] + "..."
        buttons.append([InlineKeyboardButton(text=btn_text, callback_data=f"notif_{aid}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

@router.message(AdminStates.menu, F.text == "🔔 Оповещения")
async def admin_notify_menu(message: Message, state: FSMContext):
    if not await check_edit_rights(message):
        return
    kb = await build_notify_kb()
    await message.reply("Кликай по кнопкам, чтобы включить 🔔 или выключить 🔕:", reply_markup=kb)

@router.callback_query(F.data.startswith("notif_"))
async def toggle_notif_cb(call: CallbackQuery):
    if not can_edit_settings(call.from_user.id):
        return await call.answer("Отказано", show_alert=True)

    aid = int(call.data.split("_")[1])
    notified = set(get_notify_admins())
    if aid in notified:
        notified.remove(aid)
    else:
        notified.add(aid)

    settings_data["notify_admins"] = list(notified)
    save_settings(settings_data)

    kb = await build_notify_kb()
    await call.message.edit_reply_markup(reply_markup=kb)
    await call.answer()

@router.message(AdminStates.menu, F.text == "🟢 Бот ON/OFF")
async def admin_active_menu(message: Message, state: FSMContext):
    if not await check_edit_rights(message):
        return
    await state.set_state(AdminStates.edit_active)
    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Включить"), KeyboardButton(text="Выключить")], [KeyboardButton(text=BTN_BACK)]],
        resize_keyboard=True
    )
    await message.reply("Управление работой бота:", reply_markup=kb)

@router.message(AdminStates.edit_active, F.text.in_(["Включить", "Выключить"]))
async def admin_active_finish(message: Message, state: FSMContext):
    settings_data["is_active"] = (message.text == "Включить")
    save_settings(settings_data)
    await message.reply(f"✅ Бот {'включен' if settings_data['is_active'] else 'выключен'}.")
    await show_admin_menu(message, state)

@router.message(AdminStates.menu, F.text == "🏷️ Уровни 1/2/3")
async def admin_lvl(message: Message, state: FSMContext):
    if not await check_edit_rights(message):
        return
    await state.set_state(AdminStates.edit_level)
    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="1"), KeyboardButton(text="2"), KeyboardButton(text="3")], [KeyboardButton(text=BTN_BACK)]],
        resize_keyboard=True
    )
    await message.reply("Выбери уровень:", reply_markup=kb)

@router.message(AdminStates.edit_level, F.text.in_(["1", "2", "3"]))
async def admin_lvl_fin(message: Message, state: FSMContext):
    settings_data["level"] = int(message.text)
    save_settings(settings_data)
    await message.reply("✅ Уровень обновлён.")
    await show_admin_menu(message, state)

@router.message(AdminStates.menu, F.text == "🕒 Время работы")
async def admin_work(message: Message, state: FSMContext):
    if not await check_edit_rights(message):
        return
    await state.set_state(AdminStates.edit_work)
    await message.reply("Формат: ЧЧ:ММ-ЧЧ:ММ (напр. 07:00-19:00)", reply_markup=CANCEL_KB)

@router.message(AdminStates.edit_work, F.text)
async def admin_work_fin(message: Message, state: FSMContext):
    try:
        s, e = message.text.split("-")
        re.fullmatch(r"\d{2}:\d{2}", s.strip()); re.fullmatch(r"\d{2}:\d{2}", e.strip())
        parse_hhmm(s); parse_hhmm(e)
        settings_data["work_start"], settings_data["work_end"] = s.strip(), e.strip()
        save_settings(settings_data)
        await message.reply("✅ Сохранено.")
    except:
        await message.reply("❌ Ошибка формата.")
    await show_admin_menu(message, state)

@router.message(AdminStates.menu, F.text == "⏱️ Задержка")
async def admin_del(message: Message, state: FSMContext):
    if not await check_edit_rights(message):
        return
    await state.set_state(AdminStates.edit_delay)
    await message.reply("Задержка (0-360 сек):", reply_markup=CANCEL_KB)

@router.message(AdminStates.edit_delay, F.text)
async def admin_del_fin(message: Message, state: FSMContext):
    try:
        settings_data["reply_delay"] = max(0, min(360, int(message.text)))
    except:
        return await message.reply("❌ Число!")
    save_settings(settings_data)
    await message.reply("✅ Сохранено.")
    await show_admin_menu(message, state)

@router.message(AdminStates.menu, F.text == "📝 Тексты")
async def admin_txt(message: Message, state: FSMContext):
    if not await check_edit_rights(message):
        return
    await state.set_state(AdminStates.texts_menu)
    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="1"), KeyboardButton(text="2")], [KeyboardButton(text=BTN_BACK)]],
        resize_keyboard=True
    )
    await message.reply("Изменить:\n1) Приветствие\n2) Согласие", reply_markup=kb)

@router.message(AdminStates.texts_menu, F.text.in_(["1", "2"]))
async def admin_txt_p(message: Message, state: FSMContext):
    key = "welcome_text" if message.text == "1" else "consent_text"
    await state.update_data(_text_key=key)
    await state.set_state(AdminStates.edit_text_value)
    await message.reply(
        f"Новый текст для <b>{key}</b>:\nТекущий:\n<code>{settings_data['texts'][key]}</code>",
        reply_markup=CANCEL_KB
    )

@router.message(AdminStates.edit_text_value, F.text)
async def admin_txt_fin(message: Message, state: FSMContext):
    data = await state.get_data()
    settings_data["texts"][data["_text_key"]] = message.text
    save_settings(settings_data)
    await message.reply("✅ Текст обновлён.")
    await show_admin_menu(message, state)

@router.message(AdminStates.menu, F.text == "🧾 Логи")
async def admin_logs(message: Message, state: FSMContext):
    await state.set_state(AdminStates.logs_n)
    await message.reply("Сколько строк?", reply_markup=CANCEL_KB)

@router.message(AdminStates.logs_n, F.text)
async def admin_logs_f(message: Message, state: FSMContext):
    try:
        n = max(1, min(300, int(message.text)))
    except:
        return await message.reply("Число 1-300.")

    if not os.path.exists(LOG_FILE):
        text = "Пусто."
    else:
        with open(LOG_FILE, "r", encoding="utf-8") as f:
            text = "\n".join(ln.strip() for ln in f.readlines()[-n:])

    if len(text) > 3900:
        text = text[-3900:]
    await message.reply(f"🧾 <b>Логи:</b>\n\n{text}")
    await show_admin_menu(message, state)

# =========================
# RUN
# =========================
async def main():
    await botlog("BOT START")
    dp.include_router(router)
    dp.include_router(activity_router)
    dp.include_router(members_router)

    try:
        # временно отключаем для диагностики
        # await bot.delete_webhook(drop_pending_updates=True)
        await bot.delete_webhook(drop_pending_updates=True)
    except Exception as e:
        print("delete_webhook failed:", repr(e))

    asyncio.create_task(inactivity_watcher(bot, ADMIN_USER_IDS, botlog, sleep_seconds=60))

    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())

if __name__ == "__main__":
    asyncio.run(main())