"""Autenticación mínima por contraseña única para las acciones protegidas.

La supervisión es pública; arrancar/parar el bot y editar la configuración exigen
login. Se usa una sola contraseña (``DASHBOARD_PASSWORD``) y la sesión se guarda
en una cookie firmada (``SessionMiddleware`` con ``DASHBOARD_SECRET``).
"""

from __future__ import annotations

import hmac
import os

from fastapi import HTTPException, Request, status
from fastapi.responses import RedirectResponse

_SESSION_KEY = "authed"


def password_configured() -> bool:
    return bool(os.getenv("DASHBOARD_PASSWORD"))


def verify_password(candidate: str) -> bool:
    """Compara en tiempo constante con ``DASHBOARD_PASSWORD``."""
    expected = os.getenv("DASHBOARD_PASSWORD", "")
    if not expected:
        return False
    return hmac.compare_digest(candidate, expected)


def login_session(request: Request) -> None:
    request.session[_SESSION_KEY] = True


def logout_session(request: Request) -> None:
    request.session.pop(_SESSION_KEY, None)


def is_authed(request: Request) -> bool:
    return bool(request.session.get(_SESSION_KEY))


def require_login(request: Request) -> None:
    """Dependencia FastAPI: redirige a /login si no hay sesión válida.

    Para peticiones HTMX/AJAX devuelve 401 en vez de redirigir (el front decide).
    """
    if is_authed(request):
        return
    if request.headers.get("HX-Request") == "true":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    raise HTTPException(
        status_code=status.HTTP_303_SEE_OTHER,
        headers={"Location": "/login"},
    )
