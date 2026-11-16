"""Browser-only OAuth endpoints, separate from delegated club API permissions."""

from html import escape

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse

from cu_clubs.services import ValidationError
from cu_clubs.yandex_auth import YandexAuth, exchange

COOKIE = "cu_clubs_yandex"
HEADERS = {
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
}


def result_page(settings, text, *, status=200):
    username = settings.telegram_bot_username or ""
    body = (
        '<!doctype html><html lang="ru"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "<title>Вход в CU Clubs</title><body><h1>Вход в CU Clubs</h1><p>"
        + escape(text)
        + '</p><p><a href="https://t.me/'
        + escape(username, quote=True)
        + '">Вернуться в бот</a></p></body></html>'
    )
    response = HTMLResponse(body, status_code=status, headers=HEADERS)
    response.delete_cookie(COOKIE, path="/auth/yandex", secure=True, httponly=True, samesite="lax")
    return response


def install_yandex(app, settings):
    @app.get("/auth/yandex/start", include_in_schema=False)
    async def start(request: Request):
        if not settings.yandex_auth_enabled:
            return result_page(settings, "Вход через Яндекс пока не включён.", status=503)
        try:
            async with app.state.sessions.begin() as session:
                url, browser = await YandexAuth(session, settings).start_browser(
                    request.query_params.get("ticket", "")[:300]
                )
        except ValidationError as error:
            return result_page(settings, str(error), status=400)
        response = RedirectResponse(url, status_code=303, headers=HEADERS)
        response.set_cookie(
            COOKIE,
            browser,
            max_age=600,
            path="/auth/yandex",
            secure=True,
            httponly=True,
            samesite="lax",
        )
        return response

    @app.get("/auth/yandex/callback", include_in_schema=False)
    async def callback(request: Request):
        if not settings.yandex_auth_enabled:
            return result_page(settings, "Вход через Яндекс пока не включён.", status=503)
        try:
            async with app.state.sessions.begin() as session:
                key = await YandexAuth(session, settings).claim_callback(
                    request.query_params.get("state", ""),
                    request.cookies.get(COOKIE, ""),
                )
            # Commit single-use state first; never hold DB locks across provider I/O.
            profile = None
            if not request.query_params.get("error"):
                profile = await exchange(settings, key, request.query_params.get("code", ""))
            async with app.state.sessions.begin() as session:
                accepted = await YandexAuth(session, settings).record_result(key, profile)
        except ValidationError as error:
            return result_page(settings, str(error), status=400)
        if not accepted:
            return result_page(
                settings,
                "Вход не завершён. Войди с университетской почтой "
                "@edu.centraluniversity.ru через новую ссылку в боте.",
                status=400,
            )
        return result_page(
            settings, "Вернись в бот, нажми «Проверить вход» и подтверди привязку почты."
        )
