"""Mensagens para clientes: histórico, limite de frequência por perfil e fila de envio espaçada."""
import json
import os
import random
import re
import uuid
from datetime import datetime, timedelta
from statistics import median

import pizzaria as P

ESQUEMA = """
CREATE TABLE IF NOT EXISTS envios (id INTEGER PRIMARY KEY AUTOINCREMENT, cliente_id TEXT, chave TEXT, nome TEXT, numero TEXT,
  campanha TEXT, texto TEXT, lote TEXT, criado TEXT, agendado TEXT, enviado TEXT, status TEXT, erro TEXT, via TEXT,
  ia INTEGER DEFAULT 0, msg_id TEXT);
CREATE INDEX IF NOT EXISTS envios_cli ON envios(cliente_id);
CREATE INDEX IF NOT EXISTS envios_fila ON envios(status, agendado);
CREATE TABLE IF NOT EXISTS optout (cliente_id TEXT PRIMARY KEY, em TEXT, motivo TEXT);
CREATE TABLE IF NOT EXISTS lembretes (id INTEGER PRIMARY KEY AUTOINCREMENT, cliente_id TEXT, chave TEXT, nome TEXT, texto TEXT,
  quando TEXT, criado TEXT, feito TEXT, avisado INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS reservas (id INTEGER PRIMARY KEY AUTOINCREMENT, nome TEXT, telefone TEXT, data TEXT, hora TEXT,
  pessoas INTEGER, rodizio INTEGER, valor_pago REAL, forma_pagamento TEXT, observacao TEXT, status TEXT DEFAULT 'confirmada',
  cliente_id TEXT, chave TEXT, origem TEXT, criado TEXT, atualizado TEXT);
CREATE TABLE IF NOT EXISTS listas (id INTEGER PRIMARY KEY AUTOINCREMENT, titulo TEXT, descricao TEXT, origem TEXT,
  criado TEXT, atualizado TEXT, itens TEXT);
"""

PERFIS = [  # código, nome, explicação
    ("fa", "Fã", "8+ pedidos, pede a cada 10 dias ou menos e pediu nos últimos 20 dias"),
    ("frequente", "Frequente", "4+ pedidos, pede a cada 3 semanas ou menos e pediu nos últimos 45 dias"),
    ("novo", "Novo", "1 ou 2 pedidos, o primeiro nos últimos 30 dias"),
    ("ocasional", "Ocasional", "pediu nos últimos 60 dias"),
    ("frio", "Frio", "último pedido entre 2 e 6 meses atrás"),
    ("congelado", "Congelado", "mais de 6 meses sem pedir"),
]
NOME_PERFIL = {k: n for k, n, _ in PERFIS}

REGRAS_PADRAO = {
    "perfis": {"fa": 3, "frequente": 5, "novo": 7, "ocasional": 7, "frio": 14, "congelado": 30},
    "pausa_sem_resposta": 3,   # mensagens seguidas sem nenhum pedido depois -> pausa
    "dias_pausa": 45,
    "por_dia": 30,
    "por_hora": 10,
    "intervalo_min": 3,        # minutos entre mensagens (sorteado entre min e max)
    "intervalo_max": 9,
    "horario": "10:00-21:00",
    "aquecimento": True,       # primeiros 7 dias de envio automático: começa com 10/dia e sobe
    "incluir_so_app": False,   # clientes que só pedem por iFood/99 (telefone veio do app)
}


def caminho_regras():
    return os.path.join(P.PASTA_DADOS, "regras_envio.json")


def carregar_regras():
    r = json.loads(json.dumps(REGRAS_PADRAO))
    try:
        with open(caminho_regras(), encoding="utf-8") as f:
            v = json.load(f)
        if isinstance(v, dict):
            for k in r:
                if k == "perfis" and isinstance(v.get("perfis"), dict):
                    for p in r["perfis"]:
                        try:
                            r["perfis"][p] = max(0, min(365, int(v["perfis"].get(p, r["perfis"][p]))))
                        except (TypeError, ValueError):
                            pass
                elif k in v:
                    r[k] = v[k]
    except (OSError, ValueError):
        pass
    for k, lo, hi in (("pausa_sem_resposta", 1, 20), ("dias_pausa", 1, 365), ("por_dia", 1, 500), ("por_hora", 1, 60),
                      ("intervalo_min", 1, 120), ("intervalo_max", 1, 240)):
        try:
            r[k] = max(lo, min(hi, int(r[k])))
        except (TypeError, ValueError):
            r[k] = REGRAS_PADRAO[k]
    if r["intervalo_max"] < r["intervalo_min"]:
        r["intervalo_max"] = r["intervalo_min"]
    try:
        janela(r)
    except ValueError:
        r["horario"] = REGRAS_PADRAO["horario"]
    r["aquecimento"] = bool(r["aquecimento"])
    r["incluir_so_app"] = bool(r["incluir_so_app"])
    return r


def salvar_regras(r):
    os.makedirs(P.PASTA_DADOS, exist_ok=True)
    tmp = caminho_regras() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(r, f, ensure_ascii=False, indent=1)
    os.replace(tmp, caminho_regras())


def janela(r):
    a, b = r["horario"].split("-")
    ha, ma = [int(x) for x in a.strip().split(":")]
    hb, mb = [int(x) for x in b.strip().split(":")]
    if not (0 <= ha < 24 and 0 <= hb <= 24 and (ha, ma) < (hb, mb)):
        raise ValueError("horário")
    return ha * 60 + ma, hb * 60 + mb


def preparar(db):
    db.executescript(ESQUEMA)
    cols = {r[1] for r in db.execute("PRAGMA table_info(lembretes)")}
    for c in ("grupo", "grupo_titulo"):
        if c not in cols:
            db.execute("ALTER TABLE lembretes ADD COLUMN %s TEXT" % c)
    db.commit()
    if not P.meta_get(db, "envios_migrado"):
        # contatos antigos (marcados à mão) viram histórico
        for cid, data, camp in db.execute("SELECT cliente_id, data, campanha FROM contatos").fetchall():
            if data:
                db.execute("INSERT INTO envios (cliente_id, campanha, criado, agendado, enviado, status, via) VALUES (?,?,?,?,?,?,?)",
                           (cid, camp, data + " 12:00", data + " 12:00", data + " 12:00", "enviado", "manual"))
        P.meta_set(db, "envios_migrado", "1")
    db.commit()


def agora_txt(t=None):
    return (t or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")


def _dt(s):
    s = (s or "")[:19]
    for f in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[:len(datetime.now().strftime(f))], f)
        except ValueError:
            continue
    return None


# ------------------------------------------------------------------ perfil e limite de cada cliente

def perfil(n, dias_sem, ritmo, dias_primeiro):
    if n >= 8 and ritmo is not None and ritmo <= 10 and dias_sem <= 20:
        return "fa"
    if n >= 4 and ritmo is not None and ritmo <= 21 and dias_sem <= 45:
        return "frequente"
    if n <= 2 and dias_primeiro <= 30:
        return "novo"
    if dias_sem <= 60:
        return "ocasional"
    if dias_sem <= 180:
        return "frio"
    return "congelado"


def cadencia(D, db, regras, agora=None):
    """Por cliente com telefone: [perfil, ultimo_envio, status, liberado_em, motivo, envios_30d, taxa_resposta, agendado]
    status: livre | aguardar | pausado | bloqueado | na_fila | so_app"""
    agora = agora or datetime.now()
    por_id = {}
    for i, c in enumerate(D["clientes"]):
        if c.get("uniu_em") is None and c.get("id") is not None:
            por_id[str(c["id"])] = i
    por_chave = {c.get("chave"): (c.get("uniu_em") if c.get("uniu_em") is not None else i) for i, c in enumerate(D["clientes"])}
    env, fila = {}, {}
    for cid, chave, enviado, status, agendado in db.execute(
            "SELECT cliente_id, chave, enviado, status, agendado FROM envios WHERE status IN ('enviado','fila')"):
        cli = por_id.get(str(cid)) if cid is not None else None
        if cli is None and chave:
            cli = por_chave.get(chave)
        if cli is None:
            continue
        if status == "fila":
            fila[cli] = min(fila.get(cli) or "9", agendado or "9")
        else:
            d = _dt(enviado)
            if d:
                env.setdefault(cli, []).append(d)
    bloq = {r[0] for r in db.execute("SELECT cliente_id FROM optout")}
    peds = {}
    for p in D["pedidos"]:
        if p[5] >= 0 and not p[7]:
            peds.setdefault(p[5], []).append(p[0])
    out = {}
    for i, c in enumerate(D["clientes"]):
        if not c.get("tel") or c.get("uniu_em") is not None:
            continue
        ds = sorted(peds.get(i) or [])
        if ds:
            pri, ult = _dt(ds[0]), _dt(ds[-1])
            dias_sem = (agora - ult).days
            dd = sorted({x[:10] for x in ds})
            iv = [(_dt(b) - _dt(a)).days for a, b in zip(dd, dd[1:])]
            ritmo = median(iv) if len(iv) >= 2 else None
            pf = perfil(len(ds), dias_sem, ritmo, (agora - pri).days)
        else:
            ult, pf = None, "congelado"
        e = sorted(env.get(i) or [])
        ultimo = e[-1] if e else None
        n30 = sum(1 for x in e if (agora - x).days < 30)
        # resposta: mensagens com pedido nos 7 dias seguintes
        # resposta: mensagens com pedido nos 7 dias seguintes (só conta mensagens com 7 dias ou que já tiveram pedido)
        resp_l = [any(x < _dt(d) <= x + timedelta(days=7) for d in ds) for x in e]
        aval = [r for x, r in zip(e, resp_l) if r or (agora - x).days >= 7]
        taxa = round(100 * sum(aval) / len(aval)) if aval else -1
        sem_resp = sum(1 for x in e if ult is None or x > ult)
        status, lib, motivo = "livre", "", ""
        if str(c.get("id")) in bloq:
            status, motivo = "bloqueado", "não quer receber mensagens"
        elif c.get("app") and not regras["incluir_so_app"]:
            status, motivo = "so_app", "só pede pelo %s (telefone veio do app)" % c.get("app")
        elif i in fila:
            status, lib, motivo = "na_fila", fila[i][:16], "já está na fila de envio"
        elif ultimo:
            if sem_resp >= regras["pausa_sem_resposta"] and agora < ultimo + timedelta(days=regras["dias_pausa"]):
                status, lib = "pausado", (ultimo + timedelta(days=regras["dias_pausa"])).strftime("%Y-%m-%d")
                motivo = "recebeu %d mensagens seguidas sem pedir" % sem_resp
            else:
                prox = ultimo + timedelta(days=regras["perfis"][pf])
                if agora < prox:
                    status, lib = "aguardar", prox.strftime("%Y-%m-%d %H:%M")
                    motivo = "perfil %s: no máximo 1 mensagem a cada %d dias" % (NOME_PERFIL[pf], regras["perfis"][pf])
        out[i] = [pf, ultimo.strftime("%Y-%m-%d %H:%M") if ultimo else "", status, lib, motivo, n30, taxa, fila.get(i, "")]
    return out


# ------------------------------------------------------------------ fila

def limite_dia(db, regras, dia):
    cap = regras["por_dia"]
    if regras["aquecimento"]:
        pri = db.execute("SELECT MIN(enviado) FROM envios WHERE via='auto' AND status='enviado'").fetchone()[0]
        k = (dia.date() - _dt(pri).date()).days if pri else 0
        if 0 <= k < 7:
            cap = min(cap, 10 + 5 * k)
    return cap


def proximo_horario(db, regras, depois, ocupados):
    """Próximo horário livre depois de `depois`, respeitando janela, limite por hora e por dia.
    ocupados: lista de datetimes já agendados/enviados (auto)."""
    ini, fim = janela(regras)
    t = depois
    for _ in range(2000):
        m = t.hour * 60 + t.minute
        if m < ini:
            t = t.replace(hour=ini // 60, minute=ini % 60, second=0) + timedelta(seconds=random.randint(0, 600))
            continue
        if m >= fim:
            t = (t + timedelta(days=1)).replace(hour=ini // 60, minute=ini % 60, second=0) + timedelta(seconds=random.randint(0, 600))
            continue
        no_dia = sum(1 for x in ocupados if x.date() == t.date())
        if no_dia >= limite_dia(db, regras, t):
            t = (t + timedelta(days=1)).replace(hour=ini // 60, minute=ini % 60, second=0) + timedelta(seconds=random.randint(0, 600))
            continue
        na_hora = sum(1 for x in ocupados if x.date() == t.date() and x.hour == t.hour)
        if na_hora >= regras["por_hora"]:
            t = t.replace(minute=0, second=0) + timedelta(hours=1, seconds=random.randint(0, 300))
            continue
        return t
    return t


def agendar(db, itens, regras, lote=None):
    """itens: [{cliente_id, chave, nome, numero, campanha, texto, ia}] -> lista de (item, horário)."""
    agora = datetime.now()
    lote = lote or uuid.uuid4().hex[:8]
    ocupados = [_dt(r[0]) for r in db.execute(
        "SELECT agendado FROM envios WHERE via='auto' AND status='fila' UNION ALL "
        "SELECT enviado FROM envios WHERE via='auto' AND status='enviado' AND enviado >= ?", ((agora - timedelta(days=1)).strftime("%Y-%m-%d"),))]
    ocupados = [x for x in ocupados if x]
    ultimo = max([x for x in ocupados] + [agora - timedelta(minutes=regras["intervalo_max"])])
    t = max(agora + timedelta(seconds=random.randint(20, 90)), ultimo + timedelta(minutes=random.uniform(regras["intervalo_min"], regras["intervalo_max"])))
    random.shuffle(itens)  # a ordem de envio não segue a lista
    res = []
    for it in itens:
        t = proximo_horario(db, regras, t, ocupados)
        ocupados.append(t)
        db.execute("INSERT INTO envios (cliente_id, chave, nome, numero, campanha, texto, lote, criado, agendado, status, via, ia) "
                   "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                   (it.get("cliente_id"), it.get("chave"), it.get("nome"), it.get("numero"), it.get("campanha"), it.get("texto"),
                    lote, agora_txt(agora), agora_txt(t), "fila", "auto", 1 if it.get("ia") else 0))
        res.append((it, t))
        t = t + timedelta(minutes=random.uniform(regras["intervalo_min"], regras["intervalo_max"]))
    db.commit()
    return lote, res


def agendar_ja(db, it, regras):
    """Envio avulso pedido na hora: vai na frente da fila, respeitando horário e limites (o despachante mantém o intervalo mínimo)."""
    agora = datetime.now()
    ocupados = [_dt(r[0]) for r in db.execute(
        "SELECT enviado FROM envios WHERE via='auto' AND status='enviado' AND enviado >= ?", ((agora - timedelta(days=1)).strftime("%Y-%m-%d"),))]
    t = proximo_horario(db, regras, agora + timedelta(seconds=10), [x for x in ocupados if x])
    lote = "avulso-" + uuid.uuid4().hex[:6]
    db.execute("INSERT INTO envios (cliente_id, chave, nome, numero, campanha, texto, lote, criado, agendado, status, via, ia) "
               "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
               (it.get("cliente_id"), it.get("chave"), it.get("nome"), it.get("numero"), it.get("campanha"), it.get("texto"),
                lote, agora_txt(agora), agora_txt(t), "fila", "auto", 0))
    db.commit()
    return lote, [(it, t)]


def reagendar_pendentes(db, regras):
    """Depois de uma pausa (ou PC desligado), espalha de novo o que ficou para trás, sem mandar tudo de uma vez."""
    agora = datetime.now()
    pend = db.execute("SELECT id FROM envios WHERE status='fila' ORDER BY agendado").fetchall()
    if not pend:
        return 0
    ocupados = [_dt(r[0]) for r in db.execute("SELECT enviado FROM envios WHERE via='auto' AND status='enviado' AND enviado >= ?",
                                              ((agora - timedelta(days=1)).strftime("%Y-%m-%d"),))]
    ocupados = [x for x in ocupados if x]
    t = agora + timedelta(minutes=random.uniform(1, regras["intervalo_min"]))
    for (eid,) in pend:
        t = proximo_horario(db, regras, t, ocupados)
        ocupados.append(t)
        db.execute("UPDATE envios SET agendado=? WHERE id=?", (agora_txt(t), eid))
        t = t + timedelta(minutes=random.uniform(regras["intervalo_min"], regras["intervalo_max"]))
    db.commit()
    return len(pend)


def resumo(db, regras):
    agora = datetime.now()
    hoje = agora.strftime("%Y-%m-%d")
    fila = db.execute("SELECT id, nome, campanha, agendado, lote, ia FROM envios WHERE status='fila' ORDER BY agendado").fetchall()
    enviados = db.execute("SELECT COUNT(*) FROM envios WHERE via='auto' AND status='enviado' AND enviado >= ?", (hoje,)).fetchone()[0]
    manuais = db.execute("SELECT COUNT(*) FROM envios WHERE via='manual' AND status='enviado' AND enviado >= ?", (hoje,)).fetchone()[0]
    recentes = db.execute("SELECT id, nome, campanha, COALESCE(enviado, agendado), status, erro, via, texto FROM envios "
                          "WHERE status IN ('enviado','erro','cancelado') ORDER BY COALESCE(enviado, agendado) DESC LIMIT 80").fetchall()
    return {
        "pausada": P.meta_get(db, "fila_pausada") or "",
        "limite_hoje": limite_dia(db, regras, agora),
        "enviados_hoje": enviados, "manuais_hoje": manuais,
        "fila": [{"id": r[0], "nome": r[1], "campanha": r[2], "quando": r[3][:16], "lote": r[4], "ia": r[5]} for r in fila[:300]],
        "fila_total": len(fila),
        "fim_previsto": fila[-1][3][:16] if fila else "",
        "recentes": [{"id": r[0], "nome": r[1], "campanha": r[2], "quando": (r[3] or "")[:16], "status": r[4], "erro": r[5], "via": r[6],
                      "texto": (r[7] or "")[:400]} for r in recentes],
    }


def historico_cliente(db, cid, chave):
    rows = db.execute("SELECT COALESCE(enviado, agendado), campanha, status, via, texto, erro FROM envios WHERE cliente_id=? OR (chave=? AND chave IS NOT NULL) "
                      "ORDER BY COALESCE(enviado, agendado) DESC LIMIT 60", (str(cid), chave)).fetchall()
    bloq = db.execute("SELECT em FROM optout WHERE cliente_id=?", (str(cid),)).fetchone()
    return {"mensagens": [{"quando": (r[0] or "")[:16], "campanha": r[1], "status": r[2], "via": r[3], "texto": r[4], "erro": r[5]} for r in rows],
            "bloqueado": bool(bloq)}


# ------------------------------------------------------------------ lembretes (clientes para lembrar depois)

def lembretes(db, todos=False):
    q = "SELECT id, cliente_id, chave, nome, texto, quando, criado, feito, grupo, grupo_titulo FROM lembretes"
    if not todos:
        q += " WHERE feito IS NULL OR feito >= ?"
        rows = db.execute(q + " ORDER BY feito IS NOT NULL, COALESCE(quando, '9999'), criado", ((datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d"),)).fetchall()
    else:
        rows = db.execute(q + " ORDER BY criado DESC").fetchall()
    return [{"id": r[0], "cliente_id": r[1], "chave": r[2], "nome": r[3], "texto": r[4], "quando": r[5] or "", "criado": (r[6] or "")[:16],
             "feito": (r[7] or "")[:16], "grupo": r[8] or "", "grupo_titulo": r[9] or ""} for r in rows]


def _data_ok(quando):
    quando = str(quando or "").strip()[:10]
    if quando:
        try:
            datetime.strptime(quando, "%Y-%m-%d")
        except ValueError:
            quando = ""
    return quando or None


def novo_grupo():
    return "g" + datetime.now().strftime("%y%m%d%H%M%S%f")[:-3]


def lembrete_acao(db, d):
    acao = d.get("acao")
    if acao == "salvar":
        texto = str(d.get("texto") or "").strip()[:1000]
        quando = str(d.get("quando") or "").strip()[:10]
        if quando:
            try:
                datetime.strptime(quando, "%Y-%m-%d")
            except ValueError:
                quando = ""
        if d.get("id"):
            db.execute("UPDATE lembretes SET texto=?, quando=?, avisado=0 WHERE id=?", (texto, quando or None, int(d["id"])))
        else:
            db.execute("INSERT INTO lembretes (cliente_id, chave, nome, texto, quando, criado, grupo, grupo_titulo) VALUES (?,?,?,?,?,?,?,?)",
                       (str(d.get("cliente_id") or ""), str(d.get("chave") or ""), str(d.get("nome") or "")[:120], texto, quando or None, agora_txt(),
                        str(d.get("grupo") or "") or None, str(d.get("grupo_titulo") or "")[:150] or None))
    elif acao == "salvar_varios":
        # vários clientes de uma vez, num grupo com título (ex.: o que a IA separou)
        texto = str(d.get("texto") or "").strip()[:1000]
        quando = _data_ok(d.get("quando"))
        grupo = str(d.get("grupo") or "") or novo_grupo()
        titulo = str(d.get("grupo_titulo") or texto or "Grupo")[:150]
        for it in (d.get("itens") or [])[:1000]:
            db.execute("INSERT INTO lembretes (cliente_id, chave, nome, texto, quando, criado, grupo, grupo_titulo) VALUES (?,?,?,?,?,?,?,?)",
                       (str(it.get("cliente_id") or ""), str(it.get("chave") or ""), str(it.get("nome") or "")[:120],
                        (str(it.get("texto") or "").strip()[:1000] or texto), quando, agora_txt(), grupo, titulo))
    elif acao in ("feito_grupo", "reabrir_grupo", "apagar_grupo", "renomear_grupo", "data_grupo"):
        g = str(d.get("grupo") or "")
        if not g:
            return False
        if acao == "feito_grupo":
            db.execute("UPDATE lembretes SET feito=? WHERE grupo=? AND feito IS NULL", (agora_txt(), g))
        elif acao == "reabrir_grupo":
            db.execute("UPDATE lembretes SET feito=NULL WHERE grupo=?", (g,))
        elif acao == "apagar_grupo":
            db.execute("DELETE FROM lembretes WHERE grupo=?", (g,))
        elif acao == "renomear_grupo":
            db.execute("UPDATE lembretes SET grupo_titulo=? WHERE grupo=?", (str(d.get("titulo") or "")[:150], g))
        else:
            db.execute("UPDATE lembretes SET quando=?, avisado=0 WHERE grupo=? AND feito IS NULL", (_data_ok(d.get("quando")), g))
    elif acao == "feito":
        db.execute("UPDATE lembretes SET feito=? WHERE id=?", (agora_txt(), int(d.get("id") or 0)))
    elif acao == "reabrir":
        db.execute("UPDATE lembretes SET feito=NULL WHERE id=?", (int(d.get("id") or 0),))
    elif acao == "apagar":
        db.execute("DELETE FROM lembretes WHERE id=?", (int(d.get("id") or 0),))
    else:
        return False
    db.commit()
    return True


def lembretes_a_avisar(db):
    hoje = datetime.now().strftime("%Y-%m-%d")
    rows = db.execute("SELECT id, nome, texto FROM lembretes WHERE feito IS NULL AND avisado=0 AND quando IS NOT NULL AND quando <= ?", (hoje,)).fetchall()
    for r in rows:
        db.execute("UPDATE lembretes SET avisado=1 WHERE id=?", (r[0],))
    db.commit()
    return rows


# ------------------------------------------------------------------ listas de clientes (páginas salvas, muitas vindas da IA)

def _itens_ok(itens):
    out, vistos = [], set()
    for it in (itens or [])[:3000]:
        if not isinstance(it, dict):
            continue
        k = (str(it.get("chave") or ""), str(it.get("grupo") or ""))
        if not k[0] or k in vistos:
            continue
        vistos.add(k)
        out.append({"chave": k[0], "cliente_id": str(it.get("cliente_id") or ""), "nome": str(it.get("nome") or "")[:120],
                    "grupo": k[1][:150], "nota": str(it.get("nota") or "")[:300]})
    return out


def listas(db):
    rows = db.execute("SELECT id, titulo, descricao, origem, criado, atualizado, itens FROM listas ORDER BY COALESCE(atualizado, criado) DESC").fetchall()
    out = []
    for r in rows:
        try:
            itens = json.loads(r[6] or "[]")
        except ValueError:
            itens = []
        out.append({"id": r[0], "titulo": r[1] or "Lista", "descricao": r[2] or "", "origem": r[3] or "", "criado": (r[4] or "")[:16],
                    "atualizado": (r[5] or "")[:16], "itens": itens})
    return out


def lista_acao(db, d):
    acao = d.get("acao")
    lid = int(d.get("id") or 0)
    if acao == "salvar":
        itens = _itens_ok(d.get("itens"))
        titulo = str(d.get("titulo") or "Lista")[:150]
        if lid:
            db.execute("UPDATE listas SET titulo=?, descricao=?, itens=?, atualizado=? WHERE id=?",
                       (titulo, str(d.get("descricao") or "")[:2000], json.dumps(itens, ensure_ascii=False), agora_txt(), lid))
        else:
            cur = db.execute("INSERT INTO listas (titulo, descricao, origem, criado, atualizado, itens) VALUES (?,?,?,?,?,?)",
                             (titulo, str(d.get("descricao") or "")[:2000], str(d.get("origem") or "")[:30], agora_txt(), agora_txt(),
                              json.dumps(itens, ensure_ascii=False)))
            lid = cur.lastrowid
    elif acao == "renomear" and lid:
        db.execute("UPDATE listas SET titulo=?, descricao=COALESCE(?, descricao), atualizado=? WHERE id=?",
                   (str(d.get("titulo") or "Lista")[:150], d.get("descricao"), agora_txt(), lid))
    elif acao in ("remover_itens", "adicionar_itens", "mover_itens") and lid:
        r = db.execute("SELECT itens FROM listas WHERE id=?", (lid,)).fetchone()
        if not r:
            return 0
        itens = json.loads(r[0] or "[]")
        if acao == "remover_itens":
            tira = {(str(x.get("chave")), str(x.get("grupo") or "")) for x in d.get("itens") or []}
            itens = [x for x in itens if (x["chave"], x.get("grupo") or "") not in tira]
        elif acao == "mover_itens":
            alvo = str(d.get("grupo") or "")[:150]
            muda = {str(x.get("chave")) for x in d.get("itens") or []}
            for x in itens:
                if x["chave"] in muda:
                    x["grupo"] = alvo
            itens = _itens_ok(itens)
        else:
            itens = _itens_ok(itens + list(d.get("itens") or []))
        db.execute("UPDATE listas SET itens=?, atualizado=? WHERE id=?", (json.dumps(itens, ensure_ascii=False), agora_txt(), lid))
    elif acao == "apagar" and lid:
        db.execute("DELETE FROM listas WHERE id=?", (lid,))
    else:
        return 0
    db.commit()
    return lid or 1


# ------------------------------------------------------------------ reservas

STATUS_RESERVA = ("confirmada", "chegou", "nao_veio", "cancelada")
CAMPOS_RESERVA = ("nome", "telefone", "data", "hora", "pessoas", "rodizio", "valor_pago", "forma_pagamento", "observacao", "status",
                  "cliente_id", "chave")


def _reserva_limpa(d, parcial=False):
    """Confere e normaliza os campos de uma reserva. Levanta ValueError com a explicação se algo estiver errado."""
    out = {}
    for k in CAMPOS_RESERVA:
        if k not in d or (parcial and d.get(k) is None):
            continue
        v = d.get(k)
        if k == "data":
            v = str(v or "").strip()[:10]
            datetime.strptime(v, "%Y-%m-%d")
        elif k == "hora":
            v = str(v or "").strip().lower().replace("h", ":")
            if v.endswith(":"):
                v += "00"
            if v and ":" not in v:
                v += ":00"
            hh, mm = (v.split(":") + ["0"])[:2]
            v = "%02d:%02d" % (int(hh), int(mm or 0))
            if not (0 <= int(hh) <= 23 and 0 <= int(mm or 0) <= 59):
                raise ValueError("hora inválida")
        elif k == "pessoas":
            v = int(v) if v not in (None, "") else None
            if v is not None and not 0 < v <= 500:
                raise ValueError("número de pessoas inválido")
        elif k == "rodizio":
            v = None if v in (None, "") else (1 if v in (True, 1, "1", "sim", "true", "s") else 0)
        elif k == "valor_pago":
            v = float(str(v).replace("R$", "").replace(".", "").replace(",", ".")) if isinstance(v, str) and v.strip() else (float(v) if v not in (None, "") else None)
        elif k == "status":
            v = str(v or "confirmada")
            if v not in STATUS_RESERVA:
                raise ValueError("status inválido")
        elif k == "telefone":
            v = re.sub(r"\D", "", str(v or ""))[:15] or None
        else:
            v = (str(v).strip()[:1000] if v not in (None, "") else None)
        out[k] = v
    if not parcial:
        for k in ("nome", "data", "hora"):
            if not out.get(k):
                raise ValueError("falta %s" % k)
    return out


def reservas(db, de=None, ate=None, todas=False):
    q = ("SELECT id, nome, telefone, data, hora, pessoas, rodizio, valor_pago, forma_pagamento, observacao, status, cliente_id, chave, "
         "origem, criado, atualizado FROM reservas")
    args = []
    if not todas:
        de = de or datetime.now().strftime("%Y-%m-%d")
        q += " WHERE data >= ?"
        args.append(de)
        if ate:
            q += " AND data <= ?"
            args.append(ate)
    q += " ORDER BY data, hora, id"
    cols = ("id", "nome", "telefone", "data", "hora", "pessoas", "rodizio", "valor_pago", "forma_pagamento", "observacao", "status",
            "cliente_id", "chave", "origem", "criado", "atualizado")
    return [dict(zip(cols, r)) for r in db.execute(q, args).fetchall()]


def reserva_acao(db, d):
    """Cria/edita/muda status/apaga. Retorna a reserva (dict) ou levanta ValueError."""
    acao = d.get("acao") or "salvar"
    rid = int(d.get("id") or 0)
    if acao == "apagar":
        db.execute("DELETE FROM reservas WHERE id=?", (rid,))
        db.commit()
        return {"id": rid, "apagada": True}
    if acao == "status":
        st = str(d.get("status") or "")
        if st not in STATUS_RESERVA:
            raise ValueError("status inválido")
        db.execute("UPDATE reservas SET status=?, atualizado=? WHERE id=?", (st, agora_txt(), rid))
    elif rid:
        campos = _reserva_limpa(d, parcial=True)
        if campos:
            db.execute("UPDATE reservas SET %s, atualizado=? WHERE id=?" % ", ".join("%s=?" % k for k in campos),
                       list(campos.values()) + [agora_txt(), rid])
    else:
        campos = _reserva_limpa(d)
        campos.setdefault("status", "confirmada")
        campos["origem"] = str(d.get("origem") or "manual")[:20]
        campos["criado"] = campos["atualizado"] = agora_txt()
        cur = db.execute("INSERT INTO reservas (%s) VALUES (%s)" % (", ".join(campos), ",".join("?" * len(campos))), list(campos.values()))
        rid = cur.lastrowid
    db.commit()
    r = [x for x in reservas(db, todas=True) if x["id"] == rid]
    if not r:
        raise ValueError("reserva não encontrada")
    return r[0]
