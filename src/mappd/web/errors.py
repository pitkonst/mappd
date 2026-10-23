"""Error answers: JSON under /api, the error page everywhere else; 503 when the DB is down."""

from typing import TYPE_CHECKING

import psycopg
import sqlalchemy.exc
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from mappd.web.http import ConflictError
from mappd.web.templates import CORE

if TYPE_CHECKING:
    from fastapi import FastAPI, Request
    from starlette.responses import Response

API_PREFIX = "/api/"
DB_DOWN = "the database is unavailable"


def _page(request: Request, status: int, message: str) -> Response:
    context = {"status": status, "message": message}
    return CORE.TemplateResponse(request, "error.html", context, status_code=status)


async def _http_error(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, HTTPException)  # noqa: S101 - registered for HTTPException only
    if request.url.path.startswith(API_PREFIX):
        if isinstance(exc, ConflictError):  # flat, as its OpenAPI model says
            return JSONResponse(exc.body.model_dump(), status_code=409)
        return await http_exception_handler(request, exc)
    return _page(request, exc.status_code, str(exc.detail))


async def _db_down(request: Request, _exc: Exception) -> Response:
    if request.url.path.startswith(API_PREFIX):
        return JSONResponse({"detail": DB_DOWN}, status_code=503)
    return _page(request, 503, DB_DOWN)


def install(app: FastAPI) -> None:
    """Register the handlers on `app`."""
    app.add_exception_handler(HTTPException, _http_error)
    for error in (psycopg.OperationalError, sqlalchemy.exc.OperationalError):
        app.add_exception_handler(error, _db_down)
