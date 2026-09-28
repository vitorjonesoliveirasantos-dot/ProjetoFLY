# ProjetoFLY — Mercado Livre

API FastAPI inicial para conectar uma conta vendedora do Mercado Livre, listar anúncios e pedidos, alterar preço e estoque e publicar anúncios.

## Implantação

1. Crie um banco PostgreSQL persistente e um serviço Web Python no Render usando `render.yaml` deste repositório.
2. Configure as variáveis de `.env.example` no painel do Render. `DATABASE_URL` deve apontar para o PostgreSQL. Nunca envie `MELI_CLIENT_SECRET`, `ADMIN_API_KEY` ou tokens ao GitHub.
3. No [DevCenter do Mercado Livre](https://developers.mercadolivre.com.br/pt_br/realizacao-de-testes/crie-uma-aplicacao-no-mercado-livre), crie uma aplicação com escopos de leitura e escrita e endereço HTTPS de retorno `https://SEU-SERVICO.onrender.com/auth/callback`. Se for usar renovações sem o vendedor on-line, habilite a permissão off-line oferecida pelo cadastro. O endereço deve corresponder exatamente a `MELI_REDIRECT_URI`.
4. Configure `MELI_CLIENT_ID`, `MELI_CLIENT_SECRET`, `MELI_REDIRECT_URI` e `PUBLIC_BASE_URL` no Render e aguarde o deploy. `GET /` deve retornar `{"status":"ok"}`.
5. Faça `GET /auth/start` com o cabeçalho `X-API-Key` definido por `ADMIN_API_KEY`. Abra `authorization_url`, entre na conta vendedora e aceite a autorização. O retorno apresenta `seller_id`.

## Rotas protegidas

Todas as rotas abaixo exigem `X-API-Key` e `{seller_id}` da conta conectada:

| Método | Rota | Ação |
| --- | --- | --- |
| GET | `/sellers` | Listar contas conectadas |
| GET | `/sellers/{seller_id}/items` | IDs dos anúncios com paginação |
| GET | `/sellers/{seller_id}/items/{item_id}` | Detalhes do anúncio |
| GET | `/sellers/{seller_id}/orders` | Pedidos com paginação |
| PUT | `/sellers/{seller_id}/items/{item_id}/stock` | `{"available_quantity": 10}` |
| PUT | `/sellers/{seller_id}/items/{item_id}/price` | `{"price": 29.90}` |
| POST | `/sellers/{seller_id}/items` | JSON de publicação conforme a categoria |

A publicação depende dos atributos obrigatórios da categoria e das regras de User Products. Alterações de estoque e preço podem depender do tipo de anúncio e da configuração de estoque. Trate erros retornados pelo Mercado Livre caso a caso antes de automatizar operações em massa.

## Desenvolvimento local

Instale `requirements.txt`, defina as variáveis de ambiente em um gerenciador seguro e execute `uvicorn app:app --reload`. Não use uma URL local como retorno no cadastro de produção: o Mercado Livre exige HTTPS.

## Próxima etapa do plugin

Esta API é a base da integração. Para que o plugin do ChatGPT acesse os dados, implante um servidor MCP autenticado sobre ela e configure o endpoint real no `mcp.json` do plugin. Não exponha a API publicamente sem `X-API-Key`, nem inclua a chave no manifesto do plugin.
