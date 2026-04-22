import logging
import os
import time
from typing import Optional

from bson import ObjectId
from bson.binary import Binary
from pymongo import MongoClient
from pymongo.collection import Collection
from pymongo.database import Database
from pymongo.results import InsertOneResult

from config import MONGO_DB_NAME, MONGO_URI

logger = logging.getLogger(__name__)

db: Optional[Database] = None
users_collection: Optional[Collection] = None
conversations_collection: Optional[Collection] = None
messages_collection: Optional[Collection] = None
user_avatars_collection: Optional[Collection] = None
facebook_comments_collection: Optional[Collection] = None


def _now_ms() -> int:
    return int(time.time() * 1000)


def _to_object_id(value: object) -> Optional[ObjectId]:
    if isinstance(value, ObjectId):
        return value
    if isinstance(value, str):
        try:
            return ObjectId(value)
        except Exception:
            return None
    return None


def _user_lookup_filter(sender_id: str) -> dict:
    user_id = str(sender_id or "").strip()
    return {"$or": [{"user_id": user_id}, {"psid": user_id}]}


_is_vercel = bool(os.getenv("VERCEL"))
_is_localhost_uri = ("localhost" in MONGO_URI) or ("127.0.0.1" in MONGO_URI)
_skip_mongo_on_vercel = _is_vercel and (not MONGO_URI or _is_localhost_uri)

if _skip_mongo_on_vercel:
    logger.warning("MongoDB disabled on Vercel because MONGO_URI is missing or localhost.")
else:
    try:
        mongo_client = MongoClient(
            MONGO_URI,
            serverSelectionTimeoutMS=3000,
            connectTimeoutMS=3000,
            socketTimeoutMS=3000,
        )
        db = mongo_client[MONGO_DB_NAME]

        users_collection = db["users"]
        conversations_collection = db["conversations"]
        messages_collection = db["messages"]
        user_avatars_collection = db["user_avatars"]
        facebook_comments_collection = db["facebook_comments"]

        try:
            users_collection.create_index("user_id", unique=True, sparse=True)
        except Exception as exc:
            logger.warning("Failed to ensure unique index users.user_id: %s", exc)

        try:
            users_collection.create_index("psid", unique=True)
        except Exception as exc:
            logger.warning("Failed to ensure unique index users.psid: %s", exc)

        try:
            conversations_collection.create_index([("user_id", 1), ("page_id", 1)], unique=True)
            conversations_collection.create_index("updated_at")
            messages_collection.create_index([("conversation_id", 1), ("timestamp", -1)])
            messages_collection.create_index("timestamp")
            user_avatars_collection.create_index("user_id", unique=True)
            user_avatars_collection.create_index("updated_at")
            facebook_comments_collection.create_index("id", unique=True)
            facebook_comments_collection.create_index("user_id")
            facebook_comments_collection.create_index("post_id")
            facebook_comments_collection.create_index("created_at")
            facebook_comments_collection.create_index("is_replied")
            facebook_comments_collection.create_index("is_messaged")
        except Exception as exc:
            logger.warning("Failed to ensure message/conversation indexes: %s", exc)
    except Exception as exc:
        logger.error("Failed to connect to MongoDB: %s", exc)


def get_user_state(sender_id: str) -> str:
    if users_collection is not None:
        user_doc = users_collection.find_one(_user_lookup_filter(sender_id))
        if user_doc and "state" in user_doc:
            return user_doc["state"]
    return "GREETING"


def get_user(sender_id: str) -> Optional[dict]:
    if users_collection is not None:
        return users_collection.find_one(_user_lookup_filter(sender_id))
    return None


def has_user_interaction(user_id: str) -> bool:
    normalized_user_id = str(user_id or "").strip()
    if not normalized_user_id:
        return False

    if users_collection is not None:
        user_doc = users_collection.find_one(_user_lookup_filter(normalized_user_id), {"_id": 1})
        if user_doc:
            return True

    if conversations_collection is not None:
        conversation_doc = conversations_collection.find_one({"user_id": normalized_user_id}, {"_id": 1})
        if conversation_doc:
            return True

    return False


def has_recent_user_interaction(user_id: str, within_hours: int = 24) -> bool:
    normalized_user_id = str(user_id or "").strip()
    if not normalized_user_id or within_hours <= 0:
        return False

    if conversations_collection is None or messages_collection is None:
        return False

    try:
        conversation_ids = [
            item["_id"]
            for item in conversations_collection.find({"user_id": normalized_user_id}, {"_id": 1})
            if item.get("_id") is not None
        ]
        if not conversation_ids:
            return False

        threshold_ms = _now_ms() - (within_hours * 60 * 60 * 1000)
        recent_message = messages_collection.find_one(
            {
                "conversation_id": {"$in": conversation_ids},
                "sender": "user",
                "timestamp": {"$gte": threshold_ms},
            },
            {"_id": 1},
        )
        return recent_message is not None
    except Exception as exc:
        logger.error("Failed to check recent interaction for user=%s: %s", normalized_user_id, exc)
        return False


def upsert_facebook_comment_log(
    comment_id: str,
    user_id: str,
    message: str,
    post_id: str,
    created_at: Optional[int] = None,
    matched_keyword: str = "",
) -> None:
    if facebook_comments_collection is None:
        return

    normalized_comment_id = str(comment_id or "").strip()
    if not normalized_comment_id:
        return

    normalized_user_id = str(user_id or "").strip()
    normalized_post_id = str(post_id or "").strip()
    now_ms = _now_ms()
    created_at_value = created_at if isinstance(created_at, int) and created_at > 0 else now_ms
    matched_value = str(matched_keyword or "").strip()

    try:
        facebook_comments_collection.update_one(
            {"id": normalized_comment_id},
            {
                "$set": {
                    "user_id": normalized_user_id,
                    "message": (message or "").strip(),
                    "post_id": normalized_post_id,
                    "updated_at": now_ms,
                    "matched_keyword": matched_value,
                },
                "$setOnInsert": {
                    "id": normalized_comment_id,
                    "created_at": created_at_value,
                    "is_replied": False,
                    "is_messaged": False,
                    "message_status": "received",
                    "reply_status": "not_requested",
                },
            },
            upsert=True,
        )
    except Exception as exc:
        logger.error("Failed to upsert facebook comment log for comment=%s: %s", normalized_comment_id, exc)


def get_facebook_comment_log(comment_id: str) -> Optional[dict]:
    if facebook_comments_collection is None:
        return None

    normalized_comment_id = str(comment_id or "").strip()
    if not normalized_comment_id:
        return None

    try:
        return facebook_comments_collection.find_one({"id": normalized_comment_id})
    except Exception as exc:
        logger.error("Failed to get facebook comment log for comment=%s: %s", normalized_comment_id, exc)
        return None


def update_facebook_comment_log(comment_id: str, updates: dict) -> None:
    if facebook_comments_collection is None:
        return

    normalized_comment_id = str(comment_id or "").strip()
    if not normalized_comment_id or not updates:
        return

    update_payload = {key: value for key, value in (updates or {}).items()}
    update_payload["updated_at"] = _now_ms()

    try:
        facebook_comments_collection.update_one(
            {"id": normalized_comment_id},
            {"$set": update_payload},
            upsert=False,
        )
    except Exception as exc:
        logger.error("Failed to update facebook comment log for comment=%s: %s", normalized_comment_id, exc)


def save_user_avatar_asset(user_id: str, image_bytes: bytes, content_type: str, source_url: str = "") -> bool:
    if user_avatars_collection is None:
        return False

    normalized_user_id = str(user_id or "").strip()
    if not normalized_user_id or not image_bytes:
        return False

    now_ms = _now_ms()
    normalized_content_type = (content_type or "").strip().lower() or "image/jpeg"
    normalized_source_url = (source_url or "").strip()

    try:
        user_avatars_collection.update_one(
            {"user_id": normalized_user_id},
            {
                "$set": {
                    "user_id": normalized_user_id,
                    "content_type": normalized_content_type,
                    "data": Binary(image_bytes),
                    "size": len(image_bytes),
                    "source_url": normalized_source_url,
                    "updated_at": now_ms,
                },
                "$setOnInsert": {"created_at": now_ms},
            },
            upsert=True,
        )
        return True
    except Exception as exc:
        logger.error("Failed to save user avatar asset for %s: %s", normalized_user_id, exc)
        return False


def get_user_avatar_asset(user_id: str) -> Optional[dict]:
    if user_avatars_collection is None:
        return None

    normalized_user_id = str(user_id or "").strip()
    if not normalized_user_id:
        return None

    try:
        avatar_doc = user_avatars_collection.find_one(
            {"user_id": normalized_user_id},
            {"_id": 0, "content_type": 1, "data": 1, "size": 1, "updated_at": 1},
        )
    except Exception as exc:
        logger.error("Failed to load user avatar asset for %s: %s", normalized_user_id, exc)
        return None

    if not avatar_doc:
        return None

    raw_data = avatar_doc.get("data")
    if isinstance(raw_data, Binary):
        binary_data = bytes(raw_data)
    elif isinstance(raw_data, (bytes, bytearray)):
        binary_data = bytes(raw_data)
    else:
        return None

    if not binary_data:
        return None

    return {
        "content_type": (avatar_doc.get("content_type") or "image/jpeg"),
        "data": binary_data,
        "size": avatar_doc.get("size"),
        "updated_at": avatar_doc.get("updated_at"),
    }


def update_user_profile(sender_id: str, profile_data: dict) -> None:
    if users_collection is not None:
        try:
            user_id = str(sender_id or "").strip()
            now_ms = _now_ms()

            full_name = (profile_data.get("name") or "").strip()
            if not full_name:
                first_name = (profile_data.get("first_name") or "").strip()
                last_name = (profile_data.get("last_name") or "").strip()
                full_name = " ".join(part for part in (first_name, last_name) if part).strip()

            profile_pic = (profile_data.get("profile_pic") or profile_data.get("avatar") or "").strip()

            update_data = {
                "user_id": user_id,
                "psid": user_id,
                "updated_at": now_ms,
                "name": full_name or None,
                "profile_pic": profile_pic or None,
                "first_name": profile_data.get("first_name"),
                "last_name": profile_data.get("last_name"),
            }
            update_data = {key: value for key, value in update_data.items() if value is not None}

            if update_data:
                users_collection.update_one(
                    _user_lookup_filter(user_id),
                    {
                        "$set": update_data,
                        "$unset": {"avatar": ""},
                        "$setOnInsert": {"created_at": now_ms},
                    },
                    upsert=True,
                )
        except Exception as exc:
            logger.error("Failed to update user profile in MongoDB: %s", exc)


def set_user_state(sender_id: str, new_state: str) -> None:
    if users_collection is not None:
        try:
            user_id = str(sender_id or "").strip()
            now_ms = _now_ms()
            users_collection.update_one(
                _user_lookup_filter(user_id),
                {
                    "$set": {
                        "user_id": user_id,
                        "psid": user_id,
                        "state": new_state,
                        "updated_at": now_ms,
                    },
                    "$setOnInsert": {"created_at": now_ms},
                },
                upsert=True,
            )
        except Exception as exc:
            logger.error("Failed to update state in MongoDB: %s", exc)


def has_user_been_greeted(sender_id: str) -> bool:
    if users_collection is not None:
        user_doc = users_collection.find_one(_user_lookup_filter(sender_id))
        if user_doc and user_doc.get("has_greeted"):
            return True
    return False


def mark_user_as_greeted(sender_id: str) -> None:
    if users_collection is not None:
        try:
            user_id = str(sender_id or "").strip()
            now_ms = _now_ms()
            users_collection.update_one(
                _user_lookup_filter(user_id),
                {
                    "$set": {
                        "user_id": user_id,
                        "psid": user_id,
                        "has_greeted": True,
                        "updated_at": now_ms,
                    },
                    "$setOnInsert": {"created_at": now_ms},
                },
                upsert=True,
            )
        except Exception as exc:
            logger.error("Failed to mark user as greeted in MongoDB: %s", exc)


def upsert_conversation(user_id: str, page_id: str, last_message: str, updated_at: Optional[int] = None) -> Optional[ObjectId]:
    if conversations_collection is None:
        return None

    timestamp = updated_at if updated_at is not None else _now_ms()
    page_value = (page_id or "").strip()

    try:
        conversations_collection.update_one(
            {"user_id": user_id, "page_id": page_value},
            {
                "$set": {
                    "last_message": last_message,
                    "updated_at": timestamp,
                },
                "$setOnInsert": {"created_at": timestamp},
            },
            upsert=True,
        )
        conversation = conversations_collection.find_one(
            {"user_id": user_id, "page_id": page_value},
            {"_id": 1},
        )
        if conversation:
            return conversation.get("_id")
    except Exception as exc:
        logger.error("Failed to upsert conversation user=%s page=%s: %s", user_id, page_value, exc)
    return None


def save_conversation_message(
    conversation_id: object,
    sender: str,
    content: str,
    timestamp: Optional[int] = None,
    message_id: Optional[str] = None,
) -> Optional[InsertOneResult]:
    if messages_collection is None:
        return None

    conversation_oid = _to_object_id(conversation_id)
    if conversation_oid is None:
        logger.warning("Invalid conversation_id when saving message: %s", conversation_id)
        return None

    payload = {
        "conversation_id": conversation_oid,
        "sender": sender,
        "content": content,
        "timestamp": timestamp if timestamp is not None else _now_ms(),
    }
    if message_id:
        payload["message_id"] = message_id

    try:
        return messages_collection.insert_one(payload)
    except Exception as exc:
        logger.error("Failed to save message conversation=%s sender=%s: %s", conversation_id, sender, exc)
        return None


def save_incoming_message(
    user_id: str,
    page_id: str,
    content: str,
    timestamp: Optional[int] = None,
    message_id: Optional[str] = None,
) -> Optional[ObjectId]:
    conversation_id = upsert_conversation(
        user_id=user_id,
        page_id=page_id,
        last_message=content,
        updated_at=timestamp,
    )
    if conversation_id is None:
        return None

    save_conversation_message(
        conversation_id=conversation_id,
        sender="user",
        content=content,
        timestamp=timestamp,
        message_id=message_id,
    )
    return conversation_id


def save_outgoing_message(
    user_id: str,
    page_id: str,
    content: str,
    timestamp: Optional[int] = None,
    message_id: Optional[str] = None,
) -> Optional[ObjectId]:
    conversation_id = upsert_conversation(
        user_id=user_id,
        page_id=page_id,
        last_message=content,
        updated_at=timestamp,
    )
    if conversation_id is None:
        return None

    save_conversation_message(
        conversation_id=conversation_id,
        sender="page",
        content=content,
        timestamp=timestamp,
        message_id=message_id,
    )
    return conversation_id


def save_message(sender_id: str, sender_type: str, text: str, timestamp: int = None) -> None:
    sender = "user" if sender_type == "user" else "page"
    page_id = ""
    conversation_id = upsert_conversation(
        user_id=sender_id,
        page_id=page_id,
        last_message=text,
        updated_at=timestamp,
    )
    if conversation_id is not None:
        save_conversation_message(
            conversation_id=conversation_id,
            sender=sender,
            content=text,
            timestamp=timestamp,
        )


def get_chat_history(sender_id: str, limit: int = 10) -> list:
    if conversations_collection is None or messages_collection is None:
        return []

    try:
        conversation = conversations_collection.find_one(
            {"user_id": sender_id},
            sort=[("updated_at", -1)],
        )
        if not conversation:
            return []

        conversation_id = conversation.get("_id")
        cursor = (
            messages_collection.find(
                {"conversation_id": conversation_id},
                {"_id": 0, "sender": 1, "content": 1, "timestamp": 1},
            )
            .sort("timestamp", -1)
            .limit(limit)
        )
        items = list(cursor)
        items.reverse()

        history = []
        for item in items:
            sender = item.get("sender")
            history.append(
                {
                    "sender_type": "user" if sender == "user" else "bot",
                    "text": item.get("content", ""),
                    "timestamp": item.get("timestamp"),
                }
            )
        return history
    except Exception as exc:
        logger.error("Failed to fetch chat history from MongoDB: %s", exc)
        return []
