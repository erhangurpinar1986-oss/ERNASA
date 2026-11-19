"""Bind CONTROL to an explicit HTTPS host without duplicating production records."""
import os
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path
from threading import Thread
from urllib.parse import urlsplit

import httpx
from starlette.responses import PlainTextResponse, Response

from . import app, postgres, sales


# These are strictly new sales tables; existing CONTROL, İK and Academy tables stay untouched.
SALES_TABLES = frozenset({
    "sales_accounts", "sales_sessions", "sales_customers",
    "sales_customer_identities", "sales_claims", "sales_opportunities",
    "sales_activities", "sales_proposals", "sales_decisions",
    "sales_subscriptions", "sales_collections", "sales_commissions",
    "sales_adjustments", "sales_audit",
})


def ensure_sales_schema(db):
    """Install only a missing, complete Sales Hub schema in one PG transaction.

    Never repair a partly installed schema automatically; that needs operator review.
    This is called only for configured CONTROL PostgreSQL, never for HR databases.
    """
    if not hasattr(db, "raw"):
        return
    with db.raw:
        with db.raw.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(184093, 47622)")
            cursor.execute(
                "SELECT tablename FROM pg_catalog.pg_tables "
                "WHERE schemaname='ernasa_control' AND tablename LIKE 'sales\\_%' ESCAPE '\\'"
            )
            existing = {row[0] for row in cursor.fetchall()}
            missing = SALES_TABLES - existing
            if not missing:
                return
            if existing:
                raise RuntimeError(
                    "Sales Hub şeması kısmen mevcut; otomatik kurulum durduruldu."
                )
            cursor.execute("SET LOCAL search_path = ernasa_control, pg_catalog")
            cursor.execute(sales.DDL)
            migrations = Path(__file__).resolve().parents[1] / "migrations" / "control_postgresql"
            cursor.execute((migrations / "003_sales_hub.up.sql").read_text(encoding="utf-8"))
            cursor.execute(
                "SELECT tablename FROM pg_catalog.pg_tables "
                "WHERE schemaname='ernasa_control' AND tablename LIKE 'sales\\_%' ESCAPE '\\'"
            )
            installed = {row[0] for row in cursor.fetchall()}
            if not SALES_TABLES.issubset(installed):
                raise RuntimeError("Sales Hub tablo doğrulaması başarısız.")


@contextmanager
def start_control():
    origin = os.getenv("ERNASA_CONTROL_PUBLIC_ORIGIN", "")
    configured_path = os.getenv("ERNASA_CONTROL_DB_PATH", "")
    dsn = os.getenv("ERNASA_CONTROL_DATABASE_URL", "")
    if not origin and not configured_path and not dsn:
        yield None
        return
    url = urlsplit(origin)
    path = Path(configured_path)
    if (url.scheme != "https" or not url.hostname or url.username or url.password
            or url.path or url.query or url.fragment):
        raise RuntimeError("CONTROL requires an HTTPS origin.")
    if dsn and configured_path:
        raise RuntimeError("Configure a single CONTROL database.")
    if not dsn and (not path.is_absolute() or not path.is_file()):
        raise RuntimeError("CONTROL requires a PostgreSQL URL or an existing local database path.")
    target = postgres.Target(dsn) if dsn else path
    if len(os.getenv("ERNASA_IK_CONTROL_KEY", "")) < 32:
        raise RuntimeError("CONTROL bridge key missing or too short.")
    if os.getenv("ERNASA_IK_LOCAL_PREVIEW") == "1" or os.getenv("ERNASA_IK_PORTAL_PORT"):
        raise RuntimeError("Preview settings are incompatible with this CONTROL gateway.")
    connection = app.connect(target) if dsn else sqlite3.connect(path.as_uri()+"?mode=ro",uri=True)
    with closing(connection) as db:
        if not db.execute("SELECT 1 FROM users WHERE role='founder' AND active=1").fetchone():
            raise RuntimeError("The existing CONTROL database has no active founder.")
        db.execute("SELECT 1 FROM ik_interview_templates LIMIT 1").fetchone()
        if dsn:
            ensure_sales_schema(db)

    with app.server(target,0) as server:
        import candidate_experience
        server.experience_summary = candidate_experience.summary
        server.origin = origin
        server.public_prefix = "/control"
        server.internal_host = f"127.0.0.1:{server.server_port}"
        old = os.environ.get("ERNASA_IK_CONTROL_URL")
        os.environ["ERNASA_IK_CONTROL_URL"] = f"http://{server.internal_host}"
        worker = Thread(target=server.serve_forever,daemon=True)
        worker.start()
        try:
            yield server
        finally:
            server.shutdown()
            worker.join()
            if old is None:
                os.environ.pop("ERNASA_IK_CONTROL_URL",None)
            else:
                os.environ["ERNASA_IK_CONTROL_URL"] = old


async def forward(request, server):
    if request.headers.get("host", "").lower() != urlsplit(server.origin).netloc.lower():
        return PlainTextResponse("Bulunamadı", status_code=404)
    path = request.scope["path"].removeprefix("/control") or "/"
    if path.startswith("//") or path.rstrip("/") == "/internal/ik" or request.method not in {"GET","POST"}:
        return PlainTextResponse("Bulunamadı",status_code=404)
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > 10*1024*1024+8192:
            return PlainTextResponse("İstek boyutu sınır dışında.",status_code=413)
    headers = {name:request.headers[name] for name in ("cookie","origin","content-type") if name in request.headers}
    headers["host"] = urlsplit(server.origin).netloc
    url = httpx.URL(scheme="http",host="127.0.0.1",port=server.server_port,path=path,query=request.scope["query_string"])
    try:
        async with httpx.AsyncClient(trust_env=False,follow_redirects=False,timeout=35) as client:
            result = await client.request(request.method,url,headers=headers,content=bytes(data))
    except httpx.HTTPError:
        return PlainTextResponse("CONTROL şu anda kullanılamıyor.",status_code=503)
    response = Response(result.content,status_code=result.status_code)
    allowed = {"content-type","cache-control","content-security-policy","referrer-policy","x-content-type-options",
               "location","set-cookie"}
    for name,value in result.headers.multi_items():
        if name in allowed:
            response.headers.append(name,value)
    return response
