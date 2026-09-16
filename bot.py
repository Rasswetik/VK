import asyncio
import html
import json
import logging
import os
import secrets
import sqlite3
from datetime import datetime
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, FSInputFile, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

try:
    from cryptography.fernet import Fernet
except ImportError:  # pragma: no cover
    Fernet = None

BASE_DIR = Path(os.getenv("BOT_DATA_DIR", "data")).resolve()
DB_PATH = BASE_DIR / "bot.sqlite3"
FILES_DIR = BASE_DIR / "chats"
UPLOADS_DIR = BASE_DIR / "uploads"
REPORTS_DIR = BASE_DIR / "reports"
LOGS_DIR = BASE_DIR / "logs"
for p in (BASE_DIR, FILES_DIR, UPLOADS_DIR, REPORTS_DIR, LOGS_DIR):
    p.mkdir(parents=True, exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("telegram_bot")

BOT_TOKEN = os.getenv("BOT_TOKEN", "8933461342:AAHnYqUrL6mEEOo-l3hDerzn2SwB6MXMq-0").strip()
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}
MASTER_KEY = os.getenv("BOT_MASTER_KEY", "").strip()


def get_cipher():
    if not MASTER_KEY:
        return None
    if Fernet is None:
        raise RuntimeError("Установите cryptography: pip install cryptography")
    return Fernet(MASTER_KEY.encode())


CIPHER = get_cipher()


def protect(value: str) -> str:
    if not value:
        return ""
    return CIPHER.encrypt(value.encode()).decode() if CIPHER else value


def reveal(value: str) -> str:
    if not value:
        return ""
    return CIPHER.decrypt(value.encode()).decode() if CIPHER else value


class DB:
    def __init__(self, path: Path):
        self.path = path
        self.init()

    def conn(self):
        c = sqlite3.connect(self.path)
        c.row_factory = sqlite3.Row
        return c

    def init(self):
        with self.conn() as c:
            c.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                username TEXT,
                created_at TEXT NOT NULL,
                last_seen TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                vk_name TEXT DEFAULT '',
                token TEXT DEFAULT '',
                user_id INTEGER,
                active INTEGER NOT NULL DEFAULT 1,
                flood INTEGER NOT NULL DEFAULT 0,
                added INTEGER NOT NULL DEFAULT 0,
                sent INTEGER NOT NULL DEFAULT 0,
                group_id TEXT DEFAULT 'default',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS groups_ (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                level TEXT NOT NULL,
                message TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                filename TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            INSERT OR IGNORE INTO groups_(id, name, created_at) VALUES ('default', 'Основная', datetime('now'));
            """)

    def touch_user(self, tg_id, username):
        now = datetime.now().isoformat(timespec="seconds")
        with self.conn() as c:
            c.execute("""INSERT INTO users(telegram_id,username,created_at,last_seen)
                       VALUES(?,?,?,?) ON CONFLICT(telegram_id) DO UPDATE SET username=excluded.username,last_seen=excluded.last_seen""",
                      (tg_id, username or "", now, now))

    def stats(self):
        with self.conn() as c:
            accounts = c.execute("SELECT COUNT(*) n FROM accounts").fetchone()["n"]
            active = c.execute("SELECT COUNT(*) n FROM accounts WHERE active=1 AND flood=0").fetchone()["n"]
            groups = c.execute("SELECT COUNT(*) n FROM groups_").fetchone()["n"]
            files = sum(1 for x in FILES_DIR.rglob("*.txt"))
            return accounts, active, groups, files

    def accounts(self):
        with self.conn() as c:
            return c.execute("SELECT * FROM accounts ORDER BY id DESC").fetchall()

    def account(self, account_id):
        with self.conn() as c:
            return c.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()

    def add_account(self, name, token="", vk_name="", user_id=None):
        with self.conn() as c:
            c.execute("INSERT INTO accounts(name,vk_name,token,user_id,created_at) VALUES(?,?,?,?,?)",
                      (name, vk_name, protect(token), user_id, datetime.now().isoformat(timespec="seconds")))
            return c.lastrowid

    def toggle_account(self, account_id):
        with self.conn() as c:
            c.execute("UPDATE accounts SET active=1-active WHERE id=?", (account_id,))

    def delete_account(self, account_id):
        with self.conn() as c:
            c.execute("DELETE FROM accounts WHERE id=?", (account_id,))

    def groups(self):
        with self.conn() as c:
            return c.execute("SELECT g.*, COUNT(a.id) count FROM groups_ g LEFT JOIN accounts a ON a.group_id=g.id GROUP BY g.id ORDER BY g.name").fetchall()

    def add_group(self, name):
        gid = secrets.token_hex(4)
        with self.conn() as c:
            c.execute("INSERT INTO groups_(id,name,created_at) VALUES(?,?,?)", (gid, name, datetime.now().isoformat(timespec="seconds")))
        return gid

    def delete_group(self, gid):
        if gid == "default":
            return
        with self.conn() as c:
            c.execute("UPDATE accounts SET group_id='default' WHERE group_id=?", (gid,))
            c.execute("DELETE FROM groups_ WHERE id=?", (gid,))

    def set_setting(self, key, value):
        with self.conn() as c:
            c.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))

    def setting(self, key, default=""):
        with self.conn() as c:
            row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return row["value"] if row else default

    def add_log(self, message, level="INFO"):
        with self.conn() as c:
            c.execute("INSERT INTO logs(level,message,created_at) VALUES(?,?,?)", (level, message, datetime.now().isoformat(timespec="seconds")))

    def logs(self, limit=15):
        with self.conn() as c:
            return c.execute("SELECT * FROM logs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    def add_report(self, kind, filename):
        with self.conn() as c:
            c.execute("INSERT INTO reports(kind,filename,created_at) VALUES(?,?,?)", (kind, filename, datetime.now().isoformat(timespec="seconds")))

    def reports(self):
        with self.conn() as c:
            return c.execute("SELECT * FROM reports ORDER BY id DESC LIMIT 30").fetchall()


db = DB(DB_PATH)


class Form(StatesGroup):
    add_account = State()
    add_group = State()
    create_folder = State()


def is_admin(user_id: int) -> bool:
    return not ADMIN_IDS or user_id in ADMIN_IDS


def main_kb():
    b = InlineKeyboardBuilder()
    b.button(text="📊 Дашборд", callback_data="menu:dashboard")
    b.button(text="👥 Аккаунты", callback_data="menu:accounts")
    b.button(text="📁 Группы", callback_data="menu:groups")
    b.button(text="💬 Файлы / чаты", callback_data="menu:files")
    b.button(text="📈 Отчёты", callback_data="menu:reports")
    b.button(text="⚙️ Настройки", callback_data="menu:settings")
    b.button(text="🧾 Логи", callback_data="menu:logs")
    b.adjust(2, 2, 2, 1)
    return b.as_markup()


def back_kb():
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ Назад", callback_data="menu:home")
    return b.as_markup()


def dashboard_text():
    accounts, active, groups, files = db.stats()
    return (
        "<b>🤖 VK Helper — Telegram Control</b>\n\n"
        f"👥 Аккаунтов: <b>{accounts}</b>\n"
        f"🟢 Активных: <b>{active}</b>\n"
        f"📁 Групп: <b>{groups}</b>\n"
        f"📄 TXT-файлов: <b>{files}</b>\n\n"
        "Выбирай раздел ниже. Все данные хранятся локально в каталоге <code>data/</code>."
    )


async def show_dashboard(target: Message | CallbackQuery):
    text = dashboard_text()
    if isinstance(target, CallbackQuery):
        await target.message.edit_text(text, reply_markup=main_kb())
        await target.answer()
    else:
        await target.answer(text, reply_markup=main_kb())


router = Router()


@router.message(CommandStart())
async def start(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Доступ закрыт.")
        return
    db.touch_user(message.from_user.id, message.from_user.username)
    await show_dashboard(message)


@router.callback_query(F.data == "menu:home")
async def home(call: CallbackQuery):
    await show_dashboard(call)


@router.callback_query(F.data == "menu:dashboard")
async def dashboard(call: CallbackQuery):
    await show_dashboard(call)


@router.callback_query(F.data == "menu:accounts")
async def accounts_menu(call: CallbackQuery):
    rows = db.accounts()
    b = InlineKeyboardBuilder()
    b.button(text="➕ Добавить аккаунт", callback_data="account:add")
    for a in rows[:30]:
        status = "🟢" if a["active"] and not a["flood"] else ("🔴" if a["flood"] else "⚪")
        b.button(text=f"{status} {a['name'][:22]}", callback_data=f"account:view:{a['id']}")
    b.button(text="⬅️ Назад", callback_data="menu:home")
    b.adjust(1)
    await call.message.edit_text("<b>👥 Аккаунты</b>\n\nВыбери аккаунт или добавь новый.", reply_markup=b.as_markup())
    await call.answer()


@router.callback_query(F.data == "account:add")
async def account_add(call: CallbackQuery, state: FSMContext):
    await state.set_state(Form.add_account)
    await call.message.edit_text(
        "<b>➕ Добавление аккаунта</b>\n\n"
        "Отправь одной строкой:\n<code>Имя | VK access token</code>\n\n"
        "Токен хранится в БД. Для шифрования задай <code>BOT_MASTER_KEY</code>.",
        reply_markup=back_kb(),
    )
    await call.answer()


@router.message(Form.add_account)
async def account_add_value(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    raw = (message.text or "").strip()
    if "|" not in raw:
        await message.answer("⚠️ Формат: <code>Имя | token</code>")
        return
    name, token = [x.strip() for x in raw.split("|", 1)]
    if not name or not token:
        await message.answer("⚠️ Имя и токен обязательны.")
        return
    aid = db.add_account(name=name, token=token)
    db.add_log(f"Добавлен аккаунт #{aid}: {name}")
    await state.clear()
    await message.answer(f"✅ Аккаунт <b>{html.escape(name)}</b> добавлен.", reply_markup=main_kb())


@router.callback_query(F.data.startswith("account:view:"))
async def account_view(call: CallbackQuery):
    aid = int(call.data.rsplit(":", 1)[1])
    a = db.account(aid)
    if not a:
        await call.answer("Аккаунт не найден", show_alert=True)
        return
    status = "🟢 Активен" if a["active"] else "⚪ Выключен"
    if a["flood"]:
        status = "🔴 Ограничен"
    b = InlineKeyboardBuilder()
    b.button(text="🔄 Вкл / выкл", callback_data=f"account:toggle:{aid}")
    b.button(text="🗑️ Удалить", callback_data=f"account:delete:{aid}")
    b.button(text="⬅️ К аккаунтам", callback_data="menu:accounts")
    b.adjust(2, 1)
    await call.message.edit_text(
        f"<b>👤 {html.escape(a['name'])}</b>\n\n"
        f"Статус: {status}\n"
        f"VK ID: <code>{a['user_id'] or '—'}</code>\n"
        f"Добавлено: <b>{a['added']}</b>\n"
        f"Отправлено: <b>{a['sent']}</b>",
        reply_markup=b.as_markup(),
    )
    await call.answer()


@router.callback_query(F.data.startswith("account:toggle:"))
async def account_toggle(call: CallbackQuery):
    aid = int(call.data.rsplit(":", 1)[1])
    db.toggle_account(aid)
    db.add_log(f"Изменён статус аккаунта #{aid}")
    await account_view(call)


@router.callback_query(F.data.startswith("account:delete:"))
async def account_delete(call: CallbackQuery):
    aid = int(call.data.rsplit(":", 1)[1])
    db.delete_account(aid)
    db.add_log(f"Удалён аккаунт #{aid}")
    await accounts_menu(call)


@router.callback_query(F.data == "menu:groups")
async def groups_menu(call: CallbackQuery):
    rows = db.groups()
    text = "<b>📁 Группы аккаунтов</b>\n\n" + "\n".join(f"📂 {html.escape(r['name'])} — {r['count']} акк." for r in rows)
    b = InlineKeyboardBuilder()
    b.button(text="➕ Создать группу", callback_data="group:add")
    for r in rows:
        if r["id"] != "default":
            b.button(text=f"🗑️ {r['name'][:24]}", callback_data=f"group:delete:{r['id']}")
    b.button(text="⬅️ Назад", callback_data="menu:home")
    b.adjust(1)
    await call.message.edit_text(text, reply_markup=b.as_markup())
    await call.answer()


@router.callback_query(F.data == "group:add")
async def group_add(call: CallbackQuery, state: FSMContext):
    await state.set_state(Form.add_group)
    await call.message.edit_text("<b>➕ Новая группа</b>\n\nОтправь название группы.", reply_markup=back_kb())
    await call.answer()


@router.message(Form.add_group)
async def group_add_value(message: Message, state: FSMContext):
    name = (message.text or "").strip()[:80]
    if not name:
        await message.answer("⚠️ Название пустое.")
        return
    gid = db.add_group(name)
    db.add_log(f"Создана группа: {name}")
    await state.clear()
    await message.answer(f"✅ Группа <b>{html.escape(name)}</b> создана.", reply_markup=main_kb())


@router.callback_query(F.data.startswith("group:delete:"))
async def group_delete(call: CallbackQuery):
    gid = call.data.rsplit(":", 1)[1]
    db.delete_group(gid)
    db.add_log(f"Удалена группа {gid}")
    await groups_menu(call)


@router.callback_query(F.data == "menu:files")
async def files_menu(call: CallbackQuery):
    folders = []
    for p in sorted(FILES_DIR.iterdir()) if FILES_DIR.exists() else []:
        if p.is_dir():
            txts = list(p.rglob("*.txt"))
            folders.append((p.name, len(txts), sum(x.stat().st_size for x in txts)))
    text = "<b>💬 Файлы / чаты</b>\n\n"
    text += "\n".join(f"📂 {html.escape(n)} — {c} файлов" for n, c, _ in folders) or "Пока нет папок."
    b = InlineKeyboardBuilder()
    b.button(text="➕ Создать папку", callback_data="folder:add")
    b.button(text="📄 Последние файлы", callback_data="files:list")
    b.button(text="⬅️ Назад", callback_data="menu:home")
    b.adjust(1)
    await call.message.edit_text(text, reply_markup=b.as_markup())
    await call.answer()


@router.callback_query(F.data == "folder:add")
async def folder_add(call: CallbackQuery, state: FSMContext):
    await state.set_state(Form.create_folder)
    await call.message.edit_text("<b>📂 Новая папка</b>\n\nОтправь название.", reply_markup=back_kb())
    await call.answer()


@router.message(Form.create_folder)
async def folder_add_value(message: Message, state: FSMContext):
    name = (message.text or "").strip()
    safe = "".join(ch if ch.isalnum() or ch in " _-" else "_" for ch in name).strip() or f"chat_{secrets.token_hex(3)}"
    (FILES_DIR / safe).mkdir(parents=True, exist_ok=True)
    db.add_log(f"Создана папка: {safe}")
    await state.clear()
    await message.answer(f"✅ Папка <code>{html.escape(safe)}</code> создана.", reply_markup=main_kb())


@router.callback_query(F.data == "files:list")
async def files_list(call: CallbackQuery):
    files = sorted(FILES_DIR.rglob("*.txt"), key=lambda p: p.stat().st_mtime, reverse=True)[:20]
    if not files:
        text = "<b>📄 Файлы</b>\n\nПусто."
    else:
        text = "<b>📄 Последние файлы</b>\n\n" + "\n".join(f"• <code>{html.escape(str(p.relative_to(FILES_DIR)))}</code>" for p in files)
    await call.message.edit_text(text, reply_markup=back_kb())
    await call.answer()


@router.callback_query(F.data == "menu:reports")
async def reports_menu(call: CallbackQuery):
    rows = db.reports()
    text = "<b>📈 Отчёты</b>\n\n" + ("\n".join(f"📄 {html.escape(r['filename'])} · {r['created_at']}" for r in rows) or "Отчётов пока нет.")
    await call.message.edit_text(text, reply_markup=back_kb())
    await call.answer()


@router.callback_query(F.data == "menu:logs")
async def logs_menu(call: CallbackQuery):
    rows = db.logs(15)
    text = "<b>🧾 Последние события</b>\n\n" + ("\n".join(f"<code>{r['created_at']}</code> · {html.escape(r['message'])}" for r in rows) or "Лог пуст.")
    await call.message.edit_text(text, reply_markup=back_kb())
    await call.answer()


@router.callback_query(F.data == "menu:settings")
async def settings_menu(call: CallbackQuery):
    delay = db.setting("random_delays", "1") == "1"
    interval = db.setting("interval", "5")
    b = InlineKeyboardBuilder()
    b.button(text=f"⏱️ Задержки: {'🟢 ВКЛ' if delay else '⚪ ВЫКЛ'}", callback_data="setting:delay")
    b.button(text=f"🔢 Интервал: {interval} сек", callback_data="setting:interval")
    b.button(text="🧹 Очистить логи", callback_data="setting:clear_logs")
    b.button(text="⬅️ Назад", callback_data="menu:home")
    b.adjust(1)
    await call.message.edit_text(
        "<b>⚙️ Настройки</b>\n\n"
        "Интерфейс и состояние хранятся в SQLite.\n"
        "Папки автоматически создаются при запуске.",
        reply_markup=b.as_markup(),
    )
    await call.answer()


@router.callback_query(F.data == "setting:delay")
async def setting_delay(call: CallbackQuery):
    old = db.setting("random_delays", "1") == "1"
    db.set_setting("random_delays", "0" if old else "1")
    await settings_menu(call)


@router.callback_query(F.data == "setting:clear_logs")
async def setting_clear_logs(call: CallbackQuery):
    with db.conn() as c:
        c.execute("DELETE FROM logs")
    await settings_menu(call)


@router.message(F.document)
async def document_upload(message: Message):
    if not is_admin(message.from_user.id):
        return
    doc = message.document
    if not doc.file_name.lower().endswith((".txt", ".csv", ".json")):
        await message.answer("⚠️ Разрешены только TXT/CSV/JSON.")
        return
    dest = UPLOADS_DIR / f"{secrets.token_hex(4)}_{Path(doc.file_name).name}"
    await message.bot.download(doc, destination=dest)
    db.add_log(f"Загружен файл: {doc.file_name}")
    await message.answer(f"✅ Файл сохранён: <code>{html.escape(str(dest.relative_to(BASE_DIR)))}</code>", reply_markup=main_kb())


@router.message(F.text == "/stats")
async def stats_cmd(message: Message):
    await show_dashboard(message)


async def main():
    if not BOT_TOKEN:
        raise SystemExit("Не задан BOT_TOKEN")
    if ADMIN_IDS:
        log.info("Ограничение доступа: %s admin id", len(ADMIN_IDS))
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    log.info("Telegram bot started; data=%s", BASE_DIR)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
