import os
from dotenv import load_dotenv

load_dotenv()


def _env_bool(name: str, default: bool) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return max(minimum, int(raw))
    except ValueError:
        return default


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return max(minimum, float(raw))
    except ValueError:
        return default


def _env_csv(name: str, default: str) -> list[str]:
    raw = (os.getenv(name) or default).strip()
    values = [item.strip() for item in raw.split(",")]
    return [item for item in values if item]


VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "").strip()
PAGE_ACCESS_TOKEN = (os.getenv("FACEBOOK_PAGE_TOKEN") or os.getenv("PAGE_ACCESS_TOKEN", "")).strip()
PAGE_ID = os.getenv("PAGE_ID", "").strip()
APP_SECRET = os.getenv("APP_SECRET", "").strip()
GRAPH_VERSION = os.getenv("GRAPH_VERSION", "v22.0").strip()
SEND_API_URL = f"https://graph.facebook.com/{GRAPH_VERSION}/me/messages"
DEFAULT_AVATAR_URL = (os.getenv("DEFAULT_AVATAR_URL", "/default-avatar.png") or "/default-avatar.png").strip()

USER_PROFILE_REFRESH_SECONDS = _env_int("USER_PROFILE_REFRESH_SECONDS", default=0, minimum=0)

AVATAR_DOWNLOAD_TIMEOUT_SECONDS = _env_float("AVATAR_DOWNLOAD_TIMEOUT_SECONDS", default=12.0, minimum=1.0)

AVATAR_MAX_BYTES = _env_int("AVATAR_MAX_BYTES", default=5 * 1024 * 1024, minimum=1024)

TYPING_DELAY_SECONDS = _env_float("TYPING_DELAY_SECONDS", default=0.8, minimum=0.0)

SEND_MAX_RETRIES = _env_int("SEND_MAX_RETRIES", default=2, minimum=0)

SEND_RETRY_BACKOFF_SECONDS = _env_float("SEND_RETRY_BACKOFF_SECONDS", default=1.0, minimum=0.0)

SEND_MIN_INTERVAL_SECONDS = _env_float("SEND_MIN_INTERVAL_SECONDS", default=0.25, minimum=0.0)

SEND_MIN_INTERVAL_PER_USER_SECONDS = _env_float("SEND_MIN_INTERVAL_PER_USER_SECONDS", default=1.0, minimum=0.0)

FACEBOOK_COMMENT_AUTOMATION_ENABLED = _env_bool("FACEBOOK_COMMENT_AUTOMATION_ENABLED", default=True)
FACEBOOK_COMMENT_KEYWORDS = _env_csv("FACEBOOK_COMMENT_KEYWORDS", "inbox,gia,bao nhieu")
FACEBOOK_COMMENT_REPLY_ENABLED = _env_bool("FACEBOOK_COMMENT_REPLY_ENABLED", default=True)
FACEBOOK_COMMENT_REPLY_TEMPLATE = (
    os.getenv("FACEBOOK_COMMENT_REPLY_TEMPLATE", "Check inbox giúp mình nhé!")
    or "Check inbox giúp mình nhé!"
).strip()
FACEBOOK_COMMENT_DELAY_MIN_SECONDS = _env_float("FACEBOOK_COMMENT_DELAY_MIN_SECONDS", default=1.0, minimum=0.0)
FACEBOOK_COMMENT_DELAY_MAX_SECONDS = _env_float("FACEBOOK_COMMENT_DELAY_MAX_SECONDS", default=3.0, minimum=0.0)
if FACEBOOK_COMMENT_DELAY_MIN_SECONDS > FACEBOOK_COMMENT_DELAY_MAX_SECONDS:
    FACEBOOK_COMMENT_DELAY_MIN_SECONDS, FACEBOOK_COMMENT_DELAY_MAX_SECONDS = (
        FACEBOOK_COMMENT_DELAY_MAX_SECONDS,
        FACEBOOK_COMMENT_DELAY_MIN_SECONDS,
    )
FACEBOOK_COMMENT_REQUIRE_PREVIOUS_INTERACTION = _env_bool(
    "FACEBOOK_COMMENT_REQUIRE_PREVIOUS_INTERACTION",
    default=True,
)
FACEBOOK_COMMENT_ENFORCE_24H_WINDOW = _env_bool("FACEBOOK_COMMENT_ENFORCE_24H_WINDOW", default=True)
FACEBOOK_COMMENT_WINDOW_HOURS = _env_int("FACEBOOK_COMMENT_WINDOW_HOURS", default=24, minimum=1)
FACEBOOK_COMMENT_QUEUE_SIZE = _env_int("FACEBOOK_COMMENT_QUEUE_SIZE", default=1000, minimum=10)
FACEBOOK_COMMENT_WORKERS = _env_int("FACEBOOK_COMMENT_WORKERS", default=1, minimum=1)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017/").strip()
MONGO_DB_NAME = os.getenv("MONGO_DB_NAME", "chat_w_sutie").strip()

PORT = int((os.getenv("PORT", "5000") or "5000").strip())
