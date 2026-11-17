"""Funções de IA do painel: mensagem personalizada, resposta a avaliação, resumo do dia e perguntas sobre os dados.

Para a IA vão só: primeiro nome, histórico de pedidos (itens, datas, valores, canal) e avaliações.
Telefone, endereço e e-mail nunca saem do computador.
"""
import json
import os
import re
import sqlite3
import threading
import time
from collections import Counter
from datetime import datetime, timedelta
from statistics import median

import pizzaria as P
from ia import ErroIA

DIAS = ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"]  # weekday() do Python
CASA = ("Pizzaria Mariá, em Recife: no almoço funciona como restaurante (quentinhas, pratos feitos, feijoada etc.) "
        "e à noite como pizzaria. Pedidos chegam pelo app próprio (cardápio digital), WhatsApp, iFood e 99Food.")


def _grupo(canal_id):
    return {"ifood": "iFood", "food99": "99Food"}.get(canal_id, "próprio")


def primeiro_nome(nome):
    p = (nome or "").strip().split()
    n = p[0] if p else ""
    return n[:1].upper() + n[1:].lower() if n else ""


# ------------------------------------------------------------------ contexto do cliente

def contexto_cliente(D, cli, perfil):
    """Texto com o que a IA precisa saber do cliente (sem telefone/endereço)."""
    c = D["clientes"][cli]
    canais = D["canais"]
    rows = [p for p in D["pedidos"] if p[5] == cli]
    ok = [p for p in rows if not p[7]]
    linhas = ["Primeiro nome: %s" % (primeiro_nome(c.get("nome")) or "(sem nome)")]
    if ok:
        datas = sorted(datetime.strptime(p[0], "%Y-%m-%d %H:%M") for p in ok)
        gasto = sum(p[4] or 0 for p in ok)
        dias = (datetime.now() - datas[-1]).days
        linhas.append("Pedidos: %d (R$ %.0f no total, ticket médio R$ %.0f). Cliente desde %s." %
                      (len(ok), gasto, gasto / len(ok), datas[0].strftime("%m/%Y")))
        linhas.append("Último pedido: há %d dia%s (%s)." % (dias, "" if dias == 1 else "s", datas[-1].strftime("%d/%m") + ", " + DIAS[datas[-1].weekday()] + datas[-1].strftime(" %H:%M")))
        dd = sorted({d.date() for d in datas})
        iv = [(b - a).days for a, b in zip(dd, dd[1:])]
        if len(iv) >= 2:
            linhas.append("Costuma pedir a cada %d dias." % round(median(iv)))
        dw = Counter(d.weekday() for d in datas).most_common(1)[0]
        if dw[1] / len(datas) >= 0.3 and len(datas) >= 3:
            linhas.append("Dia preferido: %s (%d%% dos pedidos)." % (DIAS[dw[0]], round(100 * dw[1] / len(datas))))
        tc = Counter(D["turnos"][p[1]] for p in ok)
        linhas.append("Turnos: " + ", ".join("%s %d" % (k, v) for k, v in tc.most_common()))
        gc = Counter(_grupo(canais[p[2]]["id"]) for p in ok)
        linhas.append("Canais: " + ", ".join("%s %d" % (k, v) for k, v in gc.most_common()))
        promos = D.get("promos_casa") or []
        pc = Counter(promos[k]["nome"] for p in ok if len(p) > 13 and p[13] for k in range(len(promos)) if p[13] >> k & 1)
        if pc:
            linhas.append("Promoções da casa que já usou: " + ", ".join("%s (%dx)" % kv for kv in pc.most_common()))
    peds = (perfil or {}).get("pedidos") or []
    itens, sab = Counter(), Counter()
    for x in [x for x in peds if not x.get("cancelado")][:20]:
        for it in x.get("itens") or []:
            itens[it.get("n")] += it.get("q") or 1
            for o in it.get("o") or []:
                if not re.search(r"borda|refri|coca|adicional|sem |molho", o, re.I):
                    sab[o] += 1
    if itens:
        linhas.append("Itens que mais pede: " + ", ".join("%s (%dx)" % kv for kv in itens.most_common(5)))
    if sab:
        linhas.append("Sabores/opções que mais escolhe: " + ", ".join("%s (%dx)" % kv for kv in sab.most_common(6)))
    if peds:
        u = next((x for x in peds if not x.get("cancelado")), None)
        if u:
            desc = "; ".join("%s%s" % (it.get("n"), (" (" + ", ".join(it.get("o")[:4]) + ")") if it.get("o") else "") for it in u.get("itens") or [])
            linhas.append("Último pedido: %s" % desc)
    for a in ((perfil or {}).get("avaliacoes") or [])[:3]:
        linhas.append("Avaliação em %s: nota %s%s%s" % (a["data"][:10], a["nota"], (". Notas baixas: " + a["baixas"]) if a.get("baixas") else "",
                                                         ('. Comentário: "%s"' % a["comentario"]) if a.get("comentario") else ""))
    return "\n".join(linhas)


# ------------------------------------------------------------------ mensagens

SIS_MSG = ("Você escreve mensagens curtas de WhatsApp em nome da " + CASA +
           "\nRegras:\n"
           "- Português do Brasil, tom próximo e simpático, como o dono falaria com um cliente conhecido. Nada de linguagem de marketing.\n"
           "- No máximo 3 frases curtas e no máximo 2 emojis.\n"
           "- Chame o cliente pelo primeiro nome.\n"
           "- Não comece com 'aqui é da pizzaria'. Só se apresente (como 'Pizzaria Mariá') se a mensagem for sobre uma avaliação.\n"
           "- Use o histórico do cliente para personalizar: cite o prato de forma natural (ex.: 'aquela pizza de camarão', "
           "'aquela quentinha de frango'), o dia ou o turno em que ele costuma pedir, etc. Não cite números de pedidos nem valores gastos.\n"
           "- Mantenha EXATAMENTE a mesma oferta/condição da mensagem de referência (desconto, preço, cupom, validade). "
           "Se a referência não tem oferta, não ofereça nada. Nunca invente promoção.\n"
           "- Quando fizer sentido, convide a pedir pelo app próprio.\n"
           "- Responda só com o texto da mensagem, sem aspas.")

SIS_AVAL = ("Você ajuda a " + CASA + " a responder avaliações ruins de clientes.\n"
            "Escreva duas coisas em português do Brasil, tom humano e sem ser defensivo:\n"
            "1) whatsapp: mensagem curta para o cliente no WhatsApp (até 3 frases), apresentando-se como Pizzaria Mariá, reconhecendo o problema "
            "específico que ele apontou e perguntando o que aconteceu ou oferecendo resolver. Não ofereça desconto nem brinde, a menos que a "
            "mensagem de referência ofereça.\n"
            "2) resposta_publica: resposta pública para a avaliação (até 3 frases), educada, sem citar dados pessoais.\n"
            'Responda APENAS com JSON: {"whatsapp": "...", "resposta_publica": "..."}')


def gerar_mensagem(ia, D, cli, perfil, motivo, referencia, tipo=""):
    ctx = contexto_cliente(D, cli, perfil)
    hoje = datetime.now()
    pedido = ("Hoje é %s, %s.\nMotivo do contato: %s\nMensagem de referência (mesma oferta): %s\n\nDados do cliente:\n%s" %
              (DIAS[hoje.weekday()], hoje.strftime("%d/%m %H:%M"), motivo or "-", referencia or "-", ctx))
    if tipo == "avaliacao":
        txt = ia.gerar(SIS_AVAL, pedido, max_tokens=600)
        m = re.search(r"\{.*\}", txt, re.S)
        try:
            j = json.loads(m.group(0)) if m else {}
        except ValueError:
            j = {}
        return {"texto": (j.get("whatsapp") or txt).strip(), "resposta_publica": (j.get("resposta_publica") or "").strip()}
    return {"texto": ia.gerar(SIS_MSG, pedido, max_tokens=400).strip().strip('"')}


# ------------------------------------------------------------------ resumo do dia

def _metricas(D):
    rows = [p for p in D["pedidos"] if not p[7] and not (len(p) > 12 and p[12])]
    canais = D["canais"]
    agora = datetime.now()
    hoje = agora.date()
    por_dia = {}
    for p in rows:
        d = datetime.strptime(p[0], "%Y-%m-%d %H:%M")
        x = por_dia.setdefault(d.date(), {"n": 0, "v": 0.0, "g": Counter(), "t": Counter(), "ate_agora": [0, 0.0]})
        x["n"] += 1
        x["v"] += p[4] or 0
        x["g"][_grupo(canais[p[2]]["id"])] += p[4] or 0
        x["t"][D["turnos"][p[1]]] += p[4] or 0
        if (d.hour, d.minute) <= (agora.hour, agora.minute):
            x["ate_agora"][0] += 1
            x["ate_agora"][1] += p[4] or 0
    vazio = {"n": 0, "v": 0.0, "g": Counter(), "t": Counter(), "ate_agora": [0, 0.0]}

    def media(dias, campo):
        xs = [por_dia.get(d, vazio) for d in dias]
        if campo == "ate_agora":
            return [sum(x["ate_agora"][0] for x in xs) / len(xs), sum(x["ate_agora"][1] for x in xs) / len(xs)]
        return sum(x[campo] for x in xs) / len(xs)

    def bloco(dia, rotulo):
        x = por_dia.get(dia, vazio)
        ant = [dia - timedelta(days=7 * k) for k in range(1, 5)]
        g = ", ".join("%s R$ %.0f" % kv for kv in x["g"].most_common())
        gm = Counter()
        for d in ant:
            for k, v in por_dia.get(d, vazio)["g"].items():
                gm[k] += v / 4
        t = ", ".join("%s R$ %.0f" % kv for kv in x["t"].most_common())
        return ("%s (%s %s): %d pedidos, R$ %.0f. Média das 4 %ss anteriores: %.0f pedidos, R$ %.0f.\n  Por canal: %s (média: %s)\n  Por turno: %s" %
                (rotulo, DIAS[dia.weekday()], dia.strftime("%d/%m"), x["n"], x["v"], DIAS[dia.weekday()], media(ant, "n"), media(ant, "v"),
                 g or "-", ", ".join("%s R$ %.0f" % kv for kv in gm.most_common()) or "-", t or "-"))

    linhas = [bloco(hoje - timedelta(days=1), "Ontem")]
    ant = [hoje - timedelta(days=7 * k) for k in range(1, 5)]
    ha = por_dia.get(hoje, vazio)["ate_agora"]
    ma = media(ant, "ate_agora")
    linhas.append("Hoje até agora (%s): %d pedidos, R$ %.0f. Nas 4 %ss anteriores até este horário: %.0f pedidos, R$ %.0f em média." %
                  (agora.strftime("%H:%M"), ha[0], ha[1], DIAS[hoje.weekday()], ma[0], ma[1]))
    s1 = sum(por_dia.get(hoje - timedelta(days=k), vazio)["v"] for k in range(1, 8))
    s2 = sum(por_dia.get(hoje - timedelta(days=k), vazio)["v"] for k in range(8, 15))
    linhas.append("Últimos 7 dias: R$ %.0f. Os 7 dias antes disso: R$ %.0f." % (s1, s2))
    promos = D.get("promos_casa") or []
    js_dow = (hoje.weekday() + 1) % 7
    hoje_p = [p["nome"] + " (" + p.get("oferta", "") + ")" for p in promos if js_dow in (p.get("dias") or [])]
    if hoje_p:
        linhas.append("Promoção da casa hoje: " + "; ".join(hoje_p))
    for k, pr in enumerate(promos):
        if not pr.get("dias"):
            continue
        usos = [sum(1 for p in rows if len(p) > 13 and p[13] >> k & 1 and datetime.strptime(p[0][:10], "%Y-%m-%d").date() >= hoje - timedelta(days=7 * (w + 1))
                    and datetime.strptime(p[0][:10], "%Y-%m-%d").date() < hoje - timedelta(days=7 * w)) for w in range(4)]
        linhas.append("%s: pedidos com a promoção nas últimas 4 semanas (da mais recente): %s" % (pr["nome"], ", ".join(map(str, usos))))
    al = Counter(a.get("tipo") for a in (D.get("alertas") or []))
    if al:
        linhas.append("Alertas abertos no painel: " + ", ".join("%s %d" % kv for kv in al.most_common()))
    return "\n".join(linhas)


SIS_RESUMO = ("Você é o analista da " + CASA + "\nEscreva para o dono um resumo do dia, em português do Brasil, direto e útil:\n"
              "- 3 a 5 tópicos curtos (comece cada um com '• '), no máximo 1 emoji por tópico.\n"
              "- Diga o que está acima ou abaixo do normal e onde (canal, turno, promoção), com os números principais.\n"
              "- Termine com UMA ação concreta para hoje.\n"
              "- Use só os números fornecidos. Não invente nada. Se algo não dá para concluir, não fale.")


def resumo_dia(ia, D):
    return ia.gerar(SIS_RESUMO, _metricas(D), max_tokens=500).strip()


# ------------------------------------------------------------------ perguntas sobre os dados

ANALISE = {"trava": threading.Lock(), "gerado": 0, "versao": None}


def caminho_analise():
    return os.path.join(P.PASTA_DADOS, "analise.db")


def montar_analise(db, D, versao):
    """Base simplificada (somente leitura) para a IA consultar. Refeita quando os dados mudam (no máximo a cada 10 min)."""
    with ANALISE["trava"]:
        if ANALISE["versao"] == versao and os.path.exists(caminho_analise()):
            return
        if ANALISE["versao"] is not None and time.time() - ANALISE["gerado"] < 600 and os.path.exists(caminho_analise()):
            return
        tmp = caminho_analise() + ".tmp"
        if os.path.exists(tmp):
            os.remove(tmp)
        out = sqlite3.connect(tmp)
        out.executescript("""
CREATE TABLE pedidos (id INTEGER PRIMARY KEY, data TEXT, dia_semana TEXT, hora INTEGER, turno TEXT, canal TEXT, grupo_canal TEXT,
  tipo TEXT, cancelado INTEGER, total REAL, taxa_entrega REAL, desconto REAL, cupom TEXT, cliente_id INTEGER, bairro TEXT, promocoes TEXT);
CREATE TABLE itens (pedido_id INTEGER, data TEXT, turno TEXT, grupo_canal TEXT, item TEXT, quantidade REAL, valor REAL, opcoes TEXT);
CREATE TABLE clientes (id INTEGER PRIMARY KEY, primeiro_nome TEXT, pedidos INTEGER, total_gasto REAL, primeiro_pedido TEXT,
  ultimo_pedido TEXT, dias_sem_pedir INTEGER, canais TEXT, bairro TEXT, pontos INTEGER);
CREATE TABLE avaliacoes (data TEXT, cliente_id INTEGER, nota REAL, comentario TEXT);
""")
        canais, promos = D["canais"], D.get("promos_casa") or []
        por_pid, ped_rows = {}, []
        agg = {}
        for p in D["pedidos"]:
            if len(p) > 12 and p[12]:
                continue
            d = datetime.strptime(p[0], "%Y-%m-%d %H:%M")
            cid = canais[p[2]]["id"]
            pm = ", ".join(promos[k]["nome"] for k in range(len(promos)) if len(p) > 13 and p[13] >> k & 1)
            ped_rows.append((p[11], p[0], DIAS[d.weekday()], d.hour, D["turnos"][p[1]], canais[p[2]]["nome"], _grupo(cid), D["tipos"][p[3]],
                             p[7], p[4], p[8], p[9], D["cupons"][p[10]] if p[10] is not None and p[10] >= 0 else None,
                             p[5] if p[5] >= 0 else None, D["bairros"][p[6]] if p[6] >= 0 else None, pm or None))
            por_pid[p[11]] = (p[0], D["turnos"][p[1]], _grupo(cid))
            if p[5] >= 0 and not p[7]:
                a = agg.setdefault(p[5], {"n": 0, "v": 0.0, "pri": p[0], "ult": p[0], "g": set(), "b": Counter()})
                a["n"] += 1
                a["v"] += p[4] or 0
                a["pri"] = min(a["pri"], p[0])
                a["ult"] = max(a["ult"], p[0])
                a["g"].add(_grupo(cid))
                if p[6] >= 0:
                    a["b"][D["bairros"][p[6]]] += 1
        out.executemany("INSERT OR REPLACE INTO pedidos VALUES (%s)" % ",".join("?" * 16), ped_rows)
        lote = []
        for pid, st, det in db.execute("SELECT id, status, detalhe_json FROM pedidos WHERE detalhe_json IS NOT NULL AND status='closed'"):
            info = por_pid.get(pid)
            if not info:
                continue
            try:
                d = json.loads(det)
            except ValueError:
                continue
            for it in d.get("items") or []:
                ops = ", ".join((o.get("name") or "").strip() for o in (it.get("options") or []) if o.get("name"))
                lote.append((pid, info[0], info[1], info[2], (it.get("name") or "").strip(), it.get("quantity") or 1, it.get("total_price"), ops or None))
            if len(lote) > 5000:
                out.executemany("INSERT INTO itens VALUES (?,?,?,?,?,?,?,?)", lote)
                lote = []
        out.executemany("INSERT INTO itens VALUES (?,?,?,?,?,?,?,?)", lote)
        hoje = datetime.now()
        out.executemany("INSERT INTO clientes VALUES (?,?,?,?,?,?,?,?,?,?)", [
            (i, primeiro_nome(D["clientes"][i].get("nome")), a["n"], round(a["v"], 2), a["pri"], a["ult"],
             (hoje - datetime.strptime(a["ult"], "%Y-%m-%d %H:%M")).days, ", ".join(sorted(a["g"])),
             a["b"].most_common(1)[0][0] if a["b"] else None, D["clientes"][i].get("pontos") or 0) for i, a in agg.items()])
        out.executemany("INSERT INTO avaliacoes VALUES (?,?,?,?)",
                        [(a[0], a[1] if a[1] >= 0 else None, a[2], a[3] or None) for a in D.get("avaliacoes") or []])
        out.execute("CREATE INDEX ip ON itens(pedido_id)")
        out.execute("CREATE INDEX pd ON pedidos(data)")
        out.commit()
        out.close()
        os.replace(tmp, caminho_analise())
        ANALISE["versao"], ANALISE["gerado"] = versao, time.time()


def _autorizar(acao, *a):
    ok = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, getattr(sqlite3, "SQLITE_RECURSIVE", 33)}
    return sqlite3.SQLITE_OK if acao in ok else sqlite3.SQLITE_DENY


def consultar(sql):
    sql = (sql or "").strip().rstrip(";")
    if not re.match(r"(?is)^\s*(select|with)\b", sql) or ";" in sql:
        return "Erro: só é permitido um SELECT."
    con = sqlite3.connect("file:%s?mode=ro" % caminho_analise().replace("\\", "/"), uri=True)
    try:
        con.set_authorizer(_autorizar)
        fim = time.time() + 8
        con.set_progress_handler(lambda: 1 if time.time() > fim else 0, 20000)
        cur = con.execute(sql)
        cols = [c[0] for c in cur.description or []]
        rows = cur.fetchmany(201)
    except sqlite3.Error as e:
        return "Erro no SQL: %s" % e
    finally:
        con.close()
    linhas = [" | ".join(cols)] + [" | ".join("" if v is None else (("%.2f" % v) if isinstance(v, float) else str(v)) for v in r) for r in rows[:200]]
    txt = "\n".join(linhas)
    if len(rows) > 200:
        txt += "\n(mais de 200 linhas: resultado cortado; use GROUP BY/LIMIT)"
    return txt[:14000]


def sistema_pergunta(D):
    hoje = datetime.now()
    return ("Você é o analista de dados da " + CASA + " Responda às perguntas do dono em português do Brasil, de forma curta e clara, "
            "com os números principais (R$ com vírgula decimal) e, quando ajudar, uma recomendação prática.\n"
            "Use a ferramenta consultar_sql para buscar os dados; nunca invente números. Pode fazer várias consultas.\n"
            "Hoje é %s, %s. Turnos: %s. 'grupo_canal' é próprio, iFood ou 99Food. Pedidos cancelados têm cancelado=1 (exclua-os das vendas).\n"
            "Base SQLite (datas no formato 'AAAA-MM-DD HH:MM'):\n"
            "- pedidos(id, data, dia_semana, hora, turno, canal, grupo_canal, tipo, cancelado, total, taxa_entrega, desconto, cupom, cliente_id, bairro, promocoes)\n"
            "- itens(pedido_id, data, turno, grupo_canal, item, quantidade, valor, opcoes)  -- opcoes = sabores/bordas/acompanhamentos, separados por vírgula; só pedidos concluídos\n"
            "- clientes(id, primeiro_nome, pedidos, total_gasto, primeiro_pedido, ultimo_pedido, dias_sem_pedir, canais, bairro, pontos)\n"
            "- avaliacoes(data, cliente_id, nota, comentario)\n"
            "Promoções da casa (coluna promocoes): %s.\n"
            "Para nomes de itens/sabores use LIKE com %% (ex.: item LIKE '%%calabresa%%' OR opcoes LIKE '%%calabresa%%'). "
            "Não há telefone nem endereço na base; se pedirem, diga que isso fica só no perfil do cliente no painel. "
            "Não use tabelas, só texto curto com tópicos se precisar." %
            (DIAS[hoje.weekday()], hoje.strftime("%d/%m/%Y %H:%M"), ", ".join(D["turnos"][:-1]),
             "; ".join("%s (%s)" % (p["nome"], p.get("oferta", "")) for p in D.get("promos_casa") or []) or "nenhuma"))


FERRAMENTA_SQL = [{"name": "consultar_sql",
                   "description": "Executa uma consulta SQL (somente SELECT, SQLite) na base de vendas da Mariá e devolve até 200 linhas.",
                   "parameters": {"type": "object", "properties": {"sql": {"type": "string", "description": "Uma consulta SELECT"}},
                                  "required": ["sql"]}}]


def perguntar(ia, D, mensagens):
    msgs = [{"role": m.get("role"), "content": str(m.get("content") or "")[:4000]} for m in mensagens[-12:]
            if m.get("role") in ("user", "assistant") and m.get("content")]
    if not msgs or msgs[-1]["role"] != "user":
        raise ErroIA("Faça uma pergunta.")
    resp, feitas = ia.conversar(sistema_pergunta(D), msgs, FERRAMENTA_SQL,
                                lambda nome, args: consultar(args.get("sql")) if nome == "consultar_sql" else "ferramenta desconhecida",
                                max_tokens=1500, forte=True, voltas=10)
    return {"resposta": resp, "consultas": len(feitas)}
