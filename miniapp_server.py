import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from aiohttp import web
from aiogram.utils.web_app import safe_parse_webapp_init_data


WEBAPP_DIR = Path(__file__).resolve().parent / "webapp"
MAX_MEDIA_SIZE = 20 * 1024 * 1024
ALLOWED_MEDIA_TYPES = {
    "image/jpeg",
    "image/png",
    "image/webp",
    "video/mp4",
    "video/quicktime",
}


def create_miniapp_app(
    bot_token: str,
    load_operator_data: Callable[[int], dict | None],
    submit_report: Callable[..., object],
) -> web.Application:
    app = web.Application(client_max_size=MAX_MEDIA_SIZE + 1024 * 1024)

    def get_telegram_user(request: web.Request) -> int:
        authorization = request.headers.get("Authorization", "")
        if not authorization.startswith("tma "):
            raise web.HTTPUnauthorized(
                text=json.dumps({"error": "Telegram authorization required"}),
                content_type="application/json",
            )

        try:
            init_data = safe_parse_webapp_init_data(
                bot_token, authorization[4:]
            )
        except (ValueError, TypeError):
            raise web.HTTPUnauthorized(
                text=json.dumps({"error": "Invalid Telegram authorization"}),
                content_type="application/json",
            ) from None

        if init_data.user is None or init_data.auth_date is None:
            raise web.HTTPUnauthorized(
                text=json.dumps({"error": "Telegram user data is missing"}),
                content_type="application/json",
            )

        now = datetime.now(timezone.utc)
        auth_date = init_data.auth_date
        if auth_date.tzinfo is None:
            auth_date = auth_date.replace(tzinfo=timezone.utc)
        if auth_date > now + timedelta(minutes=1) or now - auth_date > timedelta(days=1):
            raise web.HTTPUnauthorized(
                text=json.dumps({"error": "Telegram authorization expired"}),
                content_type="application/json",
            )
        return init_data.user.id

    async def index(_: web.Request) -> web.FileResponse:
        return web.FileResponse(WEBAPP_DIR / "index.html")

    async def bootstrap(request: web.Request) -> web.Response:
        telegram_id = get_telegram_user(request)
        operator_data = load_operator_data(telegram_id)
        if operator_data is None:
            raise web.HTTPForbidden(
                text=json.dumps({"error": "Operator access is not enabled"}),
                content_type="application/json",
            )
        return web.json_response(operator_data)

    async def create_report(request: web.Request) -> web.Response:
        telegram_id = get_telegram_user(request)
        operator_data = load_operator_data(telegram_id)
        if operator_data is None:
            raise web.HTTPForbidden(
                text=json.dumps({"error": "Operator access is not enabled"}),
                content_type="application/json",
            )

        try:
            form = await request.post()
            object_id = int(form.get("object_id", ""))
            issue_type = str(form.get("issue_type", "")).strip()
            description = str(form.get("description", "")).strip()
            media = form.get("media")
        except (ValueError, TypeError):
            raise web.HTTPBadRequest(
                text=json.dumps({"error": "Invalid report fields"}),
                content_type="application/json",
            ) from None

        permitted_ids = {item["id"] for item in operator_data["objects"]}
        if object_id not in permitted_ids:
            raise web.HTTPForbidden(
                text=json.dumps({"error": "Object is not assigned to this operator"}),
                content_type="application/json",
            )
        allowed_issue_types = {
            item["value"] for item in operator_data["issue_types"]
        }
        if issue_type not in allowed_issue_types:
            raise web.HTTPBadRequest(
                text=json.dumps({"error": "Invalid issue type"}),
                content_type="application/json",
            )
        if len(description) > 500:
            raise web.HTTPBadRequest(
                text=json.dumps({"error": "Description is too long"}),
                content_type="application/json",
            )
        if not isinstance(media, web.FileField):
            raise web.HTTPBadRequest(
                text=json.dumps({"error": "Photo or video is required"}),
                content_type="application/json",
            )
        if media.content_type not in ALLOWED_MEDIA_TYPES:
            raise web.HTTPBadRequest(
                text=json.dumps({"error": "Unsupported media format"}),
                content_type="application/json",
            )

        media_bytes = media.file.read(MAX_MEDIA_SIZE + 1)
        if not media_bytes or len(media_bytes) > MAX_MEDIA_SIZE:
            raise web.HTTPRequestEntityTooLarge(
                max_size=MAX_MEDIA_SIZE,
                actual_size=len(media_bytes),
            )

        try:
            result = await submit_report(
                telegram_id=telegram_id,
                object_id=object_id,
                issue_type=issue_type,
                description=description,
                media_bytes=media_bytes,
                media_name=Path(media.filename or "upload").name,
                media_type=media.content_type,
            )
        except ValueError as exc:
            raise web.HTTPBadRequest(
                text=json.dumps({"error": str(exc)}),
                content_type="application/json",
            ) from exc
        except Exception:
            logging.exception("Mini App reportni guruhga yuborishda xatolik")
            raise web.HTTPBadGateway(
                text=json.dumps({"error": "Report could not be delivered to the group"}),
                content_type="application/json",
            ) from None
        return web.json_response(result, status=201)

    app.router.add_get("/", index)
    app.router.add_get("/styles.css", lambda _: web.FileResponse(WEBAPP_DIR / "styles.css"))
    app.router.add_get("/app.js", lambda _: web.FileResponse(WEBAPP_DIR / "app.js"))
    app.router.add_get("/api/bootstrap", bootstrap)
    app.router.add_post("/api/reports", create_report)
    return app