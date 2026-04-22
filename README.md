# Facebook Fanpage Chatbot

Chatbot Messenger + auto inbox tu comment cho Facebook Fanpage, viet bang Flask.
Du an ho tro 2 luong chinh:

- Nhac tra loi tin nhan Messenger (webhook `messages`)
- Tu dong inbox khi user comment bai viet (webhook `feed/comment`)

## Tinh nang

- Verify webhook Meta (`/webhook` GET + `VERIFY_TOKEN`)
- Verify signature webhook POST (`X-Hub-Signature-256`) neu co `APP_SECRET`
- Tu dong gui tin nhan text qua Messenger Send API
- Tu dong reply comment truoc khi gui inbox (co the bat/tat)
- Loc comment noise/spam truoc khi gui inbox
- Luu log user, conversation, message, comment vao MongoDB
- Cache avatar user vao MongoDB va phuc vu qua endpoint noi bo
- Retry + exponential backoff khi goi Facebook Graph API

## Kien truc nhanh

- `app.py`: Flask app, webhook handler, queue worker cho auto comment
- `chatbot_logic.py`: logic sinh noi dung tra loi va spam/noise filter
- `facebook_api.py`: wrapper cho Graph API (send message, reply comment, profile, signature)
- `database.py`: thao tac MongoDB
- `api/index.py`: entrypoint de deploy len Vercel

## Yeu cau

- Python `3.12.x`
- MongoDB (local hoac cloud)
- Meta App da ket noi voi Page
- Page Access Token hop le

## Cai dat local

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

## Bien moi truong quan trong

Chinh trong file `.env`:

- `VERIFY_TOKEN`: token verify webhook
- `FACEBOOK_PAGE_TOKEN` hoac `PAGE_ACCESS_TOKEN`: token gui tin nhan/reply comment
- `PAGE_ID`: ID fanpage
- `APP_SECRET`: dung de verify signature webhook POST
- `GEMINI_API_KEY`: bat AI generation (neu bo trong se dung fallback message)
- `MONGO_URI`, `MONGO_DB_NAME`: ket noi database
- `PORT`: cong chay local (mac dinh `5000`)

Nhom bien auto inbox comment:

- `FACEBOOK_COMMENT_AUTOMATION_ENABLED=true|false`
- `FACEBOOK_COMMENT_REPLY_ENABLED=true|false`
- `FACEBOOK_COMMENT_REPLY_TEMPLATE=...`
- `FACEBOOK_COMMENT_DELAY_MIN_SECONDS`, `FACEBOOK_COMMENT_DELAY_MAX_SECONDS`
- `FACEBOOK_COMMENT_REQUIRE_PREVIOUS_INTERACTION`
- `FACEBOOK_COMMENT_ENFORCE_24H_WINDOW`
- `FACEBOOK_COMMENT_WINDOW_HOURS`
- `FACEBOOK_COMMENT_QUEUE_SIZE`
- `FACEBOOK_COMMENT_WORKERS`

## Chay local

```powershell
python app.py
```

Endpoint local:

- `GET /` -> chatbot running
- `GET /health` -> health check
- `GET /webhook` -> verify webhook
- `POST /webhook` -> nhan event tu Meta
- `GET /media/avatars/<user_id>` -> tra avatar cache

De test webhook tu internet, mo tunnel:

```powershell
ngrok http 5000
```

Sau do dung callback URL: `https://<ngrok-domain>/webhook`

## Cau hinh tren Meta Developers

1. Tao App va them san pham Messenger
2. Ket noi app voi fanpage can dung
3. Cau hinh Webhooks callback URL + `VERIFY_TOKEN`
4. Subscribe cac event:
   - `messages`
   - `feed` (de nhan event comment)
5. Tao/cap lai page access token neu token het han

## Luong auto inbox comment

1. User comment bai viet cua page
2. Meta gui `feed/comment` ve `/webhook`
3. Server log comment vao `facebook_comments`
4. Bot danh gia noise/spam
5. Neu hop le:
   - Tao message inbox (AI hoac fallback)
   - Co the reply comment (neu bat)
   - Delay theo config
   - Gui inbox qua `/me/messages`
6. Update trang thai: `message_status`, `is_replied`, `is_messaged`

## Deploy Vercel

Repo da co `vercel.json` va entrypoint `api/index.py`.

- Import repo len Vercel
- Set day du env vars trong Project Settings
- Deploy

## Troubleshooting nhanh

- Loi token (Facebook error code `190`): cap lai `PAGE_ACCESS_TOKEN`
- Webhook bi `403`: kiem tra `APP_SECRET`, `VERIFY_TOKEN`, callback URL
- Khong gui duoc inbox tu comment: xem cac flag
  `FACEBOOK_COMMENT_AUTOMATION_ENABLED`,
  `FACEBOOK_COMMENT_REQUIRE_PREVIOUS_INTERACTION`,
  `FACEBOOK_COMMENT_ENFORCE_24H_WINDOW`
