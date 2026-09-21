# -*- coding: utf-8 -*-
"""Resposta via API z.ai direta (alternativa v1 — sem depender do Hermes CLI no container).
Usa o SOUL.md do profile como system prompt."""
import json, os, urllib.request
from pathlib import Path

ZAI_API = "https://api.z.ai/api/paas/v4/chat/completions"

# Memória de conversa por chatid (em RAM; sobrevive enquanto o container viver)
_CONVS: dict[str, list[dict]] = {}
_MAX_TURNS = 12  # 12 trocas cliente+bot = 24 messages; mantém custo sob controle

def conv_history(chatid: str) -> list[dict]:
    return _CONVS.get(chatid, [])

def conv_append(chatid: str, role: str, content: str):
    h = _CONVS.setdefault(chatid, [])
    h.append({"role": role, "content": content})
    # mantém só as últimas _MAX_TURNS trocas (system não entra aqui)
    if len(h) > _MAX_TURNS * 2:
        del h[: len(h) - _MAX_TURNS * 2]

def conv_clear(chatid: str):
    _CONVS.pop(chatid, None)

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
        f"[ESCALADO] e explique o porquê. Nunca invente preço fora da tabela. "
        f"Considere TODO o histórico da conversa abaixo — nunca recomece o pitch do zero "
        f"se o cliente já está avançado."
    )
    messages = [{"role": "system", "content": system}] + conv_history(chatid)
    messages.append({"role": "user", "content": f"Mensagem do cliente: {msg}"})

    last_err = None
    # Tentativa 1: z.ai direto · Tentativa 2: retry z.ai (30s, para 429) · Tentativa 3: OpenRouter
    for attempt in range(3):
        if attempt == 0:
            url, hdr_auth, model = ZAI_API, f"Bearer {key}", "glm-5.3-flash"
        elif attempt == 1:
            import time as _t
            _t.sleep(30)
            url, hdr_auth, model = ZAI_API, f"Bearer {key}", "glm-5.3-flash"
        else:
            or_key = os.getenv("OPENROUTER_API_KEY", "")
            if not or_key:
                raise RuntimeError(f"zai_respond falhou (sem fallback): {last_err}")
            url, hdr_auth, model = "https://openrouter.ai/api/v1/chat/completions", f"Bearer {or_key}", "z-ai/glm-5.3-flash"
        body = json.dumps({"model": model, "messages": messages}).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers={
            "Authorization": hdr_auth, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                d = json.loads(r.read())
            reply = (d["choices"][0]["message"]["content"] or "").strip()[:1500]
            if reply:
                conv_append(chatid, "user", f"Mensagem do cliente: {msg}")
                conv_append(chatid, "assistant", reply)
            return reply or None
        except Exception as e:
            last_err = e
            continue
    raise RuntimeError(f"zai_respond falhou (3 tentativas): {last_err}") from last_err
