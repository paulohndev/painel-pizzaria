# -*- coding: utf-8 -*-
"""
Recomendações de promoção por cliente.

Regras simples e explicáveis (cada sugestão vem com o "porquê"), calculadas a
partir dos pedidos concluídos do canal próprio:

  aniversario   aniversário nos próximos 15 dias
  segundo       fez só 1 pedido recentemente -> puxar o 2º pedido
  reativar      era cliente frequente e sumiu -> oferta no prato/sabor favorito
  lembrete      está "atrasado" em relação ao próprio ritmo -> só lembrar, sem desconto
  outro_turno   só pede almoço (ou só pizza) -> apresentar o outro turno
  complemento   leva sempre A mas nunca B, e quem leva A costuma levar B
  sabor_novo    pede sempre o mesmo sabor -> sabor que clientes parecidos amam
"""
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from statistics import median

DIAS_SEMANA = ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"]
PRIORIDADE = ["aniversario", "segundo", "reativar", "lembrete", "conhecer_noite", "conhecer_almoco", "complemento", "sabor_novo"]


def limpar(nome):
    """'01 Pizza Grande (35cm)!!!' -> 'Pizza Grande'; '1/2 Bacon' -> 'Bacon'."""
    n = re.sub(r"\(.*?\)", "", nome or "")
    n = re.sub(r"^\s*(\d+\s*/\s*\d+|\d+)\s+", "", n)
    n = re.sub(r"[!?.:;|\-\s]+$", "", n)
    n = re.sub(r"^\s*monte\s+(sua|seu)\s+(\w)", lambda m: m.group(2).upper(), n, flags=re.I)
    return re.sub(r"\s+", " ", n).strip()


def eh_pizza(item):
    i = (item or "").lower()
    return "pizza" in i or "sabor" in i


def sabor_do_nome(item):
    """'Pizza Grande de Frango com Catupiry (35cm)' -> 'Frango com Catupiry'"""
    m = re.match(r"\s*pizza\s+(?:\S+\s+){0,2}de\s+(.+)", limpar(item), re.I)
    return limpar(m.group(1)) if m else ""


def classificar_opcao(item, grupo, nome):
    """'sabor' (só em pizza), 'extra' (borda/adicional de pizza) ou None (ex.: guarnição do almoço)."""
    g, n = (grupo or "").lower(), (nome or "").lower()
    if not eh_pizza(item) and "sabor" not in g:
        return None
    if "borda" in g or "borda" in n:
        return "extra"
    if "sabor" in g or not g:
        if any(x in n for x in ("adicional", "extra", "sem ", "molho", "refri", "coca", "tradicional", "massa")):
            return "extra"
        return "sabor"
    return "extra"


def eh_grupo_sabor(grupo, nome=""):
    g = (grupo or "").lower()
    if not g:  # iFood/99 mandam as opções sem nome de grupo: sabor, a não ser que seja borda/adicional
        n = (nome or "").lower()
        return not any(x in n for x in ("borda", "adicional", "extra", "sem ", "com ", "molho", "refri", "coca"))
    return "sabor" in g or ("pizza" in g and "borda" not in g)


def _top(counter, n=1):
    return [k for k, _ in counter.most_common(n)]


def recomendar(compras, clientes, turnos_nomes, cfg, ref=None):
    """
    compras: lista de dicts {cli, dt, t, ifood, total, itens:[nome], sabores:[nome], extras:[nome]}
             (só pedidos concluídos com cliente identificado)
    clientes: lista do painel (índice = cli)
    """
    ref = ref or datetime.now()
    dias_ativo = cfg["dias_ativo"]
    n_turnos = len(turnos_nomes) - 1  # último é "Outro horário"
    idx_almoco = next((i for i, n in enumerate(turnos_nomes) if n.lower().startswith("almo")), None)
    idx_noite = next((i for i, n in enumerate(turnos_nomes) if n.lower().startswith("noit")), None)

    proprio = compras  # todos os canais; quem não tem telefone (iFood) é filtrado na tela

    # -------------------------------------------------- estatísticas gerais
    # turno principal de cada item (para saber o que é "de almoço" e o que é "de pizzaria")
    item_turno = defaultdict(Counter)
    for c in compras:
        for it in set(c["itens"]):
            item_turno[it][c["t"]] += 1

    def lado(item):
        cnt = item_turno.get(item)
        if not cnt:
            return None
        tot = sum(cnt.values())
        for t in (idx_almoco, idx_noite):
            if t is not None and cnt[t] / tot >= 0.7:
                return t
        return "ambos"

    # item principal x acompanhamento: principal = item mais caro do pedido
    vezes_principal, vezes = Counter(), Counter()
    for c in compras:
        vezes.update(set(c["itens"]))
        if c.get("principal"):
            vezes_principal[c["principal"]] += 1

    def eh_principal(item):
        return vezes[item] and vezes_principal[item] / vezes[item] >= 0.5

    # associação dentro do mesmo pedido: itens + extras (borda, adicionais)
    n_ped = len(proprio) or 1
    cont = Counter()
    par = Counter()
    for c in proprio:
        els = set(c["itens"]) | set("+" + e for e in c["extras"])
        for a in els:
            cont[a] += 1
        for a in els:
            for b in els:
                if a != b:
                    par[(a, b)] += 1
    MIN_SUP = max(15, int(n_ped * 0.005))
    regras = defaultdict(list)  # a -> [(b, conf, lift)]
    pares_top = []
    for (a, b), nab in par.items():
        if cont[a] < MIN_SUP or cont[b] < MIN_SUP or nab < 8:
            continue
        conf = nab / cont[a]
        lift = conf / (cont[b] / n_ped)
        if lift >= 1.15 and conf >= 0.12 and conf < 0.98 and not a.startswith("+") \
                and eh_principal(a) and (b.startswith("+") or not eh_principal(b)):
            regras[a].append((b, conf, lift))
            if b.startswith("+") and "borda" not in b.lower():
                continue
            pares_top.append({"a": a.lstrip("+"), "b": b.lstrip("+"), "juntos": nab,
                                  "conf": round(conf, 3), "lift": round(lift, 2)})
    for a in regras:
        regras[a].sort(key=lambda x: -x[1])
    pares_top.sort(key=lambda p: -p["conf"] * min(1, p["juntos"] / 50))

    # por cliente
    por_cli = defaultdict(list)
    for c in proprio:
        por_cli[c["cli"]].append(c)

    # afinidade de sabores entre clientes (quem pede X também pede Y)
    cli_sabores = {k: set(s for c in v for s in c["sabores"]) for k, v in por_cli.items()}
    sab_cli = Counter(s for ss in cli_sabores.values() for s in ss)
    n_cli_sab = sum(1 for ss in cli_sabores.values() if ss) or 1
    sab_par = Counter()
    for ss in cli_sabores.values():
        for a in ss:
            for b in ss:
                if a != b:
                    sab_par[(a, b)] += 1

    def sabor_parecido(fav, provados):
        melhor, score = None, 0
        for g in sab_cli:
            if g in provados or sab_cli[fav] < 10 or sab_cli[g] < 10:
                continue
            conf = sab_par[(fav, g)] / sab_cli[fav]
            lift = conf / (sab_cli[g] / n_cli_sab)
            s = conf * min(lift, 3)
            if s > score:
                melhor, score = (g, conf), s
        return melhor

    # o que clientes "dos dois turnos" pedem no outro turno (para apresentar o turno novo)
    def mais_pedido_no_turno(t, base_clientes):
        it, sab = Counter(), Counter()
        for k in base_clientes:
            for c in por_cli[k]:
                if c["t"] == t:
                    it.update(set(c["itens"]))
                    sab.update(set(c["sabores"]))
        return it, sab

    clientes_turnos = {k: Counter(c["t"] for c in v) for k, v in por_cli.items()}
    dois_turnos = [k for k, tc in clientes_turnos.items()
                   if idx_almoco is not None and idx_noite is not None and tc[idx_almoco] and tc[idx_noite]]
    noite_it, noite_sab = mais_pedido_no_turno(idx_noite, dois_turnos if len(dois_turnos) >= 20 else por_cli.keys())
    almoco_it, almoco_sab = mais_pedido_no_turno(idx_almoco, dois_turnos if len(dois_turnos) >= 20 else por_cli.keys())

    def principal(it_counter, sab_counter, t):
        itens_turno = [i for i, _ in it_counter.most_common() if lado(i) == t and eh_principal(i)]
        if t == idx_noite and sab_counter:
            return _top(sab_counter)[0], (itens_turno[0] if itens_turno else "")
        return (itens_turno[0] if itens_turno else (_top(it_counter) or [""])[0]), ""

    sug_noite = principal(noite_it, noite_sab, idx_noite) if idx_noite is not None else ("", "")
    sug_almoco = principal(almoco_it, almoco_sab, idx_almoco) if idx_almoco is not None else ("", "")

    # -------------------------------------------------- regras por cliente
    saida = []
    perfis = {}
    for k, peds in por_cli.items():
        cad = clientes[k]
        peds.sort(key=lambda c: c["dt"])
        n = len(peds)
        ult = peds[-1]["dt"]
        dias = (ref - ult).days
        gasto = sum(c["total"] or 0 for c in peds)
        ticket = gasto / n if n else 0
        itens = Counter(i for c in peds for i in set(c["itens"]))
        sabores = Counter(s for c in peds for s in set(c["sabores"]))
        extras = Counter(e for c in peds for e in set(c["extras"]))
        tc = clientes_turnos[k]
        turno_fav = tc.most_common(1)[0][0]
        principais = Counter(c.get("principal") for c in peds if c.get("principal"))
        fav_item = _top(principais)[0] if principais else (_top(itens)[0] if itens else "")
        com_sabor = Counter(c.get("principal") for c in peds if c["sabores"])
        fav_sabor = _top(sabores)[0] if sabores else ""
        # favorito: o sabor, se o item favorito é uma pizza (item com sabores); senão o item
        favorito = fav_sabor if (fav_sabor and com_sabor.get(fav_item)) else fav_item

        # ritmo do cliente
        dias_pedido = sorted(set(c["dt"].date() for c in peds))
        intervalos = [(b - a).days for a, b in zip(dias_pedido, dias_pedido[1:])]
        ritmo = median(intervalos) if len(intervalos) >= 2 else None
        dia_sem, n_dia = Counter(c["dt"].weekday() for c in peds).most_common(1)[0]
        horas = sorted(c["dt"].hour + c["dt"].minute / 60 for c in peds if c["t"] == turno_fav)
        hora_tip = horas[len(horas) // 2] if horas else peds[-1]["dt"].hour
        envio_h = int(hora_tip - 1) % 24
        quando = "%s, por volta das %dh" % (DIAS_SEMANA[dia_sem], envio_h)

        recs = []

        def add(tipo, titulo, motivo, sugestao="", extra=None):
            r = {"tipo": tipo, "titulo": titulo, "motivo": motivo, "sugestao": sugestao}
            if extra:
                r.update(extra)
            recs.append(r)

        # aniversário
        nasc = cad.get("nasc")
        if nasc and len(nasc) >= 10:
            try:
                mes, dia = int(nasc[5:7]), int(nasc[8:10])
                for ano in (ref.year, ref.year + 1):
                    try:
                        aniv = datetime(ano, mes, dia)
                    except ValueError:
                        aniv = datetime(ano, mes, 28)
                    falta = (aniv.date() - ref.date()).days
                    if 0 <= falta <= 15:
                        add("aniversario", "Aniversário em %d dia(s)" % falta if falta else "Aniversário hoje",
                            "Faz aniversário em %02d/%02d. Mimo de aniversário fideliza muito." % (dia, mes))
                        break
            except ValueError:
                pass

        # 2º pedido
        if n == 1 and dias <= dias_ativo:
            add("segundo", "Puxar o 2º pedido",
                "Fez o 1º pedido há %d dias (%s). Quem faz o 2º pedido tem muito mais chance de virar cliente fiel." % (dias, favorito or "pedido"))

        # reativar
        elif n >= 2 and dias > max(dias_ativo, (ritmo or 0) * 2) and dias <= 365:
            add("reativar", "Reativar com o favorito",
                "Fez %d pedidos (R$ %.0f no total) e não pede há %d dias. O favorito é %s." % (n, gasto, dias, favorito or "—"))

        # lembrete (atrasado no próprio ritmo, ainda ativo)
        elif ritmo and n >= 3 and dias <= dias_ativo and dias >= max(4, ritmo * 1.4):
            add("lembrete", "Está na hora do pedido",
                "Costuma pedir a cada %d dias e já faz %d. Ainda é cliente ativo: um lembrete costuma bastar, sem desconto." % (round(ritmo), dias))

        # outro turno
        if n >= 3 and idx_almoco is not None and idx_noite is not None:
            if tc[idx_almoco] and not tc[idx_noite] and sug_noite[0]:
                add("conhecer_noite", "Apresentar a pizzaria",
                    "Fez %d pedidos no almoço e nunca pediu à noite. Quem pede nos dois turnos costuma escolher %s." % (tc[idx_almoco], sug_noite[0]),
                    sug_noite[0])
            elif tc[idx_noite] and not tc[idx_almoco] and sug_almoco[0]:
                add("conhecer_almoco", "Apresentar o almoço",
                    "Fez %d pedidos à noite e nunca pediu almoço. Quem pede nos dois turnos costuma escolher %s." % (tc[idx_noite], sug_almoco[0]),
                    sug_almoco[0])

        # complemento (cross-sell)
        if n >= 3:
            ja = set(itens) | set("+" + e for e in extras)
            taxa = {x: itens.get(x, 0) / n for x in itens}
            taxa.update({"+" + e: v / n for e, v in extras.items()})
            melhor = None
            for a, _ in principais.most_common(2):
                for b, conf, lift in regras.get(a, []):
                    if taxa.get(b, 0) > 0.1:
                        continue
                    if b.startswith("+") and "borda" not in b.lower():
                        continue  # só sugere item separado (bebida, sobremesa) ou borda
                    lb = lado(b) if not b.startswith("+") else "ambos"
                    if lb not in ("ambos", lado(a)):
                        continue
                    if melhor is None or conf > melhor[2]:
                        melhor = (a, b, conf)
                    break
            if melhor:
                a, b, conf = melhor
                add("complemento", "Oferecer um complemento",
                    "Pede %s e quase nunca leva %s. Nos pedidos de %s da casa, %d%% vêm com %s." % (
                        a, b.lstrip("+"), a, round(conf * 100), b.lstrip("+")),
                    b.lstrip("+"), {"base": a})

        # sabor novo
        n_sab = sum(1 for c in peds if c["sabores"])
        if n_sab >= 3 and fav_sabor and sabores[fav_sabor] / n_sab >= 0.6:
            sp = sabor_parecido(fav_sabor, set(sabores))
            if sp:
                add("sabor_novo", "Sugerir um sabor novo",
                    "Pede %s em %d%% das pizzas. %d%% de quem gosta de %s também pede %s, que ele nunca provou." % (
                        fav_sabor, round(sabores[fav_sabor] / n_sab * 100), round(sp[1] * 100), fav_sabor, sp[0]),
                    sp[0], {"fav": fav_sabor})

        # perfil curto de todo cliente (usado na Lista do dia): favorito, dia da semana (0=domingo), hora boa p/ mandar, % no dia, ritmo
        perfis[k] = [favorito, (dia_sem + 1) % 7, envio_h, round(n_dia / n, 2), round(ritmo) if ritmo else None, turno_fav]
        if not recs:
            continue
        recs.sort(key=lambda r: PRIORIDADE.index(r["tipo"]))
        saida.append({
            "cli": k, "n": n, "dias": dias, "gasto": round(gasto, 2), "ticket": round(ticket, 2),
            "turno": turno_fav, "favorito": favorito, "fav_item": fav_item, "quando": quando,
            "ritmo": round(ritmo) if ritmo else None, "recs": recs,
        })

    return {"clientes": saida, "pares": pares_top[:25], "perfis": perfis}
