import os
import shlex
import sqlite3
import logging
import io
import calendar
from datetime import datetime, date
import pytz
from dotenv import load_dotenv

# Configure Matplotlib for Headless Environment
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

# Scheduler & Telegram API
from apscheduler.schedulers.background import BackgroundScheduler
import telebot
from telebot import types

# Load configurations
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
REGISTRATION_TOKEN = os.getenv("REGISTRATION_TOKEN")

# Setup Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("OktaflowBot")

# Verify Token Settings
if not TELEGRAM_TOKEN:
    logger.critical("CRITICAL: TELEGRAM_TOKEN is missing in the environment or .env file.")
    raise ValueError("TELEGRAM_TOKEN is not defined.")
if not REGISTRATION_TOKEN:
    logger.critical("CRITICAL: REGISTRATION_TOKEN is missing in the environment or .env file.")
    raise ValueError("REGISTRATION_TOKEN is not defined.")

# Initialize Telegram Bot
bot = telebot.TeleBot(TELEGRAM_TOKEN)

# SQLite Settings
DB_FILE = "finance_bot.db"

def get_db_conn():
    """Create and return a new SQLite database connection."""
    conn = sqlite3.connect(DB_FILE, timeout=15.0)
    conn.row_factory = sqlite3.Row
    return conn

# ──────────────────────────────────────────────────────────────────────
# DATABASE SCHEMA & INITIALIZATION
# ──────────────────────────────────────────────────────────────────────

def init_db():
    """Create all required tables and indexes if they do not exist."""
    conn = get_db_conn()
    cursor = conn.cursor()
    try:
        # 1. users table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                username TEXT NOT NULL
            )
        """)

        # 2. categories table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                type TEXT NOT NULL CHECK(type IN ('in', 'out')),
                category_name TEXT NOT NULL
            )
        """)

        # 3. payment_methods table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS payment_methods (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                method_name TEXT NOT NULL,
                wallet_group TEXT NOT NULL CHECK(wallet_group IN ('cash', 'bank', 'lainnya'))
            )
        """)

        # 4. flow_ledger table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS flow_ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                username TEXT NOT NULL,
                type TEXT NOT NULL CHECK(type IN ('in', 'out', 'tf')),
                nominal INTEGER NOT NULL CHECK(nominal >= 0),
                category TEXT,
                method_source TEXT NOT NULL,
                method_dest TEXT,
                description TEXT NOT NULL
            )
        """)

        # 5. transient_states table for FSM and Dual-Gate cache
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS transient_states (
                telegram_id INTEGER PRIMARY KEY,
                type TEXT NOT NULL CHECK(type IN ('in', 'out', 'tf')),
                step TEXT NOT NULL,
                nominal INTEGER,
                category TEXT,
                method_source TEXT,
                method_dest TEXT,
                description TEXT,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Optimization Indexes
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_cat_user ON categories(username)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_pm_user ON payment_methods(username)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_ledger_user ON flow_ledger(username)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_ledger_time ON flow_ledger(timestamp)")

        conn.commit()
        logger.info("SQLite database and indexes initialized successfully.")
    except Exception as e:
        conn.rollback()
        logger.critical(f"Database initialization failed: {e}")
        raise
    finally:
        conn.close()

def seed_user_data(username):
    """Seed default categories and payment methods for a specific registered user."""
    conn = get_db_conn()
    cursor = conn.cursor()
    try:
        # Check if categories already seeded
        cursor.execute("SELECT count(*) FROM categories WHERE username = ?", (username,))
        cat_count = cursor.fetchone()[0]
        if cat_count == 0:
            out_categories = ["Makan", "Transport", "Belanja-Online", "Payment", "Tagihan", "Hiburan", "Hutang", "Lainnya"]
            in_categories = ["Gaji", "Bonus", "Investasi", "Pinjaman", "Lainnya"]
            
            for c in out_categories:
                cursor.execute(
                    "INSERT INTO categories (username, type, category_name) VALUES (?, 'out', ?)",
                    (username, c)
                )
            for c in in_categories:
                cursor.execute(
                    "INSERT INTO categories (username, type, category_name) VALUES (?, 'in', ?)",
                    (username, c)
                )
            logger.info(f"Seeded default categories for user: {username}")

        # Check if payment methods already seeded
        cursor.execute("SELECT count(*) FROM payment_methods WHERE username = ?", (username,))
        pm_count = cursor.fetchone()[0]
        if pm_count == 0:
            methods = [
                ("tunai", "cash"),
                ("cash", "cash"),
                ("bca", "bank"),
                ("mandiri", "bank"),
                ("cimb", "bank"),
                ("bri", "bank"),
                ("gopay", "lainnya"),
                ("ovo", "lainnya"),
                ("dana", "lainnya"),
                ("shopeepay", "lainnya")
            ]
            for name, group in methods:
                cursor.execute(
                    "INSERT INTO payment_methods (username, method_name, wallet_group) VALUES (?, ?, ?)",
                    (username, name, group)
                )
            logger.info(f"Seeded default payment methods for user: {username}")

        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.error(f"Error seeding default data for {username}: {e}")
        raise
    finally:
        conn.close()

# ──────────────────────────────────────────────────────────────────────
# AUTHENTICATION & ENVIRONMENT UTILITIES
# ──────────────────────────────────────────────────────────────────────

def get_user_identifier(from_user):
    """Return lowercase Telegram username or string of telegram ID if username is absent."""
    if from_user.username:
        return from_user.username.lower()
    return f"user_{from_user.id}"

def check_user_registered(telegram_id):
    """Check if the user exists in the database users table."""
    conn = get_db_conn()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT 1 FROM users WHERE telegram_id = ?", (telegram_id,))
        return cursor.fetchone() is not None
    except Exception as e:
        logger.error(f"Error checking user registration: {e}")
        return False
    finally:
        conn.close()

# ──────────────────────────────────────────────────────────────────────
# SMART NOMINAL HELPER
# ──────────────────────────────────────────────────────────────────────

def parse_nominal(s: str) -> int:
    """Auto-convert Indonesian/English shorthand notations (50k, 1.5jt, 200rb) to integers."""
    s = s.strip().lower()
    s = s.replace("rp.", "").replace("rp", "").replace(" ", "")
    if not s:
        raise ValueError("Nominal string is empty")

    # Handle suffixes
    if s.endswith('k'):
        val = float(s[:-1].replace(',', '.'))
        return int(val * 1000)
    if s.endswith('rb'):
        val = float(s[:-2].replace(',', '.'))
        return int(val * 1000)
    if s.endswith('jt'):
        val = float(s[:-2].replace(',', '.'))
        return int(val * 1000000)

    # Clean up standard formats (like 1.000.000 or 1,000,000)
    if s.count('.') > 1:
        s = s.replace('.', '')
    if s.count(',') > 1:
        s = s.replace(',', '')

    # Analyze single separators
    if '.' in s and ',' in s:
        # Standard complex format (e.g., 1,500.00 or 1.500,00)
        if s.find(',') < s.find('.'):
            s = s.replace(',', '')
        else:
            s = s.replace('.', '').replace(',', '.')
    elif '.' in s:
        parts = s.split('.')
        if len(parts[-1]) == 3:  # E.g. 50.000
            s = s.replace('.', '')
        # Else keep dot as a float decimal point
    elif ',' in s:
        parts = s.split(',')
        if len(parts[-1]) == 3:  # E.g. 50,000
            s = s.replace(',', '')
        else:
            s = s.replace(',', '.')

    try:
        val = float(s)
        return int(round(val))
    except ValueError:
        raise ValueError(f"Format nominal tidak dapat diubah: '{s}'")

# ──────────────────────────────────────────────────────────────────────
# REAL-TIME WALLET VALIDATION
# ──────────────────────────────────────────────────────────────────────

def calculate_wallet_balance(username: str, method_name: str) -> int:
    """Calculate the live net balance for a payment method from the flow_ledger history."""
    conn = get_db_conn()
    cursor = conn.cursor()
    try:
        # Query payment_methods master table for username to find the exact registered name
        cursor.execute("SELECT method_name FROM payment_methods WHERE username = ?", (username,))
        exact_method_name = None
        for row in cursor.fetchall():
            m_name = row['method_name']
            if m_name.lower().replace(' ', '-') == method_name.lower().replace(' ', '-'):
                exact_method_name = m_name
                break
        
        if not exact_method_name:
            exact_method_name = method_name

        # Total Inflow: cash received or transferred into this wallet
        cursor.execute(
            """
            SELECT COALESCE(SUM(nominal), 0) FROM flow_ledger 
            WHERE username = ? AND (
                (type = 'in' AND method_source = ?) OR
                (type = 'tf' AND method_dest = ?)
            )
            """,
            (username, exact_method_name, exact_method_name)
        )
        total_in = cursor.fetchone()[0]

        # Total Outflow: cash spent or transferred out of this wallet
        cursor.execute(
            """
            SELECT COALESCE(SUM(nominal), 0) FROM flow_ledger 
            WHERE username = ? AND (
                (type = 'out' AND method_source = ?) OR
                (type = 'tf' AND method_source = ?)
            )
            """,
            (username, exact_method_name, exact_method_name)
        )
        total_out = cursor.fetchone()[0]

        return total_in - total_out
    except Exception as e:
        logger.error(f"Error calculating balance for {username} [{method_name}]: {e}")
        return 0
    finally:
        conn.close()

# ──────────────────────────────────────────────────────────────────────
# DUAL-GATE STATE HELPERS
# ──────────────────────────────────────────────────────────────────────

def get_transient_state(telegram_id: int):
    """Retrieve FSM/Confirmation data for the user from transient_states."""
    conn = get_db_conn()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT * FROM transient_states WHERE telegram_id = ?", (telegram_id,))
        row = cursor.fetchone()
        return dict(row) if row else None
    except Exception as e:
        logger.error(f"Error getting transient state: {e}")
        return None
    finally:
        conn.close()

def set_transient_state(telegram_id: int, type_val: str, step: str, **kwargs):
    """Insert or update FSM/Confirmation data in transient_states."""
    conn = get_db_conn()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT 1 FROM transient_states WHERE telegram_id = ?", (telegram_id,))
        exists = cursor.fetchone()

        cols = ["type", "step", "updated_at"]
        vals = [type_val, step, datetime.now().isoformat()]
        for k, v in kwargs.items():
            cols.append(k)
            vals.append(v)

        if exists:
            set_str = ", ".join([f"{col} = ?" for col in cols])
            cursor.execute(
                f"UPDATE transient_states SET {set_str} WHERE telegram_id = ?",
                vals + [telegram_id]
            )
        else:
            cols.append("telegram_id")
            vals.append(telegram_id)
            placeholders = ", ".join(["?"] * len(vals))
            cursor.execute(
                f"INSERT INTO transient_states ({', '.join(cols)}) VALUES ({placeholders})",
                vals
            )
        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.error(f"Error setting transient state: {e}")
    finally:
        conn.close()

def delete_transient_state(telegram_id: int):
    """Delete transient data for the user to reset/flush state."""
    conn = get_db_conn()
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM transient_states WHERE telegram_id = ?", (telegram_id,))
        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.error(f"Error deleting transient state: {e}")
    finally:
        conn.close()

def get_proper_category_name(username: str, type_val: str, category_hyphen: str) -> str:
    """Retrieve original category casing from master table using its normalized/hyphenated string."""
    conn = get_db_conn()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT category_name FROM categories WHERE username = ? AND type = ?",
            (username, type_val)
        )
        for row in cursor.fetchall():
            name = row['category_name']
            if name.lower().replace(' ', '-') == category_hyphen.lower().replace(' ', '-'):
                return name
        return category_hyphen.replace('-', ' ').title()
    finally:
        conn.close()

def get_proper_method_name(username: str, method_hyphen: str) -> str:
    """Retrieve original payment method casing from master table using its normalized/hyphenated string."""
    conn = get_db_conn()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT method_name FROM payment_methods WHERE username = ?", (username,))
        for row in cursor.fetchall():
            name = row['method_name']
            if name.lower().replace(' ', '-') == method_hyphen.lower().replace(' ', '-'):
                return name
        return method_hyphen.replace('-', ' ')
    finally:
        conn.close()

def validate_category(username: str, type_val: str, category_hyphen: str) -> bool:
    """Validate if the normalized category name exists in categories table."""
    conn = get_db_conn()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT category_name FROM categories WHERE username = ? AND type = ?",
            (username, type_val)
        )
        cats = [row['category_name'].lower().replace(' ', '-') for row in cursor.fetchall()]
        return category_hyphen.lower().replace(' ', '-') in cats
    except Exception as e:
        logger.error(f"Error validating category: {e}")
        return False
    finally:
        conn.close()

def validate_method(username: str, method_hyphen: str) -> bool:
    """Validate if the normalized payment method name exists in payment_methods table."""
    conn = get_db_conn()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT method_name FROM payment_methods WHERE username = ?", (username,))
        methods = [row['method_name'].lower().replace(' ', '-') for row in cursor.fetchall()]
        return method_hyphen.lower().replace(' ', '-') in methods
    except Exception as e:
        logger.error(f"Error validating payment method: {e}")
        return False
    finally:
        conn.close()

# ──────────────────────────────────────────────────────────────────────
# TELEGRAM BOT HANDLERS
# ──────────────────────────────────────────────────────────────────────

# 1. Unprotected Registrasi Command
@bot.message_handler(commands=['daftar'])
def handle_registration(message):
    telegram_id = message.from_user.id
    
    if check_user_registered(telegram_id):
        bot.reply_to(message, "💡 Anda sudah terdaftar di sistem Oktaflow!")
        return

    text_parts = message.text.strip().split()
    if len(text_parts) < 2:
        bot.reply_to(
            message,
            "⚠️ Format salah! Gunakan perintah `/daftar [token_registrasi]` untuk melakukan registrasi.",
            parse_mode="Markdown"
        )
        return

    token = text_parts[1]
    if token != REGISTRATION_TOKEN:
        bot.reply_to(message, "❌ Token registrasi salah! Silakan hubungi administrator Anda.")
        return

    username = get_user_identifier(message.from_user)
    conn = get_db_conn()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "INSERT INTO users (telegram_id, username) VALUES (?, ?)",
            (telegram_id, username)
        )
        conn.commit()
        
        # Seed categories & payment methods
        seed_user_data(username)
        
        bot.reply_to(
            message,
            f"🎉 *Registrasi Berhasil!*\n\n"
            f"Akun Anda telah terdaftar sebagai *@{username}* di sistem Oktaflow.\n"
            f"Database default kategori dan dompet Anda telah di-seed secara otomatis.\n\n"
            f"Silakan ketik /start atau /menu untuk mulai menggunakan bot!",
            parse_mode="Markdown"
        )
    except Exception as e:
        conn.rollback()
        logger.error(f"Error registering user: {e}")
        bot.reply_to(message, "❌ Gagal melakukan registrasi karena kesalahan database.")
    finally:
        conn.close()

# Middleware Check for Protected Handlers
def gatekeeper_authenticated(message):
    """Verify if user is registered, and block with rejection prompt if not."""
    telegram_id = message.from_user.id
    if check_user_registered(telegram_id):
        return True
    
    bot.reply_to(
        message,
        "⚠️ Akses ditolak! Anda belum terdaftar di sistem Oktaflow. "
        "Silakan hubungi admin atau gunakan perintah `/daftar [token]` jika Anda memiliki token registrasi.",
        parse_mode="Markdown"
    )
    return False

# 2. Customization Commands
@bot.message_handler(commands=['addmethod'])
def handle_add_method(message):
    if not gatekeeper_authenticated(message): return
    username = get_user_identifier(message.from_user)
    
    text_parts = message.text.strip().split()
    if len(text_parts) < 3:
        bot.reply_to(
            message,
            "⚠️ Format salah! Gunakan perintah:\n"
            "`/addmethod [nama_metode_tanpa_spasi] [wallet_group]`\n\n"
            "Pilihan wallet_group: `cash`, `bank`, `lainnya`.\n"
            "Contoh: `/addmethod jenius bank`",
            parse_mode="Markdown"
        )
        return

    method_name = text_parts[1].lower().replace(' ', '-')
    wallet_group = text_parts[2].lower()

    if wallet_group not in ['cash', 'bank', 'lainnya']:
        bot.reply_to(message, "❌ Pilihan wallet_group tidak valid! Harus salah satu dari: `cash`, `bank`, `lainnya`.")
        return

    if validate_method(username, method_name):
        bot.reply_to(message, f"💡 Metode '{method_name}' sudah terdaftar dalam akun Anda.")
        return

    conn = get_db_conn()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "INSERT INTO payment_methods (username, method_name, wallet_group) VALUES (?, ?, ?)",
            (username, method_name, wallet_group)
        )
        conn.commit()
        bot.reply_to(
            message,
            f"✅ *Sukses!* Metode pembayaran *{method_name}* ({wallet_group}) telah ditambahkan.",
            parse_mode="Markdown"
        )
    except Exception as e:
        conn.rollback()
        logger.error(f"Error adding method: {e}")
        bot.reply_to(message, "❌ Gagal menambahkan metode pembayaran.")
    finally:
        conn.close()

@bot.message_handler(commands=['addcategory'])
def handle_add_category(message):
    if not gatekeeper_authenticated(message): return
    username = get_user_identifier(message.from_user)

    text_parts = message.text.strip().split()
    if len(text_parts) < 3:
        bot.reply_to(
            message,
            "⚠️ Format salah! Gunakan perintah:\n"
            "`/addcategory [in/out] [nama_kategori_dengan_penghubung]`\n\n"
            "Contoh: `/addcategory out makan-malam`",
            parse_mode="Markdown"
        )
        return

    type_val = text_parts[1].lower()
    category_name = text_parts[2].replace(' ', '-')

    if type_val not in ['in', 'out']:
        bot.reply_to(message, "❌ Pilihan tipe kategori tidak valid! Harus salah satu dari: `in`, `out`.")
        return

    if validate_category(username, type_val, category_name):
        bot.reply_to(message, f"💡 Kategori '{category_name}' ({type_val}) sudah terdaftar dalam akun Anda.")
        return

    conn = get_db_conn()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "INSERT INTO categories (username, type, category_name) VALUES (?, ?, ?)",
            (username, type_val, category_name)
        )
        conn.commit()
        bot.reply_to(
            message,
            f"✅ *Sukses!* Kategori *{category_name}* ({type_val}) telah ditambahkan.",
            parse_mode="Markdown"
        )
    except Exception as e:
        conn.rollback()
        logger.error(f"Error adding category: {e}")
        bot.reply_to(message, "❌ Gagal menambahkan kategori baru.")
    finally:
        conn.close()

# 3. Dual-Gate Yes/No Confirmation Command
@bot.message_handler(commands=['yes', 'no'])
def handle_confirmation(message):
    if not gatekeeper_authenticated(message): return
    telegram_id = message.from_user.id
    username = get_user_identifier(message.from_user)
    state = get_transient_state(telegram_id)

    if not state or state['step'] != 'confirm':
        bot.reply_to(message, "💡 Tidak ada data transaksi yang sedang menunggu konfirmasi.")
        return

    cmd = message.text.strip().lower()
    
    if cmd == '/yes':
        type_val = state['type']
        nominal = state['nominal']
        category = state['category']
        method_source = state['method_source']
        method_dest = state['method_dest']
        description = state['description']

        # Enforce Real-Time balance check immediately before write
        if type_val in ['out', 'tf']:
            method_source_hyphen = method_source.lower().replace(' ', '-')
            balance = calculate_wallet_balance(username, method_source_hyphen)
            if nominal > balance:
                bot.reply_to(
                    message,
                    f"Transaksi ditolak! Saldo kantong {method_source} tidak mencukupi, bro.\n"
                    f"Saldo saat ini: Rp {balance:,}.\n\n"
                    "Cache transaksi FSM telah dibersihkan."
                )
                delete_transient_state(telegram_id)
                return

        conn = get_db_conn()
        cursor = conn.cursor()
        try:
            cursor.execute(
                """
                INSERT INTO flow_ledger (username, type, nominal, category, method_source, method_dest, description)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (username, type_val, nominal, category, method_source, method_dest, description)
            )
            conn.commit()
            
            # Reset FSM state immediately
            delete_transient_state(telegram_id)
            
            emoji = "💸" if type_val == 'out' else ("💰" if type_val == 'in' else "🔄")
            type_lbl = "Pengeluaran" if type_val == 'out' else ("Pemasukan" if type_val == 'in' else "Transfer")
            
            bot.reply_to(
                message,
                f"✅ *Sukses!* Transaksi {type_lbl} telah dicatat ke database permanen.\n"
                f"{emoji} Nominal: *Rp {nominal:,}*\n"
                f"📍 Sumber/Metode: {method_source}" + (f" ➡️ {method_dest}" if method_dest else "") + "\n"
                f"📝 Keterangan: {description}",
                parse_mode="Markdown"
            )
        except Exception as e:
            conn.rollback()
            logger.error(f"Error saving permanent transaction: {e}")
            bot.reply_to(message, "❌ Gagal mencatat transaksi karena kesalahan internal database.")
        finally:
            conn.close()

    elif cmd == '/no':
        delete_transient_state(telegram_id)
        bot.reply_to(message, "🚫 Transaksi dibatalkan! Cache data transaksi telah dibersihkan.")

# 4. Conversational FSM Step Triggers
@bot.message_handler(commands=['menu_in', 'menu_out', 'menu_tf'])
def trigger_conversational_fsm(message):
    if not gatekeeper_authenticated(message): return
    telegram_id = message.from_user.id
    cmd = message.text.strip().lower().split()[0]
    
    if cmd == '/menu_in':
        set_transient_state(telegram_id, type_val='in', step='nominal')
        bot.send_message(
            message.chat.id,
            "💰 *Menu Pemasukan (In)*\n\n"
            "Silakan masukkan nominal pemasukan (misal: 50k, 1.5jt, atau 50000):",
            parse_mode="Markdown"
        )
    elif cmd == '/menu_out':
        set_transient_state(telegram_id, type_val='out', step='nominal')
        bot.send_message(
            message.chat.id,
            "💸 *Menu Pengeluaran (Out)*\n\n"
            "Silakan masukkan nominal pengeluaran (misal: 50k, 150rb, atau 20000):",
            parse_mode="Markdown"
        )
    elif cmd == '/menu_tf':
        set_transient_state(telegram_id, type_val='tf', step='nominal')
        bot.send_message(
            message.chat.id,
            "🔄 *Menu Transfer (Tf)*\n\n"
            "Silakan masukkan nominal transfer (misal: 50k, 2jt, atau 100000):",
            parse_mode="Markdown"
        )

# 5. Generic Text Handler for active FSM steps (nominal & description)
@bot.message_handler(func=lambda msg: get_transient_state(msg.from_user.id) is not None and get_transient_state(msg.from_user.id)['step'] in ['nominal', 'description'])
def handle_fsm_inputs(message):
    telegram_id = message.from_user.id
    state = get_transient_state(telegram_id)
    username = get_user_identifier(message.from_user)
    
    step = state['step']
    type_val = state['type']

    if step == 'nominal':
        try:
            nominal = parse_nominal(message.text)
        except ValueError as ve:
            bot.reply_to(message, f"❌ Nominal tidak valid: {ve}\n\nSilakan masukkan nominal kembali:")
            return

        if type_val in ['in', 'out']:
            # Move to Category Step
            set_transient_state(telegram_id, type_val=type_val, step='category', nominal=nominal)
            
            # Fetch Categories from Database
            conn = get_db_conn()
            cursor = conn.cursor()
            cursor.execute(
                "SELECT category_name FROM categories WHERE username = ? AND type = ?",
                (username, type_val)
            )
            cats = [row['category_name'] for row in cursor.fetchall()]
            conn.close()

            if not cats:
                bot.reply_to(message, "❌ Anda belum memiliki kategori terdaftar. Gunakan `/addcategory` terlebih dahulu.")
                delete_transient_state(telegram_id)
                return

            markup = types.InlineKeyboardMarkup(row_width=2)
            buttons = []
            for cat in cats:
                cat_hyphen = cat.replace(' ', '-')
                buttons.append(types.InlineKeyboardButton(cat, callback_data=f"fsm:cat:{cat_hyphen}"))
            markup.add(*buttons)
            
            bot.send_message(message.chat.id, "🏷️ *Pilih Kategori:*", reply_markup=markup, parse_mode="Markdown")

        elif type_val == 'tf':
            # Move to Source Wallet Step
            set_transient_state(telegram_id, type_val=type_val, step='method_source', nominal=nominal)
            
            # Fetch Methods
            conn = get_db_conn()
            cursor = conn.cursor()
            cursor.execute("SELECT method_name FROM payment_methods WHERE username = ?", (username,))
            methods = [row['method_name'] for row in cursor.fetchall()]
            conn.close()

            if not methods:
                bot.reply_to(message, "❌ Anda belum memiliki metode pembayaran terdaftar. Gunakan `/addmethod` terlebih dahulu.")
                delete_transient_state(telegram_id)
                return

            markup = types.InlineKeyboardMarkup(row_width=2)
            buttons = []
            for pm in methods:
                pm_hyphen = pm.replace(' ', '-')
                buttons.append(types.InlineKeyboardButton(pm, callback_data=f"fsm:src:{pm_hyphen}"))
            markup.add(*buttons)
            
            bot.send_message(message.chat.id, "💳 *Pilih Rekening Asal (Source):*", reply_markup=markup, parse_mode="Markdown")

    elif step == 'description':
        desc_str = message.text.strip()
        nominal = state['nominal']
        method_source = state['method_source']

        # Verify Real-Time balance for Out/Tf before showing confirmation summary
        if type_val in ['out', 'tf']:
            method_source_hyphen = method_source.lower().replace(' ', '-')
            balance = calculate_wallet_balance(username, method_source_hyphen)
            if nominal > balance:
                bot.reply_to(
                    message,
                    f"Transaksi ditolak! Saldo kantong {method_source} tidak mencukupi, bro.\n"
                    f"Saldo saat ini: Rp {balance:,}.\n\n"
                    "Memori transaksi FSM telah dibersihkan."
                )
                delete_transient_state(telegram_id)
                return

        # Prepare summary screen
        if type_val in ['in', 'out']:
            category = state['category']
            set_transient_state(
                telegram_id,
                type_val=type_val,
                step='confirm',
                nominal=nominal,
                category=category,
                method_source=method_source,
                description=desc_str
            )
            
            type_label = "Pemasukan" if type_val == 'in' else "Pengeluaran"
            summary = (
                "Berikut resume data yang akan dicatat:\n\n"
                f"🔹 Tipe: {type_label}\n"
                f"💰 Nominal: Rp {nominal:,}\n"
                f"🏷️ Kategori: {category}\n"
                f"💳 Metode: {method_source}\n"
                f"📝 Keterangan: {desc_str}\n\n"
                "Balas /yes untuk simpan permanen atau /no untuk membatalkan."
            )
            bot.send_message(message.chat.id, summary)

        elif type_val == 'tf':
            method_dest = state['method_dest']
            set_transient_state(
                telegram_id,
                type_val=type_val,
                step='confirm',
                nominal=nominal,
                method_source=method_source,
                method_dest=method_dest,
                description=desc_str
            )
            
            summary = (
                "Berikut resume data yang akan dicatat:\n\n"
                "🔄 Tipe: Transfer\n"
                f"💰 Nominal: Rp {nominal:,}\n"
                f"💳 Dari Metode: {method_source}\n"
                f"💳 Ke Metode: {method_dest}\n"
                f"📝 Keterangan: {desc_str}\n\n"
                "Balas /yes untuk simpan permanen atau /no untuk membatalkan."
            )
            bot.send_message(message.chat.id, summary)

# 6. Inline Button Callbacks for Category/Wallet steps
@bot.callback_query_handler(func=lambda call: call.data.startswith('fsm:'))
def handle_fsm_callbacks(call):
    telegram_id = call.from_user.id
    username = get_user_identifier(call.from_user)
    state = get_transient_state(telegram_id)

    if not state:
        bot.answer_callback_query(call.id, "Sesi interaktif telah berakhir atau kadaluarsa.")
        return

    parts = call.data.split(':')
    action = parts[1]
    value = parts[2]
    
    step = state['step']
    type_val = state['type']

    if action == 'cat' and step == 'category':
        # Retrieve proper name casing
        category_proper = get_proper_category_name(username, type_val, value)
        set_transient_state(telegram_id, type_val=type_val, step='method_source', nominal=state['nominal'], category=category_proper)
        
        # Load user payment methods
        conn = get_db_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT method_name FROM payment_methods WHERE username = ?", (username,))
        methods = [row['method_name'] for row in cursor.fetchall()]
        conn.close()

        markup = types.InlineKeyboardMarkup(row_width=2)
        buttons = []
        for pm in methods:
            pm_hyphen = pm.replace(' ', '-')
            buttons.append(types.InlineKeyboardButton(pm, callback_data=f"fsm:src:{pm_hyphen}"))
        markup.add(*buttons)

        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=f"🏷️ Kategori terpilih: *{category_proper}*\n\n💳 *Pilih Metode Pembayaran:*",
            reply_markup=markup,
            parse_mode="Markdown"
        )
        bot.answer_callback_query(call.id)

    elif action == 'src' and step == 'method_source':
        method_proper = get_proper_method_name(username, value)
        
        if type_val == 'tf':
            set_transient_state(
                telegram_id,
                type_val=type_val,
                step='method_dest',
                nominal=state['nominal'],
                method_source=method_proper
            )
            
            # Load other methods, excluding source
            conn = get_db_conn()
            cursor = conn.cursor()
            cursor.execute("SELECT method_name FROM payment_methods WHERE username = ?", (username,))
            methods = [row['method_name'] for row in cursor.fetchall()]
            conn.close()

            methods = [m for m in methods if m.lower().replace(' ', '-') != value.lower().replace(' ', '-')]

            markup = types.InlineKeyboardMarkup(row_width=2)
            buttons = []
            for pm in methods:
                pm_hyphen = pm.replace(' ', '-')
                buttons.append(types.InlineKeyboardButton(pm, callback_data=f"fsm:dst:{pm_hyphen}"))
            markup.add(*buttons)

            bot.edit_message_text(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                text=f"💳 Metode Asal: *{method_proper}*\n\n💳 *Pilih Rekening Tujuan (Destination):*",
                reply_markup=markup,
                parse_mode="Markdown"
            )
        else:
            # Pemasukan / Pengeluaran
            set_transient_state(
                telegram_id,
                type_val=type_val,
                step='description',
                nominal=state['nominal'],
                category=state['category'],
                method_source=method_proper
            )

            bot.edit_message_text(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                text=f"💳 Metode Pembayaran: *{method_proper}*\n\n📝 *Silakan ketik keterangan/deskripsi transaksi (balas dengan teks bebas):*",
                parse_mode="Markdown"
            )
        bot.answer_callback_query(call.id)

    elif action == 'dst' and step == 'method_dest':
        method_dest_proper = get_proper_method_name(username, value)
        
        set_transient_state(
            telegram_id,
            type_val=type_val,
            step='description',
            nominal=state['nominal'],
            method_source=state['method_source'],
            method_dest=method_dest_proper
        )

        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=f"💳 Metode Tujuan: *{method_dest_proper}*\n\n📝 *Silakan ketik keterangan/deskripsi transaksi (balas dengan teks bebas):*",
            parse_mode="Markdown"
        )
        bot.answer_callback_query(call.id)
    else:
        bot.answer_callback_query(call.id, "Aksi tidak dikenal pada langkah ini.")

# 7. One-Line Command Parser (Hyphen-Based parsing using shlex)
@bot.message_handler(func=lambda msg: msg.text and msg.text.strip().lower().split()[0] in ['out', 'in', 'tf'])
def handle_one_line_commands(message):
    if not gatekeeper_authenticated(message): return
    username = get_user_identifier(message.from_user)
    telegram_id = message.from_user.id
    
    text = message.text.strip()
    tokens = text.split(maxsplit=4)

    cmd = tokens[0].lower()

    if cmd == 'out':
        # out [nominal] [kategori-baku] [metode-baku] [sisa teks]
        if len(tokens) < 4:
            bot.reply_to(
                message,
                "❌ Format pengeluaran salah!\n"
                "Gunakan format: `out [nominal] [kategori-baku] [metode-baku] [sisa keterangan]`\n\n"
                "Contoh: `out 50k belanja-online shopee-pay beli kemeja baru`"
            )
            return

        nominal_str = tokens[1]
        category_str = tokens[2]
        method_str = tokens[3]
        desc_str = tokens[4] if len(tokens) > 4 else "Pengeluaran"

        try:
            nominal = parse_nominal(nominal_str)
        except ValueError as ve:
            bot.reply_to(message, f"❌ Nominal tidak valid: {ve}")
            return

        category_hyphen = category_str.lower().replace(' ', '-')
        method_hyphen = method_str.lower().replace(' ', '-')

        # Validations
        if not validate_category(username, 'out', category_hyphen):
            conn = get_db_conn()
            cursor = conn.cursor()
            cursor.execute("SELECT category_name FROM categories WHERE username = ? AND type = 'out'", (username,))
            cats = [row['category_name'] for row in cursor.fetchall()]
            conn.close()

            bot.reply_to(
                message,
                f"❌ Kategori '{category_str}' tidak terdaftar!\n"
                f"Pilihan kategori Out Anda:\n" + ", ".join(cats) + "\n\n"
                f"💡 Tambahkan kategori baru dengan: `/addcategory out nama-kategori`"
            )
            return

        if not validate_method(username, method_hyphen):
            conn = get_db_conn()
            cursor = conn.cursor()
            cursor.execute("SELECT method_name FROM payment_methods WHERE username = ?", (username,))
            methods = [row['method_name'] for row in cursor.fetchall()]
            conn.close()

            bot.reply_to(
                message,
                f"❌ Metode pembayaran '{method_str}' tidak terdaftar!\n"
                f"Pilihan metode Anda:\n" + ", ".join(methods) + "\n\n"
                f"💡 Tambahkan metode baru dengan: `/addmethod nama-metode wallet_group`"
            )
            return

        # Real-time balance check
        balance = calculate_wallet_balance(username, method_hyphen)
        if nominal > balance:
            bot.reply_to(
                message,
                f"Transaksi ditolak! Saldo kantong {method_str} tidak mencukupi, bro.\n"
                f"Saldo saat ini: Rp {balance:,}"
            )
            return

        # Commit to Confirmation
        category_proper = get_proper_category_name(username, 'out', category_hyphen)
        method_proper = get_proper_method_name(username, method_hyphen)

        set_transient_state(
            telegram_id,
            type_val='out',
            step='confirm',
            nominal=nominal,
            category=category_proper,
            method_source=method_proper,
            description=desc_str
        )

        summary = (
            "Berikut resume data yang akan dicatat:\n\n"
            "🔹 Tipe: Pengeluaran\n"
            f"💰 Nominal: Rp {nominal:,}\n"
            f"🏷️ Kategori: {category_proper}\n"
            f"💳 Metode: {method_proper}\n"
            f"📝 Keterangan: {desc_str}\n\n"
            "Balas /yes untuk simpan permanen atau /no untuk membatalkan."
        )
        bot.reply_to(message, summary)

    elif cmd == 'in':
        # in [nominal] [sumber-baku] [metode-baku] [sisa teks]
        if len(tokens) < 4:
            bot.reply_to(
                message,
                "❌ Format pemasukan salah!\n"
                "Gunakan format: `in [nominal] [sumber-baku] [metode-baku] [sisa keterangan]`\n\n"
                "Contoh: `in 10jt bonus-project bca kelar aplikasi oktaflow`"
            )
            return

        nominal_str = tokens[1]
        category_str = tokens[2]
        method_str = tokens[3]
        desc_str = tokens[4] if len(tokens) > 4 else "Pemasukan"

        try:
            nominal = parse_nominal(nominal_str)
        except ValueError as ve:
            bot.reply_to(message, f"❌ Nominal tidak valid: {ve}")
            return

        category_hyphen = category_str.lower().replace(' ', '-')
        method_hyphen = method_str.lower().replace(' ', '-')

        # Validations
        if not validate_category(username, 'in', category_hyphen):
            conn = get_db_conn()
            cursor = conn.cursor()
            cursor.execute("SELECT category_name FROM categories WHERE username = ? AND type = 'in'", (username,))
            cats = [row['category_name'] for row in cursor.fetchall()]
            conn.close()

            bot.reply_to(
                message,
                f"❌ Kategori/Sumber '{category_str}' tidak terdaftar!\n"
                f"Pilihan kategori In Anda:\n" + ", ".join(cats) + "\n\n"
                f"💡 Tambahkan kategori baru dengan: `/addcategory in nama-kategori`"
            )
            return

        if not validate_method(username, method_hyphen):
            conn = get_db_conn()
            cursor = conn.cursor()
            cursor.execute("SELECT method_name FROM payment_methods WHERE username = ?", (username,))
            methods = [row['method_name'] for row in cursor.fetchall()]
            conn.close()

            bot.reply_to(
                message,
                f"❌ Metode pembayaran '{method_str}' tidak terdaftar!\n"
                f"Pilihan metode Anda:\n" + ", ".join(methods)
            )
            return

        category_proper = get_proper_category_name(username, 'in', category_hyphen)
        method_proper = get_proper_method_name(username, method_hyphen)

        set_transient_state(
            telegram_id,
            type_val='in',
            step='confirm',
            nominal=nominal,
            category=category_proper,
            method_source=method_proper,
            description=desc_str
        )

        summary = (
            "Berikut resume data yang akan dicatat:\n\n"
            "🔸 Tipe: Pemasukan\n"
            f"💰 Nominal: Rp {nominal:,}\n"
            f"🏷️ Kategori: {category_proper}\n"
            f"💳 Metode: {method_proper}\n"
            f"📝 Keterangan: {desc_str}\n\n"
            "Balas /yes untuk simpan permanen atau /no untuk membatalkan."
        )
        bot.reply_to(message, summary)

    elif cmd == 'tf':
        # tf [nominal] [dari-metode-baku] [ke-metode-baku] [sisa teks]
        if len(tokens) < 4:
            bot.reply_to(
                message,
                "❌ Format transfer salah!\n"
                "Gunakan format: `tf [nominal] [dari-metode-baku] [ke-metode-baku] [sisa keterangan]`\n\n"
                "Contoh: `tf 200k bca gopay bulanan`"
            )
            return

        nominal_str = tokens[1]
        method_src_str = tokens[2]
        method_dst_str = tokens[3]
        desc_str = tokens[4] if len(tokens) > 4 else "Transfer Saldo"

        try:
            nominal = parse_nominal(nominal_str)
        except ValueError as ve:
            bot.reply_to(message, f"❌ Nominal tidak valid: {ve}")
            return

        method_src_hyphen = method_src_str.lower().replace(' ', '-')
        method_dst_hyphen = method_dst_str.lower().replace(' ', '-')

        # Validations
        if not validate_method(username, method_src_hyphen):
            conn = get_db_conn()
            cursor = conn.cursor()
            cursor.execute("SELECT method_name FROM payment_methods WHERE username = ?", (username,))
            methods = [row['method_name'] for row in cursor.fetchall()]
            conn.close()

            bot.reply_to(
                message,
                f"❌ Rekening asal '{method_src_str}' tidak terdaftar!\n"
                f"Pilihan rekening Anda:\n" + ", ".join(methods)
            )
            return

        if not validate_method(username, method_dst_hyphen):
            conn = get_db_conn()
            cursor = conn.cursor()
            cursor.execute("SELECT method_name FROM payment_methods WHERE username = ?", (username,))
            methods = [row['method_name'] for row in cursor.fetchall()]
            conn.close()

            bot.reply_to(
                message,
                f"❌ Rekening tujuan '{method_dst_str}' tidak terdaftar!\n"
                f"Pilihan rekening Anda:\n" + ", ".join(methods)
            )
            return

        # Real-time balance check
        balance = calculate_wallet_balance(username, method_src_hyphen)
        if nominal > balance:
            bot.reply_to(
                message,
                f"Transaksi ditolak! Saldo kantong {method_src_str} tidak mencukupi, bro.\n"
                f"Saldo saat ini: Rp {balance:,}"
            )
            return

        method_src_proper = get_proper_method_name(username, method_src_hyphen)
        method_dst_proper = get_proper_method_name(username, method_dst_hyphen)

        set_transient_state(
            telegram_id,
            type_val='tf',
            step='confirm',
            nominal=nominal,
            method_source=method_src_proper,
            method_dest=method_dst_proper,
            description=desc_str
        )

        summary = (
            "Berikut resume data yang akan dicatat:\n\n"
            "🔄 Tipe: Transfer\n"
            f"💰 Nominal: Rp {nominal:,}\n"
            f"💳 Dari Metode: {method_src_proper}\n"
            f"💳 Ke Metode: {method_dst_proper}\n"
            f"📝 Keterangan: {desc_str}\n\n"
            "Balas /yes untuk simpan permanen atau /no untuk membatalkan."
        )
        bot.reply_to(message, summary)

# 8. Visual Dashboard Keyboard Commands
@bot.message_handler(func=lambda msg: msg.text in ["📊 Laporan Bulan Ini", "💰 Cek Saldo", "📈 Tren Keuangan"])
def handle_dashboard_actions(message):
    if not gatekeeper_authenticated(message): return
    
    action = message.text
    if action == "📊 Laporan Bulan Ini":
        send_monthly_doughnut(message)
    elif action == "💰 Cek Saldo":
        send_wallet_balances(message)
    elif action == "📈 Tren Keuangan":
        send_financial_trends(message)

# 9. Start / Menu Command
@bot.message_handler(commands=['start', 'menu'])
def show_main_menu(message):
    if not gatekeeper_authenticated(message): return
    username = get_user_identifier(message.from_user)

    # Permanent Reply Keyboard
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    btn_rep = types.KeyboardButton("📊 Laporan Bulan Ini")
    btn_bal = types.KeyboardButton("💰 Cek Saldo")
    btn_tr = types.KeyboardButton("📈 Tren Keuangan")
    markup.add(btn_rep, btn_bal, btn_tr)

    welcome = (
        f"👋 Halo *@{username}*, Selamat datang di *Oktaflow V9*!\n\n"
        "Saya adalah asisten finansial pribadi Anda yang aman, stateless, dan super cepat.\n\n"
        "💡 *Format Cepat Satu Baris (Direct Text):*\n"
        "✍️ `out [nominal] [kategori] [metode] [keterangan]`\n"
        "✍️ `in [nominal] [sumber] [metode] [keterangan]`\n"
        "✍️ `tf [nominal] [dari_metode] [ke_metode] [keterangan]`\n\n"
        "📥 *Menu Conversational Interaktif:*\n"
        "👉 /menu_in - Catat Pemasukan\n"
        "👉 /menu_out - Catat Pengeluaran\n"
        "👉 /menu_tf - Catat Transfer\n\n"
        "⚙️ *Konfigurasi & Custom:* \n"
        "🔹 `/addcategory [in/out] [nama-kategori-baru]`\n"
        "🔹 `/addmethod [nama-metode-baru] [cash/bank/lainnya]`\n\n"
        "Gunakan menu keyboard di bawah untuk analisis visual secara real-time!"
    )
    bot.send_message(message.chat.id, welcome, reply_markup=markup, parse_mode="Markdown")

# 10. General Catch-All for unmatched registered messages
@bot.message_handler(func=lambda msg: True)
def handle_catch_all(message):
    if not gatekeeper_authenticated(message): return
    
    bot.reply_to(
        message,
        "💡 Perintah tidak dikenali oleh Oktaflow.\n\n"
        "Gunakan format penulisan cepat:\n"
        "✍️ `out 50k makan cash beli makan siang`\n"
        "✍️ `in 1.5jt bonus mandiri bonus project`\n"
        "✍️ `tf 200k cash gopay topup ewallet`\n\n"
        "Atau jalankan menu conversational:\n"
        "👉 /menu_in, /menu_out, /menu_tf\n\n"
        "Atau kirim /start untuk memunculkan tombol analisis di layar."
    )

# ──────────────────────────────────────────────────────────────────────
# ANALYTICS DASHBOARD ENGINE (MATPLOTLIB)
# ──────────────────────────────────────────────────────────────────────

def send_monthly_doughnut(message):
    """Generate and send Matplotlib doughnut chart for expenses in the current month."""
    username = get_user_identifier(message.from_user)
    now = datetime.now()
    year_str = f"{now.year:04d}"
    month_str = f"{now.month:02d}"
    month_name = now.strftime('%B %Y')

    conn = get_db_conn()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            SELECT category, SUM(nominal) as total FROM flow_ledger
            WHERE username = ? AND type = 'out'
            AND strftime('%Y', timestamp) = ? AND strftime('%m', timestamp) = ?
            GROUP BY category
            """,
            (username, year_str, month_str)
        )
        rows = cursor.fetchall()

        if not rows:
            bot.reply_to(message, f"📊 Belum ada data pengeluaran untuk bulan {month_name}, bro!")
            return

        categories = [row['category'] if row['category'] else 'Lainnya' for row in rows]
        totals = [row['total'] for row in rows]
        grand_total = sum(totals)

        # Plot doughnut chart
        plt.style.use('ggplot')
        fig, ax = plt.subplots(figsize=(6, 6), subplot_kw=dict(aspect="equal"))
        
        # HSL Tailored / Harmonious palette
        colors = ['#ff7f0e', '#1f77b4', '#2ca02c', '#d62728', '#9467bd', '#8c564b', '#e377c2', '#7f7f7f', '#bcbd22', '#17becf']
        if len(categories) > len(colors):
            colors = colors * (len(categories) // len(colors) + 1)
        colors = colors[:len(categories)]

        wedges, texts, autotexts = ax.pie(
            totals,
            labels=categories,
            autopct=lambda pct: f"{pct:.1f}%\n(Rp {int(pct*grand_total/100):,})",
            colors=colors,
            startangle=140,
            pctdistance=0.75,
            textprops=dict(color="black", weight="bold"),
            wedgeprops=dict(width=0.4, edgecolor='white', linewidth=2)
        )

        plt.setp(autotexts, size=8, weight="bold")
        plt.setp(texts, size=9)

        ax.set_title(f"Laporan Pengeluaran Bulan Ini\n{month_name}\n(Total: Rp {grand_total:,})", fontsize=12, weight="bold", pad=20)

        # Output to buffer
        buf = io.BytesIO()
        plt.savefig(buf, format='png', dpi=300, bbox_inches='tight', transparent=True)
        buf.seek(0)
        plt.close(fig)

        bot.send_photo(
            message.chat.id,
            buf,
            caption=f"📊 *Laporan Pengeluaran Bulan Ini ({month_name})*\n\n"
                    f"Total Pengeluaran: *Rp {grand_total:,}*\n\n"
                    "Tetap disiplin dan jaga pengeluaran Anda, bro! 💪",
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Error drawing monthly doughnut chart: {e}")
        bot.reply_to(message, "❌ Gagal menghasilkan grafik laporan bulanan.")
    finally:
        conn.close()

def send_wallet_balances(message):
    """Aggregate individual wallet balances grouped by Cash, Bank, and Lainnya."""
    username = get_user_identifier(message.from_user)

    conn = get_db_conn()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT method_name, wallet_group FROM payment_methods WHERE username = ?",
            (username,)
        )
        rows = cursor.fetchall()

        if not rows:
            bot.reply_to(message, "💰 Anda belum memiliki rekening atau dompet terdaftar.")
            return

        groups = {'cash': [], 'bank': [], 'lainnya': []}
        total_worth = 0

        for row in rows:
            name = row['method_name']
            group = row['wallet_group'].lower()
            
            # Live calculation
            balance = calculate_wallet_balance(username, name.lower().replace(' ', '-'))
            total_worth += balance
            
            if group in groups:
                groups[group].append((name, balance))
            else:
                groups['lainnya'].append((name, balance))

        report = "💵 *Ringkasan Saldo Dompet - Oktaflow* 💵\n"
        report += "──────────────────────────\n\n"

        for key, title, emoji in [('cash', 'TUNAI / CASH', '💵'), ('bank', 'PERBANKAN / BANK', '🏦'), ('lainnya', 'E-MONEY & OTHERS', '📱')]:
            methods = groups[key]
            subtotal = sum(m[1] for m in methods)
            
            report += f"{emoji} *{title}*\n"
            if not methods:
                report += " _(Belum ada dompet di-seed/terdaftar)_\n"
            else:
                for name, bal in methods:
                    bal_str = f"Rp {bal:,}" if bal >= 0 else f"-Rp {abs(bal):,}"
                    report += f" • {name}: `{bal_str}`\n"
            report += f" ── *Subtotal {title.split()[0].title()}*: `Rp {subtotal:,}`\n\n"

        report += "──────────────────────────\n"
        report += "💼 *CONSOLIDATED NET WORTH*:\n"
        report += f"✨ *Rp {total_worth:,}*"

        bot.reply_to(message, report, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Error calculating consolidated worth: {e}")
        bot.reply_to(message, "❌ Gagal mengambil ringkasan saldo dompet.")
    finally:
        conn.close()

def send_financial_trends(message):
    """Generate daily Line and Bar dual charts depicting monthly balance trends and cash flow."""
    username = get_user_identifier(message.from_user)
    now = datetime.now()
    start_of_month = date(now.year, now.month, 1)
    month_name = now.strftime('%B %Y')

    conn = get_db_conn()
    cursor = conn.cursor()
    try:
        # 1. Starting Balance before the 1st day of the current calendar month
        cursor.execute(
            """
            SELECT 
                (SELECT COALESCE(SUM(nominal), 0) FROM flow_ledger WHERE username = ? AND type = 'in' AND datetime(timestamp) < datetime(?)) -
                (SELECT COALESCE(SUM(nominal), 0) FROM flow_ledger WHERE username = ? AND type = 'out' AND datetime(timestamp) < datetime(?))
            """,
            (username, start_of_month.isoformat() + " 00:00:00", username, start_of_month.isoformat() + " 00:00:00")
        )
        starting_balance = cursor.fetchone()[0]

        # 2. Daily sums for the current month
        cursor.execute(
            """
            SELECT date(timestamp) as dt, type, SUM(nominal) as total FROM flow_ledger
            WHERE username = ? AND datetime(timestamp) >= datetime(?)
            GROUP BY dt, type
            ORDER BY dt ASC
            """,
            (username, start_of_month.isoformat() + " 00:00:00")
        )
        rows = cursor.fetchall()

        # Build list of days from 1st to today (natural reset on 1st day of month)
        today_day = now.day
        days = [date(now.year, now.month, d) for d in range(1, today_day + 1)]
        
        daily_incomes = {d.strftime('%Y-%m-%d'): 0 for d in days}
        daily_expenses = {d.strftime('%Y-%m-%d'): 0 for d in days}

        for row in rows:
            dt_str = row['dt']
            t = row['type']
            total = row['total']
            
            if dt_str in daily_incomes:
                if t == 'in':
                    daily_incomes[dt_str] += total
                elif t == 'out':
                    daily_expenses[dt_str] += total

        # Compute cumulative daily net balances
        daily_balances = []
        curr_balance = starting_balance
        for d in days:
            dt_str = d.strftime('%Y-%m-%d')
            in_val = daily_incomes[dt_str]
            out_val = daily_expenses[dt_str]
            curr_balance += (in_val - out_val)
            daily_balances.append(curr_balance)

        # Matplotlib Dual Subplot Plotting
        plt.style.use('ggplot')
        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 8), sharex=True)

        dates_mpl = [mdates.date2num(d) for d in days]

        # Top Chart: Line Chart Consolidated Net Worth
        ax1.plot(dates_mpl, daily_balances, color='#1f77b4', marker='o', linewidth=2.5, label='Net Worth Consolidated')
        ax1.fill_between(dates_mpl, daily_balances, color='#1f77b4', alpha=0.1)
        ax1.set_title(f"Tren Konsolidasi Saldo & Arus Kas - {month_name}", fontsize=14, weight="bold")
        ax1.set_ylabel("Saldo Net Worth (Rp)", fontsize=11, weight="bold")
        ax1.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, loc: "{:,}".format(int(x))))
        ax1.legend(loc='upper left')

        # Bottom Chart: Side-by-Side Inflow vs Outflow
        in_values = [daily_incomes[d.strftime('%Y-%m-%d')] for d in days]
        out_values = [daily_expenses[d.strftime('%Y-%m-%d')] for d in days]

        width = 0.35
        ax2.bar([x - width/2 for x in dates_mpl], in_values, width, label='Inflow (Pemasukan)', color='#2ca02c')
        ax2.bar([x + width/2 for x in dates_mpl], out_values, width, label='Outflow (Pengeluaran)', color='#d62728')
        ax2.set_ylabel("Arus Kas Harian (Rp)", fontsize=11, weight="bold")
        ax2.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, loc: "{:,}".format(int(x))))
        ax2.set_xlabel("Tanggal", fontsize=11, weight="bold")
        ax2.legend(loc='upper left')

        # Format Dates
        ax2.xaxis.set_major_locator(mdates.DayLocator(interval=max(1, today_day // 10)))
        ax2.xaxis.set_major_formatter(mdates.DateFormatter('%d %b'))
        fig.autofmt_xdate()

        plt.tight_layout()

        # Output to buffer
        buf = io.BytesIO()
        plt.savefig(buf, format='png', dpi=300, bbox_inches='tight')
        buf.seek(0)
        plt.close(fig)

        total_in = sum(in_values)
        total_out = sum(out_values)
        net_monthly_flow = total_in - total_out

        caption = (
            f"📈 *Laporan Tren Keuangan ({month_name})*\n\n"
            f"Total Pemasukan: *Rp {total_in:,}*\n"
            f"Total Pengeluaran: *Rp {total_out:,}*\n"
            f"Net Cash Flow: *Rp {net_monthly_flow:,}*\n"
            f"Saldo Akhir: *Rp {curr_balance:,}*"
        )
        bot.send_photo(message.chat.id, buf, caption=caption, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Error drawing monthly trend charts: {e}")
        bot.reply_to(message, "❌ Gagal menghasilkan grafik tren keuangan bulanan.")
    finally:
        conn.close()

# ──────────────────────────────────────────────────────────────────────
# BACKGROUND REMINDER AUTOMATION (APScheduler)
# ──────────────────────────────────────────────────────────────────────

def send_scheduled_reminders():
    """Trigger twice-daily broadcast reminder alerts to all registered users (12:00 PM & 08:00 PM WIB)."""
    logger.info("Scheduler task: starting scheduled financial reminders...")
    conn = get_db_conn()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT telegram_id, username FROM users")
        rows = cursor.fetchall()
        
        for row in rows:
            tg_id = row['telegram_id']
            uname = row['username']
            try:
                msg = (
                    f"🔔 *Oktaflow Financial Reminder* 🔔\n\n"
                    f"Halo bro *@{uname}*! Jangan lupa mencatat seluruh pengeluaran, pemasukan, atau transfer Anda hari ini ya.\n\n"
                    f"💡 *Catat cepat lewat chat:* \n"
                    f"👉 `out 50k makan cash beli makan siang`\n"
                    f"👉 `in 1.5jt bonus mandiri bonus project`\n"
                    f"👉 `tf 200k cash gopay topup gopay`\n\n"
                    f"Atau buka menu interaktif: /menu_in, /menu_out, /menu_tf.\n"
                    f"Tetap disiplin finansial! 💪"
                )
                bot.send_message(tg_id, msg, parse_mode="Markdown")
                logger.info(f"Reminder sent successfully to {uname} ({tg_id})")
            except Exception as ue:
                logger.warning(f"Unable to send reminder to user {uname} ({tg_id}): {ue}")
    except Exception as e:
        logger.error(f"Scheduled reminder task encountered error: {e}")
    finally:
        conn.close()

# Initialize Scheduler
scheduler = BackgroundScheduler(timezone=pytz.timezone('Asia/Jakarta'))
# Add cron triggers forced to Jakarta Timezone
scheduler.add_job(send_scheduled_reminders, 'cron', hour=12, minute=0)
scheduler.add_job(send_scheduled_reminders, 'cron', hour=20, minute=0)

# ──────────────────────────────────────────────────────────────────────
# MAIN RUNTIME BLOCK
# ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logger.info("Starting Oktaflow Bot Service...")
    
    # 1. Initialize SQLite
    init_db()

    # 2. Start Background Scheduler
    scheduler.start()
    logger.info("APScheduler initialized: scheduled reminders set for 12:00 PM & 08:00 PM WIB.")

    # 3. Launch Polling
    logger.info("Telegram Bot Long Polling started successfully.")
    try:
        bot.infinity_polling(timeout=20, long_polling_timeout=15)
    except Exception as e:
        logger.critical(f"Bot infinity polling crashed: {e}")
    finally:
        scheduler.shutdown()
        logger.info("Oktaflow Bot Service stopped.")
