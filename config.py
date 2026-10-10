import os

# Все секреты берутся из переменных окружения (Render -> Environment).
# В коде и в репозитории их хранить нельзя.

BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is required.")

_raw_admin_ids = os.environ.get("SUPERADMIN_IDS", "")
ADMIN_IDS = [int(x) for x in _raw_admin_ids.replace(",", " ").split() if x.strip()]
if not ADMIN_IDS:
    raise RuntimeError("SUPERADMIN_IDS is required (comma or space separated Telegram IDs).")

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is required (PostgreSQL connection string).")

PORT = int(os.environ.get("PORT", "10000"))
