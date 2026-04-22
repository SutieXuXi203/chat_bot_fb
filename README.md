# Facebook Fanpage Chatbot

A Flask-based chatbot for Facebook Fanpages with two main automation flows:

- Messenger auto-replies via webhook `messages`
- Auto inbox after post comments via webhook `feed/comment`

## Features

- Meta webhook verification (`GET /webhook` + `VERIFY_TOKEN`)
- POST signature verification (`X-Hub-Signature-256`) when `APP_SECRET` is set
- Automatic text replies through the Messenger Send API
- Optional public reply to a comment before sending inbox
- Noise/spam filtering before sending auto inbox
- MongoDB logging for users, conversations, messages, and comment events
- User avatar caching in MongoDB with an internal media endpoint
- Retry with exponential backoff for Facebook Graph API calls

## Project Structure

- `app.py`: Flask app, webhook handlers, queue workers for comment automation
- `chatbot_logic.py`: reply generation logic and spam/noise filters
- `facebook_api.py`: Facebook Graph API integration (send message, reply comment, profile, signature checks)
- `database.py`: MongoDB access layer
- `api/index.py`: Vercel entrypoint

## Requirements

- Python `3.12.x`
- MongoDB (local or cloud)
- A Meta app connected to your Facebook Page
- A valid Page Access Token

## Local Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

## Important Environment Variables

Configure these in `.env`:

- `VERIFY_TOKEN`: webhook verification token
- `FACEBOOK_PAGE_TOKEN` or `PAGE_ACCESS_TOKEN`: token for sending messages and replying to comments
- `PAGE_ID`: Facebook Page ID
- `APP_SECRET`: used to verify webhook request signatures
- `GEMINI_API_KEY`: enables AI response generation (fallback text is used if empty)
- `MONGO_URI`, `MONGO_DB_NAME`: MongoDB connection settings
- `PORT`: local server port (default: `5000`)

Comment automation variables:

- `FACEBOOK_COMMENT_AUTOMATION_ENABLED=true|false`
- `FACEBOOK_COMMENT_REPLY_ENABLED=true|false`
- `FACEBOOK_COMMENT_REPLY_TEMPLATE=...`
- `FACEBOOK_COMMENT_DELAY_MIN_SECONDS`, `FACEBOOK_COMMENT_DELAY_MAX_SECONDS`
- `FACEBOOK_COMMENT_REQUIRE_PREVIOUS_INTERACTION`
- `FACEBOOK_COMMENT_ENFORCE_24H_WINDOW`
- `FACEBOOK_COMMENT_WINDOW_HOURS`
- `FACEBOOK_COMMENT_QUEUE_SIZE`
- `FACEBOOK_COMMENT_WORKERS`

## Run Locally

```powershell
python app.py
```

Local endpoints:

- `GET /` -> service status
- `GET /health` -> health check
- `GET /webhook` -> webhook verification
- `POST /webhook` -> receives Meta webhook events
- `GET /media/avatars/<user_id>` -> serves cached avatar data

To test webhooks from the internet, open a tunnel:

```powershell
ngrok http 5000
```

Then use callback URL: `https://<ngrok-domain>/webhook`

## Meta Developer Configuration

1. Create a Meta app and add Messenger product
2. Connect the app to the target Facebook Page
3. Configure Webhooks callback URL and `VERIFY_TOKEN`
4. Subscribe to events:
   - `messages`
   - `feed` (for comment events)
5. Generate or refresh your Page Access Token when needed

## Comment Auto-Inbox Flow

1. A user comments on a Page post
2. Meta sends `feed/comment` event to `/webhook`
3. Server stores the event in `facebook_comments`
4. Bot checks whether the comment is noise/spam
5. If valid:
   - Generate inbox message (AI or fallback)
   - Optionally reply to the comment
   - Wait for configured delay
   - Send Messenger inbox via `/me/messages`
6. Update status fields: `message_status`, `is_replied`, `is_messaged`

## Deploy on Vercel

This repository already includes `vercel.json` and `api/index.py`.

- Import the repository into Vercel
- Add all required environment variables in Project Settings
- Deploy

## Quick Troubleshooting

- Token error (Facebook code `190`): refresh `PAGE_ACCESS_TOKEN`
- Webhook returns `403`: verify `APP_SECRET`, `VERIFY_TOKEN`, and callback URL
- Auto inbox from comments does not send: check
  `FACEBOOK_COMMENT_AUTOMATION_ENABLED`,
  `FACEBOOK_COMMENT_REQUIRE_PREVIOUS_INTERACTION`,
  `FACEBOOK_COMMENT_ENFORCE_24H_WINDOW`
