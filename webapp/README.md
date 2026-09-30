# Telegram Mini App

This is the operator report Mini App served by `main.py` on `WEBAPP_HOST:WEBAPP_PORT`.

Telegram requires `WEBAPP_URL` to be a public HTTPS URL. Point that URL at this server (directly on a supported host, or through an HTTPS reverse proxy). Set `WEBAPP_URL`, `WEBAPP_HOST`, and `WEBAPP_PORT` in `.env`, restart the bot, then send `/start` and choose a language to refresh the reply keyboard.

The API rejects requests without a valid, recent Telegram `initData` signature. Opening the page in a normal browser shows a notice instead of granting data access.
