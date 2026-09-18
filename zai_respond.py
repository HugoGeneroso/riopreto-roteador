# -*- coding: utf-8 -*-
"""Resposta via API z.ai direta (alternativa v1 — sem depender do Hermes CLI no container).
Usa o SOUL.md do profile como system prompt."""
import json, os, urllib.request
from pathlib import Path

ZAI_API = "https://api.z.ai/api/paas/v4/chat/completions"

def zai_respond(profile: str, chatid: str, msg: str, pipeline_ctx: str = "") -> str | None:
    key = os.getenv("ZAI_API_KEY", "")
    if not key:
        return None
    soul_path = Path(os.getenv("SOULS_DIR", "/data/souls")) / f"SOUL-{profile}.md"
    soul = soul_path.read_text(encoding="utf-8") if soul_path.exists() else "Você é um vendedor consultivo honesto da Rio Preto Tech."
    system = (
        f"{soul}\n\n"
        f"## Contexto do pipeline (resumo)\n{pipeline_ctx[:2500]}\n\n"
        f"## Instrução\nResponda APENAS com a mensagem de WhatsApp que enviará ao "
        f"cliente (curta, PT-BR). Se a mensagem exigir decisão do Hugo, comece com "
        f"[ESCALADO] e explique o porquê. Nunca invente preço fora da tabela."
    )
    body = json.dumps({
        "model": "glm-5.3-flash",
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": f"Mensagem do cliente ({chatid}): {msg}"},
        ],
        "thinking": {"type": "enabled"},
    }).encode("utf-8")
    req = urllib.request.Request(ZAI_API, data=body, headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            d = json.loads(r.read())
        return d["choices"][0]["message"]["content"].strip()[:1500] or None
    except Exception as e:
        raise RuntimeError(f"zai_respond falhou: {e}") from e
