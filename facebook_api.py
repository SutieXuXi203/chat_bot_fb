import hashlib
import hmac
import json
import logging
import os
import re
import threading
import time
from typing import Any, Optional

import requests

from config import (
    AVATAR_DOWNLOAD_TIMEOUT_SECONDS,
    AVATAR_MAX_BYTES,
    APP_SECRET,
    GRAPH_VERSION,
    PAGE_ACCESS_TOKEN,
    PAGE_ID,
    SEND_API_URL,
    SEND_MAX_RETRIES,
    SEND_MIN_INTERVAL_PER_USER_SECONDS,
    SEND_MIN_INTERVAL_SECONDS,
    SEND_RETRY_BACKOFF_SECONDS,
    TYPING_DELAY_SECONDS,
)

logger = logging.getLogger(__name__)
CONVERSATIONS_API_VERSION = (os.getenv("CONVERSATIONS_API_VERSION", GRAPH_VERSION) or GRAPH_VERSION).strip()
_send_lock = threading.Lock()
_last_send_ts = 0.0
_last_send_by_psid: dict[str, float] = {}
_RETRYABLE_FB_ERROR_CODES = {4, 17, 341, 613}


def _log_token_expired_alert(operation: str, error_code: Any, error_subcode: Any, error_message: str) -> None:
    logger.critical(
        "%s blocked: Facebook token is invalid/expired (code=%s subcode=%s message=%s). "
        "Please refresh PAGE_ACCESS_TOKEN immediately.",
        operation,
        error_code,
        error_subcode,
        error_message,
    )


def _sanitize_error_text(message: str) -> str:
    if not message:
        return message
    return re.sub(r"(access_token=)[^&\s]+", r"\1***", message)


def _extract_graph_error(response: requests.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError:
        return {}

    if not isinstance(payload, dict):
        return {}

    error = payload.get("error")
    if not isinstance(error, dict):
        return {}
    return error


def _graph_error_message(response: requests.Response, error: dict[str, Any]) -> str:
    message = error.get("message")
    if isinstance(message, str) and message.strip():
        return message.strip()

    raw_text = (response.text or "").strip()
    if len(raw_text) > 400:
        return f"{raw_text[:400]}..."
    return raw_text


def _is_token_invalid_or_expired(error: dict[str, Any]) -> bool:
    return error.get("code") == 190


def _is_retryable_graph_error(response: requests.Response, error: dict[str, Any]) -> bool:
    status_retryable = response.status_code == 429 or response.status_code >= 500
    code_retryable = error.get("code") in _RETRYABLE_FB_ERROR_CODES
    return status_retryable or code_retryable


def _apply_send_rate_limit(psid: str) -> None:
    global _last_send_ts

    sleep_for = 0.0
    now = time.monotonic()
    with _send_lock:
        elapsed_global = now - _last_send_ts
        wait_global = SEND_MIN_INTERVAL_SECONDS - elapsed_global

        user_last_ts = _last_send_by_psid.get(psid, 0.0)
        elapsed_user = now - user_last_ts
        wait_user = SEND_MIN_INTERVAL_PER_USER_SECONDS - elapsed_user

        sleep_for = max(0.0, wait_global, wait_user)

    if sleep_for > 0:
        time.sleep(sleep_for)

    with _send_lock:
        sent_at = time.monotonic()
        _last_send_ts = sent_at
        _last_send_by_psid[psid] = sent_at


def _post_with_retry(operation: str, **request_kwargs) -> requests.Response:
    attempt = 0
    max_attempts = SEND_MAX_RETRIES + 1

    while True:
        try:
            response = requests.post(**request_kwargs)
            if response.ok:
                return response

            error = _extract_graph_error(response)
            error_code = error.get("code")
            error_subcode = error.get("error_subcode")
            error_message = _graph_error_message(response, error)

            if _is_token_invalid_or_expired(error):
                _log_token_expired_alert(operation, error_code, error_subcode, error_message)
                raise RuntimeError(
                    f"{operation} failed: Facebook token is invalid/expired "
                    f"(code={error_code} subcode={error_subcode} message={error_message})"
                )

            can_retry = _is_retryable_graph_error(response, error) and attempt < (max_attempts - 1)

            if can_retry:
                delay = SEND_RETRY_BACKOFF_SECONDS * (2 ** attempt)
                logger.warning(
                    "%s failed (status=%s code=%s subcode=%s). retrying attempt=%s/%s in %.2fs",
                    operation,
                    response.status_code,
                    error_code,
                    error_subcode,
                    attempt + 2,
                    max_attempts,
                    delay,
                )
                time.sleep(delay)
                attempt += 1
                continue

            raise RuntimeError(
                f"{operation} failed: status={response.status_code} code={error_code} "
                f"subcode={error_subcode} message={error_message}"
            )
        except requests.RequestException as exc:
            safe_error = _sanitize_error_text(str(exc))
            can_retry = attempt < (max_attempts - 1)
            if can_retry:
                delay = SEND_RETRY_BACKOFF_SECONDS * (2 ** attempt)
                logger.warning(
                    "%s request error: %s. retrying attempt=%s/%s in %.2fs",
                    operation,
                    safe_error,
                    attempt + 2,
                    max_attempts,
                    delay,
                )
                time.sleep(delay)
                attempt += 1
                continue

            raise RuntimeError(f"{operation} request error after retries: {safe_error}") from exc


def verify_signature(raw_body: bytes, signature_header: Optional[str]) -> bool:
    if not APP_SECRET:
        return True
    if not signature_header:
        return False

    method, sep, signature = signature_header.strip().partition("=")
    if sep != "=":
        return False

    method = method.strip().lower()
    signature = signature.strip().strip('"').lower()

    algo_map = {
        "sha1": hashlib.sha1,
        "sha256": hashlib.sha256,
    }
    hash_algo = algo_map.get(method)
    if hash_algo is None or not signature:
        return False

    expected = hmac.new(APP_SECRET.encode("utf-8"), raw_body, hash_algo).hexdigest().lower()
    return hmac.compare_digest(expected, signature)


def get_user_profile(psid: str) -> Optional[dict]:
    if not PAGE_ACCESS_TOKEN:
        logger.error("Missing PAGE_ACCESS_TOKEN. Cannot fetch user profile.")
        return None

    user_id = str(psid or "").strip()
    if not user_id:
        return None

    url = f"https://graph.facebook.com/{GRAPH_VERSION}/{user_id}"
    params = {
        "fields": "name,first_name,last_name,profile_pic",
        "access_token": PAGE_ACCESS_TOKEN,
    }

    max_attempts = SEND_MAX_RETRIES + 1
    for attempt in range(max_attempts):
        try:
            response = requests.get(url, params=params, timeout=15)
        except requests.RequestException as exc:
            safe_error = _sanitize_error_text(str(exc))
            can_retry = attempt < (max_attempts - 1)
            if can_retry:
                delay = SEND_RETRY_BACKOFF_SECONDS * (2 ** attempt)
                logger.warning(
                    "User profile request error for %s: %s. retrying attempt=%s/%s in %.2fs",
                    user_id,
                    safe_error,
                    attempt + 2,
                    max_attempts,
                    delay,
                )
                time.sleep(delay)
                continue

            logger.error("Error fetching user profile for %s after retries: %s", user_id, safe_error)
            return None
        except Exception as exc:
            logger.error("Error fetching user profile for %s: %s", user_id, exc)
            return None

        if response.ok:
            payload = response.json() if response.content else {}
            if not isinstance(payload, dict):
                logger.warning("Unexpected profile payload for %s: %s", user_id, type(payload).__name__)
                return None

            name = payload.get("name")
            first_name = payload.get("first_name")
            last_name = payload.get("last_name")
            if not isinstance(name, str) or not name.strip():
                first = first_name.strip() if isinstance(first_name, str) else ""
                last = last_name.strip() if isinstance(last_name, str) else ""
                # Prefer family-name first for Vietnamese names.
                name = " ".join(part for part in (last, first) if part)

            profile_pic = payload.get("profile_pic")
            return {
                "name": name.strip() if isinstance(name, str) else "",
                "first_name": first_name.strip() if isinstance(first_name, str) else "",
                "last_name": last_name.strip() if isinstance(last_name, str) else "",
                "profile_pic": profile_pic.strip() if isinstance(profile_pic, str) else "",
            }

        error = _extract_graph_error(response)
        error_code = error.get("code")
        error_subcode = error.get("error_subcode")
        error_message = _graph_error_message(response, error)

        if _is_token_invalid_or_expired(error):
            logger.error(
                "Unable to fetch user profile for %s: Facebook token is invalid/expired "
                "(code=%s subcode=%s message=%s)",
                user_id,
                error_code,
                error_subcode,
                error_message,
            )
            return None

        can_retry = _is_retryable_graph_error(response, error) and attempt < (max_attempts - 1)
        if can_retry:
            delay = SEND_RETRY_BACKOFF_SECONDS * (2 ** attempt)
            logger.warning(
                "Unable to fetch user profile for %s (status=%s code=%s subcode=%s). "
                "retrying attempt=%s/%s in %.2fs",
                user_id,
                response.status_code,
                error_code,
                error_subcode,
                attempt + 2,
                max_attempts,
                delay,
            )
            time.sleep(delay)
            continue

        logger.warning(
            "Unable to fetch user profile for %s (status=%s code=%s subcode=%s message=%s)",
            user_id,
            response.status_code,
            error_code,
            error_subcode,
            error_message,
        )
        return None

    return None


def download_avatar_image(image_url: str) -> Optional[dict]:
    avatar_url = str(image_url or "").strip()
    if not avatar_url:
        return None

    response: Optional[requests.Response] = None
    try:
        response = requests.get(avatar_url, timeout=AVATAR_DOWNLOAD_TIMEOUT_SECONDS, stream=True)
        if not response.ok:
            logger.warning("Unable to download avatar (status=%s url=%s)", response.status_code, avatar_url[:200])
            return None

        content_type_raw = (response.headers.get("Content-Type") or "").strip().lower()
        content_type = content_type_raw.split(";", 1)[0].strip()
        if not content_type.startswith("image/"):
            logger.warning("Avatar response is not an image for url=%s content_type=%s", avatar_url[:200], content_type_raw)
            return None

        chunks: list[bytes] = []
        total_bytes = 0
        for chunk in response.iter_content(chunk_size=8192):
            if not chunk:
                continue
            total_bytes += len(chunk)
            if total_bytes > AVATAR_MAX_BYTES:
                logger.warning(
                    "Avatar exceeded max size for url=%s size=%s max=%s",
                    avatar_url[:200],
                    total_bytes,
                    AVATAR_MAX_BYTES,
                )
                return None
            chunks.append(chunk)

        if total_bytes == 0:
            return None

        return {
            "content_type": content_type,
            "bytes": b"".join(chunks),
            "size": total_bytes,
        }
    except requests.RequestException as exc:
        logger.warning("Avatar download request error for url=%s error=%s", avatar_url[:200], _sanitize_error_text(str(exc)))
        return None
    except Exception as exc:
        logger.error("Unexpected avatar download error for url=%s error=%s", avatar_url[:200], exc)
        return None
    finally:
        if response is not None:
            response.close()


def get_user_profile_picture(psid: str) -> Optional[str]:
    if not PAGE_ACCESS_TOKEN:
        logger.error("Missing PAGE_ACCESS_TOKEN. Cannot fetch user profile picture.")
        return None

    url = f"https://graph.facebook.com/{GRAPH_VERSION}/{psid}"
    params = {
        "fields": "profile_pic",
        "access_token": PAGE_ACCESS_TOKEN,
    }

    try:
        response = requests.get(url, params=params, timeout=15)
        if not response.ok:
            error = _extract_graph_error(response)
            if _is_token_invalid_or_expired(error):
                logger.error(
                    "Unable to fetch profile picture for %s: Facebook token is invalid/expired "
                    "(code=%s subcode=%s message=%s)",
                    psid,
                    error.get("code"),
                    error.get("error_subcode"),
                    _graph_error_message(response, error),
                )
                return None

            logger.warning(
                "Unable to fetch profile picture for %s (status=%s message=%s)",
                psid,
                response.status_code,
                _graph_error_message(response, error),
            )
            return None

        payload = response.json() if response.content else {}
        profile_pic = payload.get("profile_pic") if isinstance(payload, dict) else None
        return profile_pic if isinstance(profile_pic, str) and profile_pic.strip() else None
    except Exception as exc:
        logger.error("Error fetching profile picture for %s: %s", psid, exc)
        return None


def _split_display_name(full_name: str) -> tuple[str, str]:
    name = (full_name or "").strip()
    if not name:
        return "", ""

    parts = [part for part in name.split() if part]
    if len(parts) == 1:
        return parts[0], ""

    # Heuristic: with many Vietnamese names, given name is the last token.
    first_name = parts[-1]
    last_name = " ".join(parts[:-1])
    return first_name, last_name


def _get_page_identity() -> Optional[dict]:
    if not PAGE_ACCESS_TOKEN:
        return None

    if PAGE_ID:
        return {"id": PAGE_ID}

    url = f"https://graph.facebook.com/{GRAPH_VERSION}/me"
    params = {
        "fields": "id,name",
        "access_token": PAGE_ACCESS_TOKEN,
    }

    try:
        response = requests.get(url, params=params, timeout=15)
        if not response.ok:
            return None
        return response.json()
    except Exception:
        return None


def _extract_profile_from_conversations_payload(payload: dict, user_id: str, page_id: str) -> Optional[dict]:
    user_id_str = str(user_id).strip()
    page_id_str = str(page_id or "").strip()

    for conversation in payload.get("data", []):
        participants = conversation.get("participants", {}).get("data", [])
        for participant in participants:
            participant_name = (participant.get("name") or "").strip()
            if not participant_name:
                continue

            participant_id = str(participant.get("id", "")).strip()
            participant_email = (participant.get("email") or "").strip().lower()
            participant_email_user = participant_email.split("@", 1)[0] if "@" in participant_email else ""

            if participant_id == user_id_str or participant_email_user == user_id_str:
                first_name, last_name = _split_display_name(participant_name)
                return {
                    "name": participant_name,
                    "first_name": first_name,
                    "last_name": last_name,
                    "profile_pic": "",
                }

            if page_id_str and participant_id == page_id_str:
                continue

    return None


def get_user_profile_from_conversations(user_id: str) -> Optional[dict]:
    if not PAGE_ACCESS_TOKEN:
        logger.error("Missing PAGE_ACCESS_TOKEN. Cannot fetch conversation participants.")
        return None

    page_info = _get_page_identity() or {}
    page_id = str(page_info.get("id", "")).strip()

    conversation_nodes: list[str] = []
    if page_id:
        conversation_nodes.append(page_id)
    conversation_nodes.append("me")

    tried_nodes: set[str] = set()
    for node in conversation_nodes:
        if node in tried_nodes:
            continue
        tried_nodes.add(node)

        url = f"https://graph.facebook.com/{CONVERSATIONS_API_VERSION}/{node}/conversations"
        params = {
            "user_id": user_id,
            "fields": "participants{id,name,email}",
            "access_token": PAGE_ACCESS_TOKEN,
        }

        try:
            response = requests.get(url, params=params, timeout=15)
            if not response.ok:
                error = _extract_graph_error(response)
                if _is_token_invalid_or_expired(error):
                    logger.error(
                        "Unable to fetch conversations for %s via %s: Facebook token is invalid/expired "
                        "(code=%s subcode=%s message=%s)",
                        user_id,
                        node,
                        error.get("code"),
                        error.get("error_subcode"),
                        _graph_error_message(response, error),
                    )
                    continue

                logger.warning(
                    "Unable to fetch conversations for %s via %s (status=%s body=%s)",
                    user_id,
                    node,
                    response.status_code,
                    response.text,
                )
                continue

            payload = response.json()
            profile = _extract_profile_from_conversations_payload(payload, user_id, page_id)
            if profile:
                profile_picture = get_user_profile_picture(user_id)
                if profile_picture:
                    profile["profile_pic"] = profile_picture
                return profile

            logger.info(
                "No participant matched user %s in conversations via %s. payload_keys=%s",
                user_id,
                node,
                list(payload.keys()),
            )
        except Exception as exc:
            logger.error("Error fetching conversation participants for %s via %s: %s", user_id, node, exc)

    return None


def send_image_file(psid: str, file_path: str) -> None:
    if not PAGE_ACCESS_TOKEN:
        raise RuntimeError("Missing PAGE_ACCESS_TOKEN")

    if not os.path.exists(file_path):
        logger.error(f"Image not found at {file_path}")
        return

    _apply_send_rate_limit(psid)

    if TYPING_DELAY_SECONDS > 0:
        time.sleep(TYPING_DELAY_SECONDS / 2)

    payload = {
        "recipient": json.dumps({"id": psid}),
        "message": json.dumps({"attachment": {"type": "image", "payload": {"is_reusable": True}}}),
    }

    max_attempts = SEND_MAX_RETRIES + 1
    for attempt in range(max_attempts):
        try:
            with open(file_path, "rb") as image_file:
                files = {
                    "filedata": (os.path.basename(file_path), image_file, "image/png"),
                }
                response = requests.post(
                    SEND_API_URL,
                    params={"access_token": PAGE_ACCESS_TOKEN},
                    data=payload,
                    files=files,
                    timeout=30,
                )

            if response.ok:
                return

            error = _extract_graph_error(response)
            error_code = error.get("code")
            error_subcode = error.get("error_subcode")
            error_message = _graph_error_message(response, error)

            if _is_token_invalid_or_expired(error):
                logger.error(
                    "Send Image API failed: Facebook token is invalid/expired "
                    "(code=%s subcode=%s message=%s)",
                    error_code,
                    error_subcode,
                    error_message,
                )
                return

            can_retry = _is_retryable_graph_error(response, error) and attempt < (max_attempts - 1)
            if can_retry:
                delay = SEND_RETRY_BACKOFF_SECONDS * (2 ** attempt)
                logger.warning(
                    "Send Image API failed (status=%s code=%s subcode=%s). retrying attempt=%s/%s in %.2fs",
                    response.status_code,
                    error_code,
                    error_subcode,
                    attempt + 2,
                    max_attempts,
                    delay,
                )
                time.sleep(delay)
                continue

            logger.error(
                "Send Image API failed status=%s code=%s subcode=%s message=%s",
                response.status_code,
                error_code,
                error_subcode,
                error_message,
            )
            return
        except requests.RequestException as exc:
            safe_error = _sanitize_error_text(str(exc))
            can_retry = attempt < (max_attempts - 1)
            if can_retry:
                delay = SEND_RETRY_BACKOFF_SECONDS * (2 ** attempt)
                logger.warning(
                    "Send image request error: %s. retrying attempt=%s/%s in %.2fs",
                    safe_error,
                    attempt + 2,
                    max_attempts,
                    delay,
                )
                time.sleep(delay)
                continue

            logger.error("Error uploading image after retries: %s", safe_error)
            return
        except Exception as exc:
            logger.error("Unexpected error uploading image: %s", exc)
            return


def send_text(psid: str, text: str) -> Optional[dict]:
    if not PAGE_ACCESS_TOKEN:
        raise RuntimeError("Missing PAGE_ACCESS_TOKEN")

    _apply_send_rate_limit(psid)

    typing_payload = {
        "recipient": {"id": psid},
        "sender_action": "typing_on",
    }
    try:
        typing_response = _post_with_retry(
            operation="Typing action",
            url=SEND_API_URL,
            params={"access_token": PAGE_ACCESS_TOKEN},
            json=typing_payload,
            timeout=15,
        )
        if not typing_response.ok:
            logger.error(
                "Typing action failed status=%s body=%s",
                typing_response.status_code,
                typing_response.text,
            )
    except Exception as exc:
        logger.error("Error sending typing indicator: %s", exc)

    if TYPING_DELAY_SECONDS > 0:
        time.sleep(TYPING_DELAY_SECONDS)

    payload = {
        "recipient": {"id": psid},
        "message": {"text": text},
    }

    try:
        response = _post_with_retry(
            operation="Send API",
            url=SEND_API_URL,
            params={"access_token": PAGE_ACCESS_TOKEN},
            json=payload,
            timeout=15,
        )
        response_body = response.json()
        logger.info("Sent message to %s", psid)
        return response_body if isinstance(response_body, dict) else None
    except Exception as exc:
        logger.error("Error sending message: %s", exc)
        return None


def reply_to_comment(comment_id: str, text: str) -> Optional[dict]:
    if not PAGE_ACCESS_TOKEN:
        raise RuntimeError("Missing PAGE_ACCESS_TOKEN")

    normalized_comment_id = str(comment_id or "").strip()
    message_text = str(text or "").strip()
    if not normalized_comment_id or not message_text:
        return None

    url = f"https://graph.facebook.com/{GRAPH_VERSION}/{normalized_comment_id}/comments"
    payload = {"message": message_text}

    try:
        response = _post_with_retry(
            operation="Reply comment API",
            url=url,
            params={"access_token": PAGE_ACCESS_TOKEN},
            data=payload,
            timeout=15,
        )
        response_body = response.json() if response.content else {}
        if isinstance(response_body, dict):
            logger.info("Replied to comment=%s", normalized_comment_id)
            return response_body
        return None
    except Exception as exc:
        logger.error("Error replying comment=%s: %s", normalized_comment_id, exc)
        return None
