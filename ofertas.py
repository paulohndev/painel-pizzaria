"""Ideias de oferta: o que oferecer a cada cliente e que promoções novas a casa pode criar.

Tudo aqui é regra local (não gasta crédito da IA). A Zoe usa estes fatos quando o dono pede ideias
com mais criatividade (ia_painel.ideias_zoe)."""

import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from statistics import median

DIAS = ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"]   # weekday() do Python
DIAS_JS = ["domingo", "segunda", "terça", "quarta", "quinta", "sexta", "sábado"]

RE_BEBIDA = re.compile(r"coca|guaran|fanta|antar|refri|suco|água|agua|sprite|kuat|cerveja|pepsi|soda|\bks\b|lata|\b[12],?5?\s?l\b|\b1l\b|2l\b", re.I)
RE_SOBREMESA = re.compile(r"pudim|mousse|brownie|sorvete|palha|sobremesa|doce de|bolo|petit|pa[cç]oca", re.I)
RE_BORDA = re.compile(r"borda", re.I)
RE_EXTRA = re.compile(r"aplica[cç][aã]o|adicional|extra|quentinha|caixa|talher|guardanapo|sach|embalagem|observa", re.I)
RE_PIZZA = re.compile(r"pizza|clone|rod[ií]zio", re.I)


def _r(v):
    s = "%.2f" % v
    return "R$ " + s.replace(".", ",")


def _r0(v):
    return "R$ %.0f" % v


def _preco_99(v):
    """Arredonda para baixo terminando em ,99 (ex.: 63,40 -> 62,99)."""
    import math
    return max(0.99, math.floor(v) - 0.01) if v >= 1 else v


def no_turno(t):
    t = str(t or "").lower()
    return "à noite" if t.startswith("noit") else ("no " + t if t else "")


def _limpo(s):
    return re.sub(r"\s+", " ", str(s or "")).strip()


def _sabor_de_opcao(o):
    o = _limpo(o)
    if not o or RE_BEBIDA.search(o) or RE_BORDA.search(o) or RE_EXTRA.search(o) or RE_SOBREMESA.search(o):
        return None
    if re.search(r"\bsem\b|molho|ponto|bem passad|corte|fatia", o, re.I):
        return None
    return o


def catalogo(D):
    """{nome: {"p": preço, "cat": categoria}} dos itens ativos, mais a lista de combos."""
    itens, combos = {}, []
    for x in D.get("catalogo") or []:
        itens[x["n"].lower()] = x
        if re.search(r"combo", x["n"] + " " + x.get("cat", ""), re.I) and x.get("p"):
            n_piz = 0
            m = re.search(r"(\d+)\s*pizzas", x["n"], re.I)
            if m:
                n_piz = int(m.group(1))
            else:
                n_piz = len(re.findall(r"pizza", x["n"], re.I))
            combos.append({"n": x["n"], "p": x.get("pp") or x["p"], "pizzas": n_piz, "bebida": bool(RE_BEBIDA.search(x["n"])),
                           "doce": bool(re.search(r"doce", x["n"], re.I))})
    return itens, combos


def _precos_loja(D):
    """Preço médio por item vendido (todas as vendas) e mais vendidos por tipo."""
    q, v = Counter(), Counter()
    for t, c, nome, qt, val in D.get("itens") or []:
        n = D["nomes_it"][nome] if isinstance(nome, int) and nome < len(D.get("nomes_it") or []) else nome
        q[n] += qt or 0
        v[n] += val or 0
    preco = {n: v[n] / q[n] for n in q if q[n]}
    return preco, q


def fatos_cliente(D, cli, perfil=None, agora=None):
    agora = agora or datetime.now()
    rows = [p for p in D["pedidos"] if p[5] == cli and not p[7]]
    if not rows:
        return None
    canais = D["canais"]
    datas = sorted(datetime.strptime(p[0], "%Y-%m-%d %H:%M") for p in rows)
    tot = [p[4] for p in rows if p[4]]
    dd = sorted({d.date() for d in datas})
    iv = [(b - a).days for a, b in zip(dd, dd[1:])]
    dw = Counter(d.weekday() for d in datas)
    dia_fav, n_dia = dw.most_common(1)[0]
    horas = sorted(d.hour for d in datas)
    turnos = Counter(D["turnos"][p[1]] for p in rows)
    f = {
        "n": len(rows), "gasto": sum(tot), "ticket": (sum(tot) / len(tot)) if tot else 0,
        "dias": (agora - datas[-1]).days, "ritmo": round(median(iv)) if len(iv) >= 2 else None,
        "dia_fav": dia_fav if (n_dia / len(rows) >= 0.3 and len(rows) >= 3) else None, "dia_share": n_dia / len(rows),
        "hora": horas[len(horas) // 2], "turno": turnos.most_common(1)[0][0],
        "turnos": dict(turnos), "proprio": sum(1 for p in rows if not canais[p[2]].get("ifood")),
        "com_desconto": sum(1 for p in rows if (p[9] or 0) > 0) / len(rows),
        "desde": datas[0].strftime("%m/%Y"),
        "promos_usadas": Counter(),
    }
    promos = D.get("promos_casa") or []
    for p in rows:
        if len(p) > 13 and p[13]:
            for k in range(len(promos)):
                if p[13] >> k & 1:
                    f["promos_usadas"][promos[k].get("nome")] += 1
    # itens (do perfil, com preço pago)
    peds = [x for x in ((perfil or {}).get("pedidos") or []) if not x.get("cancelado")]
    itens, sabores, pago = Counter(), Counter(), defaultdict(list)
    beb = sob = bor = 0
    pizzas_ped = []
    tamanhos = Counter()
    for x in peds:
        tem_b = tem_s = tem_bo = False
        npz = 0
        for it in x.get("itens") or []:
            n = _limpo(it.get("n"))
            qt = it.get("q") or 1
            if RE_EXTRA.search(n) and not RE_PIZZA.search(n):
                continue
            if RE_BEBIDA.search(n):
                tem_b = True
            elif RE_SOBREMESA.search(n):
                tem_s = True
            else:
                itens[n] += qt
                if it.get("v"):
                    pago[n].append(float(it["v"]) / qt)
            if RE_PIZZA.search(n):
                npz += qt * (2 if re.search(r"2 pizzas|casal|clone", n, re.I) else 3 if re.search(r"03 pizzas|3 pizzas|galera", n, re.I) else 1)
                m = re.search(r"grande|m[eé]dia|pequena|broto|fam[ií]lia", n, re.I)
                if m:
                    tamanhos[m.group(0).lower().replace("é", "e")] += qt
            for o in it.get("o") or []:
                if RE_BEBIDA.search(o):
                    tem_b = True
                elif RE_BORDA.search(o):
                    tem_bo = True
                elif RE_SOBREMESA.search(o):
                    tem_s = True
                elif RE_PIZZA.search(n):
                    s = _sabor_de_opcao(o)
                    if s:
                        sabores[s] += 1
        beb += tem_b
        sob += tem_s
        bor += tem_bo
        if npz:
            pizzas_ped.append(npz)
    np_ = len(peds) or 1
    f.update({
        "itens": itens.most_common(6), "sabores": sabores.most_common(6),
        "preco_pago": {k: sum(v) / len(v) for k, v in pago.items() if v},
        "bebida": beb / np_ if peds else None, "sobremesa": sob / np_ if peds else None, "borda": bor / np_ if peds else None,
        "pizzas_por_pedido": (sum(pizzas_ped) / len(pizzas_ped)) if pizzas_ped else 0,
        "pedidos_pizza": len(pizzas_ped) / np_ if peds else 0,
        "tamanho": tamanhos.most_common(1)[0][0] if tamanhos else "",
        "cupons": Counter(x.get("cupom") for x in peds if x.get("cupom")).most_common(4),
        "tem_itens": bool(peds),
    })
    return f


def _mais_vendido(D, regex, excluir=None):
    preco, q = _precos_loja(D)
    cand = [(n, qt) for n, qt in q.items() if regex.search(n) and not (excluir and excluir.search(n)) and preco.get(n, 0) > 0]
    cand.sort(key=lambda x: -x[1])
    return (cand[0][0], preco[cand[0][0]]) if cand else (None, 0)


def _preco_cat(D, nome):
    itens, _ = catalogo(D)
    x = itens.get((nome or "").lower())
    if x and (x.get("pp") or x.get("p")):
        return x.get("pp") or x["p"]
    preco, _q = _precos_loja(D)
    return preco.get(nome) or 0


def ideias_cliente(D, cli, perfil=None, agora=None, sugerir_cupom=None):
    """Lista de ideias de oferta para um cliente, da mais indicada para a menos."""
    agora = agora or datetime.now()
    f = fatos_cliente(D, cli, perfil, agora)
    if not f:
        return {"ideias": [], "resumo": "Sem pedidos deste cliente."}
    c = D["clientes"][cli]
    da = (D.get("config") or {}).get("dias_ativo") or 45
    ritmo = f["ritmo"] or 14
    atraso = f["dias"] / max(ritmo, 3)
    if f["n"] == 1:
        estado = "novo" if f["dias"] <= da else "sumido"
    elif f["dias"] > max(da, ritmo * 2):
        estado = "sumido"
    elif atraso >= 1.3 or f["dias"] > da:
        estado = "atrasado"
    else:
        estado = "no_ritmo"
    vip = f["n"] >= 10 or f["gasto"] >= 1000
    fav_item = f["itens"][0][0] if f["itens"] else ""
    fav_sabor = f["sabores"][0][0] if f["sabores"] else ""
    fav_txt = ("pizza de " + fav_sabor.lower()) if (fav_sabor and RE_PIZZA.search(fav_item)) else (fav_item or "o de sempre")
    ideias = []

    def add(tipo, ic, titulo, oferta, porque, como, custo=None, instr="", peso=0):
        ideias.append({"tipo": tipo, "ic": ic, "titulo": titulo, "oferta": oferta, "porque": porque, "como": como,
                       "custo": None if custo is None else round(custo, 2), "instr": instr, "peso": peso})

    quando = ""
    if f["dia_fav"] is not None:
        quando = "%s, por volta das %dh" % (DIAS[f["dia_fav"]], max(f["hora"] - 1, 10))
    else:
        quando = "%s, por volta das %dh" % (no_turno(f["turno"]), max(f["hora"] - 1, 10))

    # 1) combo do catálogo que combina com o jeito dele pedir
    _it, combos = catalogo(D)
    if combos and f["pedidos_pizza"] >= 0.5 and f["ticket"]:
        quer = 2 if f["pizzas_por_pedido"] >= 1.6 else 1
        cands = [x for x in combos if (x["pizzas"] or 1) >= quer and (x["pizzas"] or 1) <= quer + 1 and not x["doce"]]
        cands.sort(key=lambda x: (abs((x["pizzas"] or 1) - quer), abs(x["p"] - f["ticket"])))
        if cands and cands[0]["p"] <= f["ticket"] * 1.15:
            cb = cands[0]
            extra = " e ainda leva refrigerante" if cb["bebida"] and (f["bebida"] or 0) < 0.5 else ""
            add("combo_existente", "🍕", "Combo que já existe: " + cb["n"].split("|")[0].strip(),
                "%s por %s" % (cb["n"], _r(cb["p"])),
                "Gasta em média %s por pedido e pede %s pizza%s por vez. Este combo custa %s%s." % (
                    _r0(f["ticket"]), "1" if quer == 1 else "2 ou mais", "" if quer == 1 else "s", _r(cb["p"]), extra),
                "Sem custo extra: o combo já está no cardápio. É só lembrar o cliente dele.", 0,
                "fale do %s (%s), que combina com o jeito dele pedir" % (cb["n"], _r(cb["p"])), peso=3 if estado == "no_ritmo" else 1)

    # 2) combo personalizado: o favorito + o que ele nunca leva
    if fav_item and f["tem_itens"] and f["n"] >= 2:
        comp = None
        if (f["bebida"] or 0) < 0.3:
            nb, pb = _mais_vendido(D, RE_BEBIDA, re.compile(r"lata|ks|500", re.I)) if f["pizzas_por_pedido"] >= 1 else _mais_vendido(D, RE_BEBIDA)
            if nb:
                comp = ("bebida", nb, pb, "quase nunca leva bebida (%d%% dos pedidos)" % round((f["bebida"] or 0) * 100))
        if not comp and (f["sobremesa"] or 0) < 0.15:
            ns, ps = _mais_vendido(D, RE_SOBREMESA)
            if ns:
                comp = ("sobremesa", ns, ps, "nunca ou quase nunca pede sobremesa")
        if not comp and RE_PIZZA.search(fav_item) and (f["borda"] or 0) < 0.2:
            comp = ("borda", "borda recheada", 15.0, "quase nunca pede borda recheada")
        if comp:
            base = f["preco_pago"].get(fav_item) or _preco_cat(D, fav_item) or f["ticket"]
            cheio = base + comp[2]
            preco = _preco_99(cheio * 0.9)
            desc = cheio - preco
            nome_fav = fav_item + ((" (" + fav_sabor.strip() + ")") if fav_sabor and RE_PIZZA.search(fav_item) else "")
            add("combo_novo", "✨", "Combo personalizado: favorito + %s" % comp[0],
                "%s + %s por %s (separado sai %s)" % (nome_fav, comp[1], _r(preco), _r(cheio)),
                "O que mais pede é %s e %s. Juntar no combo aumenta o pedido e o desconto fica em cima do item que ele ainda não compra." % (
                    nome_fav, comp[3]),
                "Crie no Cardápio Web um cupom de %s (ex.: COMBO%d, 1 uso) ou um item de combo. Custo: ~%s por pedido." % (
                    _r0(round(desc)), round(desc), _r(desc)),
                desc, "ofereça um combo só pra ele: %s + %s por %s (separado sai %s)" % (nome_fav, comp[1], _r(preco), _r(cheio)),
                peso=2 if estado in ("atrasado", "no_ritmo") else 1)

    # 3) cupom: o que já existe ou um cupom novo
    if sugerir_cupom and estado != "no_ritmo":
        s = sugerir_cupom(D, cli, agora)
        cp = s.get("cupom") if not s.get("nenhum") else None
        pct = 0.20 if f["dias"] > 120 else (0.13 if estado == "sumido" else 0.08)
        if vip:
            pct += 0.03
        alvo = max(5, min(round(f["ticket"] * pct / 5) * 5, int(f["ticket"] * 0.2 / 5) * 5)) if f["ticket"] else 5
        if cp and cp["estado"] == "hoje":
            add("cupom", "🎟️", "Cupom que já existe: " + cp["cod"], "%s (%s)" % (cp["desc"], cp["regra"]), s.get("motivo", ""),
                "Já está cadastrado e valendo. Só mandar.", cp.get("val"),
                "ofereça o cupom %s (%s; %s)" % (cp["cod"], cp["desc"], cp["regra"]), peso=3 if estado == "sumido" else 2)
        else:
            cod = ("VOLTA%d" if estado == "sumido" else "SAUDADE%d" if estado == "atrasado" else "BEMVINDO%d") % alvo
            dia = " Que valha %s, o dia que ele mais pede." % DIAS[f["dia_fav"]] if f["dia_fav"] is not None else ""
            add("cupom_novo", "🆕", "Criar cupom: " + cod,
                "R$ %d de desconto, 1 uso por cliente, válido por 7 dias" % alvo,
                "Ticket médio %s; %s. Um desconto de uns %d%% do ticket costuma bastar.%s" % (
                    _r0(f["ticket"]), {"sumido": "sumiu há %d dias" % f["dias"], "atrasado": "está atrasado (último há %d dias, costuma a cada ~%d)" % (f["dias"], ritmo),
                                       "novo": "fez só o 1º pedido"}.get(estado, ""), round(alvo / f["ticket"] * 100) if f["ticket"] else 0,
                    (" Os cupons que existem não servem agora (%s)." % (cp["cod"] + " " + cp["estado"].replace("_", " ")) if cp else "")),
                "Crie no Cardápio Web: código %s, R$ %d, 1 uso por cliente, validade de 7 dias.%s" % (cod, alvo, dia), alvo,
                "ofereça o cupom %s (R$ %d de desconto no próximo pedido pelo app, válido por 7 dias)" % (cod, alvo),
                peso=3 if estado == "sumido" else 2)

    # 4) promoção da casa que combina com o dia ou com o que ele pede
    hoje_js = (agora.weekday() + 1) % 7
    dia_js = ((f["dia_fav"] + 1) % 7) if f["dia_fav"] is not None else None
    melhor = None
    for pr in D.get("promos_casa") or []:
        if not isinstance(pr, dict) or pr.get("ativa", True) is False or not pr.get("oferta"):
            continue
        if RE_PIZZA.search((pr.get("nome") or "") + " " + pr["oferta"]) and f["pedidos_pizza"] < 0.3 and f["tem_itens"]:
            continue  # promoção de pizza para quem não pede pizza
        pts, why = 0, []
        if pr.get("dias") and dia_js in pr["dias"]:
            pts += 2
            why.append("ele costuma pedir %s, o dia desta promoção" % DIAS_JS[dia_js])
        sab = [s.lower() for s in pr.get("sabores") or []]
        if sab and fav_sabor and any(s in fav_sabor.lower() for s in sab):
            pts += 2
            why.append("o sabor favorito dele (%s) está na promoção" % fav_sabor.strip().lower())
        if any(t.lower() in fav_item.lower() for t in pr.get("itens") or [] if t):
            pts += 1
            why.append("já pede este tipo de item")
        if f["promos_usadas"].get(pr.get("nome")):
            pts += 1
            why.append("já usou %dx" % f["promos_usadas"][pr["nome"]])
        if pts and (melhor is None or pts > melhor[0]):
            melhor = (pts, pr, why)
    if melhor and melhor[0] >= 2:
        pr = melhor[1]
        dias_txt = ", ".join(DIAS_JS[d] for d in pr.get("dias") or []) or "todo dia"
        vale = (not pr.get("dias")) or hoje_js in pr["dias"]
        add("promo", "📣", "Promoção da casa: " + pr["nome"], pr["oferta"] + " (" + dias_txt + ")",
            "; ".join(melhor[2]).capitalize() + ".",
            "Já está no cardápio. " + ("Vale hoje." if vale else "Mande no dia (%s)." % dias_txt), 0,
            "fale da promoção %s (%s), que vale %s" % (pr["nome"], pr["oferta"], "hoje" if vale else dias_txt),
            peso=3 if vale else 1)

    # 5) novidade sem desconto: sabor novo / outro turno (do motor de recomendações)
    rec = next((r for r in ((D.get("promo") or {}).get("clientes") or []) if r.get("cli") == cli), None)
    for r in (rec or {}).get("recs") or []:
        if r["tipo"] == "sabor_novo":
            add("novidade", "🍕", "Sabor novo para provar: " + r["sugestao"], "sugerir a pizza de %s" % r["sugestao"].lower(), r["motivo"],
                "Sem custo. Se quiser empurrar, junte com o cupom.", 0, "sugira provar a pizza de %s" % r["sugestao"].lower(), peso=1)
        elif r["tipo"] in ("conhecer_noite", "conhecer_almoco"):
            outro = "a pizzaria (à noite)" if r["tipo"] == "conhecer_noite" else "o almoço"
            sug = r["sugestao"] if r["sugestao"].lower() not in ("almoço", "almoco", "pizza") else ""
            add("novidade", "🌙" if r["tipo"] == "conhecer_noite" else "🍽️", "Apresentar " + outro,
                "conhecer %s%s" % (outro, (", começando por " + sug) if sug else ""),
                r["motivo"], "Um desconto pequeno só no primeiro pedido do outro turno costuma bastar.", 0,
                "convide para conhecer %s%s" % (outro, (", sugerindo " + sug) if sug else ""), peso=2)

    # 6) cliente fiel no ritmo: mimo em vez de desconto
    if vip and estado == "no_ritmo":
        ns, ps = _mais_vendido(D, RE_SOBREMESA)
        add("mimo", "🎁", "Mimo para cliente fiel", "%s de presente no próximo pedido" % ((ns or "uma sobremesa").lower()),
            "Já são %d pedidos (%s no total) e pede sozinho, a cada ~%d dias. Desconto aqui seria dinheiro perdido; um agrado fideliza." % (
                f["n"], _r0(f["gasto"]), ritmo),
            "Avise a cozinha para mandar junto (ou crie um cupom de item no Cardápio Web). Custo: ~%s." % _r(ps or 8), ps or 8,
            "agradeça a fidelidade e diga que no próximo pedido vai %s de presente" % ((ns or "uma sobremesa").lower()), peso=2)

    if estado == "no_ritmo" and not any(i["tipo"] in ("promo", "combo_existente", "novidade", "mimo") for i in ideias):
        add("lembrete", "🔔", "Só um lembrete", "lembrar do %s, sem desconto" % fav_txt,
            "Pede a cada ~%d dias e o último foi há %d. Está no ritmo: não precisa dar desconto." % (ritmo, f["dias"]),
            "Sem custo.", 0, "mande só um lembrete carinhoso do %s, sem oferecer desconto" % fav_txt, peso=1)

    ideias.sort(key=lambda i: (-i["peso"], i["custo"] or 0))
    st = {"novo": "fez só o 1º pedido", "sumido": "sumido (último pedido há %d dias)" % f["dias"],
          "atrasado": "atrasado (último há %d dias; costuma a cada ~%d)" % (f["dias"], ritmo),
          "no_ritmo": "no ritmo (último há %d dias; costuma a cada ~%d)" % (f["dias"], ritmo)}[estado]
    resumo = "%d pedido%s, ticket médio %s, %s. Melhor momento para mandar: %s." % (
        f["n"], "" if f["n"] == 1 else "s", _r0(f["ticket"]), st, quando)
    for i in ideias:
        i.pop("peso", None)
    return {"ideias": ideias[:5], "resumo": resumo, "estado": estado, "quando": quando,
            "fatos": _fatos_txt(f, fav_txt, estado, quando, c)}


def _fatos_txt(f, fav_txt, estado, quando, c):
    l = ["Pedidos: %d desde %s (%s no total, ticket médio %s)." % (f["n"], f["desde"], _r0(f["gasto"]), _r0(f["ticket"])),
         "Situação: %s; último pedido há %d dias%s." % (estado.replace("_", " "), f["dias"], ", costuma pedir a cada ~%d dias" % f["ritmo"] if f["ritmo"] else ""),
         "Melhor momento: %s. Turnos: %s." % (quando, ", ".join("%s %d" % kv for kv in f["turnos"].items())),
         "Pede pelo app próprio em %d de %d pedidos; %d%% dos pedidos tiveram desconto." % (f["proprio"], f["n"], round(f["com_desconto"] * 100))]
    if f["itens"]:
        l.append("Itens que mais pede: " + ", ".join("%s (%dx)" % kv for kv in f["itens"]))
    if f["sabores"]:
        l.append("Sabores: " + ", ".join("%s (%dx)" % kv for kv in f["sabores"]))
    if f["tem_itens"]:
        l.append("Leva bebida em %d%% dos pedidos, sobremesa em %d%%, borda em %d%%. Pizzas por pedido: %.1f%s." % (
            round((f["bebida"] or 0) * 100), round((f["sobremesa"] or 0) * 100), round((f["borda"] or 0) * 100),
            f["pizzas_por_pedido"], (", tamanho " + f["tamanho"]) if f["tamanho"] else ""))
    if f["cupons"]:
        l.append("Cupons que já usou: " + ", ".join("%s (%dx)" % kv for kv in f["cupons"]))
    if f["promos_usadas"]:
        l.append("Promoções da casa que já usou: " + ", ".join("%s (%dx)" % kv for kv in f["promos_usadas"].most_common()))
    if c.get("cashback"):
        l.append("Saldo de cashback: %s." % _r(float(c["cashback"])))
    return "\n".join(l)


# ------------------------------------------------------------------ ideias para a casa

def ideias_loja(D, agora=None):
    """Fatos para criar promoções novas: dias fracos, pares que vendem juntos e não são combo, grupos de clientes parados,
    uso dos cupons. Cada ideia já vem com uma proposta."""
    agora = agora or datetime.now()
    rows = [p for p in D["pedidos"] if not p[7]]
    ini = (agora - timedelta(days=56)).strftime("%Y-%m-%d")
    rec = [p for p in rows if p[0] >= ini]
    ideias = []
    turnos = D["turnos"]
    # dias fracos, por turno
    for ti, tn in enumerate(turnos[:-1]):
        por = defaultdict(float)
        sem = defaultdict(set)
        for p in rec:
            if p[1] != ti:
                continue
            d = datetime.strptime(p[0][:10], "%Y-%m-%d")
            por[d.weekday()] += p[4] or 0
            sem[d.weekday()].add(p[0][:10])
        med = {k: por[k] / max(len(sem[k]), 1) for k in por if len(sem[k]) >= 3}
        if len(med) < 4:
            continue
        media = sum(med.values()) / len(med)
        fraco = min(med, key=med.get)
        if med[fraco] < media * 0.8:
            dia_js = (fraco + 1) % 7
            tem = [p["nome"] for p in D.get("promos_casa") or [] if isinstance(p, dict) and dia_js in (p.get("dias") or [])]
            ideias.append({"tipo": "dia_fraco", "ic": "📉", "titulo": "%s fraca %s" % (DIAS[fraco].capitalize(), no_turno(tn)),
                           "fato": "Nas últimas 8 semanas, %s %s vendeu em média %s por dia, %d%% abaixo da média dos outros dias (%s)." % (
                               DIAS[fraco], no_turno(tn), _r0(med[fraco]), round((1 - med[fraco] / media) * 100), _r0(media)),
                           "proposta": ("Já tem %s nesse dia: vale reforçar a divulgação ou mudar a oferta." % ", ".join(tem)) if tem else
                           "Criar uma promoção fixa para %s %s (ex.: um sabor da semana com preço especial ou bebida grátis acima de um valor)." % (DIAS[fraco], no_turno(tn))})
    # pares que saem juntos e não viraram combo
    _it, combos = catalogo(D)
    nomes_combo = " ".join(c["n"].lower() for c in combos)
    for pr in ((D.get("promo") or {}).get("pares") or [])[:12]:
        a, b = pr["a"], pr["b"]
        if b.lower() in nomes_combo and a.lower() in nomes_combo:
            continue
        if RE_BORDA.search(b):
            continue
        pa, pb = _preco_cat(D, a), _preco_cat(D, b)
        cheio = (pa or 0) + (pb or 0)
        prop = "Combo %s + %s%s." % (a, b, (" por %s (separado %s)" % (_r(_preco_99(cheio * 0.9)), _r(cheio))) if pa and pb else "")
        ideias.append({"tipo": "combo", "ic": "🍕", "titulo": "Combo novo: %s + %s" % (a, b),
                       "fato": "%d pedidos levaram os dois juntos; %d%% de quem pede %s leva %s (%.1fx mais que o normal)." % (
                           pr["juntos"], round(pr["conf"] * 100), a, b, pr["lift"]),
                       "proposta": prop})
        if sum(1 for i in ideias if i["tipo"] == "combo") >= 3:
            break
    # favoritos de quem sumiu
    da = (D.get("config") or {}).get("dias_ativo") or 45
    perf = (D.get("promo") or {}).get("perfis") or {}
    ult = {}
    for p in rows:
        if p[5] is not None and p[5] >= 0:
            ult[p[5]] = max(ult.get(p[5], ""), p[0])
    lim_a = (agora - timedelta(days=da)).strftime("%Y-%m-%d")
    lim_b = (agora - timedelta(days=180)).strftime("%Y-%m-%d")
    sum_fav = Counter()
    for k, pf in perf.items():
        k = int(k)
        u = ult.get(k, "")
        if lim_b <= u < lim_a and pf and pf[0] and D["clientes"][k].get("tel") and not D["clientes"][k].get("ifood"):
            sum_fav[pf[0]] += 1
    for fav, n in sum_fav.most_common(2):
        if n >= 15:
            ideias.append({"tipo": "segmento", "ic": "👋", "titulo": "%d clientes que amam %s sumiram" % (n, fav.lower()),
                           "fato": "%d clientes com telefone, que têm %s como favorito, não pedem há mais de %d dias (e menos de 6 meses)." % (n, fav.lower(), da),
                           "proposta": "Campanha \"saudade de %s\": cupom de volta (R$ 8 a R$ 10, 1 uso, 7 dias) só para esse grupo." % fav.lower()})
    # cupons: uso e validade
    nomes = D.get("cupons") or []
    uso = Counter(nomes[p[10]] for p in rec if len(p) > 10 and p[10] is not None and 0 <= p[10] < len(nomes))
    hoje = agora.strftime("%Y-%m-%d")
    venc = [c["cod"] for c in D.get("cupons_loja") or [] if c.get("ativo", True) and c.get("ate") and c["ate"] < hoje]
    if venc:
        ideias.append({"tipo": "cupom", "ic": "🎟️", "titulo": "Cupons vencidos", "fato": "Vencidos no Cardápio Web: %s." % ", ".join(venc),
                       "proposta": "Renove a validade dos que funcionam ou apague os que não usa, para a Zoe não sugerir cupom que não vale."})
    if uso:
        ideias.append({"tipo": "cupom", "ic": "📊", "titulo": "Cupons mais usados (8 semanas)",
                       "fato": ", ".join("%s: %d pedidos" % kv for kv in uso.most_common(5)) + ".",
                       "proposta": "O que mais sai é o formato que o cliente entende: repita esse formato nas promoções novas."})
    return {"ideias": ideias}


def ideias_loja_txt(D, agora=None):
    return "\n".join("- %s: %s Proposta: %s" % (i["titulo"], i["fato"], i["proposta"]) for i in ideias_loja(D, agora)["ideias"])
