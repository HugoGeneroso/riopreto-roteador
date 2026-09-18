# Roteador WhatsApp — serviço Railway

Recebe webhooks da UazAPI e roteia mensagens pros agentes (Closer/CS).

## Variáveis de ambiente (configurar no Railway)
| Var | Valor |
|---|---|
| `UAZAPI_URL` | https://nexusai.uazapi.com |
| `UAZAPI_TOKEN` | token da instância |
| `WEBHOOK_SECRET` | segredo qualquer (também seta na UazAPI ao criar o webhook) |
| `HERMES_HOME` | path onde o binário `hermes` está NO CONTAINER (ver nota abaixo) |
| `WORKSPACE` | path do workspace agencia-riopreto acessível pelo container |

## ⚠️ Nota sobre o Hermes no container
O roteador chama `hermes -p closer -z ...` via subprocess. Isso exige o Hermes
CLI + profiles DENTRO do container — o que não é trivial no Railway.

**Alternativa v1 (recomendada pra começar):** o roteador chama a **API z.ai
diretamente** com o SOUL.md do agente no system prompt (sem depender do CLI
Hermes). Mesmo cérebro, menos infra. O flag `HERMES_HOME` vira opcional e o
main.py usa `zai_respond()` quando `ZAI_API_KEY` está setada.

**Alternativa v2 (futura):** empacotar o Hermes CLI + profiles numa imagem maior.

## Setup da UazAPI (1x)
1. Criar webhook: `POST /webhook` com `{"url": "https://<roteador>.up.railway.app/webhook/uazapi", "events": ["messages.upsert"], "addUrlSecret": "<WEBHOOK_SECRET>"}`
2. Confirmar recebimento em `/webhook` (GET)

## Testes
- `GET /healthz` → `{"ok": true}`
- Postar um payload de mensagem fake com número do Hugo → deve logar e IGNORAR (Hugo não gera auto-resposta)
- Postar mensagem de lead autorizado → deve responder via Closer
