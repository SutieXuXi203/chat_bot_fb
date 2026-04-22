import logging
import os
import queue
import random
import threading
import time
import unicodedata
from typing import Optional

from flask import Flask, Response, request

from config import (
    DEFAULT_AVATAR_URL,
    FACEBOOK_COMMENT_AUTOMATION_ENABLED,
    FACEBOOK_COMMENT_DELAY_MAX_SECONDS,
    FACEBOOK_COMMENT_DELAY_MIN_SECONDS,
    FACEBOOK_COMMENT_ENFORCE_24H_WINDOW,
    FACEBOOK_COMMENT_KEYWORDS,
    FACEBOOK_COMMENT_QUEUE_SIZE,
    FACEBOOK_COMMENT_REPLY_ENABLED,
    FACEBOOK_COMMENT_REPLY_TEMPLATE,
    FACEBOOK_COMMENT_REQUIRE_PREVIOUS_INTERACTION,
    FACEBOOK_COMMENT_WINDOW_HOURS,
    FACEBOOK_COMMENT_WORKERS,
    PAGE_ID,
    PORT,
    USER_PROFILE_REFRESH_SECONDS,
    VERIFY_TOKEN,
)
from chatbot_logic import build_comment_reply
from database import (
    get_facebook_comment_log,
    get_user,
    get_user_avatar_asset,
    has_recent_user_interaction,
    has_user_interaction,
    save_incoming_message,
    save_outgoing_message,
    save_user_avatar_asset,
    update_facebook_comment_log,
    update_user_profile,
    upsert_facebook_comment_log,
)
from facebook_api import (
    download_avatar_image,
    get_user_profile,
    get_user_profile_from_conversations,
    reply_to_comment,
    send_text,
    verify_signature,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    handlers=(
        [logging.StreamHandler()]
        if os.getenv("VERCEL")
        else [logging.FileHandler("app.log", encoding="utf-8"), logging.StreamHandler()]
    ),
)
logger = logging.getLogger(__name__)
_PLACEHOLDER_NAMES = {"facebook user", "nguoi dung facebook"}
_AVATAR_MEDIA_PREFIX = "/media/avatars"
_RUN_COMMENT_TASK_INLINE = bool(os.getenv("VERCEL"))
_COMMENT_TASK_QUEUE: queue.Queue[dict] = queue.Queue(maxsize=FACEBOOK_COMMENT_QUEUE_SIZE)
_COMMENT_WORKERS_STARTED = False
_COMMENT_WORKERS_LOCK = threading.Lock()

app = Flask(__name__)


@app.route("/", methods=["GET"])
def home():
    return "Chatbot is running", 200


@app.route("/health", methods=["GET"])
def health_check():
    return "OK", 200


@app.before_request
def ensure_background_workers_started() -> None:
    _start_comment_workers_once()


@app.route(f"{_AVATAR_MEDIA_PREFIX}/<user_id>", methods=["GET"])
def get_user_avatar(user_id: str):
    normalized_user_id = str(user_id or "").strip()
    avatar_asset = get_user_avatar_asset(normalized_user_id)
    if not avatar_asset:
        return "Not Found", 404

    content_type = (avatar_asset.get("content_type") or "image/jpeg").strip() or "image/jpeg"
    avatar_bytes = avatar_asset.get("data") or b""
    response = Response(avatar_bytes, mimetype=content_type)
    response.headers["Cache-Control"] = "public, max-age=86400"
    return response


@app.route("/debug/meta", methods=["GET"])
def meta_verification():
    mode = request.args.get("hub.mode")
    verify_token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and verify_token == VERIFY_TOKEN:
        logger.info("WEBHOOK_VERIFIED")
        return challenge, 200

    logger.warning("Failed verification. mode=%s token=%s", mode, verify_token)
    return "Forbidden", 403


@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        return meta_verification()

    raw_body = request.get_data()
    signature = request.headers.get("X-Hub-Signature-256") or request.headers.get("X-Hub-Signature")
    if not verify_signature(raw_body, signature):
        logger.warning(
            "Invalid signature (header_present=%s body_len=%s user_agent=%s)",
            bool(signature),
            len(raw_body),
            request.headers.get("User-Agent", "unknown"),
        )
        return "Forbidden", 403

    data = request.get_json(silent=True)
    if not data:
        return "OK", 200

    if data.get("object") != "page":
        return "Not Found", 404

    for entry in data.get("entry", []):
        entry_page_id = str(entry.get("id") or "").strip()
        entry_time = entry.get("time")

        for messaging_event in entry.get("messaging", []):
            try:
                handle_messaging_event(messaging_event)
            except Exception as exc:
                logger.exception("Error processing messaging event: %s", exc)

        for change_event in entry.get("changes", []):
            try:
                handle_feed_change_event(change_event, page_id=entry_page_id, entry_time=entry_time)
            except Exception as exc:
                logger.exception("Error processing feed change event: %s", exc)

    return "EVENT_RECEIVED", 200


def _now_ms() -> int:
    return int(time.time() * 1000)


def _normalize_for_keyword_match(text: str) -> str:
    value = unicodedata.normalize("NFKD", str(text or "").lower())
    value = "".join(char for char in value if not unicodedata.combining(char))
    return " ".join(value.split())


def _match_keyword(comment_text: str) -> str:
    normalized_comment = _normalize_for_keyword_match(comment_text)
    if not normalized_comment:
        return ""

    for raw_keyword in FACEBOOK_COMMENT_KEYWORDS:
        normalized_keyword = _normalize_for_keyword_match(raw_keyword)
        if normalized_keyword and normalized_keyword in normalized_comment:
            return raw_keyword

    return ""


def _render_template(template: str, user_id: str, comment_text: str, keyword: str) -> str:
    content = str(template or "").strip()
    if not content:
        return ""

    try:
        return content.format(user_id=user_id, comment=comment_text, keyword=keyword)
    except Exception:
        return content


def _enqueue_comment_task(task: dict) -> None:
    comment_id = str(task.get("comment_id") or "").strip()
    if not comment_id:
        return

    try:
        _COMMENT_TASK_QUEUE.put_nowait(task)
        update_facebook_comment_log(comment_id, {"message_status": "queued"})
        logger.info("Queued facebook comment task comment_id=%s", comment_id)
    except queue.Full:
        message = "comment queue is full"
        logger.error("Unable to enqueue comment_id=%s because %s", comment_id, message)
        update_facebook_comment_log(
            comment_id,
            {"message_status": "failed", "message_error": message},
        )


def _start_comment_workers_once() -> None:
    global _COMMENT_WORKERS_STARTED

    if not FACEBOOK_COMMENT_AUTOMATION_ENABLED:
        return
    if _RUN_COMMENT_TASK_INLINE:
        return

    if _COMMENT_WORKERS_STARTED:
        return

    with _COMMENT_WORKERS_LOCK:
        if _COMMENT_WORKERS_STARTED:
            return

        for index in range(FACEBOOK_COMMENT_WORKERS):
            worker = threading.Thread(
                target=_comment_worker_loop,
                name=f"fb-comment-worker-{index + 1}",
                daemon=True,
            )
            worker.start()

        _COMMENT_WORKERS_STARTED = True
        logger.info("Started %s comment worker(s)", FACEBOOK_COMMENT_WORKERS)


def _comment_worker_loop() -> None:
    while True:
        task = _COMMENT_TASK_QUEUE.get()
        comment_id = str((task or {}).get("comment_id") or "").strip()

        try:
            _process_comment_task(task)
        except Exception as exc:
            logger.exception("Unhandled error when processing comment task comment_id=%s: %s", comment_id, exc)
            if comment_id:
                update_facebook_comment_log(
                    comment_id,
                    {
                        "message_status": "failed",
                        "message_error": f"unexpected_worker_error: {exc}",
                    },
                )
        finally:
            _COMMENT_TASK_QUEUE.task_done()


def _dispatch_comment_task(task: dict) -> None:
    comment_id = str((task or {}).get("comment_id") or "").strip()
    if not comment_id:
        return

    if _RUN_COMMENT_TASK_INLINE:
        logger.info("Processing facebook comment inline comment_id=%s", comment_id)
        _process_comment_task(task)
        return

    _enqueue_comment_task(task)


def _process_comment_task(task: dict) -> None:
    comment_id = str((task or {}).get("comment_id") or "").strip()
    user_id = str((task or {}).get("user_id") or "").strip()
    post_id = str((task or {}).get("post_id") or "").strip()
    comment_text = str((task or {}).get("message") or "").strip()
    matched_keyword = str((task or {}).get("matched_keyword") or "").strip()
    page_id = str((task or {}).get("page_id") or PAGE_ID or "").strip()

    if not comment_id or not user_id:
        return

    update_facebook_comment_log(comment_id, {"message_status": "processing"})
    existing_log = get_facebook_comment_log(comment_id) or {}

    if existing_log.get("is_messaged"):
        logger.info("Skip comment_id=%s because message is already sent", comment_id)
        return

    if FACEBOOK_COMMENT_REQUIRE_PREVIOUS_INTERACTION:
        if not has_user_interaction(user_id):
            update_facebook_comment_log(
                comment_id,
                {
                    "message_status": "skipped_no_interaction",
                    "message_error": "user_has_not_interacted_with_page",
                },
            )
            return

        if FACEBOOK_COMMENT_ENFORCE_24H_WINDOW and not has_recent_user_interaction(
            user_id,
            within_hours=FACEBOOK_COMMENT_WINDOW_HOURS,
        ):
            update_facebook_comment_log(
                comment_id,
                {
                    "message_status": "skipped_outside_24h_window",
                    "message_error": "outside_24h_messaging_window",
                },
            )
            return

    message_text, generation_status = build_comment_reply(comment_text)
    if not message_text:
        if generation_status in {"skipped_noise_comment", "skipped_spam_comment"}:
            update_facebook_comment_log(
                comment_id,
                {
                    "is_messaged": False,
                    "message_status": generation_status,
                    "message_error": "",
                },
            )
            return

        update_facebook_comment_log(
            comment_id,
            {
                "is_messaged": False,
                "message_status": "failed_ai_generation",
                "message_error": generation_status or "ai_generation_returned_empty",
            },
        )
        return

    if FACEBOOK_COMMENT_REPLY_ENABLED and not existing_log.get("is_replied"):
        reply_text = _render_template(FACEBOOK_COMMENT_REPLY_TEMPLATE, user_id, comment_text, matched_keyword)
        if reply_text:
            reply_result = reply_to_comment(comment_id, reply_text)
            if isinstance(reply_result, dict):
                update_facebook_comment_log(
                    comment_id,
                    {
                        "is_replied": True,
                        "reply_status": "success",
                        "reply_message_id": reply_result.get("id"),
                        "reply_error": "",
                    },
                )
            else:
                update_facebook_comment_log(
                    comment_id,
                    {
                        "is_replied": False,
                        "reply_status": "failed",
                        "reply_error": "reply_comment_api_failed",
                    },
                )

    delay_seconds = random.uniform(FACEBOOK_COMMENT_DELAY_MIN_SECONDS, FACEBOOK_COMMENT_DELAY_MAX_SECONDS)
    if delay_seconds > 0:
        time.sleep(delay_seconds)

    send_result = send_text(user_id, message_text)
    if isinstance(send_result, dict):
        save_outgoing_message(
            user_id=user_id,
            page_id=page_id,
            content=message_text,
            timestamp=_now_ms(),
            message_id=send_result.get("message_id"),
        )
        update_facebook_comment_log(
            comment_id,
            {
                "is_messaged": True,
                "message_status": "success",
                "message_id": send_result.get("message_id"),
                "message_error": "",
                "message_source": generation_status,
            },
        )
        logger.info("Successfully sent auto message for comment_id=%s post_id=%s", comment_id, post_id)
        return

    update_facebook_comment_log(
        comment_id,
        {
            "is_messaged": False,
            "message_status": "failed",
            "message_error": "send_message_api_failed",
        },
    )


def _parse_comment_change(change: dict) -> Optional[dict]:
    field = str((change or {}).get("field") or "").strip().lower()
    value = (change or {}).get("value") or {}
    if field != "feed" or not isinstance(value, dict):
        return None

    item = str(value.get("item") or "").strip().lower()
    verb = str(value.get("verb") or "").strip().lower()
    if item != "comment" or verb not in {"add"}:
        return None

    comment_id = str(value.get("comment_id") or "").strip()
    user_id = str((value.get("from") or {}).get("id") or "").strip()
    message = str(value.get("message") or "").strip()
    post_id = str(value.get("post_id") or "").strip()

    if not comment_id or not user_id:
        return None

    return {
        "comment_id": comment_id,
        "user_id": user_id,
        "message": message,
        "post_id": post_id,
    }


def handle_feed_change_event(change: dict, page_id: str, entry_time: Optional[int] = None) -> None:
    parsed = _parse_comment_change(change)
    if not parsed:
        return

    comment_id = parsed["comment_id"]
    user_id = parsed["user_id"]
    message = parsed["message"]
    post_id = parsed["post_id"]
    normalized_page_id = str(page_id or PAGE_ID or "").strip()

    if user_id and normalized_page_id and user_id == normalized_page_id:
        logger.info("Skip page-owned comment event comment_id=%s", comment_id)
        return

    matched_keyword = _match_keyword(message)
    created_at = entry_time if isinstance(entry_time, int) else _now_ms()

    upsert_facebook_comment_log(
        comment_id=comment_id,
        user_id=user_id,
        message=message,
        post_id=post_id,
        created_at=created_at,
        matched_keyword=matched_keyword,
    )

    if not FACEBOOK_COMMENT_AUTOMATION_ENABLED:
        update_facebook_comment_log(comment_id, {"message_status": "skipped_automation_disabled"})
        return

    existing_log = get_facebook_comment_log(comment_id) or {}
    existing_status = str(existing_log.get("message_status") or "").strip().lower()
    blocked_statuses = {"processing", "success", "skipped_noise_comment", "skipped_spam_comment"}
    if not _RUN_COMMENT_TASK_INLINE:
        blocked_statuses.add("queued")

    if existing_log.get("is_messaged") or existing_status in blocked_statuses:
        logger.info("Skip duplicate webhook for comment_id=%s", comment_id)
        return

    _dispatch_comment_task(
        {
            "comment_id": comment_id,
            "user_id": user_id,
            "message": message,
            "post_id": post_id,
            "page_id": normalized_page_id,
            "matched_keyword": matched_keyword,
        }
    )


def _get_cached_name(user_doc: dict) -> str:
    name = ((user_doc or {}).get("name") or "").strip()
    if name:
        return name

    first_name = ((user_doc or {}).get("first_name") or "").strip()
    last_name = ((user_doc or {}).get("last_name") or "").strip()
    return " ".join(part for part in (last_name, first_name) if part).strip()


def _get_cached_profile_pic(user_doc: dict) -> str:
    # Keep backward compatibility for old documents that still have "avatar".
    profile_pic = (user_doc or {}).get("profile_pic") or (user_doc or {}).get("avatar") or ""
    return profile_pic.strip()


def _build_internal_avatar_url(user_id: str) -> str:
    return f"{_AVATAR_MEDIA_PREFIX}/{user_id}"


def _is_internal_avatar_url(url: str) -> bool:
    return str(url or "").strip().startswith(f"{_AVATAR_MEDIA_PREFIX}/")


def _persist_avatar_to_internal_storage(user_id: str, avatar_url: str) -> str:
    source_url = str(avatar_url or "").strip()
    if not source_url or source_url == DEFAULT_AVATAR_URL:
        return ""

    if _is_internal_avatar_url(source_url):
        return source_url

    avatar_payload = download_avatar_image(source_url)
    if not avatar_payload:
        return ""

    image_bytes = avatar_payload.get("bytes")
    content_type = (avatar_payload.get("content_type") or "image/jpeg").strip() or "image/jpeg"
    if not isinstance(image_bytes, (bytes, bytearray)) or not image_bytes:
        return ""

    saved = save_user_avatar_asset(
        user_id=user_id,
        image_bytes=bytes(image_bytes),
        content_type=content_type,
        source_url=source_url,
    )
    if not saved:
        return ""

    return _build_internal_avatar_url(user_id)


def _is_placeholder_name(value: str) -> bool:
    normalized = " ".join((value or "").strip().lower().split())
    return normalized in _PLACEHOLDER_NAMES


def _resolve_profile_name(profile: dict) -> str:
    direct_name = (profile.get("name") or "").strip()
    if direct_name:
        return direct_name

    first_name = (profile.get("first_name") or "").strip()
    last_name = (profile.get("last_name") or "").strip()
    return " ".join(part for part in (last_name, first_name) if part).strip()


def _is_profile_stale(user_doc: dict) -> bool:
    if USER_PROFILE_REFRESH_SECONDS <= 0:
        return False

    updated_at = (user_doc or {}).get("updated_at")
    if not isinstance(updated_at, (int, float)):
        return True

    max_age_ms = USER_PROFILE_REFRESH_SECONDS * 1000
    age_ms = int(time.time() * 1000) - int(updated_at)
    return age_ms >= max_age_ms


def _should_fetch_profile(user_doc: dict) -> bool:
    if not user_doc:
        return True

    cached_name = _get_cached_name(user_doc)
    if not cached_name or _is_placeholder_name(cached_name):
        return True

    if not _get_cached_profile_pic(user_doc):
        return True

    return _is_profile_stale(user_doc)


def _sync_sender_profile(sender_id: str, user_doc: dict) -> None:
    cached_name = _get_cached_name(user_doc)
    cached_profile_pic = _get_cached_profile_pic(user_doc)

    if not _should_fetch_profile(user_doc):
        if (
            cached_profile_pic
            and not _is_internal_avatar_url(cached_profile_pic)
            and cached_profile_pic != DEFAULT_AVATAR_URL
        ):
            internal_avatar = _persist_avatar_to_internal_storage(sender_id, cached_profile_pic)
            if internal_avatar:
                update_user_profile(sender_id, {"profile_pic": internal_avatar})
                logger.info("Migrated cached avatar to internal storage for sender=%s", sender_id)
        return

    profile = get_user_profile(sender_id) or {}
    resolved_name = _resolve_profile_name(profile)
    resolved_profile_pic = (profile.get("profile_pic") or "").strip()

    if not resolved_name:
        conversation_profile = get_user_profile_from_conversations(sender_id) or {}
        resolved_name = _resolve_profile_name(conversation_profile)
        if not resolved_profile_pic:
            resolved_profile_pic = (conversation_profile.get("profile_pic") or "").strip()

    update_payload = {}
    if resolved_name and not _is_placeholder_name(resolved_name):
        update_payload["name"] = resolved_name
    elif cached_name and not _is_placeholder_name(cached_name):
        update_payload["name"] = cached_name

    internal_profile_pic_url = ""
    if resolved_profile_pic:
        internal_profile_pic_url = _persist_avatar_to_internal_storage(sender_id, resolved_profile_pic)
        if not internal_profile_pic_url:
            logger.warning("Failed to persist resolved avatar for sender=%s", sender_id)

    if not internal_profile_pic_url and cached_profile_pic and not _is_internal_avatar_url(cached_profile_pic):
        internal_profile_pic_url = _persist_avatar_to_internal_storage(sender_id, cached_profile_pic)

    if internal_profile_pic_url:
        update_payload["profile_pic"] = internal_profile_pic_url
    elif _is_internal_avatar_url(cached_profile_pic):
        update_payload["profile_pic"] = cached_profile_pic
    else:
        update_payload["profile_pic"] = DEFAULT_AVATAR_URL

    update_user_profile(sender_id, update_payload)
    if "name" in update_payload:
        logger.info("Cached profile for sender=%s with name=%s", sender_id, update_payload["name"])
    else:
        logger.warning("Unable to resolve sender name=%s yet. Avatar fallback applied.", sender_id)


def _build_auto_reply_text(user_text: str) -> str:
    normalized = " ".join((user_text or "").strip().lower().split())

    if normalized in {"hello", "hi", "xin chao", "xin chao ban", "xin chao a"}:
        return "Chào bạn, mình có thể giúp gì?"

    return "Bên mình sẽ phản hồi sớm."


def handle_messaging_event(event: dict) -> None:
    sender_id = str((event.get("sender") or {}).get("id") or "").strip()
    page_id = str((event.get("recipient") or {}).get("id") or "").strip()
    timestamp = event.get("timestamp")
    message = event.get("message") or {}

    if not sender_id or not page_id or not message:
        return

    if message.get("is_echo"):
        logger.info("Skip echo message from page. sender=%s", sender_id)
        return

    text = (message.get("text") or "").strip()
    if not text:
        logger.info("Skip non-text message sender=%s", sender_id)
        return

    message_id = message.get("mid")

    save_incoming_message(
        user_id=sender_id,
        page_id=page_id,
        content=text,
        timestamp=timestamp,
        message_id=message_id,
    )

    user_doc = get_user(sender_id)
    _sync_sender_profile(sender_id, user_doc or {})

    reply_text = _build_auto_reply_text(text)
    send_result = send_text(sender_id, reply_text)

    if isinstance(send_result, dict):
        save_outgoing_message(
            user_id=sender_id,
            page_id=page_id,
            content=reply_text,
            timestamp=_now_ms(),
            message_id=send_result.get("message_id"),
        )
    else:
        logger.error("Failed to send auto reply to user=%s", sender_id)


if __name__ == "__main__":
    _start_comment_workers_once()
    logger.info("Starting server on port %s", PORT)
    app.run(host="127.0.0.1", port=PORT, debug=True)
