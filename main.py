import asyncio
import html
import logging
import os
import sqlite3
from datetime import datetime

from aiohttp import web
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    MenuButtonWebApp,
    ReplyKeyboardMarkup,
    WebAppInfo,
)
from dotenv import load_dotenv

from database import DB_NAME, init_db
from miniapp_server import create_miniapp_app


load_dotenv()
logging.basicConfig(level=logging.INFO)
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))


def get_required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"{name} .env faylida ko'rsatilmagan")
    return value


BOT_TOKEN = get_required_env("BOT_TOKEN")
try:
    GROUP_ID = int(get_required_env("GROUP_ID"))
except ValueError as exc:
    raise RuntimeError("GROUP_ID butun son bo'lishi kerak") from exc

TOPIC_IDS = {
    "Chorvoq darvoza": 21,
    "Tsum": 14,
}
WEBAPP_URL = os.getenv("WEBAPP_URL", "").strip()
WEBAPP_HOST = os.getenv("WEBAPP_HOST", "0.0.0.0")
WEBAPP_PORT = int(os.getenv("WEBAPP_PORT", os.getenv("PORT", "8080")))
if WEBAPP_URL and not WEBAPP_URL.startswith("https://"):
    raise RuntimeError("WEBAPP_URL Telegram Mini App uchun HTTPS bo'lishi kerak")


bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def get_user(telegram_id: int):
    with get_db() as conn:
        return conn.execute(
            "SELECT * FROM users WHERE telegram_id = ? AND is_active = 1",
            (telegram_id,),
        ).fetchone()


def is_admin(telegram_id: int) -> bool:
    if telegram_id == ADMIN_ID:
        return True
    with get_db() as conn:
        return conn.execute(
            "SELECT 1 FROM admins WHERE telegram_id = ? AND is_active = 1",
            (telegram_id,),
        ).fetchone() is not None


def get_admin_language(telegram_id: int) -> str:
    with get_db() as conn:
        user = conn.execute(
            "SELECT language FROM users WHERE telegram_id = ?",
            (telegram_id,),
        ).fetchone()
        if user and user["language"] in ADMIN_TEXTS:
            return user["language"]

        admin = conn.execute(
            "SELECT language FROM admins WHERE telegram_id = ? AND is_active = 1",
            (telegram_id,),
        ).fetchone()
    if admin and admin["language"] in ADMIN_TEXTS:
        return admin["language"]
    return "uz"


def get_admin_text(telegram_id: int, key: str, **values) -> str:
    return ADMIN_TEXTS[get_admin_language(telegram_id)][key].format(**values)


def add_admin(telegram_id: int, full_name: str) -> bool:
    if telegram_id == ADMIN_ID:
        return False
    with get_db() as conn:
        cursor = conn.execute(
            """
            INSERT INTO admins (telegram_id, full_name, is_active)
            VALUES (?, ?, 1)
            ON CONFLICT(telegram_id) DO UPDATE SET
                full_name = excluded.full_name,
                is_active = 1
            """,
            (telegram_id, full_name),
        )
        return cursor.rowcount == 1


def set_user_language(telegram_id: int, language: str) -> None:
    with get_db() as conn:
        conn.execute(
            "UPDATE users SET language = ? WHERE telegram_id = ?",
            (language, telegram_id),
        )
        conn.execute(
            "UPDATE admins SET language = ? WHERE telegram_id = ?",
            (language, telegram_id),
        )


def get_object_by_id(object_id: int):
    with get_db() as conn:
        return conn.execute(
            "SELECT * FROM objects WHERE id = ?", (object_id,)
        ).fetchone()


def add_new_object(name: str) -> bool:
    with get_db() as conn:
        try:
            conn.execute("INSERT INTO objects (name) VALUES (?)", (name,))
            return True
        except sqlite3.IntegrityError:
            return False


def object_name_exists(name: str) -> bool:
    with get_db() as conn:
        return conn.execute(
            "SELECT 1 FROM objects WHERE name = ?", (name,)
        ).fetchone() is not None


def save_object_topic(name: str, group_id: int, thread_id: int) -> None:
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO object_topics (object_id, group_id, thread_id)
            SELECT id, ?, ? FROM objects WHERE name = ?
            ON CONFLICT(object_id) DO UPDATE SET
                group_id = excluded.group_id,
                thread_id = excluded.thread_id
            """,
            (group_id, thread_id, name),
        )


def get_object_topic(name: str, group_id: int) -> int | None:
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT ot.thread_id
            FROM object_topics ot
            JOIN objects o ON o.id = ot.object_id
            WHERE o.name = ? AND ot.group_id = ? AND o.is_active = 1
            """,
            (name, group_id),
        ).fetchone()
    if row:
        return row["thread_id"]
    return TOPIC_IDS.get(name)


def get_all_objects(telegram_id: int):
    with get_db() as conn:
        return conn.execute(
            """
            SELECT o.*
            FROM objects o
            JOIN user_objects uo ON uo.object_id = o.id
            JOIN users u ON u.id = uo.user_id
            WHERE o.is_active = 1
              AND u.telegram_id = ?
              AND u.is_active = 1
            ORDER BY o.id
            """,
            (telegram_id,),
        ).fetchall()


def get_active_objects():
    with get_db() as conn:
        return conn.execute(
            "SELECT id, name FROM objects WHERE is_active = 1 ORDER BY id"
        ).fetchall()


def deactivate_object(object_id: int) -> bool:
    with get_db() as conn:
        row = conn.execute(
            "SELECT id FROM objects WHERE id = ? AND is_active = 1",
            (object_id,),
        ).fetchone()
        if row is None:
            return False

        conn.execute(
            "UPDATE objects SET is_active = 0 WHERE id = ?", (object_id,)
        )
        conn.execute(
            "UPDATE users SET current_object_id = NULL WHERE current_object_id = ?",
            (object_id,),
        )
        conn.execute(
            "DELETE FROM user_objects WHERE object_id = ?", (object_id,)
        )
        return True


def get_active_operators(page: int, page_size: int = 8):
    offset = max(page, 0) * page_size
    with get_db() as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM users WHERE is_active = 1"
        ).fetchone()[0]
        operators = conn.execute(
            """
            SELECT telegram_id, full_name
            FROM users
            WHERE is_active = 1
            ORDER BY full_name COLLATE NOCASE, telegram_id
            LIMIT ? OFFSET ?
            """,
            (page_size, offset),
        ).fetchall()
        return operators, total


def deactivate_operator(telegram_id: int) -> bool:
    if is_admin(telegram_id):
        raise ValueError("Admin akkauntini operator sifatida o'chirib bo'lmaydi")

    with get_db() as conn:
        row = conn.execute(
            "SELECT id FROM users WHERE telegram_id = ? AND is_active = 1",
            (telegram_id,),
        ).fetchone()
        if row is None:
            return False

        conn.execute(
            "UPDATE users SET is_active = 0, current_object_id = NULL WHERE id = ?",
            (row["id"],),
        )
        conn.execute("DELETE FROM user_objects WHERE user_id = ?", (row["id"],))
        return True


def save_operator(
    telegram_id: int, full_name: str, object_ids: list[int]
) -> bool:
    selected_object_ids = list(dict.fromkeys(object_ids))
    if not selected_object_ids:
        raise ValueError("Kamida bitta obyekt tanlanishi kerak")

    with get_db() as conn:
        active_object_ids = {
            row["id"]
            for row in conn.execute(
                "SELECT id FROM objects WHERE is_active = 1"
            ).fetchall()
        }
        if not set(selected_object_ids).issubset(active_object_ids):
            raise ValueError("Tanlangan obyektlardan biri mavjud emas")

        user = conn.execute(
            "SELECT id FROM users WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()
        current_object_id = selected_object_ids[0]

        if user:
            user_id = user["id"]
            conn.execute(
                """
                UPDATE users
                SET full_name = ?, current_object_id = ?, is_active = 1
                WHERE id = ?
                """,
                (full_name, current_object_id, user_id),
            )
            conn.execute("DELETE FROM user_objects WHERE user_id = ?", (user_id,))
            created = False
        else:
            cursor = conn.execute(
                """
                INSERT INTO users
                    (telegram_id, full_name, current_object_id, is_active)
                VALUES (?, ?, ?, 1)
                """,
                (telegram_id, full_name, current_object_id),
            )
            user_id = cursor.lastrowid
            created = True

        conn.executemany(
            "INSERT INTO user_objects (user_id, object_id) VALUES (?, ?)",
            ((user_id, object_id) for object_id in selected_object_ids),
        )
        return created


def update_user_object(telegram_id: int, object_id: int) -> None:
    with get_db() as conn:
        conn.execute(
            "UPDATE users SET current_object_id = ? WHERE telegram_id = ?",
            (object_id, telegram_id),
        )


def add_report(
    operator_id: int,
    object_id: int,
    issue_type: str,
    desc: str,
    group_msg_id: int,
) -> int:
    with get_db() as conn:
        cursor = conn.execute(
            """
            INSERT INTO reports (
                operator_id, object_id, issue_type, description,
                group_message_id, status
            )
            VALUES (?, ?, ?, ?, ?, 'OPEN')
            """,
            (operator_id, object_id, issue_type, desc, group_msg_id),
        )
        return cursor.lastrowid


def get_open_reports(operator_id: int):
    with get_db() as conn:
        return conn.execute(
            """
            SELECT r.id, r.issue_type, r.created_at, r.group_message_id,
                   o.name AS object_name
            FROM reports r
            JOIN objects o ON r.object_id = o.id
            WHERE r.operator_id = ? AND r.status = 'OPEN'
            ORDER BY r.id DESC
            """,
            (operator_id,),
        ).fetchall()


def get_report_by_id(report_id: int):
    with get_db() as conn:
        return conn.execute(
            """
            SELECT r.*, o.name AS object_name
            FROM reports r
            JOIN objects o ON o.id = r.object_id
            WHERE r.id = ?
            """,
            (report_id,),
        ).fetchone()


def resolve_report_in_db(report_id: int):
    with get_db() as conn:
        conn.execute(
            """
            UPDATE reports
            SET status = 'RESOLVED', resolved_at = CURRENT_TIMESTAMP
            WHERE id = ? AND status = 'OPEN'
            """,
            (report_id,),
        )
        row = conn.execute(
            "SELECT group_message_id FROM reports WHERE id = ?",
            (report_id,),
        ).fetchone()
        return row["group_message_id"] if row else None


class ReportFSM(StatesGroup):
    choosing_object = State()
    choosing_type = State()
    uploading_media = State()
    writing_comment = State()


class AdminFSM(StatesGroup):
    waiting_object_name = State()
    waiting_admin_id = State()
    waiting_admin_name = State()
    waiting_operator_id = State()
    waiting_operator_name = State()
    selecting_operator_objects = State()


TEXTS = {
    "uz": {
        "no_access": "⛔ Sizda ushbu botdan foydalanish huquqi yo‘q.",
        "choose_lang": "Tilni tanlang / Выберите язык:",
        "lang_set": "Til o‘zbekchaga o‘rnatildi!",
        "new": "➕ Yangi nosozlik",
        "active": "📋 Faol nosozliklarim",
        "change": "🔄 Obyektni o'zgartirish",
        "language": "🌐 Tilni o'zgartirish",
        "select_obj": "Ish joyingizni tanlang:",
        "select_type": "Nosozlik turini tanlang:",
        "send_media": "Nosozlik aks etgan <b>rasm yoki video</b> yuboring:",
        "comment": "Media qabul qilindi! Endi batafsil izoh yozing:",
        "skip": "➡️ Izohsiz davom etish",
        "no_comment": "Izoh qoldirilmadi",
        "report_sent": "✅ Xabar guruhga yetkazildi (Hisobot #{id}).",
        "no_active": "Siz yuborgan ochiq/kutilayotgan nosozliklar yo‘q.",
        "resolved_button": "✅ #{id} - {obj} ({type}) Hal bo'ldi",
        "which_resolved": "Quyidagi nosozliklardan qaysi biri tuzatildi?",
        "resolved": "✅ <b>#{id}</b> raqamli nosozlik yopildi.",
        "resolved_alert": "Nosozlik yopildi!",
    },
    "ru": {
        "no_access": "⛔ У вас нет доступа к данному боту.",
        "choose_lang": "Выберите язык / Tilni tanlang:",
        "lang_set": "Язык изменен на русский!",
        "new": "➕ Новая неисправность",
        "active": "📋 Активные неисправности",
        "change": "🔄 Сменить объект",
        "language": "🌐 Сменить язык",
        "select_obj": "Выберите рабочий объект:",
        "select_type": "Выберите тип неисправности:",
        "send_media": "Отправьте <b>фото или видео</b> с неисправностью:",
        "comment": "Медиа получено! Теперь введите подробный комментарий:",
        "skip": "➡️ Отправить без комментария",
        "no_comment": "Без комментария",
        "report_sent": "✅ Отчет отправлен в группу (Отчет #{id}).",
        "no_active": "У вас нет активных отчетов.",
        "resolved_button": "✅ #{id} - {obj} ({type}) Устранено",
        "which_resolved": "Какая из неисправностей была устранена?",
        "resolved": "✅ Неисправность <b>#{id}</b> закрыта.",
        "resolved_alert": "Неисправность закрыта!",
    },
}

ADMIN_TEXTS = {
    "uz": {
        "panel_title": "Boshqaruv paneli:",
        "panel_objects": "🏢 Obyektlar ro'yxati",
        "panel_operators": "👥 Operatorlar ro'yxati",
        "panel_add_admin": "🛡 Yangi admin qo'shish",
        "object_list": "Obyektlar ro'yxati:",
        "object_list_empty": "Faol obyektlar yo'q.",
        "object_add": "➕ Yangi obyekt qo'shish",
        "back_panel": "⬅️ Admin panel",
        "delete_yes": "Ha, o'chirilsin",
        "cancel": "Bekor qilish",
        "object_confirm": "<b>{name}</b> obyektini faolsizlantirasizmi?",
        "object_confirm_note": "Operatorlardan ruxsat olinadi; reportlar va guruh mavzusi saqlanadi.",
        "object_inactive": "🗑 <b>{name}</b> faolsizlantirildi. Reportlar tarixi va guruh mavzusi saqlandi.",
        "object_not_found": "Faol obyekt topilmadi",
        "invalid_object": "Obyekt tanlovi noto'g'ri",
        "operators_count": "Faol operatorlar: {total}{page}\nOperatorni tanlang:",
        "page": "\nSahifa: {current}/{total}",
        "operator_name_missing": "Ism kiritilmagan",
        "previous": "⬅️ Oldingi",
        "next": "Keyingi ➡️",
        "operator_add": "➕ Operator qo'shish",
        "operator_not_found": "Faol operator topilmadi",
        "operator_objects_none": "Biriktirilgan obyekt yo'q",
        "operator_profile": "👤 <b>{name}</b>\nTelegram ID: <code>{telegram_id}</code>\nObyektlar: {objects}",
        "operator_edit_objects": "🔄 Obyekt ruxsatlarini o'zgartirish",
        "operator_deactivate": "🚫 Operatorni o'chirish",
        "back_operators": "⬅️ Operatorlar ro'yxati",
        "operator_confirm": "<b>{name}</b> operatorni faolsizlantirasizmi?\nHisobotlar tarixi saqlanib qoladi.",
        "operator_inactive": "🚫 <b>{name}</b> operator faolsizlantirildi. Hisobotlar tarixi saqlandi.",
        "all_objects": "✅ Barcha obyektlarni tanlash",
        "clear_selection": "🧹 Tanlovni tozalash",
        "save": "💾 Saqlash",
        "objects_for_operator": "Operatorga beriladigan obyektlarni belgilang:",
        "select_objects": "Operatorga kerakli obyektlarni tanlang:",
        "no_objects_to_assign": "Avval admin paneldan kamida bitta obyekt qo'shing.",
        "select_one_object": "Kamida bitta obyekt tanlang",
        "all_selected": "Barcha obyektlar tanlandi",
        "selection_cleared": "Tanlov tozalandi",
        "operator_id_prompt": "Operatorning Telegram ID raqamini yuboring:",
        "admin_id_prompt": "Yangi adminning Telegram ID raqamini yuboring:",
        "id_digits": "Telegram ID faqat raqamlardan iborat bo'lishi kerak.",
        "id_invalid": "Telegram ID noto'g'ri. Qaytadan yuboring:",
        "operator_name_prompt": "Operatorning ism-familiyasini yuboring:",
        "admin_name_prompt": "Yangi adminning ism-familiyasini yuboring:",
        "name_empty": "Ism-familiya bo'sh bo'lmasligi kerak. Qayta kiriting:",
        "operator_saved": "✅ <b>{name}</b> operator {action}.\nTelegram ID: <code>{telegram_id}</code>\nOperator botga /start yuborishi kerak.",
        "added": "qo'shildi",
        "updated": "yangilandi",
        "admin_saved": "✅ <b>{name}</b> admin qilib qo'shildi.\nTelegram ID: <code>{telegram_id}</code>\nU botga /start yuborib admin paneldan foydalanishi mumkin.",
        "primary_admin_exists": "Bu foydalanuvchi allaqachon asosiy admin.",
        "admin_cannot_remove": "Admin akkauntini o'chirib bo'lmaydi",
        "access_denied": "Ruxsat yo'q",
        "bad_operator": "Operator tanlovi noto'g'ri",
        "object_name_prompt": "Yangi obyekt nomini kiriting (masalan: <i>Yunusobod post</i>):",
        "object_name_empty": "Obyekt nomi bo'sh bo'lmasligi kerak. Qayta kiriting:",
        "object_duplicate": "⚠️ <b>{name}</b> nomli obyekt bazada allaqachon mavjud.",
        "topic_name_long": "Mavzu nomi 128 belgidan oshmasligi kerak.",
        "topic_create_failed": "❌ Guruhda mavzu yaratilmadi. Guruh forum ekanini va botda mavzu yaratish huquqi borligini tekshiring. Obyekt bazaga qo'shilmadi.",
        "object_added": "✅ <b>{name}</b> bazaga qo'shildi va guruhda shu nomli mavzu yaratildi.",
        "bind_topic_usage": "Mavzu ichida /bind_topic obyekt_nomi ko'rinishida yuboring.",
        "bind_topic_missing": "'{name}' nomli obyekt topilmadi.",
        "bind_topic_success": "✅ Ushbu mavzu <b>{name}</b> obyektiga ulandi.",
        "topic_missing_report": "❌ <b>{name}</b> obyektining guruh mavzusi ulanmagan. Admin tegishli mavzuda <code>/bind_topic {name}</code> yuborsin.",
    },
    "ru": {
        "panel_title": "Панель администратора:",
        "panel_objects": "🏢 Список объектов",
        "panel_operators": "👥 Список операторов",
        "panel_add_admin": "🛡 Добавить администратора",
        "object_list": "Список объектов:",
        "object_list_empty": "Активных объектов нет.",
        "object_add": "➕ Добавить объект",
        "back_panel": "⬅️ Панель администратора",
        "delete_yes": "Да, отключить",
        "cancel": "Отмена",
        "object_confirm": "Отключить объект <b>{name}</b>?",
        "object_confirm_note": "Доступ операторов будет отозван; отчеты и тема группы сохранятся.",
        "object_inactive": "🗑 Объект <b>{name}</b> отключен. История отчетов и тема группы сохранены.",
        "object_not_found": "Активный объект не найден",
        "invalid_object": "Некорректный выбор объекта",
        "operators_count": "Активных операторов: {total}{page}\nВыберите оператора:",
        "page": "\nСтраница: {current}/{total}",
        "operator_name_missing": "Имя не указано",
        "previous": "⬅️ Назад",
        "next": "Далее ➡️",
        "operator_add": "➕ Добавить оператора",
        "operator_not_found": "Активный оператор не найден",
        "operator_objects_none": "Объекты не назначены",
        "operator_profile": "👤 <b>{name}</b>\nTelegram ID: <code>{telegram_id}</code>\nОбъекты: {objects}",
        "operator_edit_objects": "🔄 Изменить доступ к объектам",
        "operator_deactivate": "🚫 Отключить оператора",
        "back_operators": "⬅️ Список операторов",
        "operator_confirm": "Отключить оператора <b>{name}</b>?\nИстория отчетов сохранится.",
        "operator_inactive": "🚫 Оператор <b>{name}</b> отключен. История отчетов сохранена.",
        "all_objects": "✅ Выбрать все объекты",
        "clear_selection": "🧹 Снять выбор",
        "save": "💾 Сохранить",
        "objects_for_operator": "Выберите объекты, доступные оператору:",
        "select_objects": "Выберите нужные оператору объекты:",
        "no_objects_to_assign": "Сначала добавьте хотя бы один объект в панели администратора.",
        "select_one_object": "Выберите хотя бы один объект",
        "all_selected": "Выбраны все объекты",
        "selection_cleared": "Выбор очищен",
        "operator_id_prompt": "Отправьте Telegram ID оператора:",
        "admin_id_prompt": "Отправьте Telegram ID нового администратора:",
        "id_digits": "Telegram ID должен содержать только цифры.",
        "id_invalid": "Некорректный Telegram ID. Отправьте еще раз:",
        "operator_name_prompt": "Отправьте имя оператора:",
        "admin_name_prompt": "Отправьте имя нового администратора:",
        "name_empty": "Имя не должно быть пустым. Введите еще раз:",
        "operator_saved": "✅ Оператор <b>{name}</b> {action}.\nTelegram ID: <code>{telegram_id}</code>\nОператору нужно отправить боту /start.",
        "added": "добавлен",
        "updated": "обновлен",
        "admin_saved": "✅ Администратор <b>{name}</b> добавлен.\nTelegram ID: <code>{telegram_id}</code>\nОн может отправить боту /start и пользоваться панелью.",
        "primary_admin_exists": "Этот пользователь уже является главным администратором.",
        "admin_cannot_remove": "Нельзя отключить учетную запись администратора",
        "access_denied": "Нет доступа",
        "bad_operator": "Некорректный выбор оператора",
        "object_name_prompt": "Введите название нового объекта (например: <i>Пост Юнусабад</i>):",
        "object_name_empty": "Название объекта не должно быть пустым. Введите еще раз:",
        "object_duplicate": "⚠️ Объект с названием <b>{name}</b> уже существует.",
        "topic_name_long": "Название темы не должно превышать 128 символов.",
        "topic_create_failed": "❌ Не удалось создать тему группы. Проверьте, что группа является форумом и у бота есть право создавать темы. Объект не добавлен.",
        "object_added": "✅ Объект <b>{name}</b> добавлен, в группе создана тема с таким же названием.",
        "bind_topic_usage": "Отправьте команду внутри темы в формате /bind_topic название_объекта.",
        "bind_topic_missing": "Объект с названием «{name}» не найден.",
        "bind_topic_success": "✅ Эта тема привязана к объекту <b>{name}</b>.",
        "topic_missing_report": "❌ Для объекта <b>{name}</b> не привязана тема группы. Администратор должен отправить <code>/bind_topic {name}</code> внутри нужной темы.",
    },
}

ADMIN_PANEL_BUTTONS = {
    "uz": "⚙️ Admin panel",
    "ru": "⚙️ Панель администратора",
}


GROUP_TEXTS = {
    "new_report": (
        "🚨 <b>НОВАЯ НЕИСПРАВНОСТЬ</b>\n\n"
        "🏢 <b>Объект:</b> {obj}\n"
        "⚠️ <b>Проблема:</b> {type}\n"
        "👤 <b>Оператор:</b> {op}\n"
        "💬 <b>Комментарий:</b> {desc}\n\n"
        "📌 <b>Статус:</b> 🔴 В ожидании..."
    ),
    "resolved": (
        "✅ <b>НЕИСПРАВНОСТЬ УСТРАНЕНА</b>\n\n"
        "Отчет #{id} успешно закрыт.\n"
        "Объект: <b>{obj}</b>\n"
        "Проблема: <b>{type}</b>\n"
        "Закрыл: <b>{op}</b>"
    ),
}


def get_language_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🇺🇿 O'zbekcha", callback_data="lang:uz"),
                InlineKeyboardButton(text="🇷🇺 Русский", callback_data="lang:ru"),
            ]
        ]
    )


def get_text(user, key: str) -> str:
    lang = user["language"] if user and user["language"] in TEXTS else "uz"
    return TEXTS[lang][key]


def get_skip_comment_kb(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=TEXTS[lang]["skip"], callback_data="skip_comment")]
        ]
    )


def get_object_confirmation_kb(lang: str) -> InlineKeyboardMarkup:
    if lang == "ru":
        continue_text = "✅ Продолжить на этом объекте"
        change_text = "🔄 Сменить объект"
    else:
        continue_text = "✅ Shu obyektda davom etish"
        change_text = "🔄 Obyektni o'zgartirish"

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=continue_text,
                    callback_data="confirm_current_obj",
                )
            ],
            [
                InlineKeyboardButton(
                    text=change_text,
                    callback_data="change_obj_menu",
                )
            ],
        ]
    )


def get_objects_list_kb(telegram_id: int) -> InlineKeyboardMarkup:
    buttons = [
        [
            InlineKeyboardButton(
                text=obj["name"], callback_data=f"set_obj:{obj['id']}"
            )
        ]
        for obj in get_all_objects(telegram_id)
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_issue_types_kb(lang: str) -> InlineKeyboardMarkup:
    labels = {
        "uz": ("🚧 Shlagbaum", "📹 Kamera", "💳 Terminal", "🌐 Server/Tarmoq"),
        "ru": ("🚧 Шлагбаум", "📹 Камера", "💳 Терминал", "🌐 Сервер/Сеть"),
    }
    shlagbaum, camera, terminal, server = labels.get(lang, labels["uz"])
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=shlagbaum, callback_data="issue:Shlagbaum"
                ),
                InlineKeyboardButton(
                    text=camera, callback_data="issue:Kamera"
                ),
            ],
            [
                InlineKeyboardButton(
                    text=terminal,
                    callback_data="issue:To'lov terminali",
                ),
                InlineKeyboardButton(
                    text=server,
                    callback_data="issue:Server/Tarmoq",
                ),
            ],
        ]
    )


def get_main_reply_kb(lang: str, user_id: int = None) -> ReplyKeyboardMarkup:
    text = TEXTS[lang]
    buttons = [
        [KeyboardButton(text=text["new"])],
        [
            KeyboardButton(text=text["active"]),
            KeyboardButton(text=text["change"]),
        ],
        [KeyboardButton(text=text["language"])],
    ]
    if user_id is not None and is_admin(user_id):
        buttons.append(
            [KeyboardButton(text=ADMIN_PANEL_BUTTONS.get(lang, ADMIN_PANEL_BUTTONS["uz"]))]
        )
    return ReplyKeyboardMarkup(
        keyboard=buttons,
        resize_keyboard=True,
    )


def get_admin_panel_kb(lang: str) -> InlineKeyboardMarkup:
    text = ADMIN_TEXTS[lang]
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=text["panel_objects"],
                    callback_data="admin_objects",
                )
            ],
            [
                InlineKeyboardButton(
                    text=text["panel_operators"],
                    callback_data="admin_operators:0",
                )
            ],
            [
                InlineKeyboardButton(
                    text=text["panel_add_admin"],
                    callback_data="admin_add_admin",
                )
            ],
        ]
    )


def get_admin_objects_kb(objects, lang: str) -> InlineKeyboardMarkup:
    text = ADMIN_TEXTS[lang]
    buttons = [
        [
            InlineKeyboardButton(
                text=f"🏢 {obj['name'][:48]}",
                callback_data=f"admin_object_delete:{obj['id']}",
            )
        ]
        for obj in objects
    ]
    buttons.append(
        [
            InlineKeyboardButton(
                text=text["object_add"],
                callback_data="admin_add_object",
            )
        ]
    )
    buttons.append(
        [InlineKeyboardButton(text=text["back_panel"], callback_data="admin_home")]
    )
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_object_delete_confirm_kb(
    object_id: int, lang: str
) -> InlineKeyboardMarkup:
    text = ADMIN_TEXTS[lang]
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=text["delete_yes"],
                    callback_data=f"admin_object_deactivate:{object_id}",
                ),
                InlineKeyboardButton(
                    text=text["cancel"], callback_data="admin_objects"
                ),
            ]
        ]
    )


def get_operator_list_kb(
    operators, total: int, page: int, lang: str
) -> InlineKeyboardMarkup:
    text = ADMIN_TEXTS[lang]
    buttons = [
        [
            InlineKeyboardButton(
                text=f"{(operator['full_name'] or text['operator_name_missing'])[:32]} | {operator['telegram_id']}",
                callback_data=(
                    f"admin_operator_view:{operator['telegram_id']}:{page}"
                ),
            )
        ]
        for operator in operators
    ]
    last_page = max(0, (total - 1) // 8)
    navigation = []
    if page > 0:
        navigation.append(
            InlineKeyboardButton(
                text=text["previous"], callback_data=f"admin_operators:{page - 1}"
            )
        )
    if page < last_page:
        navigation.append(
            InlineKeyboardButton(
                text=text["next"], callback_data=f"admin_operators:{page + 1}"
            )
        )
    if navigation:
        buttons.append(navigation)
    buttons.append(
        [
            InlineKeyboardButton(
                text=text["operator_add"],
                callback_data="admin_add_operator",
            )
        ]
    )
    buttons.append(
        [InlineKeyboardButton(text=text["back_panel"], callback_data="admin_home")]
    )
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_operator_manage_kb(
    telegram_id: int, page: int, lang: str
) -> InlineKeyboardMarkup:
    text = ADMIN_TEXTS[lang]
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=text["operator_edit_objects"],
                    callback_data=f"admin_operator_edit:{telegram_id}:{page}",
                )
            ],
            [
                InlineKeyboardButton(
                    text=text["operator_deactivate"],
                    callback_data=f"admin_operator_deactivate:{telegram_id}:{page}",
                )
            ],
            [
                InlineKeyboardButton(
                    text=text["back_operators"],
                    callback_data=f"admin_operators:{page}",
                )
            ],
        ]
    )


def get_operator_remove_confirm_kb(
    telegram_id: int, page: int, lang: str
) -> InlineKeyboardMarkup:
    text = ADMIN_TEXTS[lang]
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=text["delete_yes"],
                    callback_data=f"admin_operator_remove:{telegram_id}:{page}",
                ),
                InlineKeyboardButton(
                    text=text["cancel"],
                    callback_data=f"admin_operator_view:{telegram_id}:{page}",
                ),
            ]
        ]
    )


def get_operator_objects_kb(
    selected_ids: set[int], lang: str
) -> InlineKeyboardMarkup:
    text = ADMIN_TEXTS[lang]
    buttons = [
        [
            InlineKeyboardButton(
                text=f"{'✅' if obj['id'] in selected_ids else '▫️'} {obj['name']}",
                callback_data=f"admin_operator_object:{obj['id']}",
            )
        ]
        for obj in get_active_objects()
    ]
    buttons.append(
        [
            InlineKeyboardButton(
                text=text["all_objects"],
                callback_data="admin_operator_all",
            )
        ]
    )
    buttons.append(
        [
            InlineKeyboardButton(
                text=text["clear_selection"],
                callback_data="admin_operator_clear",
            ),
            InlineKeyboardButton(
                text=text["save"],
                callback_data="admin_operator_save",
            ),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=buttons)


@dp.message(CommandStart())
async def cmd_start(message: types.Message, state: FSMContext) -> None:
    await state.clear()
    user = get_user(message.from_user.id)

    if not user and not is_admin(message.from_user.id):
        await message.answer(
            "⛔ Sizda ushbu botdan foydalanish huquqi yo‘q. "
            "Operatorlar ro'yxatida emassiz."
        )
        return

    await message.answer(
        "Tilni tanlang / Выберите язык:",
        reply_markup=get_language_kb(),
    )


@dp.message(F.chat.id == GROUP_ID, Command("bind_topic"))
async def bind_object_topic(message: types.Message) -> None:
    if not message.from_user or not is_admin(message.from_user.id):
        return

    parts = (message.text or "").split(maxsplit=1)
    lang = get_admin_language(message.from_user.id)
    if len(parts) != 2 or message.message_thread_id is None:
        await message.answer(ADMIN_TEXTS[lang]["bind_topic_usage"])
        return

    object_name = parts[1].strip()
    if not object_name_exists(object_name):
        await message.answer(
            get_admin_text(message.from_user.id, "bind_topic_missing", name=html.escape(object_name))
        )
        return

    save_object_topic(object_name, GROUP_ID, message.message_thread_id)
    await message.answer(
        get_admin_text(
            message.from_user.id, "bind_topic_success", name=html.escape(object_name)
        ),
        parse_mode="HTML",
    )


@dp.message(F.chat.id == GROUP_ID)
async def log_group_topic(message: types.Message) -> None:
    logging.info(
        "GROUP_TOPIC chat_id=%s thread_id=%s text=%r",
        message.chat.id,
        message.message_thread_id,
        message.text,
    )


@dp.message(F.text.in_(list(ADMIN_PANEL_BUTTONS.values())))
async def admin_panel_menu(message: types.Message) -> None:
    if not is_admin(message.from_user.id):
        return

    lang = get_admin_language(message.from_user.id)
    await message.answer(
        get_admin_text(message.from_user.id, "panel_title"),
        reply_markup=get_admin_panel_kb(lang),
    )


@dp.callback_query(F.data == "admin_home")
async def return_to_admin_panel(callback: types.CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer(get_admin_text(callback.from_user.id, "access_denied"), show_alert=True)
        return

    lang = get_admin_language(callback.from_user.id)
    await callback.message.edit_text(
        get_admin_text(callback.from_user.id, "panel_title"),
        reply_markup=get_admin_panel_kb(lang),
    )
    await callback.answer()


@dp.callback_query(F.data == "admin_objects")
async def show_admin_objects(callback: types.CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer(get_admin_text(callback.from_user.id, "access_denied"), show_alert=True)
        return

    lang = get_admin_language(callback.from_user.id)
    objects = get_active_objects()
    text = get_admin_text(callback.from_user.id, "object_list")
    if not objects:
        text = get_admin_text(callback.from_user.id, "object_list_empty")
    await callback.message.edit_text(
        text, reply_markup=get_admin_objects_kb(objects, lang)
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("admin_object_delete:"))
async def confirm_object_deactivation(callback: types.CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer(get_admin_text(callback.from_user.id, "access_denied"), show_alert=True)
        return

    try:
        object_id = int(callback.data.split(":", 1)[1])
    except (ValueError, AttributeError):
        await callback.answer(get_admin_text(callback.from_user.id, "invalid_object"), show_alert=True)
        return

    obj = get_object_by_id(object_id)
    if obj is None or not obj["is_active"]:
        await callback.answer(get_admin_text(callback.from_user.id, "object_not_found"), show_alert=True)
        return

    lang = get_admin_language(callback.from_user.id)
    await callback.message.edit_text(
        get_admin_text(callback.from_user.id, "object_confirm", name=html.escape(obj["name"]))
        + "\n"
        + ADMIN_TEXTS[lang]["object_confirm_note"],
        parse_mode="HTML",
        reply_markup=get_object_delete_confirm_kb(object_id, lang),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("admin_object_deactivate:"))
async def deactivate_object_from_admin(callback: types.CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer(get_admin_text(callback.from_user.id, "access_denied"), show_alert=True)
        return

    try:
        object_id = int(callback.data.split(":", 1)[1])
    except (ValueError, AttributeError):
        await callback.answer(get_admin_text(callback.from_user.id, "invalid_object"), show_alert=True)
        return

    obj = get_object_by_id(object_id)
    if obj is None or not deactivate_object(object_id):
        await callback.answer(get_admin_text(callback.from_user.id, "object_not_found"), show_alert=True)
        return

    lang = get_admin_language(callback.from_user.id)
    await callback.message.edit_text(
        get_admin_text(callback.from_user.id, "object_inactive", name=html.escape(obj["name"])),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=ADMIN_TEXTS[lang]["object_list"],
                        callback_data="admin_objects",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=ADMIN_TEXTS[lang]["back_panel"], callback_data="admin_home"
                    )
                ],
            ]
        ),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("admin_operators:"))
async def show_operators_list(callback: types.CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer(get_admin_text(callback.from_user.id, "access_denied"), show_alert=True)
        return

    try:
        page = max(0, int(callback.data.split(":", 1)[1]))
    except (ValueError, AttributeError):
        page = 0

    operators, total = get_active_operators(page)
    last_page = max(0, (total - 1) // 8)
    clamped_page = min(page, last_page)
    if clamped_page != page:
        page = clamped_page
        operators, total = get_active_operators(page)

    lang = get_admin_language(callback.from_user.id)
    page_text = (
        get_admin_text(
            callback.from_user.id,
            "page",
            current=page + 1,
            total=last_page + 1,
        )
        if total
        else ""
    )
    await callback.message.edit_text(
        get_admin_text(
            callback.from_user.id,
            "operators_count",
            total=total,
            page=page_text,
        ),
        reply_markup=get_operator_list_kb(operators, total, page, lang),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("admin_operator_view:"))
async def show_operator_details(callback: types.CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer(get_admin_text(callback.from_user.id, "access_denied"), show_alert=True)
        return

    try:
        _, raw_telegram_id, raw_page = callback.data.split(":")
        telegram_id = int(raw_telegram_id)
        page = max(0, int(raw_page))
    except (ValueError, AttributeError):
        await callback.answer(get_admin_text(callback.from_user.id, "bad_operator"), show_alert=True)
        return

    operator = get_user(telegram_id)
    if operator is None:
        await callback.answer(get_admin_text(callback.from_user.id, "operator_not_found"), show_alert=True)
        return

    objects = get_all_objects(telegram_id)
    object_names = ", ".join(html.escape(obj["name"]) for obj in objects)
    if not object_names:
        object_names = get_admin_text(callback.from_user.id, "operator_objects_none")
    safe_name = html.escape(
        operator["full_name"]
        or get_admin_text(callback.from_user.id, "operator_name_missing")
    )
    lang = get_admin_language(callback.from_user.id)
    await callback.message.edit_text(
        get_admin_text(
            callback.from_user.id,
            "operator_profile",
            name=safe_name,
            telegram_id=telegram_id,
            objects=object_names,
        ),
        parse_mode="HTML",
        reply_markup=get_operator_manage_kb(telegram_id, page, lang),
    )
    await callback.answer()


@dp.callback_query(F.data == "admin_add_object")
async def start_add_object(
    callback: types.CallbackQuery, state: FSMContext
) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer(get_admin_text(callback.from_user.id, "access_denied"), show_alert=True)
        return

    await callback.message.edit_text(
        get_admin_text(callback.from_user.id, "object_name_prompt"),
        parse_mode="HTML",
    )
    await state.set_state(AdminFSM.waiting_object_name)
    await callback.answer()


@dp.message(AdminFSM.waiting_object_name, F.text)
async def process_save_object(
    message: types.Message, state: FSMContext
) -> None:
    if not is_admin(message.from_user.id):
        await state.clear()
        return

    obj_name = message.text.strip()
    if not obj_name:
        await message.answer(get_admin_text(message.from_user.id, "object_name_empty"))
        return

    safe_obj_name = html.escape(obj_name)
    if object_name_exists(obj_name):
        await message.answer(
            get_admin_text(message.from_user.id, "object_duplicate", name=safe_obj_name),
            parse_mode="HTML",
        )
        await state.clear()
        return

    if len(obj_name) > 128:
        await message.answer(get_admin_text(message.from_user.id, "topic_name_long"))
        return

    try:
        topic = await bot.create_forum_topic(chat_id=GROUP_ID, name=obj_name)
    except Exception:
        logging.exception("Obyekt uchun guruh mavzusini yaratib bo'lmadi")
        await message.answer(
            get_admin_text(message.from_user.id, "topic_create_failed")
        )
        return

    if not add_new_object(obj_name):
        await message.answer(
            get_admin_text(message.from_user.id, "object_duplicate", name=safe_obj_name),
            parse_mode="HTML",
        )
        await state.clear()
        return

    save_object_topic(obj_name, GROUP_ID, topic.message_thread_id)
    await message.answer(
        get_admin_text(message.from_user.id, "object_added", name=safe_obj_name),
        parse_mode="HTML",
    )

    await state.clear()


@dp.callback_query(F.data == "admin_add_admin")
async def start_add_admin(
    callback: types.CallbackQuery, state: FSMContext
) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer(get_admin_text(callback.from_user.id, "access_denied"), show_alert=True)
        return

    await state.clear()
    await state.set_state(AdminFSM.waiting_admin_id)
    await callback.message.answer(
        get_admin_text(callback.from_user.id, "admin_id_prompt")
    )
    await callback.answer()


@dp.message(AdminFSM.waiting_admin_id, F.text)
async def process_admin_id(message: types.Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        await state.clear()
        return

    raw_telegram_id = message.text.strip()
    if not raw_telegram_id.isdecimal():
        await message.answer(get_admin_text(message.from_user.id, "id_digits"))
        return

    telegram_id = int(raw_telegram_id)
    if telegram_id <= 0 or telegram_id > 9_223_372_036_854_775_807:
        await message.answer(get_admin_text(message.from_user.id, "id_invalid"))
        return

    await state.update_data(new_admin_telegram_id=telegram_id)
    await state.set_state(AdminFSM.waiting_admin_name)
    await message.answer(get_admin_text(message.from_user.id, "admin_name_prompt"))


@dp.message(AdminFSM.waiting_admin_name, F.text)
async def process_admin_name(message: types.Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        await state.clear()
        return

    full_name = message.text.strip()
    if not full_name:
        await message.answer(get_admin_text(message.from_user.id, "name_empty"))
        return

    data = await state.get_data()
    telegram_id = data["new_admin_telegram_id"]
    if add_admin(telegram_id, full_name):
        await message.answer(
            get_admin_text(
                message.from_user.id,
                "admin_saved",
                name=html.escape(full_name),
                telegram_id=telegram_id,
            ),
            parse_mode="HTML",
        )
    else:
        await message.answer(
            get_admin_text(message.from_user.id, "primary_admin_exists")
        )
    await state.clear()


@dp.callback_query(F.data == "admin_add_operator")
async def start_add_operator(
    callback: types.CallbackQuery, state: FSMContext
) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer(get_admin_text(callback.from_user.id, "access_denied"), show_alert=True)
        return

    await state.clear()
    await state.set_state(AdminFSM.waiting_operator_id)
    await callback.message.answer(
        get_admin_text(callback.from_user.id, "operator_id_prompt")
    )
    await callback.answer()


@dp.message(AdminFSM.waiting_operator_id, F.text)
async def process_operator_id(
    message: types.Message, state: FSMContext
) -> None:
    if not is_admin(message.from_user.id):
        await state.clear()
        return

    raw_telegram_id = message.text.strip()
    if not raw_telegram_id.isdecimal():
        await message.answer(get_admin_text(message.from_user.id, "id_digits"))
        return

    telegram_id = int(raw_telegram_id)
    if telegram_id <= 0 or telegram_id > 9_223_372_036_854_775_807:
        await message.answer(get_admin_text(message.from_user.id, "id_invalid"))
        return

    await state.update_data(operator_telegram_id=telegram_id)
    await state.set_state(AdminFSM.waiting_operator_name)
    await message.answer(get_admin_text(message.from_user.id, "operator_name_prompt"))


@dp.message(AdminFSM.waiting_operator_name, F.text)
async def process_operator_name(
    message: types.Message, state: FSMContext
) -> None:
    if not is_admin(message.from_user.id):
        await state.clear()
        return

    full_name = message.text.strip()
    if not full_name:
        await message.answer(get_admin_text(message.from_user.id, "name_empty"))
        return

    objects = get_active_objects()
    if not objects:
        await message.answer(
            get_admin_text(message.from_user.id, "no_objects_to_assign")
        )
        await state.clear()
        return

    await state.update_data(
        operator_full_name=full_name,
        selected_object_ids=[],
        admin_language=get_admin_language(message.from_user.id),
    )
    await state.set_state(AdminFSM.selecting_operator_objects)
    await message.answer(
        get_admin_text(message.from_user.id, "select_objects"),
        reply_markup=get_operator_objects_kb(
            set(), get_admin_language(message.from_user.id)
        ),
    )


@dp.callback_query(
    AdminFSM.selecting_operator_objects,
    F.data.startswith("admin_operator_object:"),
)
async def toggle_operator_object(
    callback: types.CallbackQuery, state: FSMContext
) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer(get_admin_text(callback.from_user.id, "access_denied"), show_alert=True)
        return

    try:
        object_id = int(callback.data.split(":", 1)[1])
    except (ValueError, AttributeError):
        await callback.answer(get_admin_text(callback.from_user.id, "invalid_object"), show_alert=True)
        return

    if object_id not in {obj["id"] for obj in get_active_objects()}:
        await callback.answer(get_admin_text(callback.from_user.id, "object_not_found"), show_alert=True)
        return

    data = await state.get_data()
    selected_ids = set(data.get("selected_object_ids", []))
    lang = data.get("admin_language", get_admin_language(callback.from_user.id))
    if object_id in selected_ids:
        selected_ids.remove(object_id)
    else:
        selected_ids.add(object_id)

    await state.update_data(selected_object_ids=sorted(selected_ids))
    await callback.message.edit_reply_markup(
        reply_markup=get_operator_objects_kb(selected_ids, lang)
    )
    await callback.answer()


@dp.callback_query(
    AdminFSM.selecting_operator_objects, F.data == "admin_operator_all"
)
async def select_all_operator_objects(
    callback: types.CallbackQuery, state: FSMContext
) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer(get_admin_text(callback.from_user.id, "access_denied"), show_alert=True)
        return

    selected_ids = {obj["id"] for obj in get_active_objects()}
    data = await state.get_data()
    lang = data.get("admin_language", get_admin_language(callback.from_user.id))
    await state.update_data(selected_object_ids=sorted(selected_ids))
    await callback.message.edit_reply_markup(
        reply_markup=get_operator_objects_kb(selected_ids, lang)
    )
    await callback.answer(ADMIN_TEXTS[lang]["all_selected"])


@dp.callback_query(
    AdminFSM.selecting_operator_objects, F.data == "admin_operator_clear"
)
async def clear_operator_objects(
    callback: types.CallbackQuery, state: FSMContext
) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer(get_admin_text(callback.from_user.id, "access_denied"), show_alert=True)
        return

    data = await state.get_data()
    lang = data.get("admin_language", get_admin_language(callback.from_user.id))
    await state.update_data(selected_object_ids=[])
    await callback.message.edit_reply_markup(
        reply_markup=get_operator_objects_kb(set(), lang)
    )
    await callback.answer(ADMIN_TEXTS[lang]["selection_cleared"])


@dp.callback_query(F.data.startswith("admin_operator_edit:"))
async def edit_operator_access(
    callback: types.CallbackQuery, state: FSMContext
) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer(get_admin_text(callback.from_user.id, "access_denied"), show_alert=True)
        return

    try:
        _, raw_telegram_id, raw_page = callback.data.split(":")
        telegram_id = int(raw_telegram_id)
        page = max(0, int(raw_page))
    except (ValueError, AttributeError):
        await callback.answer(get_admin_text(callback.from_user.id, "bad_operator"), show_alert=True)
        return

    operator = get_user(telegram_id)
    if operator is None:
        await callback.answer(get_admin_text(callback.from_user.id, "operator_not_found"), show_alert=True)
        return

    selected_ids = [obj["id"] for obj in get_all_objects(telegram_id)]
    lang = get_admin_language(callback.from_user.id)
    await state.clear()
    await state.update_data(
        operator_telegram_id=telegram_id,
        operator_full_name=operator["full_name"] or ADMIN_TEXTS[lang]["operator_name_missing"],
        selected_object_ids=selected_ids,
        return_operator_page=page,
        admin_language=lang,
    )
    await state.set_state(AdminFSM.selecting_operator_objects)
    await callback.message.edit_text(
        ADMIN_TEXTS[lang]["objects_for_operator"],
        reply_markup=get_operator_objects_kb(set(selected_ids), lang),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("admin_operator_deactivate:"))
async def confirm_operator_deactivation(
    callback: types.CallbackQuery,
) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer(get_admin_text(callback.from_user.id, "access_denied"), show_alert=True)
        return

    try:
        _, raw_telegram_id, raw_page = callback.data.split(":")
        telegram_id = int(raw_telegram_id)
        page = max(0, int(raw_page))
    except (ValueError, AttributeError):
        await callback.answer(get_admin_text(callback.from_user.id, "bad_operator"), show_alert=True)
        return

    if is_admin(telegram_id):
        await callback.answer(get_admin_text(callback.from_user.id, "admin_cannot_remove"), show_alert=True)
        return

    operator = get_user(telegram_id)
    if operator is None:
        await callback.answer(get_admin_text(callback.from_user.id, "operator_not_found"), show_alert=True)
        return

    lang = get_admin_language(callback.from_user.id)
    safe_name = html.escape(
        operator["full_name"] or ADMIN_TEXTS[lang]["operator_name_missing"]
    )
    await callback.message.edit_text(
        get_admin_text(callback.from_user.id, "operator_confirm", name=safe_name),
        parse_mode="HTML",
        reply_markup=get_operator_remove_confirm_kb(telegram_id, page, lang),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("admin_operator_remove:"))
async def deactivate_operator_from_admin(
    callback: types.CallbackQuery,
) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer(get_admin_text(callback.from_user.id, "access_denied"), show_alert=True)
        return

    try:
        _, raw_telegram_id, raw_page = callback.data.split(":")
        telegram_id = int(raw_telegram_id)
        page = max(0, int(raw_page))
    except (ValueError, AttributeError):
        await callback.answer(get_admin_text(callback.from_user.id, "bad_operator"), show_alert=True)
        return

    operator = get_user(telegram_id)
    if operator is None:
        await callback.answer(get_admin_text(callback.from_user.id, "operator_not_found"), show_alert=True)
        return

    lang = get_admin_language(callback.from_user.id)
    safe_name = html.escape(
        operator["full_name"] or ADMIN_TEXTS[lang]["operator_name_missing"]
    )
    try:
        deactivated = deactivate_operator(telegram_id)
    except ValueError:
        await callback.answer(
            get_admin_text(callback.from_user.id, "admin_cannot_remove"),
            show_alert=True,
        )
        return

    if not deactivated:
        await callback.answer(get_admin_text(callback.from_user.id, "operator_not_found"), show_alert=True)
        return

    await callback.message.edit_text(
        get_admin_text(callback.from_user.id, "operator_inactive", name=safe_name),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=ADMIN_TEXTS[lang]["panel_operators"],
                        callback_data=f"admin_operators:{page}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=ADMIN_TEXTS[lang]["back_panel"], callback_data="admin_home"
                    )
                ],
            ]
        ),
    )
    await callback.answer()


@dp.callback_query(
    AdminFSM.selecting_operator_objects, F.data == "admin_operator_save"
)
async def save_operator_access(
    callback: types.CallbackQuery, state: FSMContext
) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer(get_admin_text(callback.from_user.id, "access_denied"), show_alert=True)
        return

    data = await state.get_data()
    lang = data.get("admin_language", get_admin_language(callback.from_user.id))
    selected_ids = data.get("selected_object_ids", [])
    if not selected_ids:
        await callback.answer(ADMIN_TEXTS[lang]["select_one_object"], show_alert=True)
        return

    try:
        created = save_operator(
            data["operator_telegram_id"],
            data["operator_full_name"],
            selected_ids,
        )
    except ValueError:
        await callback.answer(ADMIN_TEXTS[lang]["object_not_found"], show_alert=True)
        return

    action = ADMIN_TEXTS[lang]["added"] if created else ADMIN_TEXTS[lang]["updated"]
    safe_name = html.escape(data["operator_full_name"])
    await callback.message.edit_text(
        ADMIN_TEXTS[lang]["operator_saved"].format(
            name=safe_name,
            action=action,
            telegram_id=data["operator_telegram_id"],
        ),
        parse_mode="HTML",
    )
    await state.clear()
    await callback.answer()


@dp.message(F.text.in_([TEXTS["uz"]["new"], TEXTS["ru"]["new"]]))
async def btn_new_report(message: types.Message, state: FSMContext) -> None:
    await state.clear()
    user = get_user(message.from_user.id)
    if not user:
        await message.answer(TEXTS["uz"]["no_access"])
        return
    lang = user["language"] if user["language"] in TEXTS else "uz"

    current_obj = (
        get_object_by_id(user["current_object_id"])
        if user["current_object_id"]
        else None
    )

    if current_obj:
        await state.update_data(
            object_id=current_obj["id"],
            object_name=current_obj["name"],
            lang=lang,
        )
        await message.answer(
            f"Obyekt: <b>{current_obj['name']}</b>\n\n"
            + TEXTS[lang]["select_type"],
            reply_markup=get_issue_types_kb(lang),
            parse_mode="HTML",
        )
        await state.set_state(ReportFSM.choosing_type)
    else:
        await message.answer(
            TEXTS[lang]["select_obj"],
            reply_markup=get_objects_list_kb(message.from_user.id),
        )
        await state.set_state(ReportFSM.choosing_object)


@dp.message(F.text.in_([TEXTS["uz"]["active"], TEXTS["ru"]["active"]]))
async def btn_active_reports(message: types.Message) -> None:
    await cmd_active_reports(message)


@dp.message(F.text.in_([TEXTS["uz"]["change"], TEXTS["ru"]["change"]]))
async def btn_change_object(
    message: types.Message, state: FSMContext
) -> None:
    user = get_user(message.from_user.id)
    lang = user["language"] if user and user["language"] in TEXTS else "uz"
    await message.answer(
        TEXTS[lang]["select_obj"],
        reply_markup=get_objects_list_kb(message.from_user.id),
    )
    await state.set_state(ReportFSM.choosing_object)


@dp.message(F.text.in_([TEXTS["uz"]["language"], TEXTS["ru"]["language"]]))
async def btn_change_language(message: types.Message) -> None:
    await message.answer("Tilni tanlang / Выберите язык:", reply_markup=get_language_kb())


@dp.callback_query(F.data.startswith("lang:"))
async def process_language(
    callback: types.CallbackQuery, state: FSMContext
) -> None:
    lang = callback.data.split(":", 1)[1]
    if lang not in TEXTS:
        await callback.answer("Til topilmadi", show_alert=True)
        return
    set_user_language(callback.from_user.id, lang)
    await state.update_data(lang=lang)
    await callback.message.delete()
    await callback.message.answer(
        TEXTS[lang]["lang_set"],
        reply_markup=get_main_reply_kb(lang, callback.from_user.id),
    )
    user = get_user(callback.from_user.id)
    if user is None and is_admin(callback.from_user.id):
        await callback.answer()
        return

    current_obj = (
        get_object_by_id(user["current_object_id"])
        if user and user["current_object_id"]
        else None
    )
    if current_obj:
        await state.update_data(
            object_id=current_obj["id"],
            object_name=current_obj["name"],
            lang=lang,
        )
        await callback.message.answer(
            f"{TEXTS[lang]['select_type']}\n"
            f"<b>{current_obj['name']}</b>",
            reply_markup=get_object_confirmation_kb(lang),
            parse_mode="HTML",
        )
    else:
        await callback.message.answer(
            TEXTS[lang]["select_obj"],
            reply_markup=get_objects_list_kb(callback.from_user.id),
        )
        await state.set_state(ReportFSM.choosing_object)
    await callback.answer()


@dp.callback_query(F.data == "confirm_current_obj")
async def process_confirm_obj(
    callback: types.CallbackQuery, state: FSMContext
) -> None:
    data = await state.get_data()
    lang = data.get("lang", "uz")
    await callback.message.edit_text(
        f"Tanlangan obyekt: <b>{data.get('object_name')}</b>\n\n"
        "Nosozlik turini tanlang:",
        reply_markup=get_issue_types_kb(lang),
        parse_mode="HTML",
    )
    await state.set_state(ReportFSM.choosing_type)
    await callback.answer()


@dp.callback_query(F.data == "change_obj_menu")
async def process_change_obj(
    callback: types.CallbackQuery, state: FSMContext
) -> None:
    user = get_user(callback.from_user.id)
    lang = user["language"] if user and user["language"] in TEXTS else "uz"
    await callback.message.edit_text(
        TEXTS[lang]["select_obj"],
        reply_markup=get_objects_list_kb(callback.from_user.id),
    )
    await state.set_state(ReportFSM.choosing_object)
    await callback.answer()


@dp.callback_query(ReportFSM.choosing_object, F.data.startswith("set_obj:"))
async def process_set_obj(
    callback: types.CallbackQuery, state: FSMContext
) -> None:
    obj_id = int(callback.data.split(":", 1)[1])
    obj = get_object_by_id(obj_id)
    if obj is None:
        await callback.answer("Obyekt topilmadi", show_alert=True)
        return

    allowed_object_ids = {
        row["id"] for row in get_all_objects(callback.from_user.id)
    }
    if obj_id not in allowed_object_ids:
        await callback.answer("Bu obyekt sizga biriktirilmagan", show_alert=True)
        return

    update_user_object(callback.from_user.id, obj_id)
    lang = (
        get_user(callback.from_user.id)["language"] or "uz"
    )
    await state.update_data(
        object_id=obj["id"], object_name=obj["name"], lang=lang
    )
    await callback.message.edit_text(
        f"<b>{obj['name']}</b>\n{TEXTS[lang]['select_type']}",
        reply_markup=get_issue_types_kb(lang),
        parse_mode="HTML",
    )
    await state.set_state(ReportFSM.choosing_type)
    await callback.answer()


@dp.callback_query(ReportFSM.choosing_type, F.data.startswith("issue:"))
async def process_set_type(
    callback: types.CallbackQuery, state: FSMContext
) -> None:
    issue_type = callback.data.split(":", 1)[1]
    user = get_user(callback.from_user.id)
    lang = user["language"] if user and user["language"] in TEXTS else "uz"
    await state.update_data(issue_type=issue_type, lang=lang)
    await callback.message.edit_text(
        f"<b>{issue_type}</b>\n\n{TEXTS[lang]['send_media']}",
        parse_mode="HTML",
    )
    await state.set_state(ReportFSM.uploading_media)
    await callback.answer()


@dp.message(ReportFSM.uploading_media, F.photo | F.video)
async def process_media_report(
    message: types.Message, state: FSMContext
) -> None:
    data = await state.get_data()
    await state.update_data(
        media_type="photo" if message.photo else "video",
        file_id=(
            message.photo[-1].file_id
            if message.photo
            else message.video.file_id
        ),
    )
    user = get_user(message.from_user.id)
    lang = user["language"] if user and user["language"] in TEXTS else "uz"
    await message.answer(
        TEXTS[lang]["comment"],
        reply_markup=get_skip_comment_kb(lang),
        parse_mode="HTML",
    )
    await state.set_state(ReportFSM.writing_comment)


async def send_final_report(
    message_or_callback, state: FSMContext, comment: str
) -> None:
    data = await state.get_data()
    lang = data.get("lang", "uz")
    caption = GROUP_TEXTS["new_report"].format(
        obj=data["object_name"],
        type=data["issue_type"],
        op=message_or_callback.from_user.full_name,
        desc=comment,
    )
    topic_id = get_object_topic(data["object_name"], GROUP_ID)
    if topic_id is None:
        try:
            topic = await bot.create_forum_topic(
                chat_id=GROUP_ID, name=data["object_name"][:128]
            )
            topic_id = topic.message_thread_id
            save_object_topic(data["object_name"], GROUP_ID, topic_id)
        except Exception:
            logging.exception(
                "Report uchun guruh mavzusini yaratib/saqlab bo'lmadi"
            )
            target = (
                message_or_callback
                if isinstance(message_or_callback, types.Message)
                else message_or_callback.message
            )
            await target.answer(
                "❌ Guruh mavzusi avtomatik yaratilolmadi. Guruh forum "
                "ekanini va botda mavzu yaratish huquqi borligini tekshiring.",
            )
            await state.clear()
            return
    try:
        if data["media_type"] == "photo":
            sent = await bot.send_photo(
                GROUP_ID,
                data["file_id"],
                caption=caption,
                parse_mode="HTML",
                message_thread_id=topic_id,
            )
        else:
            sent = await bot.send_video(
                GROUP_ID,
                data["file_id"],
                caption=caption,
                parse_mode="HTML",
                message_thread_id=topic_id,
            )
        report_id = add_report(
            message_or_callback.from_user.id,
            data["object_id"],
            data["issue_type"],
            comment,
            sent.message_id,
        )
        text = TEXTS[lang]["report_sent"].format(id=report_id)
        if isinstance(message_or_callback, types.Message):
            await message_or_callback.answer(
                text,
                reply_markup=get_main_reply_kb(
                    lang, message_or_callback.from_user.id
                ),
            )
        else:
            await message_or_callback.message.answer(
                text,
                reply_markup=get_main_reply_kb(
                    lang, message_or_callback.from_user.id
                ),
            )
    except Exception:
        logging.exception("Hisobotni guruhga yuborishda xatolik")
        target = (
            message_or_callback
            if isinstance(message_or_callback, types.Message)
            else message_or_callback.message
        )
        await target.answer("❌ Guruhga yuborishda xatolik yuz berdi.")
    finally:
        await state.clear()


@dp.callback_query(ReportFSM.writing_comment, F.data == "skip_comment")
async def process_skip_comment(
    callback: types.CallbackQuery, state: FSMContext
) -> None:
    data = await state.get_data()
    lang = data.get("lang", "uz")
    await send_final_report(callback, state, TEXTS[lang]["no_comment"])
    await callback.answer()


@dp.message(ReportFSM.writing_comment, F.text)
async def process_text_comment(message: types.Message, state: FSMContext) -> None:
    await send_final_report(message, state, message.text)


@dp.message(Command("active"))
async def cmd_active_reports(message: types.Message) -> None:
    user = get_user(message.from_user.id)
    if not user:
        return
    lang = user["language"] if user["language"] in TEXTS else "uz"

    reports = get_open_reports(message.from_user.id)
    if not reports:
        await message.answer(TEXTS[lang]["no_active"])
        return

    buttons = [
        [
            InlineKeyboardButton(
                text=TEXTS[lang]["resolved_button"].format(
                    id=report["id"],
                    obj=report["object_name"],
                    type=report["issue_type"],
                ),
                callback_data=f"resolve:{report['id']}",
            )
        ]
        for report in reports
    ]
    await message.answer(
        TEXTS[lang]["which_resolved"],
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@dp.callback_query(F.data.startswith("resolve:"))
async def process_resolve(callback: types.CallbackQuery) -> None:
    report_id = int(callback.data.split(":", 1)[1])
    user = get_user(callback.from_user.id)
    lang = user["language"] if user and user["language"] in TEXTS else "uz"
    report = get_report_by_id(report_id)
    group_msg_id = resolve_report_in_db(report_id)

    if group_msg_id and report:
        resolved_caption = GROUP_TEXTS["resolved"].format(
            id=report_id,
            obj=report["object_name"],
            type=report["issue_type"],
            op=callback.from_user.full_name,
        )
        try:
            await bot.edit_message_caption(
                chat_id=GROUP_ID,
                message_id=group_msg_id,
                caption=resolved_caption,
                parse_mode="HTML",
            )
        except Exception:
            logging.exception("Guruhdagi hisobot xabarini tahrirlashda xatolik")

    await callback.message.edit_text(
        TEXTS[lang]["resolved"].format(id=report_id),
        parse_mode="HTML",
    )
    await callback.answer(TEXTS[lang]["resolved_alert"])


def get_miniapp_operator_data(telegram_id: int) -> dict | None:
    user = get_user(telegram_id)
    if user is None:
        return None

    lang = user["language"] if user["language"] in TEXTS else "uz"
    issue_values = (
        "Shlagbaum",
        "Kamera",
        "To'lov terminali",
        "Server/Tarmoq",
    )
    issue_labels = {
        "uz": ("🚧 Shlagbaum", "📹 Kamera", "💳 To'lov terminali", "🌐 Server / tarmoq"),
        "ru": ("🚧 Шлагбаум", "📹 Камера", "💳 Терминал оплаты", "🌐 Сервер / сеть"),
    }
    objects = get_all_objects(telegram_id)
    reports = get_open_reports(telegram_id)
    return {
        "language": lang,
        "full_name": user["full_name"] or "",
        "objects": [
            {"id": obj["id"], "name": obj["name"]} for obj in objects
        ],
        "issue_types": [
            {"value": value, "label": label}
            for value, label in zip(issue_values, issue_labels[lang])
        ],
        "reports": [
            {
                "id": report["id"],
                "issue_type": report["issue_type"],
                "created_at": str(report["created_at"]),
                "object_name": report["object_name"],
            }
            for report in reports
        ],
    }


async def submit_miniapp_report(
    *,
    telegram_id: int,
    object_id: int,
    issue_type: str,
    description: str,
    media_bytes: bytes,
    media_name: str,
    media_type: str,
) -> dict:
    user = get_user(telegram_id)
    if user is None:
        raise ValueError("Operator access is not enabled")

    permitted_objects = {
        obj["id"]: obj for obj in get_all_objects(telegram_id)
    }
    obj = permitted_objects.get(object_id)
    if obj is None:
        raise ValueError("Object is not assigned to this operator")

    allowed_issue_types = {
        "Shlagbaum",
        "Kamera",
        "To'lov terminali",
        "Server/Tarmoq",
    }
    if issue_type not in allowed_issue_types:
        raise ValueError("Invalid issue type")

    lang = user["language"] if user["language"] in TEXTS else "uz"
    clean_description = description.strip() or TEXTS[lang]["no_comment"]
    caption = GROUP_TEXTS["new_report"].format(
        obj=html.escape(obj["name"]),
        type=html.escape(issue_type),
        op=html.escape(user["full_name"] or str(telegram_id)),
        desc=html.escape(clean_description),
    )

    topic_id = get_object_topic(obj["name"], GROUP_ID)
    if topic_id is None:
        topic = await bot.create_forum_topic(
            chat_id=GROUP_ID, name=obj["name"][:128]
        )
        topic_id = topic.message_thread_id
        save_object_topic(obj["name"], GROUP_ID, topic_id)

    upload = BufferedInputFile(media_bytes, filename=media_name)
    if media_type.startswith("image/"):
        sent = await bot.send_photo(
            GROUP_ID,
            upload,
            caption=caption,
            parse_mode="HTML",
            message_thread_id=topic_id,
        )
    else:
        sent = await bot.send_video(
            GROUP_ID,
            upload,
            caption=caption,
            parse_mode="HTML",
            message_thread_id=topic_id,
        )

    report_id = add_report(
        telegram_id,
        object_id,
        issue_type,
        clean_description,
        sent.message_id,
    )
    update_user_object(telegram_id, object_id)
    return {
        "report_id": report_id,
        "report": {
            "id": report_id,
            "issue_type": issue_type,
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "object_name": obj["name"],
        },
    }


async def main() -> None:
    init_db()
    miniapp = create_miniapp_app(
        BOT_TOKEN,
        load_operator_data=get_miniapp_operator_data,
        submit_report=submit_miniapp_report,
    )
    runner = web.AppRunner(miniapp, access_log=logging.getLogger("aiohttp.access"))
    await runner.setup()
    site = web.TCPSite(runner, WEBAPP_HOST, WEBAPP_PORT)
    await site.start()
    logging.info("Mini App server listening on %s:%s", WEBAPP_HOST, WEBAPP_PORT)
    if WEBAPP_URL:
        await bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(
                text="Open",
                web_app=WebAppInfo(url=WEBAPP_URL),
            )
        )
        logging.info("Telegram Mini App menu button points to %s", WEBAPP_URL)
    else:
        logging.warning("WEBAPP_URL is empty; Telegram Mini App menu button is disabled")
    try:
        await dp.start_polling(bot)
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
