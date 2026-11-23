"""Funções de IA do painel: mensagem personalizada, resposta a avaliação, resumo do dia e perguntas sobre os dados.

Para a IA vão só: primeiro nome, histórico de pedidos (itens, datas, valores, canal) e avaliações.
Telefone, endereço e e-mail nunca saem do computador.
"""
import json
import os
import re
import sqlite3

import gestao
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
    validos = [x for x in peds if not x.get("cancelado")]
    if validos:
        linhas.append("Últimos pedidos (do mais recente):")
        for x in validos[:6]:
            desc = "; ".join("%s%s" % (it.get("n"), (" (" + ", ".join(it.get("o")[:4]) + ")") if it.get("o") else "") for it in x.get("itens") or [])
            quando = str(x.get("data") or x.get("quando") or "")[:16]
            try:
                dq = datetime.strptime(quando[:16], "%Y-%m-%d %H:%M")
                quando = "%s (%s %s)" % (dq.strftime("%d/%m"), DIAS[dq.weekday()], dq.strftime("%H:%M"))
            except ValueError:
                pass
            linhas.append("  - %s: %s%s" % (quando, desc[:300], (" · canal " + str(x.get("canal"))) if x.get("canal") else ""))
    nasc = str(c.get("nasc") or "")[:10]
    if re.match(r"\d{4}-\d{2}-\d{2}", nasc):
        try:
            hoje = datetime.now().date()
            an = datetime.strptime(nasc, "%Y-%m-%d").date().replace(year=hoje.year)
            dd = (an - hoje).days
            if -3 <= dd <= 10:
                linhas.append("Aniversário: %s (%s)." % (an.strftime("%d/%m"), "hoje" if dd == 0 else ("daqui a %d dias" % dd if dd > 0 else "foi há %d dias" % -dd)))
        except ValueError:
            pass
    for a in ((perfil or {}).get("avaliacoes") or [])[:3]:
        linhas.append("Avaliação em %s: nota %s%s%s" % (a["data"][:10], a["nota"], (". Notas baixas: " + a["baixas"]) if a.get("baixas") else "",
                                                         ('. Comentário: "%s"' % a["comentario"]) if a.get("comentario") else ""))
    return "\n".join(linhas)


# ------------------------------------------------------------------ mensagens

SIS_MSG = ("Você escreve mensagens de WhatsApp em nome do dono da " + CASA +
           "\nAntes de escrever, leia com atenção os dados do cliente e escolha 1 ou 2 detalhes REAIS e específicos dele para usar "
           "(o prato/sabor que ele sempre pede, o dia ou horário de costume, quanto tempo faz que não aparece, uma avaliação, o aniversário, "
           "a promoção do dia que combina com o que ele gosta). A mensagem tem que soar escrita para aquela pessoa, não um modelo.\n"
           "Regras:\n"
           "- Se o DONO deu uma instrução ou escreveu um rascunho, ele manda: siga exatamente o que ele pediu (assunto, valores, cupons, datas, "
           "tom, tamanho). Use os dados do cliente só para deixar mais pessoal, sem mudar o que ele disse.\n"
           "- Português do Brasil, tom próximo e natural, como o dono falaria com um cliente conhecido. Nada de linguagem de marketing, "
           "nada de 'venha conferir', 'não perca', 'imperdível'.\n"
           "- Varie: não comece sempre do mesmo jeito (evite abrir toda mensagem com 'Oi, Fulano!'), não repita frases das mensagens que "
           "ele já recebeu (listadas nos dados), e não use a mesma estrutura da mensagem de referência se puder dizer melhor.\n"
           "- Curta: de 1 a 3 frases, no máximo 2 emojis (zero se o dono pedir). Pense no horário: de tarde, 'hoje à noite'; tarde da noite, 'amanhã'.\n"
           "- Chame o cliente pelo primeiro nome.\n"
           "- A mensagem sai como se fosse do dono/da pizzaria: nunca assine nem se apresente como Zoe, assistente, robô ou IA.\n"
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


RASCUNHO = ("\n\nO DONO ESCREVEU ISTO (rascunho, ideia ou instrução): \"%s\"\n"
            "Agora a base é o texto do dono, não a mensagem de referência. Se for um rascunho, reescreva mantendo a intenção, o tom e "
            "TODAS as informações e ofertas que ele colocou, deixando mais natural e personalizado para o cliente. Se for uma instrução "
            "(ex.: 'fala da terça off', 'mais curta', 'sem emoji'), siga a instrução. Não acrescente ofertas, preços ou cupons que o dono "
            "não citou (a não ser que a instrução peça para usar a oferta da referência).")


INSTRUCAO = ("\n\nINSTRUÇÃO DO DONO (siga à risca, é o mais importante): \"%s\"\n"
             "Escreva a mensagem que ele pediu, com as condições exatamente como ele disse, personalizada com os dados do cliente.")


def _hist_msgs(historico):
    linhas = []
    for m in (historico or [])[:6]:
        if m.get("status") not in ("enviado", "na_fila", None) and m.get("via") != "manual":
            continue
        t = (m.get("texto") or "").replace("\n", " ").strip()
        linhas.append("  - %s%s: %s" % (m.get("quando", "")[:10], (" (" + m["campanha"] + ")") if m.get("campanha") else "",
                                        (t[:220] or "(mensagem pelo WhatsApp, texto não registrado)")))
    return ("\nMensagens que ele já recebeu (não repita):\n" + "\n".join(linhas)) if linhas else ""


def gerar_mensagem(ia, D, cli, perfil, motivo, referencia, tipo="", rascunho="", instrucao="", historico=None, notas=None):
    ctx = contexto_cliente(D, cli, perfil)
    hoje = datetime.now()
    dia_js = (hoje.weekday() + 1) % 7  # as promoções guardam o dia como no navegador (domingo = 0)
    promos = ["%s: %s" % (x.get("nome"), x.get("oferta") or "") for x in (D.get("promos_casa") or [])
              if isinstance(x, dict) and x.get("ativa", True) is not False and (not x.get("dias") or dia_js in (x.get("dias") or []))]
    pedido = ("Hoje é %s, %s.\nMotivo do contato: %s\nMensagem de referência (mesma oferta): %s\n%s\nDados do cliente:\n%s%s%s" %
              (DIAS[hoje.weekday()], hoje.strftime("%d/%m %H:%M"), motivo or "-", referencia or "-",
               ("Promoções da casa valendo hoje: " + "; ".join(promos)[:600] + "\n") if promos else "",
               ctx, _hist_msgs(historico), ("\nAnotações do dono sobre este cliente: " + " / ".join(notas)[:600]) if notas else ""))
    if (instrucao or "").strip():
        pedido += INSTRUCAO % instrucao.strip()[:1500]
    if (rascunho or "").strip():
        pedido += RASCUNHO % rascunho.strip()[:1500]
    if tipo == "avaliacao":
        txt = ia.gerar(SIS_AVAL, pedido, max_tokens=600)
        m = re.search(r"\{.*\}", txt, re.S)
        try:
            j = json.loads(m.group(0)) if m else {}
        except ValueError:
            j = {}
        return {"texto": (j.get("whatsapp") or txt).strip(), "resposta_publica": (j.get("resposta_publica") or "").strip()}
    forte = bool((instrucao or "").strip() or (rascunho or "").strip())
    return {"texto": ia.gerar(SIS_MSG, pedido, max_tokens=500, forte=forte).strip().strip('"')}


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


SIS_RESUMO = ("Você é a Zoe, a assistente da " + CASA + "\nEscreva para o dono um resumo do dia, em português do Brasil, direto e útil:\n"
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


def montar_analise(db, D, versao, liberar=False, extras=None):
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
        for nome, (esquema, linhas) in (extras or {}).items():
            try:
                out.execute("CREATE TABLE %s (%s)" % (nome, esquema))
                if linhas:
                    out.executemany("INSERT INTO %s VALUES (%s)" % (nome, ",".join("?" * len(linhas[0]))), linhas)
            except sqlite3.Error as e:
                print("tabela extra %s: %s" % (nome, e))
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
    return ("Você é a Zoe, a assistente de inteligência da " + CASA + " (o dono te chama pelo nome; se ele perguntar quem você é, "
            "diga que é a Zoe, assistente da Mariá, e o que você sabe fazer). Responda às perguntas do dono em português do Brasil, "
            "de forma clara, direta e simpática, "
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


GRUPOS_SCHEMA = {"type": "array", "description": "Clientes separados em grupos, um por motivo/assunto. Use SEMPRE que os clientes tiverem motivos diferentes.",
                 "items": {"type": "object", "properties": {
                     "titulo": {"type": "string", "description": "Título claro do grupo, dizendo o critério (ex.: 'Fãs de camarão sumidos há 30+ dias')"},
                     "ids": {"type": "array", "items": {"type": "integer"}},
                     "notas": {"type": "object", "description": "Opcional: {id: 'detalhe curto deste cliente'}"}},
                     "required": ["titulo", "ids"]}}

FERRAMENTAS_ACAO = [
    {"name": "mostrar_clientes",
     "description": "Mostra ao dono, abaixo da sua resposta, os clientes de que você está falando (clicáveis, com perfil, WhatsApp, lembrete e botão "
                    "'abrir como página'). Use sempre que a resposta envolver clientes específicos. Separe em grupos quando houver motivos "
                    "diferentes. ids = clientes.id da base.",
     "parameters": {"type": "object", "properties": {"titulo": {"type": "string", "description": "Título geral"},
                                                     "grupos": GRUPOS_SCHEMA,
                                                     "ids": {"type": "array", "items": {"type": "integer"}, "description": "Só se for um grupo único"},
                                                     "notas": {"type": "object"}},
                    "required": ["titulo"]}},
    {"name": "salvar_lista",
     "description": "Salva clientes como uma LISTA (página própria do painel, em 'Listas'), para o dono trabalhar com calma depois. "
                    "Use quando ele pedir para salvar, guardar, transformar em página/lista, ou quando o levantamento for grande.",
     "parameters": {"type": "object", "properties": {"titulo": {"type": "string"}, "descricao": {"type": "string", "description": "Critério usado e o que fazer"},
                                                     "grupos": GRUPOS_SCHEMA},
                    "required": ["titulo", "grupos"]}},
    {"name": "criar_lembrete",
     "description": "Cria lembretes (📌) para o dono agir depois. Use SÓ quando o dono pedir para marcar/lembrar/anotar. "
                    "Cada grupo vira um bloco separado na página de Lembretes, com o título que você der.",
     "parameters": {"type": "object", "properties": {"grupos": {"type": "array", "items": {"type": "object", "properties": {
                         "titulo": {"type": "string", "description": "Título do bloco (o critério/motivo)"},
                         "ids": {"type": "array", "items": {"type": "integer"}},
                         "texto": {"type": "string", "description": "O que fazer com esses clientes"},
                         "notas": {"type": "object", "description": "Opcional: {id: 'o que fazer com este cliente em particular'}"}},
                         "required": ["titulo", "ids", "texto"]}},
                                                     "quando": {"type": "string", "description": "Data AAAA-MM-DD (opcional)"}},
                    "required": ["grupos"]}},
    {"name": "escrever_mensagem",
     "description": "Escreve uma mensagem de WhatsApp personalizada para UM cliente, seguindo o que o dono pediu, usando o histórico completo "
                    "dele. O dono vê a mensagem com botão para abrir no WhatsApp ou enviar pelo sistema. Para vários clientes "
                    "(até 8), chame uma vez para cada; acima disso use preparar_envio.",
     "parameters": {"type": "object", "properties": {"id": {"type": "integer"},
                                                     "instrucao": {"type": "string", "description": "O que o dono quer dizer, com as palavras e condições dele"}},
                    "required": ["id", "instrucao"]}},
    {"name": "preparar_envio",
     "description": "Prepara um envio de WhatsApp para muitos clientes na tela Envios. NÃO envia: o dono revisa e confirma. Cada mensagem é "
                    "personalizada pela IA na hora do envio a partir do texto modelo. Variáveis: {nome}, {favorito}, {pontos}, {bairro}.",
     "parameters": {"type": "object", "properties": {"ids": {"type": "array", "items": {"type": "integer"}}, "texto": {"type": "string"},
                                                     "titulo": {"type": "string"}}, "required": ["ids", "texto"]}},
    {"name": "gerar_relatorio",
     "description": "Entrega um relatório para o dono abrir, imprimir ou salvar. Use quando ele pedir relatório/resumo/levantamento. "
                    "conteudo em Markdown simples (títulos com #, tópicos com -, tabelas com |).",
     "parameters": {"type": "object", "properties": {"titulo": {"type": "string"}, "conteudo": {"type": "string"}}, "required": ["titulo", "conteudo"]}},
    {"name": "abrir_perfil",
     "description": "Abre o perfil completo de um cliente na tela do dono.",
     "parameters": {"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]}},
    {"name": "criar_reserva",
     "description": "Anota uma reserva de mesa que o dono ditou. Obrigatórios: nome, data e hora. Converta 'hoje', 'amanhã', 'sexta' etc. "
                    "para a data real (AAAA-MM-DD) a partir da data de hoje. Se o cliente já existir na base, passe cliente_id.",
     "parameters": {"type": "object", "properties": {
         "nome": {"type": "string"}, "telefone": {"type": "string"},
         "data": {"type": "string", "description": "AAAA-MM-DD"}, "hora": {"type": "string", "description": "HH:MM"},
         "pessoas": {"type": "integer"}, "rodizio": {"type": "boolean", "description": "true = rodízio; false = à la carte; omita se não foi dito"},
         "valor_pago": {"type": "number", "description": "Quanto já deixou pago (sinal/antecipado), em reais"},
         "forma_pagamento": {"type": "string", "description": "pix, dinheiro, cartão…"},
         "observacao": {"type": "string", "description": "Aniversário, cadeirinha, mesa, restrição, quem indicou etc."},
         "cliente_id": {"type": "integer"}},
         "required": ["nome", "data", "hora"]}},
    {"name": "editar_reserva",
     "description": "Altera ou cancela uma reserva existente (use listar_reservas antes para achar o id). Passe só o que muda. "
                    "status: confirmada, chegou, nao_veio ou cancelada.",
     "parameters": {"type": "object", "properties": {
         "id": {"type": "integer"}, "nome": {"type": "string"}, "telefone": {"type": "string"}, "data": {"type": "string"},
         "hora": {"type": "string"}, "pessoas": {"type": "integer"}, "rodizio": {"type": "boolean"}, "valor_pago": {"type": "number"},
         "forma_pagamento": {"type": "string"}, "observacao": {"type": "string"}, "status": {"type": "string"}},
         "required": ["id"]}},
    {"name": "listar_reservas",
     "description": "Lista as reservas de um período (padrão: de hoje em diante). Use para responder sobre reservas ou achar o id de uma.",
     "parameters": {"type": "object", "properties": {"de": {"type": "string", "description": "AAAA-MM-DD"}, "ate": {"type": "string"}}}},
    {"name": "abrir_pagina",
     "description": "Leva o dono para uma página do sistema. Vendas: hoje, promocoes, envios, resultados, geral, clientes, produtos, bairros, "
                    "listas, lembretes, reservas. Gestão: g_painel, g_despesas, g_fichas, g_equipe.",
     "parameters": {"type": "object", "properties": {"pagina": {"type": "string"}}, "required": ["pagina"]}},
]

GUIA_ACOES = ("\nVocê também AGE no sistema com as ferramentas mostrar_clientes, salvar_lista, criar_lembrete, escrever_mensagem, preparar_envio, "
              "gerar_relatorio, abrir_perfil e abrir_pagina.\n"
              "- MENSAGENS JÁ MANDADAS: antes de sugerir mandar mensagem para alguém, consulte a tabela mensagens_clientes (ultima_mensagem, situacao, pode_mandar_a_partir). NÃO sugira quem está com situacao aguardar, pausado, na fila ou não quer receber; se o dono pedir alguém assim, avise a data da última mensagem e a partir de quando pode mandar. Ao listar clientes para contato, diga quando cada um recebeu a última mensagem (se recebeu). "
              "- Quando citar clientes específicos, chame mostrar_clientes. Se houver motivos diferentes, SEPARE EM GRUPOS com títulos que digam "
              "o critério (nunca misture tudo num grupo só). Dê uma nota curta por cliente quando ajudar.\n"
              "- Se o dono pedir para salvar/transformar em página/lista, use salvar_lista com os mesmos grupos.\n"
              "- Se pedir para marcar/lembrar, use criar_lembrete com um grupo por motivo (o título do grupo é o que ele vai ver na página de Lembretes).\n"
              "- Se pedir uma mensagem para alguém, use escrever_mensagem passando a instrução dele com as palavras e condições exatas que ele "
              "usou. Antes, pesquise o cliente (pedidos, itens, horários, avaliações, mensagens já recebidas) para a mensagem ser específica.\n"
              "- Para mandar a um grupo grande, use preparar_envio (ele revisa antes).\n"
              "- Pesquise de verdade antes de sugerir: confira os pedidos e itens dos clientes, não suponha.\n"
              "- Preste atenção no que o dono disse: valores, cupons, datas, tom e restrições dele valem mais que qualquer sugestão sua.\n"
              "- Se pedir relatório, use gerar_relatorio e na resposta só diga o que tem nele. Nunca diga que fez uma ação sem chamar a ferramenta.\n"
              "- Use o histórico da conversa: 'esses clientes' são os da última lista que você mostrou (os ids aparecem no histórico).\n"
              "- RESERVAS: quando o dono ditar uma reserva, use criar_reserva. Nome, data e hora são obrigatórios: se faltar algum, pergunte "
              "antes de criar. Anote tudo que ele disser: telefone, número de pessoas, se é rodízio ou não, quanto já deixou pago e a forma, "
              "e qualquer observação. Não invente o que não foi dito. Se o nome ou telefone bater com um cliente da base, passe o cliente_id. "
              "Depois confirme em uma linha curta (dia da semana, data, hora, nome, pessoas, rodízio, pago). Para mudar ou cancelar, "
              "use listar_reservas para achar a reserva e depois editar_reserva.")


def perguntar(ia, D, mensagens, api=None, liberar=False, tela="", fazer_lembrete=None, extra="", salvar_lista=None, escrever=None,
              reservas=None):
    msgs = [{"role": m.get("role"), "content": str(m.get("content") or "")[:6000]} for m in mensagens[-14:]
            if m.get("role") in ("user", "assistant") and m.get("content")]
    if not msgs or msgs[-1]["role"] != "user":
        raise ErroIA("Faça uma pergunta.")

    acoes = []
    n = len(D["clientes"])
    origem = {}

    def ids_ok(v):
        out = []
        for x in (v or [])[:500]:
            try:
                i = int(x)
            except (TypeError, ValueError):
                continue
            if 0 <= i < n:
                u = D["clientes"][i].get("uniu_em")
                j = u if u is not None else i
                if j not in out:
                    out.append(j)
                    origem[j] = i
        return out

    def notas_de(ids, notas):
        notas = {str(k): str(v)[:200] for k, v in (notas or {}).items()} if isinstance(notas, dict) else {}
        return {str(i): notas.get(str(origem.get(i, i))) or notas.get(str(i)) for i in ids if notas.get(str(origem.get(i, i))) or notas.get(str(i))}

    def grupos_de(args, campo_texto=False):
        gs = []
        brutos = args.get("grupos") if isinstance(args.get("grupos"), list) else []
        if not brutos and args.get("ids"):
            brutos = [{"titulo": args.get("titulo") or "Clientes", "ids": args.get("ids"), "notas": args.get("notas"), "texto": args.get("texto")}]
        for g in brutos[:20]:
            if not isinstance(g, dict):
                continue
            ids = ids_ok(g.get("ids"))
            if not ids:
                continue
            x = {"titulo": str(g.get("titulo") or "Clientes")[:150], "ids": ids, "notas": notas_de(ids, g.get("notas"))}
            if campo_texto:
                x["texto"] = str(g.get("texto") or "")[:500]
            gs.append(x)
        return gs

    def itens_lista(gs):
        out = []
        for g in gs:
            for i in g["ids"]:
                c = D["clientes"][i]
                out.append({"chave": c.get("chave"), "cliente_id": c.get("id"), "nome": c.get("nome"), "grupo": g["titulo"],
                            "nota": g["notas"].get(str(i), "")})
        return out

    def executar(nome, args):
        if nome == "mostrar_clientes":
            gs = grupos_de(args)
            acoes.append({"tipo": "clientes", "titulo": str(args.get("titulo") or (gs[0]["titulo"] if gs else "Clientes"))[:150], "grupos": gs,
                          "ids": [i for g in gs for i in g["ids"]]})
            return "Mostrei %d cliente(s) em %d grupo(s)." % (sum(len(g["ids"]) for g in gs), len(gs))
        if nome == "salvar_lista":
            gs = grupos_de(args)
            if not gs or salvar_lista is None:
                return "Nenhum cliente válido."
            lid = salvar_lista(str(args.get("titulo") or "Lista da IA")[:150], str(args.get("descricao") or "")[:2000], itens_lista(gs))
            acoes.append({"tipo": "lista", "id": lid, "titulo": str(args.get("titulo") or "Lista da IA")[:150],
                          "n": sum(len(g["ids"]) for g in gs), "grupos": len(gs)})
            return "Lista salva (id %s) com %d cliente(s)." % (lid, sum(len(g["ids"]) for g in gs))
        if nome == "criar_lembrete":
            gs = grupos_de(args, campo_texto=True)
            if not gs or fazer_lembrete is None:
                return "Nenhum cliente válido."
            q = str(args.get("quando") or "")[:10]
            total = 0
            for g in gs:
                total += fazer_lembrete(g["ids"], g["texto"] or g["titulo"], q, g["titulo"], g["notas"])
            acoes.append({"tipo": "lembretes", "grupos": [{"titulo": g["titulo"], "texto": g["texto"], "ids": g["ids"]} for g in gs],
                          "ids": [i for g in gs for i in g["ids"]], "quando": q})
            return "%d lembrete(s) criado(s) em %d grupo(s)." % (total, len(gs))
        if nome == "escrever_mensagem":
            ids = ids_ok([args.get("id")])
            if not ids or escrever is None:
                return "Cliente não encontrado."
            if sum(1 for a in acoes if a["tipo"] == "mensagem") >= 8:
                return "Limite de 8 mensagens por resposta; para mais clientes use preparar_envio."
            try:
                txt = escrever(ids[0], str(args.get("instrucao") or "")[:1500])
            except ErroIA as e:
                return "Não consegui escrever: %s" % e
            acoes.append({"tipo": "mensagem", "id": ids[0], "texto": txt, "instrucao": str(args.get("instrucao") or "")[:300]})
            return "Mensagem escrita e mostrada ao dono: " + txt
        if nome == "preparar_envio":
            ids = ids_ok(args.get("ids"))
            acoes.append({"tipo": "envio", "ids": ids, "texto": str(args.get("texto") or "")[:1500], "titulo": str(args.get("titulo") or "Envio sugerido pela IA")[:120]})
            return "Envio preparado para %d cliente(s). O dono vai revisar na tela Envios antes de mandar." % len(ids)
        if nome == "gerar_relatorio":
            acoes.append({"tipo": "relatorio", "titulo": str(args.get("titulo") or "Relatório")[:150], "conteudo": str(args.get("conteudo") or "")[:60000]})
            return "Relatório entregue ao dono."
        if nome == "abrir_perfil":
            ids = ids_ok([args.get("id")])
            if ids:
                acoes.append({"tipo": "perfil", "id": ids[0]})
            return "Perfil aberto." if ids else "Cliente não encontrado."
        if nome in ("criar_reserva", "editar_reserva", "listar_reservas"):
            if reservas is None:
                return "Reservas indisponíveis."
            args = dict(args or {})
            if args.get("cliente_id") is not None:
                ids = ids_ok([args.get("cliente_id")])
                args["cliente_id"] = ids[0] if ids else None
            try:
                r = reservas(nome, args)
            except ValueError as e:
                return "Não consegui: %s. Confira com o dono." % e
            if nome == "listar_reservas":
                if not r:
                    return "Nenhuma reserva no período."
                return "\n".join("id %d | %s %s | %s | %s pessoas | %s | pago %s %s | %s | status %s" % (
                    x["id"], x["data"], x["hora"], x["nome"], x["pessoas"] or "?",
                    {1: "rodízio", 0: "sem rodízio"}.get(x["rodizio"], "rodízio não informado"),
                    ("R$ %.2f" % x["valor_pago"]) if x["valor_pago"] else "nada", x["forma_pagamento"] or "",
                    x["observacao"] or "-", x["status"]) for x in r[:200])
            acoes.append({"tipo": "reserva", "acao": "criada" if nome == "criar_reserva" else "alterada", "reserva": r})
            return "Reserva %s (id %d): %s" % ("anotada" if nome == "criar_reserva" else "atualizada", r["id"], json.dumps(r, ensure_ascii=False))
        if nome == "abrir_pagina":
            pg = str(args.get("pagina") or "").strip().lower()
            if pg not in PAGINAS and pg not in PAGINAS_GESTAO:
                return "Página desconhecida."
            acoes.append({"tipo": "pagina", "pagina": pg})
            return "Página aberta."
        if nome == "consultar_sql":
            return consultar(args.get("sql"))
        if nome == "dados_da_loja":
            return _dados_loja(args.get("tipo"), args.get("busca"))
        if nome == "consultar_api" and api is not None:
            return _api_ao_vivo(api, args.get("caminho"), args.get("parametros"), liberar)
        return "ferramenta indisponível"
    sis = sistema_pergunta(D, liberar) + gestao.DESCRICAO_IA + GUIA_ACOES + (("\n" + extra) if extra else "")
    if tela:
        sis += "\nO dono está olhando agora: " + tela[:3500]
    resp, feitas = ia.conversar(sis, msgs, FERRAMENTAS + FERRAMENTAS_ACAO, executar, max_tokens=3500, forte=True, voltas=18)
    return {"resposta": resp, "consultas": len(feitas), "acoes": acoes}


# ------------------------------------------------------------------ dica de cada página

PAGINAS = {
    "hoje": "a lista do dia (quem chamar hoje: aniversários, boas-vindas, dia de costume, sumidos, migrados para apps, promoção do dia)",
    "promocoes": "as campanhas sugeridas por cliente e as promoções da casa",
    "envios": "os envios de WhatsApp (fila, quem pode receber, frequência)",
    "resultados": "os resultados das campanhas, promoções da casa e cupons",
    "geral": "a visão geral de vendas (faturamento, pedidos, canais, turnos, dias da semana)",
    "clientes": "a lista de clientes e grupos (fiéis, sumindo, novos, esfriando, perdidos)",
    "produtos": "os produtos e sabores mais vendidos e combinações",
    "bairros": "as vendas por bairro",
    "listas": "as listas de clientes salvas (muitas montadas pela IA)",
    "lembretes": "os lembretes de clientes para agir depois",
    "reservas": "as reservas de mesa (próximos dias, pessoas, rodízio, valores já pagos)",
}

PAGINAS_GESTAO = {
    "g_painel": "o painel geral do Gestão (resultado do mês, faturamento x despesas, equipe, cardápio e preços, fechamento)",
    "g_despesas": "as despesas da pizzaria (lançamentos, categorias, faturamento diário, orçamento, desperdício)",
    "g_fichas": "as fichas técnicas (custo de cada produto, insumos, preços praticados e margens)",
    "g_equipe": "a equipe e a folha de pagamento",
}

SIS_DICA = ("Você é a Zoe, a assistente da " + CASA + " O dono acabou de abrir uma página do painel. Consulte os dados com as ferramentas e escreva "
            "2 ou 3 tópicos curtíssimos (comece cada um com '• ', máximo ~25 palavras cada) com o que é mais útil ele saber AGORA sobre esse "
            "assunto: algo que mudou, uma oportunidade concreta ou um alerta, com números. Se citar clientes, chame mostrar_clientes. "
            "Nada de introdução. Nunca invente números.")


def dica_pagina(ia, D, aba, tela="", liberar=False, api=None):
    alvo = PAGINAS.get(aba) or PAGINAS_GESTAO.get(aba)
    if not alvo:
        raise ErroIA("Página sem dica.")
    r = perguntar(ia, D, [{"role": "user", "content": "Estou abrindo a página de %s. O que eu preciso saber agora?" % alvo}],
                  api=api, liberar=liberar, tela=tela, extra=SIS_DICA)
    return r


# ---------- holerite do contador: a Zoe lê o PDF/foto e devolve os dados em JSON ----------
SIS_HOLERITE = """Você lê holerites (recibos de pagamento de salário) brasileiros enviados pelo contador de uma pizzaria.
Extraia SOMENTE o que está escrito no documento. Não invente nada. Se algo não aparece, use null.
Pode haver um holerite por página, vários funcionários no mesmo arquivo, ou o mesmo holerite em duas vias: não repita funcionário.
Responda APENAS com um JSON, sem texto antes ou depois, neste formato:
{"competencia":"AAAA-MM","empresa":"...","funcionarios":[{"nome":"...","cargo":"...","admissao":"AAAA-MM-DD ou null",
 "salario_contratual":0.0,"itens":[{"codigo":"...","descricao":"...","referencia":"texto da coluna referência, ex. 30,00 ou 25:00 ou 7,50%","valor":0.0,
 "natureza":"provento|desconto|informativo",
 "tipo":"salario|hora_extra|adicional_noturno|dsr|feriado|ferias|decimo_terceiro|salario_familia|outro_provento|inss|irrf|vale_transporte|adiantamento|falta|atraso|outro_desconto|fgts|base"}],
 "total_proventos":0.0,"total_descontos":0.0,"liquido":0.0,"fgts_mes":0.0}]}
Regras: "salario" é a linha de salário/dias normais/horas normais. Adiantamento quinzenal/salarial descontado é "adiantamento".
Valores com ponto decimal (1234.56), sem R$. Bases de cálculo (base INSS, base FGTS, base IRRF) e FGTS são "informativo"."""


def ler_holerite(ia, arquivos):
    if getattr(ia, "provedor", "") != "claude":
        raise ErroIA("Para ler holerite a Zoe precisa estar usando o Claude (config.ini, seção [ia]).")
    blocos = []
    for a in (arquivos or [])[:6]:
        tipo = str(a.get("tipo") or "").lower()
        dados = str(a.get("dados") or "")
        if not dados:
            continue
        if tipo == "application/pdf":
            blocos.append({"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": dados}})
        elif tipo in ("image/jpeg", "image/png", "image/webp", "image/gif"):
            blocos.append({"type": "image", "source": {"type": "base64", "media_type": tipo, "data": dados}})
    if not blocos:
        raise ErroIA("Envie o holerite em PDF ou foto (JPG/PNG).")
    blocos.append({"type": "text", "text": "Leia os holerites acima e responda só com o JSON pedido."})
    txt, _ = ia.conversar(SIS_HOLERITE, [{"role": "user", "content": blocos}], max_tokens=8000, forte=True, voltas=0)
    t = txt.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t
        t = t.rsplit("```", 1)[0]
    i, j = t.find("{"), t.rfind("}")
    try:
        dado = json.loads(t[i:j + 1])
    except ValueError:
        raise ErroIA("Não consegui entender a leitura do holerite. Tente de novo ou mande uma foto mais nítida.")
    fs = dado.get("funcionarios") or []
    if not fs:
        raise ErroIA("Não encontrei nenhum holerite nesse arquivo.")
    return {"competencia": dado.get("competencia"), "empresa": dado.get("empresa"), "funcionarios": fs}


ESQ_MSG = ("cliente_id INTEGER, nome TEXT, ultima_mensagem TEXT, mensagens_30d INTEGER, situacao TEXT, "
           "pode_mandar_a_partir TEXT, motivo TEXT, perfil TEXT")


def tabela_mensagens(D):
    """Situação de mensagens de cada cliente (para a Zoe não sugerir quem acabou de receber)."""
    nomes = {"livre": "livre", "aguardar": "aguardar", "pausado": "pausado", "bloqueado": "não quer receber",
             "na_fila": "na fila", "so_app": "só pede por app"}
    linhas = []
    for i, e in (D.get("envio") or {}).items():
        i = int(i)
        if not e or (not e[1] and e[2] == "livre"):
            continue
        c = D["clientes"][i] if 0 <= i < len(D["clientes"]) else {}
        linhas.append((i, c.get("nome") or "", e[1] or None, e[5] or 0, nomes.get(e[2], e[2]),
                       (e[3] or "")[:16] or None, e[4] or "", e[0] or ""))
    return (ESQ_MSG, linhas)
