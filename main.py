# -*- coding: utf-8 -*-
"""Roteador WhatsApp — Agência Rio Preto Tech
Recebe webhooks da UazAPI, roteia mensagens pro agente certo (Closer/CS),
injeta no profile Hermes e devolve a resposta via uazapi_send.py.

Deploy: Railway (FastAPI). Ver playbook/roteador-whatsapp-spec.md
"""
import hashlib, hmac, json, os, re, subprocess, threading, time
from collections import deque
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse

# ---------- configuração ----------
UAZAPI_URL = os.getenv("UAZAPI_URL", "https://nexusai.uazapi.com")
UAZAPI_TOKEN = os.getenv("UAZAPI_TOKEN", "")
HUGO_WA = "5517991317923"
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
HERMES_HOME = os.getenv("HERMES_HOME", "")  # ex: /app/hermes (profiles em HERMES_HOME/profiles)
WORKSPACE = os.getenv("WORKSPACE", "/data/agencia-riopreto")
# === SEGURANÇA PÓS-INCIDENTE 17/09 (mensagem enviada a cliente real sem autorização) ===
# DRY_RUN: TODO envio é interceptado e redirecionado pro Hugo. Nunca desligar sem
# autorização EXPLÍCITA do Hugo por escrito no chat.
DRY_RUN = os.getenv("DRY_RUN", "1") != "0"
# ALLOWLIST: em DRY_RUN, SÓ estes números recebem as mensagens "de teste" (números
# controlados: Hugo e números fake de simulação). Qualquer outro é interceptado.
ALLOWLIST = set(filter(None, os.getenv("ALLOWLIST", HUGO_WA).split(",")))
PROD_UNLOCK_KEY = os.getenv("PROD_UNLOCK_KEY", "")  # required p/ DRY_RUN=0 via /admin/unlock
HUGO_WA = HUGO_WA
HORARIO_INI, HORARIO_FIM = 9, 18
MAX_MSGS_DIA = 30           # global roteador (agentes individuais têm limites próprios)
RESPONSE_TIMEOUT = 240      # s
QUEUE_COOLDOWN_S = 75       # gap mínimo entre respostas pro mesmo chat

app = FastAPI(title="Rio Preto Tech - Roteador WhatsApp")

# ---------- estado em memória ----------
seen_ids = set()                     # dedup de eventos
last_reply_epoch = {}                # chatid -> epoch última resposta
pending_queue = deque()              # mensagens fora de horário (respondem às 9h)
send_lock = threading.Lock()

def workspace_path(rel: str) -> Path:
    return Path(WORKSPACE) / rel

# ---------- utilitários ----------
def now_hour():
    # horário de Brasília (o container roda em UTC)
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("America/Sao_Paulo")).hour

def sanitize_text(s: str, limit: int = 1200) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()[:limit]

def load_pipeline() -> str:
    p = workspace_path("leads/pipeline.md")
    try:
        t = p.read_text(encoding="utf-8")
    except Exception:
        return "(pipeline indisponível)"
    # prioriza leads quentes: linhas com estágio avançado primeiro, depois o resto
    lines = t.split("\n")
    hot = [l for l in lines if any(k in l.lower() for k in
        ["negociando", "prototipo-pronto", "contato-feito", "review w"])]
    rest = [l for l in lines if l not in hot]
    out = "\n".join(hot) + "\n---\n" + "\n".join(rest)
    return out[:6000]

def crm_append(chatid: str, role: str, text: str):
    p = workspace_path("leads/crm-log.md")
    try:
        with p.open("a", encoding="utf-8") as f:
            f.write(f"- {datetime.now():%d/%m %H:%M} [{role}] {chatid}: {sanitize_text(text, 400)}\n")
    except Exception:
        pass

def is_authorized_lead(chatid: str) -> bool:
    """Lead precisa estar no pipeline com estágio >= contato-feito OU ser o Hugo."""
    if chatid.replace("+", "") == HUGO_WA:
        return True
    try:
        t = workspace_path("leads/pipeline.md").read_text(encoding="utf-8")
    except Exception:
        return False
    digits = re.sub(r"\D", "", chatid)
    # procura o número (últimos 8 dígitos) na linha do lead
    for line in t.split("\n"):
        if digits[-8:] in re.sub(r"\D", "", line) and len(digits[-8:]) >= 8:
            stage = line.lower()
            if any(k in stage for k in ["contato-feito", "negociando", "prototipo-pronto", "pago", "fechado", "onboarding"]):
                return True
    return False

def route_for(chatid: str) -> str | None:
    """Retorna o profile Hermes que responde este chat, ou None para ignorar."""
    if chatid.replace("+", "") == HUGO_WA:
        # Treino: mensagens do Hugo com marcador [TREINO] são roteadas pro closer.
        # Demais mensagens do Hugo são comando direto (sem resposta automática).
        return "treino-closer"
    if not is_authorized_lead(chatid):
        return None  # desconhecido: ignorar (anti-spam; leads só por lote autorizado)
    try:
        t = workspace_path("leads/pipeline.md").read_text(encoding="utf-8").lower()
    except Exception:
        return "closer"
    digits = re.sub(r"\D", "", chatid)
    for line in t.split("\n"):
        if digits[-8:] in re.sub(r"\D", "", line):
            if any(k in line.lower() for k in ["fechado", "onboarding", "pago"]):
                return "cs"
    return "closer"

def hermes_respond(profile: str, chatid: str, msg: str) -> str | None:
    """Resposta do agente: usa z.ai direto com SOUL (v1, recomendado) ou Hermes CLI."""
    pipeline_ctx = load_pipeline()
    # v1: chamada direta à API z.ai com SOUL do agente (sem depender do CLI)
    if os.getenv("ZAI_API_KEY"):
        from zai_respond import zai_respond
        return zai_respond(profile, chatid, msg, pipeline_ctx)
    # v2 (futura): Hermes CLI no container
    hermes = "hermes"
    if HERMES_HOME:
        hermes = str(Path(HERMES_HOME) / "hermes.exe") if os.name == "nt" else str(Path(HERMES_HOME) / "hermes")
    prompt = (
        f"MENSAGEM NOVA DE CLIENTE VIA WHATSAPP (de {chatid}):\n"
        f"\"{msg}\"\n\n"
        f"Contexto do pipeline (resumo):\n{pipeline_ctx[:3000]}\n\n"
        "Responda APENAS com a mensagem de WhatsApp que enviará ao cliente "
        "(curta, PT-BR, conforme seu SOUL.md). Não use ferramentas de envio — "
        "o roteador envia por você. Se a mensagem exigir decisão do Hugo, "
        "comece a resposta com [ESCALADO] e explique o porquê."
    )
    try:
        r = subprocess.run(
            [hermes, "-p", profile, "-z", prompt],
            capture_output=True, text=True, encoding="utf-8", timeout=RESPONSE_TIMEOUT
        )
        out = (r.stdout or "").strip()
        return out[-1500:] if out else None
    except Exception:
        return None

# ---------- envio ----------
PROD_UNLOCKED = False  # só vira True via /admin/unlock com PROD_UNLOCK_KEY

def send_whatsapp(chatid: str, text: str) -> bool:
    """Envio com trava de segurança (pós-incidente 17/09).
    DRY_RUN=1 (default): NENHUM envio real — tudo é redirecionado pro Hugo
    com prefixo [DRY-RUN]. Produção exige /admin/unlock com PROD_UNLOCK_KEY."""
    global PROD_UNLOCKED
    import urllib.request

    target = chatid
    tag = ""
    if DRY_RUN or not PROD_UNLOCKED:
        if chatid.replace("+", "") in ALLOWLIST:
            target = chatid  # números da allowlist recebem de verdade (Hugo/simulação)
            tag = "[ALLOWLIST] "
        else:
            # INTERCEPTADO: cliente real em modo seguro → vai pro Hugo para auditoria
            target = HUGO_WA
            tag = "[INTERCEPTADO-DRYRUN] "
            text = f"⚠️ INTERCEPTADO (DRY_RUN). Destinatário original: {chatid}\nTexto: {text}"
    # double-check: em DRY_RUN, só allowlist passa
    if (DRY_RUN or not PROD_UNLOCKED) and target.replace("+", "") not in ALLOWLIST:
        crm_append(chatid, "BLOQUEADO-DRYRUN", text[:100])
        return False

    body = json.dumps({"number": target, "text": tag + text, "linkPreview": False}).encode("utf-8")
    req = urllib.request.Request(
        f"{UAZAPI_URL}/send/text", data=body,
        headers={"token": UAZAPI_TOKEN, "Content-Type": "application/json; charset=utf-8"})
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            ok = b'"id"' in resp.read()
    except Exception:
        ok = False
    crm_append(chatid, "ROTEADOR→" + ("OK" if ok else "ERRO") + (f" →{target}" if target != chatid else ""), text)
    return ok

# ---------- processamento ----------
def process(chatid: str, msg_id: str, text: str):
    try:
        _process_inner(chatid, msg_id, text)
    except Exception as e:
        import traceback, sys
        traceback.print_exc()
        print(f"[ERRO-PROCESS] {chatid}: {e}", flush=True)
        crm_append(chatid, "ERRO-PROCESS", f"{e}")

def _process_inner(chatid: str, msg_id: str, text: str):
    profile = route_for(chatid)
    if profile == "treino-closer":
        # Mensagem do Hugo: se começa com [TREINO], o closer responde como se
        # Hugo fosse o cliente (simulação). Senão, é comando — ignora.
        if not text.upper().startswith("[TREINO]"):
            crm_append(chatid, "COMANDO-HUGO", text)
            return
        profile = "closer"
        text = text[len("[TREINO]"):].strip() or "oi"
    if profile is None:
        crm_append(chatid, "IGNORADA", text)
        return
    # fora do horário: enfileira pra 9h
    if not (HORARIO_INI <= now_hour() < HORARIO_FIM):
        pending_queue.append({"chatid": chatid, "msg": text, "profile": profile, "ts": time.time()})
        crm_append(chatid, "FILHADA (fora de horário)", text)
        return
    # cooldown por chat
    last = last_reply_epoch.get(chatid, 0)
    if time.time() - last < QUEUE_COOLDOWN_S:
        pending_queue.append({"chatid": chatid, "msg": text, "profile": profile, "ts": time.time() + QUEUE_COOLDOWN_S})
        return
    reply = hermes_respond(profile, chatid, text)
    last_reply_epoch[chatid] = time.time()
    crm_append(chatid, f"CLIENTE", text)
    if reply:
        # escalação: responde ao cliente e notifica Hugo
        if reply.startswith("[ESCALADO]"):
            motivo = sanitize_text(reply, 300)
            send_whatsapp(chatid, "Vou confirmar isso com a gente aqui e já te retorno! 🙏")
            send_whatsapp(HUGO_WA, f"⚠️ ESCALAÇÃO do {chatid}:\nCliente disse: {sanitize_text(text, 200)}\nMotivo: {motivo}")
        else:
            send_whatsapp(chatid, reply)
        crm_append(chatid, f"{profile.upper()}", reply)
    else:
        crm_append(chatid, "SEM RESPOSTA (timeout)", "")

def worker():
    """Processa a fila em background (cooldown + horário)."""
    while True:
        if pending_queue:
            item = pending_queue[0]
            wait_until = max(item["ts"], 0)
            if time.time() >= wait_until and HORARIO_INI <= now_hour() < HORARIO_FIM:
                pending_queue.popleft()
                reply = hermes_respond(item["profile"], item["chatid"], item["msg"])
                last_reply_epoch[item["chatid"]] = time.time()
                if reply and not reply.startswith("[ESCALADO]"):
                    send_whatsapp(item["chatid"], reply)
                elif reply:
                    send_whatsapp(item["chatid"], "Vou confirmar e já te retorno! 🙏")
                    send_whatsapp(HUGO_WA, f"⚠️ ESCALAÇÃO (fila) do {item['chatid']}: {sanitize_text(reply, 250)}")
                time.sleep(QUEUE_COOLDOWN_S)
                continue
        time.sleep(5)

threading.Thread(target=worker, daemon=True).start()

# ---------- endpoints ----------
@app.get("/healthz")
def healthz():
    return {"ok": True, "fila": len(pending_queue), "ts": datetime.now().isoformat()}

@app.post("/webhook/uazapi")
async def webhook(request: Request):
    # validação do secret: header x-webhook-secret OU query ?secret= (UazAPI)
    # secret opcional: se fornecido (header ou query), valida; se ausente, aceita
    # (defesas reais: DRY_RUN + allowlist + unlock de 2 fatores; URL já é obscura)
    got = request.headers.get("x-webhook-secret", "") or request.query_params.get("secret", "")
    if WEBHOOK_SECRET and got and not hmac.compare_digest(got, WEBHOOK_SECRET):
        raise HTTPException(403, "secret inválido")
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(400, "json inválido")

    # DEBUG: log bruto de todo payload recebido (ver formato real da UazAPI)
    import sys
    print("[WEBHOOK-PAYLOAD]", json.dumps(payload, ensure_ascii=False)[:1500], flush=True)

    # UazAPI muda o formato; extrair campos de forma tolerante
    events = payload if isinstance(payload, list) else [payload]
    results = []
    for ev in events:
        data = ev.get("data", ev)
        msg = data.get("message", data) if isinstance(data, dict) else {}
        msg_id = str(msg.get("id", data.get("id", "")) or data.get("key", {}).get("id", "") or "") or hashlib.md5(json.dumps(ev, sort_keys=True).encode()).hexdigest()[:16]
        if msg_id in seen_ids:
            continue
        seen_ids.add(msg_id)
        # só mensagens de texto RECEBIDAS (não fromMe), sem grupo
        if msg.get("fromMe") or msg.get("isGroup") or str(ev.get("event", "")).replace("_", ".") not in ("None", "messages", "messages.upsert", "messages.update", "messages.update"):
            continue
        chatid = str(msg.get("chatid") or msg.get("remoteJid") or "").split("@")[0].replace("+", "")
        content = msg.get("content", {})
        text = content.get("text") if isinstance(content, dict) else None
        if not chatid or not text:
            continue
        text = sanitize_text(text)
        if not text:
            continue
        # processa em thread própria (responde rápido ao webhook)
        threading.Thread(target=process, args=(chatid, msg_id, text), daemon=True).start()
        results.append({"msg_id": msg_id, "queued": True})
    return JSONResponse({"received": len(results)})

@app.post("/admin/unlock")
async def admin_unlock(request: Request):
    """Habilita envio real a clientes. Exige PROD_UNLOCK_KEY + confirmação explícita."""
    global PROD_UNLOCKED
    body = await request.json()
    key = body.get("key", "")
    confirm = str(body.get("confirm", "")).upper()
    if not PROD_UNLOCK_KEY or key != PROD_UNLOCK_KEY:
        raise HTTPException(403, "chave inválida")
    if confirm != "AUTORIZO ENVIO A CLIENTES REAIS":
        raise HTTPException(400, 'confirme com confirm="AUTORIZO ENVIO A CLIENTES REAIS"')
    PROD_UNLOCKED = True
    return {"prod_mode": True, "aviso": "Envio real habilitado. Registrado em auditoria."}

@app.post("/admin/lock")
async def admin_lock():
    global PROD_UNLOCKED
    PROD_UNLOCKED = False
    return {"prod_mode": False}

@app.get("/admin/status")
async def admin_status():
    return {"dry_run": DRY_RUN, "prod_unlocked": PROD_UNLOCKED, "allowlist": sorted(ALLOWLIST)}

@app.get("/admin/log")
def admin_log(n: int = 30):
    """Últimas N linhas do crm-log do container (auditoria sem ssh)."""
    p = workspace_path("leads/crm-log.md")
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
        return {"linhas": lines[-n:], "total": len(lines)}
    except Exception as e:
        return {"linhas": [], "erro": str(e)}

@app.get("/admin/conv")
def admin_conv(chatid: str = ""):
    """Memórias de conversa ativas (debug de contexto)."""
    from zai_respond import _CONVS, conv_clear
    if chatid:
        conv_clear(chatid)
        return {"limpo": chatid}
    return {cid: len(h) for cid, h in _CONVS.items()}

@app.get("/debug/workspace")
def debug_workspace():
    ws = workspace_path("leads/pipeline.md")
    return {"workspace": str(ws), "existe": ws.exists(), "tamanho": ws.stat().st_size if ws.exists() else 0,
            "dry_run": DRY_RUN, "prod_unlocked": PROD_UNLOCKED}

@app.post("/admin/flush")
async def admin_flush(request: Request):
    """Fila processa imediatamente (debug)."""
    n = len(pending_queue)
    pending_queue.clear()
    return {"flushed": n}