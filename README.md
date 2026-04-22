# Facebook Fanpage Chatbot (Python + Flask)

## 1) Cai dat

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## 2) Cau hinh bien moi truong

```powershell
Copy-Item .env.example .env
```

Sua file `.env`:
- `VERIFY_TOKEN`: token ban tu dat de verify webhook
- `FACEBOOK_PAGE_TOKEN` (uu tien) hoac `PAGE_ACCESS_TOKEN`: page access token cua fanpage
- `APP_SECRET`: app secret cua Meta app
- `MONGO_URI`, `MONGO_DB_NAME`: cau hinh MongoDB

### Bien cho auto inbox khi comment
- `FACEBOOK_COMMENT_AUTOMATION_ENABLED`: bat/tat tinh nang
- `FACEBOOK_COMMENT_KEYWORDS`: danh sach keyword cach nhau bang dau phay (chi de log/phan tich, khong con dung de chan auto inbox)
- `FACEBOOK_COMMENT_REPLY_ENABLED`: bat/tat reply comment
- `FACEBOOK_COMMENT_REPLY_TEMPLATE`: noi dung reply comment
- `FACEBOOK_COMMENT_DELAY_MIN_SECONDS`, `FACEBOOK_COMMENT_DELAY_MAX_SECONDS`: delay truoc khi gui inbox
- `FACEBOOK_COMMENT_REQUIRE_PREVIOUS_INTERACTION`: chi gui khi user da tuong tac voi page
- `FACEBOOK_COMMENT_ENFORCE_24H_WINDOW`: bat rang buoc 24h messaging window
- `FACEBOOK_COMMENT_WINDOW_HOURS`: so gio cho cua so messaging (mac dinh 24)
- `FACEBOOK_COMMENT_QUEUE_SIZE`, `FACEBOOK_COMMENT_WORKERS`: queue + worker xu ly event comment

## 3) Chay local

```powershell
python app.py
```

Mo tunnel (vi du ngrok):

```powershell
ngrok http 5000
```

Webhook URL:
- `https://<ngrok-domain>/webhook`

## 4) Cau hinh tren Meta Developer

- Tao app, them san pham Messenger, ket noi fanpage
- Cau hinh webhook callback URL + verify token
- Subscribe event:
  - `messages`
  - `feed` (de nhan comment event)
- Cap quyen va dung page access token hop le

## 5) Auto inbox flow cho comment

1. User comment vao bai viet cua page
2. Facebook gui webhook `feed/comment` ve `/webhook`
3. Server parse event + log vao collection `facebook_comments`
4. Bot loc comment noise/spam (vd: `"."`, comment vo nghia, spam link/quang cao)
5. Neu comment hop le:
   - AI tao noi dung inbox lien quan truc tiep den comment
   - optional reply comment (`/{comment-id}/comments`)
   - delay 1-3s (config)
   - gui tin nhan Messenger (`/me/messages`)
6. Cap nhat trang thai log `is_replied`, `is_messaged`, `message_status`

## 6) Luu y

- Webhook POST da verify signature (`X-Hub-Signature-256`) khi co `APP_SECRET`
- API send/reply da co retry + exponential backoff
- Khi token het han se log `CRITICAL` de ban co the alert tren he thong monitoring
