import logging
import os
import re
import unicodedata
from typing import Optional, Tuple

import google.generativeai as genai

from config import GEMINI_API_KEY
from database import get_chat_history, get_user_state, set_user_state

logger = logging.getLogger(__name__)

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

_COMMENT_SPAM_TERMS = {
    "http://",
    "https://",
    "www.",
    "t.me/",
    "zalo.me/",
    "wa.me/",
    "casino",
    "forex",
    "crypto signal",
    "kiem tien nhanh",
}
_REPLY_PREFIX_PATTERN = re.compile(r"^\[REPLY\]\s*", re.IGNORECASE)
_REASONING_LINE_PATTERN = re.compile(
    r"^\s*(?:thoughts?|analysis|reasoning|chain[- ]of[- ]thought|internal(?:\s+thinking)?|explanation|note)\s*[:\-]\s*",
    re.IGNORECASE,
)
_META_REASONING_MARKERS = (
    "the user comment",
    "is not spam",
    "should be considered",
    "i need to respond",
    "short, polite",
    "therefore",
)


def get_system_instruction(state: str) -> str:
    prompts_dir = os.path.join(os.path.dirname(__file__), "prompts")
    file_map = {
        "GREETING": "greeting.txt",
        "MIDDLE": "middle.txt",
        "END": "end.txt",
    }
    filename = file_map.get(state, "greeting.txt")
    filepath = os.path.join(prompts_dir, filename)

    try:
        with open(filepath, "r", encoding="utf-8") as file:
            return file.read()
    except Exception as exc:
        logger.error("Failed to read prompt file %s: %s", filepath, exc)
        return "Bạn là trợ lý ảo thân thiện."


def build_reply(sender_id: str, user_text: str) -> Tuple[str, bool]:
    is_greeting = False
    current_state = get_user_state(sender_id)

    if GEMINI_API_KEY:
        try:
            system_instruction = get_system_instruction(current_state)
            model = genai.GenerativeModel("gemini-2.5-flash", system_instruction=system_instruction)

            raw_history = get_chat_history(sender_id, limit=20)
            formatted_history = []

            for msg in raw_history:
                role = "user" if msg.get("sender_type") == "user" else "model"
                formatted_history.append({"role": role, "parts": [msg.get("text", "")]})

            chat_session = model.start_chat(history=formatted_history)
            response = chat_session.send_message(user_text)
            reply_text = str(getattr(response, "text", "") or "")

            if "[GREETING]" in reply_text:
                is_greeting = True
                reply_text = reply_text.replace("[GREETING]", "").strip()

            state_changed = False
            new_state = current_state
            if "[TO_MIDDLE]" in reply_text:
                new_state = "MIDDLE"
                state_changed = True
                reply_text = reply_text.replace("[TO_MIDDLE]", "").strip()
            elif "[TO_END]" in reply_text:
                new_state = "END"
                state_changed = True
                reply_text = reply_text.replace("[TO_END]", "").strip()
            elif current_state == "END":
                new_state = "MIDDLE"
                state_changed = True

            if state_changed:
                set_user_state(sender_id, new_state)

            if reply_text:
                return reply_text, is_greeting
        except Exception as exc:
            logger.error("Error generating Gemini response: %s", exc)
            return f"B\u1ea1n v\u1eeba n\u00f3i: {user_text}", False

    return f"B\u1ea1n v\u1eeba n\u00f3i: {user_text}", False


def _normalize_comment_text(comment_text: str) -> str:
    return " ".join(str(comment_text or "").strip().split())


def _is_symbol_or_punctuation(char: str) -> bool:
    category = unicodedata.category(char)
    return category.startswith("P") or category.startswith("S")


def _to_ascii_lower(text: str) -> str:
    return unicodedata.normalize("NFKD", text.lower()).encode("ascii", "ignore").decode("ascii")


def _looks_like_noise_comment(comment_text: str) -> bool:
    normalized = _normalize_comment_text(comment_text)
    if not normalized:
        return True

    if all(_is_symbol_or_punctuation(char) for char in normalized):
        return True

    compact_text = re.sub(r"\s+", " ", _to_ascii_lower(normalized)).strip()
    if compact_text in {".", "..", "...", ",", "?", "!", "??", "!!", "!!!"}:
        return True

    if re.fullmatch(r"(\w+)(\s+\1){3,}", compact_text):
        return True

    symbol_count = sum(1 for char in normalized if _is_symbol_or_punctuation(char))
    if len(normalized) >= 10 and (symbol_count / len(normalized)) >= 0.6:
        return True

    return False


def _looks_like_spam_comment(comment_text: str) -> bool:
    normalized = " ".join(_to_ascii_lower(comment_text).split())
    if not normalized:
        return True

    if any(term in normalized for term in _COMMENT_SPAM_TERMS):
        return True

    link_count = len(re.findall(r"(https?://|www\.)", normalized))
    if link_count >= 2:
        return True

    if normalized.count("#") + normalized.count("@") >= 6:
        return True

    return False


def _fallback_comment_reply(comment_text: str) -> str:
    preview = _normalize_comment_text(comment_text)
    if len(preview) > 100:
        preview = f"{preview[:97]}..."
    return f"Mình đã nhận được bình luận '{preview}'. Bên mình sẽ phản hồi chi tiết qua inbox này nhé."


def _extract_ai_text(response: object) -> str:
    text = str(getattr(response, "text", "") or "").strip()
    if text:
        return text

    candidates = getattr(response, "candidates", None)
    if not candidates:
        return ""

    for candidate in candidates:
        content = getattr(candidate, "content", None)
        parts = getattr(content, "parts", None)
        if not parts:
            continue
        for part in parts:
            part_text = str(getattr(part, "text", "") or "").strip()
            if part_text:
                return part_text

    return ""


def _parse_generated_comment_reply(ai_text: str) -> Tuple[Optional[str], str]:
    text = str(ai_text or "").strip()
    if not text:
        return None, "empty"

    text = re.sub(r"```[\w-]*", "", text).replace("```", "").strip()
    if not text:
        return None, "empty"

    if "[SKIP]" in text.upper() and "[REPLY]" not in text.upper():
        return None, "skip"

    reply_match = re.search(r"\[REPLY\]\s*(.+)", text, flags=re.IGNORECASE | re.DOTALL)
    candidate = reply_match.group(1).strip() if reply_match else text

    filtered_lines = []
    for raw_line in candidate.splitlines():
        line = str(raw_line or "").strip()
        if not line:
            continue
        line = _REASONING_LINE_PATTERN.sub("", line).strip()
        line = _REPLY_PREFIX_PATTERN.sub("", line).strip()
        if not line:
            continue

        lowered = line.lower()
        if any(marker in lowered for marker in _META_REASONING_MARKERS):
            continue
        if line.upper() == "[SKIP]":
            return None, "skip"

        filtered_lines.append(line)

    if not filtered_lines:
        return None, "empty"

    # If the model prints "thoughts" then final answer on a new line, keep only the last line.
    reply_text = " ".join(filtered_lines) if reply_match else filtered_lines[-1]
    reply_text = " ".join(reply_text.split()).strip(" \"'")

    if not reply_text:
        return None, "empty"
    if reply_text.upper() == "[SKIP]":
        return None, "skip"

    return reply_text, "ok"


def build_comment_reply(comment_text: str) -> Tuple[Optional[str], str]:
    normalized = _normalize_comment_text(comment_text)
    if _looks_like_noise_comment(normalized):
        return None, "skipped_noise_comment"

    if _looks_like_spam_comment(normalized):
        return None, "skipped_spam_comment"

    if not GEMINI_API_KEY:
        return _fallback_comment_reply(normalized), "success_fallback"

    prompt = (
        "You are an assistant for a Facebook fanpage. "
        "Decide if a user comment is spam/noise or valid.\n"
        "Rules:\n"
        "1) Return EXACTLY one line in one of two formats:\n"
        "   [SKIP]\n"
        "   [REPLY] <one short Vietnamese message (1-2 sentences) directly related to the comment>\n"
        "2) Do not include explanations, thoughts, labels, or extra text outside the required format.\n"
        "3) Keep polite and natural tone, no markdown, no emojis, no hashtags, no quotes.\n"
        "4) Do not mention being an AI.\n\n"
        f"Comment: {normalized}"
    )

    try:
        model = genai.GenerativeModel("gemini-2.5-flash")
        response = model.generate_content(prompt)
        ai_text = _extract_ai_text(response).strip()

        if not ai_text:
            return _fallback_comment_reply(normalized), "success_fallback"

        parsed_reply, parse_status = _parse_generated_comment_reply(ai_text)
        if parse_status == "skip":
            return None, "skipped_spam_comment"
        if parsed_reply:
            return parsed_reply, "success"

        return _fallback_comment_reply(normalized), "success_fallback"
    except Exception as exc:
        logger.error("Error generating comment reply from Gemini: %s", exc)
        return _fallback_comment_reply(normalized), "success_fallback"
