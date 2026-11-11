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


# ---------------------------------------------------------------------------
# Pós-venda, envios, Mercado Ads e conector MCP para o Claude (adicionado em 11/11/2026)
# ---------------------------------------------------------------------------
import json
from fastapi import Request
from fastapi.responses import JSONResponse, Response


def default_seller() -> int:
    """Usa a única conta conectada (a FLY) quando o seller_id não é informado."""
    with database() as conn:
        rows = conn.execute("SELECT seller_id FROM seller_tokens ORDER BY seller_id").fetchall()
    if not rows:
        raise HTTPException(404, "Nenhuma conta do Mercado Livre conectada; abra /auth/start")
    return rows[0][0]


async def meli_h(method: str, path: str, seller_id: int, *, params=None, body=None, headers=None):
    """Igual a meli(), mas aceita cabeçalhos extras (ex.: Api-Version do Mercado Ads, x-format-new)."""
    token = await token_for(seller_id)
    h = {"Authorization": f"Bearer {token}"}
    h.update(headers or {})
    async with httpx.AsyncClient(timeout=40) as client:
        response = await client.request(method, f"{API}{path}", params=params, json=body, headers=h)
    if response.status_code >= 400:
        raise HTTPException(response.status_code, {"message": "Mercado Livre recusou a solicitação", "details": response.json() if "json" in response.headers.get("content-type", "") else response.text[:500]})
    return response.json() if response.content else {}


# ---- Rotas REST (protegidas por X-API-Key) ----

@app.get("/sellers/{seller_id}/questions", dependencies=[Depends(require_admin)])
async def questions(seller_id: int, status: str = "UNANSWERED", limit: int = Query(50, ge=1, le=50), offset: int = 0):
    return await meli("GET", "/questions/search", seller_id, params={"seller_id": seller_id, "status": status, "api_version": 4, "sort_fields": "date_created", "sort_types": "DESC", "limit": limit, "offset": offset})


class Answer(BaseModel):
    question_id: int
    text: str = Field(min_length=1, max_length=2000)


@app.post("/sellers/{seller_id}/answers", dependencies=[Depends(require_admin)])
async def answer(seller_id: int, payload: Answer):
    return await meli("POST", "/answers", seller_id, body=payload.model_dump())


@app.get("/sellers/{seller_id}/claims", dependencies=[Depends(require_admin)])
async def claims(seller_id: int, status: str = "opened", limit: int = Query(30, ge=1, le=100), offset: int = 0):
    return await meli("GET", "/post-purchase/v1/claims/search", seller_id, params={"status": status, "limit": limit, "offset": offset, "sort": "date_created:desc"})


@app.get("/sellers/{seller_id}/claims/{claim_id}", dependencies=[Depends(require_admin)])
async def claim(seller_id: int, claim_id: int):
    detail = await meli("GET", f"/post-purchase/v1/claims/{claim_id}", seller_id)
    try:
        detail["messages"] = await meli("GET", f"/post-purchase/v1/claims/{claim_id}/messages", seller_id)
    except HTTPException:
        detail["messages"] = None
    return detail


@app.get("/sellers/{seller_id}/orders/{order_id}", dependencies=[Depends(require_admin)])
async def order(seller_id: int, order_id: int):
    return await meli("GET", f"/orders/{order_id}", seller_id)


@app.get("/sellers/{seller_id}/shipments/{shipment_id}", dependencies=[Depends(require_admin)])
async def shipment(seller_id: int, shipment_id: int):
    return await meli_h("GET", f"/shipments/{shipment_id}", seller_id, headers={"x-format-new": "true"})


@app.get("/sellers/{seller_id}/packs/{pack_id}/messages", dependencies=[Depends(require_admin)])
async def pack_messages(seller_id: int, pack_id: int):
    return await meli("GET", f"/messages/packs/{pack_id}/sellers/{seller_id}", seller_id, params={"tag": "post_sale", "mark_as_read": "false"})


class PackMessage(BaseModel):
    buyer_id: int
    text: str = Field(min_length=1, max_length=350)


@app.post("/sellers/{seller_id}/packs/{pack_id}/messages", dependencies=[Depends(require_admin)])
async def send_pack_message(seller_id: int, pack_id: int, payload: PackMessage):
    body = {"from": {"user_id": seller_id}, "to": {"user_id": payload.buyer_id}, "text": payload.text}
    return await meli("POST", f"/messages/packs/{pack_id}/sellers/{seller_id}", seller_id, params={"tag": "post_sale"}, body=body)


# ---- Conector MCP (Model Context Protocol) para adicionar no Claude ----
# Endereço: {PUBLIC_BASE_URL}/mcp/{MCP_TOKEN}. O MCP_TOKEN é um segredo longo definido no Render;
# quem não tiver o endereço completo recebe 404.

TOOLS = [
    {"name": "listar_pedidos", "description": "Lista os pedidos mais recentes da conta do Mercado Livre (mais novos primeiro). Filtro opcional de status: paid, cancelled etc.",
     "inputSchema": {"type": "object", "properties": {"status": {"type": "string"}, "limite": {"type": "integer", "default": 20}, "offset": {"type": "integer", "default": 0}}}},
    {"name": "ver_pedido", "description": "Detalhe de um pedido (itens, comprador, pagamento, pack_id e shipping id).",
     "inputSchema": {"type": "object", "properties": {"order_id": {"type": "integer"}}, "required": ["order_id"]}},
    {"name": "ver_envio", "description": "Status e rastreio de um envio (shipment) do Mercado Livre.",
     "inputSchema": {"type": "object", "properties": {"shipment_id": {"type": "integer"}}, "required": ["shipment_id"]}},
    {"name": "listar_perguntas", "description": "Perguntas de compradores nos anúncios. status: UNANSWERED (padrão) ou ANSWERED.",
     "inputSchema": {"type": "object", "properties": {"status": {"type": "string", "default": "UNANSWERED"}, "limite": {"type": "integer", "default": 20}}}},
    {"name": "responder_pergunta", "description": "Publica a resposta a uma pergunta de comprador. Só use com texto aprovado conforme as regras da FLY.",
     "inputSchema": {"type": "object", "properties": {"question_id": {"type": "integer"}, "texto": {"type": "string"}}, "required": ["question_id", "texto"]}},
    {"name": "mensagens_pedido", "description": "Lê as mensagens pós-venda de um pedido (use o pack_id do pedido; se não houver pack_id, use o order_id).",
     "inputSchema": {"type": "object", "properties": {"pack_id": {"type": "integer"}}, "required": ["pack_id"]}},
    {"name": "enviar_mensagem", "description": "Envia mensagem pós-venda ao comprador (máx. 350 caracteres). Só use com texto aprovado conforme as regras da FLY.",
     "inputSchema": {"type": "object", "properties": {"pack_id": {"type": "integer"}, "buyer_id": {"type": "integer"}, "texto": {"type": "string"}}, "required": ["pack_id", "buyer_id", "texto"]}},
    {"name": "listar_reclamacoes", "description": "Reclamações (claims) da conta. status: opened (padrão) ou closed.",
     "inputSchema": {"type": "object", "properties": {"status": {"type": "string", "default": "opened"}, "limite": {"type": "integer", "default": 30}}}},
    {"name": "ver_reclamacao", "description": "Detalhe de uma reclamação, com as mensagens dela.",
     "inputSchema": {"type": "object", "properties": {"claim_id": {"type": "integer"}}, "required": ["claim_id"]}},
    {"name": "listar_anuncios", "description": "IDs dos anúncios da conta. status opcional: active, paused.",
     "inputSchema": {"type": "object", "properties": {"status": {"type": "string"}, "limite": {"type": "integer", "default": 50}, "offset": {"type": "integer", "default": 0}}}},
    {"name": "ver_anuncio", "description": "Detalhe de um anúncio (MLB...).",
     "inputSchema": {"type": "object", "properties": {"item_id": {"type": "string"}}, "required": ["item_id"]}},
    {"name": "alterar_preco", "description": "Altera o preço de um anúncio. Só use com autorização do Vitor.",
     "inputSchema": {"type": "object", "properties": {"item_id": {"type": "string"}, "preco": {"type": "number"}}, "required": ["item_id", "preco"]}},
    {"name": "alterar_estoque", "description": "Altera o estoque disponível de um anúncio. Só use com autorização do Vitor.",
     "inputSchema": {"type": "object", "properties": {"item_id": {"type": "string"}, "quantidade": {"type": "integer"}}, "required": ["item_id", "quantidade"]}},
    {"name": "consultar_api", "description": "Consulta somente-leitura (GET) a qualquer recurso da API do Mercado Livre, ex.: Mercado Ads (/advertising/...), reputação (/users/{id}), visitas. Informe path começando com /, params opcionais e headers opcionais (ex.: {\"Api-Version\": \"2\"} para Mercado Ads).",
     "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}, "params": {"type": "object"}, "headers": {"type": "object"}}, "required": ["path"]}},
]


async def run_tool(name: str, a: dict):
    sid = default_seller()
    if name == "listar_pedidos":
        p = {"seller": sid, "sort": "date_desc", "limit": min(int(a.get("limite", 20)), 50), "offset": int(a.get("offset", 0))}
        if a.get("status"):
            p["order.status"] = a["status"]
        return await meli("GET", "/orders/search", sid, params=p)
    if name == "ver_pedido":
        return await meli("GET", f"/orders/{int(a['order_id'])}", sid)
    if name == "ver_envio":
        return await meli_h("GET", f"/shipments/{int(a['shipment_id'])}", sid, headers={"x-format-new": "true"})
    if name == "listar_perguntas":
        return await meli("GET", "/questions/search", sid, params={"seller_id": sid, "status": a.get("status", "UNANSWERED"), "api_version": 4, "sort_fields": "date_created", "sort_types": "DESC", "limit": min(int(a.get("limite", 20)), 50)})
    if name == "responder_pergunta":
        return await meli("POST", "/answers", sid, body={"question_id": int(a["question_id"]), "text": a["texto"]})
    if name == "mensagens_pedido":
        return await meli("GET", f"/messages/packs/{int(a['pack_id'])}/sellers/{sid}", sid, params={"tag": "post_sale", "mark_as_read": "false"})
    if name == "enviar_mensagem":
        if len(a["texto"]) > 350:
            raise HTTPException(422, "Mensagem acima de 350 caracteres")
        body = {"from": {"user_id": sid}, "to": {"user_id": int(a["buyer_id"])}, "text": a["texto"]}
        return await meli("POST", f"/messages/packs/{int(a['pack_id'])}/sellers/{sid}", sid, params={"tag": "post_sale"}, body=body)
    if name == "listar_reclamacoes":
        return await meli("GET", "/post-purchase/v1/claims/search", sid, params={"status": a.get("status", "opened"), "limit": min(int(a.get("limite", 30)), 100), "sort": "date_created:desc"})
    if name == "ver_reclamacao":
        return await claim(sid, int(a["claim_id"]))
    if name == "listar_anuncios":
        p = {"limit": min(int(a.get("limite", 50)), 100), "offset": int(a.get("offset", 0))}
        if a.get("status"):
            p["status"] = a["status"]
        return await meli("GET", f"/users/{sid}/items/search", sid, params=p)
    if name == "ver_anuncio":
        return await meli("GET", f"/items/{a['item_id']}", sid)
    if name == "alterar_preco":
        return await meli("PUT", f"/items/{a['item_id']}", sid, body={"price": float(a["preco"])})
    if name == "alterar_estoque":
        return await meli("PUT", f"/items/{a['item_id']}", sid, body={"available_quantity": int(a["quantidade"])})
    if name == "consultar_api":
        path = str(a["path"])
        if not path.startswith("/") or "://" in path or "/oauth" in path:
            raise HTTPException(422, "path inválido")
        return await meli_h("GET", path, sid, params=a.get("params") or None, headers={str(k): str(v) for k, v in (a.get("headers") or {}).items() if str(k).lower() != "authorization"})
    raise HTTPException(404, f"Ferramenta desconhecida: {name}")


def _rpc(id_, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": id_}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result
    return msg


@app.api_route("/mcp/{token}", methods=["GET", "POST", "DELETE"])
async def mcp(token: str, request: Request):
    expected = os.getenv("MCP_TOKEN")
    if not expected or len(expected) < 24 or not secrets.compare_digest(token, expected):
        raise HTTPException(404, "Not Found")
    if request.method != "POST":
        return Response(status_code=405)
    try:
        msg = await request.json()
    except Exception:
        return JSONResponse(_rpc(None, error={"code": -32700, "message": "JSON inválido"}), status_code=400)
    if isinstance(msg, list):
        return JSONResponse(_rpc(None, error={"code": -32600, "message": "Lote não suportado"}), status_code=400)
    method, mid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if mid is None:  # notificação (ex.: notifications/initialized)
        return Response(status_code=202)
    if method == "initialize":
        return JSONResponse(_rpc(mid, {
            "protocolVersion": params.get("protocolVersion", "2025-06-18"),
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "fly-mercadolivre", "version": "1.0.0"},
            "instructions": "Conta do Mercado Livre da FLY Distribuidora. Ferramentas que publicam ou alteram algo (responder_pergunta, enviar_mensagem, alterar_preco, alterar_estoque) só devem ser usadas com texto/valor aprovado pelo Vitor.",
        }))
    if method == "ping":
        return JSONResponse(_rpc(mid, {}))
    if method == "tools/list":
        return JSONResponse(_rpc(mid, {"tools": TOOLS}))
    if method == "tools/call":
        try:
            data = await run_tool(params.get("name"), params.get("arguments") or {})
            text = json.dumps(data, ensure_ascii=False, default=str)
            if len(text) > 90000:
                text = text[:90000] + "... [cortado]"
            return JSONResponse(_rpc(mid, {"content": [{"type": "text", "text": text}], "isError": False}))
        except HTTPException as e:
            return JSONResponse(_rpc(mid, {"content": [{"type": "text", "text": json.dumps({"status": e.status_code, "erro": e.detail}, ensure_ascii=False, default=str)}], "isError": True}))
        except Exception as e:  # nunca devolve segredo; só o tipo do erro
            return JSONResponse(_rpc(mid, {"content": [{"type": "text", "text": f"Erro interno: {type(e).__name__}"}], "isError": True}))
    return JSONResponse(_rpc(mid, error={"code": -32601, "message": f"Método não suportado: {method}"}))
