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
    if c.get("pontos"):
        linhas.append("Pontos de fidelidade acumulados: %s (só mencione se a mensagem de referência falar de pontos)" % c.get("pontos"))
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


PESSOAIS = {"phone", "phone_number", "telephone", "email", "number", "complement", "latitude", "longitude", "postal_code",
            "cpf", "cnpj", "document", "document_number", "birth_date_full", "card_number", "ip"}


def limpar_pessoais(obj, liberar):
    """Tira telefone, e-mail, número da casa, complemento, CEP, coordenadas e documentos (a menos que liberado no config)."""
    if liberar:
        return obj
    if isinstance(obj, dict):
        return {k: ("(oculto)" if k in PESSOAIS and v not in (None, "") else limpar_pessoais(v, False)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [limpar_pessoais(x, False) for x in obj]
    return obj


def montar_analise(db, D, versao, liberar=False):
    """Base completa (somente leitura) para a IA consultar, com tudo que a API do Cardápio Web devolve dos pedidos,
    clientes, avaliações, loja, cupons e cardápio. Refeita quando os dados mudam (no máximo a cada 10 min)."""
    with ANALISE["trava"]:
        if ANALISE["versao"] == (versao, liberar) and os.path.exists(caminho_analise()):
            return
        if ANALISE["versao"] is not None and ANALISE["versao"][1] == liberar and time.time() - ANALISE["gerado"] < 600 \
                and os.path.exists(caminho_analise()):
            return
        tmp = caminho_analise() + ".tmp"
        if os.path.exists(tmp):
            os.remove(tmp)
        out = sqlite3.connect(tmp)
        out.executescript("""
CREATE TABLE pedidos (id INTEGER PRIMARY KEY, numero TEXT, data TEXT, dia_semana TEXT, hora INTEGER, turno TEXT, status TEXT,
  canal TEXT, grupo_canal TEXT, tipo TEXT, momento TEXT, agendado_para TEXT, cancelado INTEGER, motivo_cancelamento TEXT,
  total REAL, subtotal_itens REAL, taxa_entrega REAL, taxa_servico REAL, taxa_adicional REAL, desconto REAL, cupom TEXT,
  pagamento TEXT, pago_online INTEGER, troco_para REAL, entregue_por TEXT, entregador_id INTEGER, taxa_entregador REAL,
  tempo_estimado_min INTEGER, origem_cliente TEXT, observacao TEXT, observacao_interna TEXT, cliente_id INTEGER,
  cliente_nome TEXT, cliente_telefone TEXT, rua TEXT, numero_casa TEXT, complemento TEXT, referencia TEXT, bairro TEXT,
  cidade TEXT, promocoes TEXT, atualizado_em TEXT);
CREATE TABLE itens (pedido_id INTEGER, data TEXT, turno TEXT, grupo_canal TEXT, item TEXT, tipo_item TEXT, quantidade REAL,
  preco_unitario REAL, valor REAL, opcoes TEXT, observacao TEXT, status TEXT, cancelado INTEGER);
CREATE TABLE opcoes (pedido_id INTEGER, data TEXT, turno TEXT, item TEXT, grupo TEXT, opcao TEXT, quantidade REAL, preco_unitario REAL);
CREATE TABLE descontos (pedido_id INTEGER, data TEXT, tipo TEXT, categoria TEXT, quem_paga TEXT, valor REAL, cupom TEXT,
  nome_cupom TEXT, pontos INTEGER, item TEXT);
CREATE TABLE pagamentos (pedido_id INTEGER, data TEXT, forma TEXT, tipo TEXT, valor REAL, status TEXT, bandeira TEXT, taxa REAL);
CREATE TABLE clientes (id INTEGER PRIMARY KEY, nome TEXT, primeiro_nome TEXT, telefone TEXT, email TEXT, aniversario TEXT,
  cadastro TEXT, pontos INTEGER, cashback REAL, pedidos INTEGER, total_gasto REAL, primeiro_pedido TEXT, ultimo_pedido TEXT,
  dias_sem_pedir INTEGER, canais TEXT, bairro TEXT, unido_de TEXT);
CREATE TABLE avaliacoes (data TEXT, pedido_id INTEGER, cliente_id INTEGER, nota REAL, comentario TEXT, respostas TEXT);
CREATE TABLE alertas_abertos (tipo TEXT, prioridade INTEGER, cliente_id INTEGER, titulo TEXT, texto TEXT, criado TEXT);
CREATE TABLE promocoes_casa (nome TEXT, dias TEXT, oferta TEXT, reconhece_por TEXT, so_app_proprio INTEGER);
CREATE TABLE cadastro_loja (tipo TEXT, json TEXT);
""")
        canais, promos = D["canais"], D.get("promos_casa") or []
        info = {}
        agg = {}
        for p in D["pedidos"]:
            d = datetime.strptime(p[0], "%Y-%m-%d %H:%M")
            cid = canais[p[2]]["id"]
            pm = ", ".join(promos[k]["nome"] for k in range(len(promos)) if len(p) > 13 and p[13] >> k & 1)
            info[p[11]] = {"row": p, "d": d, "turno": D["turnos"][p[1]], "canal": canais[p[2]]["nome"], "grupo": _grupo(cid), "pm": pm}
            if p[5] >= 0 and not p[7] and not (len(p) > 12 and p[12]):
                a = agg.setdefault(p[5], {"n": 0, "v": 0.0, "pri": p[0], "ult": p[0], "g": set(), "b": Counter()})
                a["n"] += 1
                a["v"] += p[4] or 0
                a["pri"] = min(a["pri"], p[0])
                a["ult"] = max(a["ult"], p[0])
                a["g"].add(_grupo(cid))
                if p[6] >= 0:
                    a["b"][D["bairros"][p[6]]] += 1
        oc = lambda v: v if liberar else (None if v in (None, "") else "(oculto)")
        nomes_pag = {}
        lote = {"pedidos": [], "itens": [], "opcoes": [], "descontos": [], "pagamentos": []}

        def despejar(forcar=False):
            for t, n in (("pedidos", 42), ("itens", 13), ("opcoes", 8), ("descontos", 10), ("pagamentos", 8)):
                if lote[t] and (forcar or len(lote[t]) > 4000):
                    out.executemany("INSERT OR REPLACE INTO %s VALUES (%s)" % (t, ",".join("?" * n)), lote[t])
                    lote[t] = []
        vistos = set()
        for pid, st, otype, timing, canal, criado, atual, det in db.execute(
                "SELECT id, status, order_type, order_timing, sales_channel, created_at, updated_at, detalhe_json FROM pedidos"):
            i = info.get(pid)
            if not i:
                continue
            p, dt = i["row"], i["d"]
            dj = {}
            if det:
                try:
                    dj = json.loads(det)
                except ValueError:
                    dj = {}
            cust = dj.get("customer") or {}
            end = dj.get("delivery_address") or {}
            pags = dj.get("payments") or []
            itens_sub = sum((it.get("total_price") or 0) for it in dj.get("items") or [])
            sched = dj.get("schedule") or {}
            lote["pedidos"].append((
                pid, dj.get("display_id"), p[0], DIAS[dt.weekday()], dt.hour, i["turno"], st, i["canal"], i["grupo"],
                D["tipos"][p[3]], timing, (sched.get("scheduled_date_time_start") if isinstance(sched, dict) else None),
                p[7], dj.get("cancellation_reason"), p[4], round(itens_sub, 2) if dj else None, dj.get("delivery_fee"),
                dj.get("service_fee"), dj.get("additional_fee"), p[9],
                D["cupons"][p[10]] if p[10] is not None and p[10] >= 0 else None,
                ", ".join(x.get("payment_method") or "" for x in pags) or None,
                1 if any((x.get("payment_type") or "") == "online" for x in pags) else (0 if pags else None),
                next((x.get("change_for") for x in pags if x.get("change_for")), None),
                dj.get("delivered_by"), dj.get("driver_id"), dj.get("driver_fee"), dj.get("estimated_time"),
                dj.get("customer_origin"), dj.get("observation"), dj.get("internal_observation"),
                p[5] if p[5] >= 0 else None, cust.get("name"), oc(cust.get("phone")), end.get("street"), oc(end.get("number")),
                oc(end.get("complement")), end.get("reference"), end.get("neighborhood"), end.get("city"), i["pm"] or None, atual))
            vistos.add(pid)
            for it in dj.get("items") or []:
                ops = it.get("options") or []
                nome = (it.get("name") or "").strip()
                lote["itens"].append((pid, p[0], i["turno"], i["grupo"], nome, it.get("kind"), it.get("quantity") or 1, it.get("unit_price"),
                                      it.get("total_price"), ", ".join((o.get("name") or "").strip() for o in ops if o.get("name")) or None,
                                      it.get("observation"), it.get("status"), p[7]))
                for o in ops:
                    lote["opcoes"].append((pid, p[0], i["turno"], nome, (o.get("option_group_name") or "").strip() or None,
                                           (o.get("name") or "").strip(), (o.get("quantity") or 1) * (it.get("quantity") or 1), o.get("unit_price")))
            for x in dj.get("discounts") or []:
                lote["descontos"].append((pid, p[0], x.get("kind"), x.get("category"), x.get("sponsorship"), x.get("total"),
                                          x.get("coupon_code"), x.get("coupon_name"), x.get("total_points"), x.get("item_name")))
            for x in pags:
                lote["pagamentos"].append((pid, p[0], x.get("payment_method"), x.get("payment_type"), x.get("total"), x.get("status"),
                                           x.get("card_brand"), x.get("payment_fee")))
            despejar()
        despejar(True)
        hoje = datetime.now()
        cad = {r[0]: r[1:] for r in db.execute("SELECT id, nome, telefone, email, nascimento, cadastro, pontos, cashback FROM clientes")}
        rows = []
        for i, a in agg.items():
            c = D["clientes"][i]
            k = cad.get(c.get("id")) or (None,) * 7
            rows.append((i, c.get("nome"), primeiro_nome(c.get("nome")), oc(c.get("tel")), oc(k[2]), (k[3] or "")[:10] or None,
                         (k[4] or "")[:10] or None, c.get("pontos") or 0, c.get("cashback") or 0, a["n"], round(a["v"], 2), a["pri"], a["ult"],
                         (hoje - datetime.strptime(a["ult"], "%Y-%m-%d %H:%M")).days, ", ".join(sorted(a["g"])),
                         a["b"].most_common(1)[0][0] if a["b"] else None,
                         ", ".join(u.get("nome", "") + " (" + u.get("app", "") + ")" for u in c.get("unidos") or []) or None))
        out.executemany("INSERT OR REPLACE INTO clientes VALUES (%s)" % ",".join("?" * 17), rows)
        av = []
        for oid, cust, nota, coment, criada, js in db.execute("SELECT order_id, customer_id, nota, comentario, criada, json FROM avaliacoes"):
            try:
                resp = json.loads(js).get("answers") or []
            except (ValueError, TypeError):
                resp = []
            cli = next((p[5] for p in [info.get(oid, {}).get("row")] if p), None)
            av.append(((criada or "")[:16].replace("T", " "), oid, cli if cli is not None and cli >= 0 else None, nota, coment or None,
                       "; ".join("%s: %s" % (x.get("question"), x.get("answer")) for x in resp) or None))
        out.executemany("INSERT INTO avaliacoes VALUES (?,?,?,?,?,?)", av)
        out.executemany("INSERT INTO alertas_abertos VALUES (?,?,?,?,?,?)",
                        [(a.get("tipo"), a.get("prio"), a.get("cli"), a.get("titulo"), a.get("texto"), a.get("criado")) for a in D.get("alertas") or []])
        dn = ["domingo", "segunda", "terça", "quarta", "quinta", "sexta", "sábado"]
        out.executemany("INSERT INTO promocoes_casa VALUES (?,?,?,?,?)",
                        [(p.get("nome"), ", ".join(dn[d] for d in p.get("dias") or []) or "todo dia", p.get("oferta"),
                          ", ".join(["item contém '%s'" % t for t in p.get("itens") or []] + (["cupom " + p["cupom"]] if p.get("cupom") else [])),
                          1 if p.get("so_proprio") else 0) for p in promos])
        for tipo, chave in (("loja", "loja_json"), ("formas_pagamento", "pagamentos_json"), ("cupons", "cupons_json"), ("cardapio", "catalogo_json")):
            v = P.meta_get(db, chave)
            if v:
                try:
                    v = json.dumps(limpar_pessoais(json.loads(v), liberar), ensure_ascii=False)
                except ValueError:
                    pass
                out.execute("INSERT INTO cadastro_loja VALUES (?,?)", (tipo, v))
        for ix in ("CREATE INDEX ip ON itens(pedido_id)", "CREATE INDEX pd ON pedidos(data)", "CREATE INDEX op ON opcoes(pedido_id)",
                   "CREATE INDEX pc ON pedidos(cliente_id)"):
            out.execute(ix)
        out.commit()
        out.close()
        os.replace(tmp, caminho_analise())
        ANALISE["versao"], ANALISE["gerado"] = (versao, liberar), time.time()


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


def sistema_pergunta(D, liberar=False):
    hoje = datetime.now()
    return ("Você é o analista de dados da " + CASA + " Responda às perguntas do dono em português do Brasil, de forma clara e direta, "
            "com os números principais (R$ com vírgula decimal) e, quando ajudar, uma recomendação prática.\n"
            "Você tem acesso a TODOS os dados que a API do Cardápio Web devolve. Use as ferramentas; nunca invente números. "
            "Pode fazer várias consultas, conferir e cruzar dados.\n"
            "Hoje é %s, %s. Turnos: %s. 'grupo_canal' é próprio, iFood ou 99Food. Exclua cancelados (cancelado=1) de vendas.\n"
            "Base SQLite (datas 'AAAA-MM-DD HH:MM'):\n"
            "- pedidos(id, numero, data, dia_semana, hora, turno, status, canal, grupo_canal, tipo [Entrega/Retirada/No local/Mesa], momento "
            "[immediate/scheduled], agendado_para, cancelado, motivo_cancelamento, total, subtotal_itens, taxa_entrega, taxa_servico, taxa_adicional, "
            "desconto, cupom, pagamento, pago_online, troco_para, entregue_por [merchant/ifood/...], entregador_id, taxa_entregador, "
            "tempo_estimado_min, origem_cliente, observacao, observacao_interna, cliente_id, cliente_nome, cliente_telefone, rua, numero_casa, "
            "complemento, referencia, bairro, cidade, promocoes, atualizado_em)\n"
            "- itens(pedido_id, data, turno, grupo_canal, item, tipo_item, quantidade, preco_unitario, valor, opcoes, observacao, status, cancelado)\n"
            "- opcoes(pedido_id, data, turno, item, grupo, opcao, quantidade, preco_unitario)  -- sabores, bordas, acompanhamentos\n"
            "- descontos(pedido_id, data, tipo, categoria, quem_paga [merchant=loja, ifood...], valor, cupom, nome_cupom, pontos, item)\n"
            "- pagamentos(pedido_id, data, forma, tipo [online/offline], valor, status, bandeira, taxa)\n"
            "- clientes(id, nome, primeiro_nome, telefone, email, aniversario, cadastro, pontos, cashback, pedidos, total_gasto, primeiro_pedido, "
            "ultimo_pedido, dias_sem_pedir, canais, bairro, unido_de)\n"
            "- avaliacoes(data, pedido_id, cliente_id, nota, comentario, respostas)\n"
            "- alertas_abertos(tipo, prioridade, cliente_id, titulo, texto, criado)\n"
            "- promocoes_casa(nome, dias, oferta, reconhece_por, so_app_proprio)\n"
            "- cadastro_loja(tipo [loja/formas_pagamento/cupons/cardapio], json)  -- use json_extract/json_each ou a ferramenta dados_da_loja\n"
            "Ferramentas extras: dados_da_loja (cadastro da loja, horários, formas de pagamento, cupons, cardápio com preços) e "
            "consultar_api (consulta ao vivo na API do Cardápio Web, só leitura, para o que não estiver na base ou for de agora).\n"
            "Para nomes de itens/sabores use LIKE com %% (ex.: opcao LIKE '%%calabresa%%'). %s"
            "Resposta em texto curto, com tópicos se precisar; sem tabelas grandes." %
            (DIAS[hoje.weekday()], hoje.strftime("%d/%m/%Y %H:%M"), ", ".join(D["turnos"][:-1]),
             "" if liberar else "Telefone, e-mail, número da casa e complemento aparecem como '(oculto)' (o dono pode liberar no config.ini). "))


FERRAMENTAS = [
    {"name": "consultar_sql",
     "description": "Executa uma consulta SQL (somente SELECT, SQLite) na base completa de vendas da Mariá e devolve até 200 linhas.",
     "parameters": {"type": "object", "properties": {"sql": {"type": "string", "description": "Uma consulta SELECT"}}, "required": ["sql"]}},
    {"name": "dados_da_loja",
     "description": "Devolve o JSON do cadastro da loja (tipo=loja: endereço, horários etc.), das formas de pagamento, dos cupons cadastrados ou do cardápio completo (categorias, itens, preços, complementos).",
     "parameters": {"type": "object", "properties": {"tipo": {"type": "string", "enum": ["loja", "formas_pagamento", "cupons", "cardapio"]},
                                                     "busca": {"type": "string", "description": "Opcional: texto para filtrar (ex.: nome de um item do cardápio)"}},
                    "required": ["tipo"]}},
    {"name": "consultar_api",
     "description": "Consulta AO VIVO a API do Cardápio Web (só leitura). Caminhos permitidos: /orders (pedidos das últimas 8h; parâmetro updated_since), "
                    "/orders/{id}, /orders/history (start_date, end_date, page; até 6 meses), /merchant, /merchant/payment_methods, /merchant/coupons, "
                    "/merchant/customers (page) e /merchant/customers/{id}, /merchant/reviews (start_date, end_date), /catalog e /catalog/items/{id}.",
     "parameters": {"type": "object", "properties": {"caminho": {"type": "string"}, "parametros": {"type": "object"}}, "required": ["caminho"]}},
]

CAMINHOS_API = re.compile(r"^/(orders(/history|/\d+)?|merchant(/payment_methods|/coupons|/reviews|/customers(/\d+)?)?|catalog(/(items|categories|option_groups|options)(/\d+)?)?)$")
TRAVA_API = threading.Lock()


def _dados_loja(tipo, busca):
    con = sqlite3.connect("file:%s?mode=ro" % caminho_analise().replace("\\", "/"), uri=True)
    try:
        r = con.execute("SELECT json FROM cadastro_loja WHERE tipo=?", (tipo,)).fetchone()
    finally:
        con.close()
    if not r:
        return "Não disponível (a chave da API pode não ter acesso a isso, ou ainda não foi baixado)."
    txt = r[0]
    if busca:
        try:
            alvo = busca.lower()
            achados = []

            def andar(o):
                if isinstance(o, dict):
                    if alvo in json.dumps(o, ensure_ascii=False).lower() and any(isinstance(o.get(k), str) and alvo in o.get(k, "").lower() for k in o):
                        achados.append(o)
                        return
                    for v in o.values():
                        andar(v)
                elif isinstance(o, list):
                    for v in o:
                        andar(v)
            andar(json.loads(txt))
            txt = json.dumps(achados[:30], ensure_ascii=False)
        except ValueError:
            pass
    return txt[:20000] + ("\n(cortado)" if len(txt) > 20000 else "")


def _api_ao_vivo(api, caminho, params, liberar):
    caminho = "/" + str(caminho or "").strip().strip("/")
    if not CAMINHOS_API.match(caminho):
        return "Caminho não permitido. Use um dos caminhos da descrição da ferramenta."
    params = {k: v for k, v in (params or {}).items() if isinstance(v, (str, int, float))} if isinstance(params, dict) else {}
    try:
        with TRAVA_API:
            r = api.get(caminho, params or None)
    except Exception as e:
        return "Erro da API: %s" % e
    if r is None:
        return "Não encontrado."
    txt = json.dumps(limpar_pessoais(r, liberar), ensure_ascii=False)
    return txt[:20000] + ("\n(cortado)" if len(txt) > 20000 else "")


def perguntar(ia, D, mensagens, api=None, liberar=False):
    msgs = [{"role": m.get("role"), "content": str(m.get("content") or "")[:4000]} for m in mensagens[-12:]
            if m.get("role") in ("user", "assistant") and m.get("content")]
    if not msgs or msgs[-1]["role"] != "user":
        raise ErroIA("Faça uma pergunta.")

    def executar(nome, args):
        if nome == "consultar_sql":
            return consultar(args.get("sql"))
        if nome == "dados_da_loja":
            return _dados_loja(args.get("tipo"), args.get("busca"))
        if nome == "consultar_api" and api is not None:
            return _api_ao_vivo(api, args.get("caminho"), args.get("parametros"), liberar)
        return "ferramenta indisponível"
    resp, feitas = ia.conversar(sistema_pergunta(D, liberar), msgs, FERRAMENTAS, executar, max_tokens=2000, forte=True, voltas=12)
    return {"resposta": resp, "consultas": len(feitas)}
