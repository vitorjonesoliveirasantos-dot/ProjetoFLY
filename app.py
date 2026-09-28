"""Private Mercado Livre API bridge. Never expose credentials in responses or logs."""
import os
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
import psycopg
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

API = "https://api.mercadolibre.com"
AUTH = "https://auth.mercadolivre.com.br/authorization"


def setting(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise HTTPException(503, f"Configure {name} no servidor")
    return value


def database():
    return psycopg.connect(setting("DATABASE_URL"))


@asynccontextmanager
async def lifespan(_app):
    if os.getenv("DATABASE_URL"):
        with database() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS oauth_states (state TEXT PRIMARY KEY, expires_at TIMESTAMPTZ NOT NULL)")
            conn.execute("CREATE TABLE IF NOT EXISTS seller_tokens (seller_id BIGINT PRIMARY KEY, access_token TEXT NOT NULL, refresh_token TEXT NOT NULL, expires_at TIMESTAMPTZ NOT NULL)")
    yield


app = FastAPI(title="ProjetoFLY Mercado Livre", lifespan=lifespan)


def require_admin(x_api_key: str | None = Header(default=None)):
    expected = setting("ADMIN_API_KEY")
    if not x_api_key or not secrets.compare_digest(x_api_key, expected):
        raise HTTPException(401, "Chave de acesso inválida")


async def token_for(seller_id: int) -> str:
    with database() as conn:
        row = conn.execute("SELECT access_token, refresh_token, expires_at FROM seller_tokens WHERE seller_id = %s", (seller_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Conta não conectada")
        if row[2] > datetime.now(timezone.utc) + timedelta(minutes=2):
            return row[0]
        async with httpx.AsyncClient(timeout=25) as client:
            res = await client.post(f"{API}/oauth/token", data={"grant_type": "refresh_token", "client_id": setting("MELI_CLIENT_ID"), "client_secret": setting("MELI_CLIENT_SECRET"), "refresh_token": row[1]})
        if res.status_code != 200:
            raise HTTPException(502, "Não foi possível renovar a autorização; conecte a conta novamente")
        data = res.json()
        conn.execute("UPDATE seller_tokens SET access_token=%s, refresh_token=%s, expires_at=%s WHERE seller_id=%s", (data["access_token"], data["refresh_token"], datetime.now(timezone.utc) + timedelta(seconds=data["expires_in"]), seller_id))
        return data["access_token"]


async def meli(method: str, path: str, seller_id: int, *, params=None, body=None):
    token = await token_for(seller_id)
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.request(method, f"{API}{path}", params=params, json=body, headers={"Authorization": f"Bearer {token}"})
    if response.status_code >= 400:
        raise HTTPException(response.status_code, {"message": "Mercado Livre recusou a solicitação", "details": response.json() if "json" in response.headers.get("content-type", "") else response.text[:500]})
    return response.json() if response.content else {}


@app.get("/")
def health():
    return {"status": "ok"}


@app.get("/auth/start", dependencies=[Depends(require_admin)])
def auth_start():
    state = secrets.token_urlsafe(32)
    with database() as conn:
        conn.execute("DELETE FROM oauth_states WHERE expires_at < now()")
        conn.execute("INSERT INTO oauth_states VALUES (%s, %s)", (state, datetime.now(timezone.utc) + timedelta(minutes=10)))
    url = AUTH + "?" + urlencode({"response_type": "code", "client_id": setting("MELI_CLIENT_ID"), "redirect_uri": setting("MELI_REDIRECT_URI"), "scope": "offline_access read write", "state": state})
    return {"authorization_url": url}


@app.get("/auth/callback")
async def auth_callback(code: str, state: str):
    with database() as conn:
        row = conn.execute("DELETE FROM oauth_states WHERE state=%s AND expires_at > now() RETURNING state", (state,)).fetchone()
    if not row:
        raise HTTPException(400, "Autorização expirada ou inválida")
    async with httpx.AsyncClient(timeout=25) as client:
        response = await client.post(f"{API}/oauth/token", data={"grant_type": "authorization_code", "client_id": setting("MELI_CLIENT_ID"), "client_secret": setting("MELI_CLIENT_SECRET"), "code": code, "redirect_uri": setting("MELI_REDIRECT_URI")})
    if response.status_code != 200:
        raise HTTPException(502, "Falha ao conectar a conta; confira o endereço de retorno cadastrado")
    data = response.json()
    if not data.get("refresh_token"):
        raise HTTPException(502, "Mercado Livre não concedeu Refresh Token. Confira o fluxo Refresh Token na aplicação e refaça a autorização da conta.")
    seller_id = int(data["user_id"])
    with database() as conn:
        conn.execute("INSERT INTO seller_tokens VALUES (%s,%s,%s,%s) ON CONFLICT (seller_id) DO UPDATE SET access_token=EXCLUDED.access_token, refresh_token=EXCLUDED.refresh_token, expires_at=EXCLUDED.expires_at", (seller_id, data["access_token"], data["refresh_token"], datetime.now(timezone.utc) + timedelta(seconds=data["expires_in"])))
    return {"connected": True, "seller_id": seller_id}


@app.get("/sellers", dependencies=[Depends(require_admin)])
def sellers():
    with database() as conn:
        rows = conn.execute("SELECT seller_id FROM seller_tokens ORDER BY seller_id").fetchall()
    return {"seller_ids": [row[0] for row in rows]}


@app.get("/sellers/{seller_id}/items", dependencies=[Depends(require_admin)])
async def items(seller_id: int, limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0)):
    return await meli("GET", f"/users/{seller_id}/items/search", seller_id, params={"limit": limit, "offset": offset})


@app.get("/sellers/{seller_id}/items/{item_id}", dependencies=[Depends(require_admin)])
async def item(seller_id: int, item_id: str):
    return await meli("GET", f"/items/{item_id}", seller_id)


@app.get("/sellers/{seller_id}/orders", dependencies=[Depends(require_admin)])
async def orders(seller_id: int, limit: int = Query(50, ge=1, le=50), offset: int = Query(0, ge=0)):
    return await meli("GET", "/orders/search", seller_id, params={"seller": seller_id, "limit": limit, "offset": offset})


class StockUpdate(BaseModel):
    available_quantity: int = Field(ge=0)


class PriceUpdate(BaseModel):
    price: float = Field(gt=0)


@app.put("/sellers/{seller_id}/items/{item_id}/stock", dependencies=[Depends(require_admin)])
async def update_stock(seller_id: int, item_id: str, payload: StockUpdate):
    return await meli("PUT", f"/items/{item_id}", seller_id, body=payload.model_dump())


@app.put("/sellers/{seller_id}/items/{item_id}/price", dependencies=[Depends(require_admin)])
async def update_price(seller_id: int, item_id: str, payload: PriceUpdate):
    return await meli("PUT", f"/items/{item_id}", seller_id, body=payload.model_dump())


@app.post("/sellers/{seller_id}/items", dependencies=[Depends(require_admin)])
async def create_item(seller_id: int, payload: dict):
    if not payload or not payload.get("category_id"):
        raise HTTPException(422, "Informe category_id e os atributos obrigatórios da categoria")
    return await meli("POST", "/items", seller_id, body=payload)
