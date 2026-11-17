# -*- coding: utf-8 -*-
"""
Alertas em tempo real: cruza os pedidos de agora com a rotina de cada cliente
e aponta o que merece atenção, por ordem de urgência.

Prioridade 1 (urgente, vira notificação do Windows):
  costume      hoje é o dia e quase a hora em que o cliente costuma pedir, e ele ainda não pediu
  combo        o cliente acabou de pedir e ficou sem o complemento que quase sempre leva
  avaliacao    avaliação com nota 3 ou menos nas últimas 48 horas
Prioridade 2 (importante; as 3 mais valiosas do dia viram notificação):
  afastando    cliente fiel que já passou bem do próprio ritmo de pedidos
  aniversario  aniversário hoje
Prioridade 3 (só aparece no painel):
  voltou       cliente que estava sumido acabou de pedir
  primeiro     primeiro pedido de um cliente novo nas últimas horas

Cada alerta tem um id estável: o mesmo acontecimento nunca gera duas notificações.
"""
from collections import Counter
from datetime import datetime, timedelta
from statistics import median

DIAS_JS = ["domingo", "segunda", "terça", "quarta", "quinta", "sexta", "sábado"]
STATUS_ABERTO = {"waiting_confirmation", "pending_payment", "pending_online_payment", "scheduled_confirmed", "confirmed"}


def _pd(s):
    return datetime.strptime(s[:16], "%Y-%m-%d %H:%M")


def _primeiro_nome(nome):
    p = (nome or "").strip().split()
    return p[0].capitalize() if p else ""


def gerar(D, contatos=None, agora=None, limites=None):
    """
    D: dados do painel (montar_dados). contatos: {id_cliente: datetime do último contato}.
    Retorna lista de alertas ordenada por prioridade e valor.
    """
    agora = agora or datetime.now()
    contatos = contatos or {}
    lim = {"afastando_max": 15, "dias_ativo": D.get("config", {}).get("dias_ativo", 45)}
    lim.update(limites or {})
    clientes = D["clientes"]
    perfis = (D.get("promo") or {}).get("perfis") or {}
    recs = {r["cli"]: r for r in (D.get("promo") or {}).get("clientes") or []}

    # histórico por cliente (pedidos não cancelados)
    hist = {}
    for p in D["pedidos"]:
        cli, cancel = p[5], p[7] == 1
        if cli < 0 or cancel:
            continue
        hist.setdefault(cli, []).append((_pd(p[0]), p[4] or 0, p[11] if len(p) > 11 else None))
    for v in hist.values():
        v.sort(key=lambda x: x[0])

    def contatado(cli, dias):
        c = clientes[cli]
        quando = contatos.get(str(c.get("id")))
        return quando is not None and (agora - quando).days < dias

    def tem_whats(cli):
        return len(clientes[cli].get("tel") or "") >= 10

    out = []

    def add(tipo, prio, cli, chave_evento, titulo, texto, **extra):
        c = clientes[cli] if cli is not None and cli >= 0 else {}
        a = {"id": "%s:%s" % (tipo, chave_evento), "tipo": tipo, "prio": prio, "cli": cli,
             "cliente": {k: c.get(k) for k in ("id", "nome", "tel", "ddi", "pontos", "app")} if c else None,
             "nome": c.get("nome") or "", "primeiro": _primeiro_nome(c.get("nome")), "titulo": titulo, "texto": texto,
             "valor": 0, "criado": agora.strftime("%Y-%m-%d %H:%M")}
        a.update(extra)
        out.append(a)

    hoje_js = (agora.weekday() + 1) % 7
    hora_agora = agora.hour + agora.minute / 60

    for cli, ps in hist.items():
        if not tem_whats(cli):
            continue
        n = len(ps)
        ult = ps[-1][0]
        dias = (agora - ult).days
        gasto = sum(x[1] for x in ps)
        pf = perfis.get(cli) or perfis.get(str(cli)) or []
        fav = pf[0] if pf else ""
        ritmo = pf[4] if len(pf) > 4 else None
        if ritmo is None and n >= 3:
            dd = sorted(set(x[0].date() for x in ps))
            iv = [(b - a).days for a, b in zip(dd, dd[1:])]
            ritmo = median(iv) if len(iv) >= 2 else None

        # --- hora do costume (urgente e com hora marcada)
        if n >= 4 and len(pf) > 3 and pf[1] == hoje_js and pf[3] >= 0.4 and 5 <= dias <= lim["dias_ativo"]:
            hora_envio = pf[2]  # já é 1h antes do horário de costume
            if hora_envio - 0.5 <= hora_agora <= hora_envio + 1.5 and not contatado(cli, 6):
                add("costume", 1, cli, "%s:%s" % (clientes[cli]["chave"], agora.date()),
                    "Hora certa de chamar %s" % _primeiro_nome(clientes[cli]["nome"]),
                    "%d%% dos pedidos são n%s %s, por volta das %dh. Hoje ainda não pediu (último pedido há %d dias)." % (
                        round(pf[3] * 100), "o" if hoje_js in (0, 6) else "a", DIAS_JS[hoje_js], hora_envio + 1, dias),
                    campanha="lembrete", favorito=fav, valor=gasto)

        # --- se afastando (fiel que passou do ritmo)
        if n >= 4 and ritmo and dias >= max(ritmo * 1.6, ritmo + 4, 8) and dias <= 120 and not contatado(cli, 20):
            add("afastando", 2, cli, "%s:%s" % (clientes[cli]["chave"], ult.date()),
                "%s está se afastando" % _primeiro_nome(clientes[cli]["nome"]),
                "Pedia a cada %d dias e já faz %d. São %d pedidos e R$ %.0f gastos na casa." % (round(ritmo), dias, n, gasto),
                campanha="reativar", favorito=fav, valor=gasto)

        # --- voltou depois de sumir / primeiro pedido (nas últimas 6 horas)
        if 0 <= (agora - ult).total_seconds() < 6 * 3600:
            if n == 1:
                add("primeiro", 3, cli, "%s" % (ps[-1][2] or clientes[cli]["chave"]),
                    "Primeiro pedido de %s" % _primeiro_nome(clientes[cli]["nome"]),
                    "Cliente novo acabou de pedir (R$ %.0f). Uma mensagem de boas-vindas ajuda a garantir o 2º pedido." % ps[-1][1],
                    campanha="segundo", favorito=fav, valor=ps[-1][1])
            elif n >= 2:
                gap = (ps[-1][0] - ps[-2][0]).days
                if gap >= max(lim["dias_ativo"], (ritmo or 0) * 2.5):
                    add("voltou", 3, cli, "%s" % (ps[-1][2] or clientes[cli]["chave"]),
                        "%s voltou depois de %d dias" % (_primeiro_nome(clientes[cli]["nome"]), gap),
                        "Estava sumido e acabou de pedir. Vale agradecer e garantir que a experiência foi boa.",
                        campanha=None, mensagem="Oi %s! Que alegria ver seu pedido de novo por aqui 😊 Qualquer coisa, é só chamar!" % _primeiro_nome(clientes[cli]["nome"]),
                        favorito=fav, valor=gasto)

        # --- aniversário hoje
        nasc = clientes[cli].get("nasc")
        if nasc and len(nasc) >= 10 and nasc[5:10] == agora.strftime("%m-%d") and not contatado(cli, 2):
            add("aniversario", 2, cli, "%s:%s" % (clientes[cli]["chave"], agora.year),
                "Aniversário de %s hoje 🎉" % _primeiro_nome(clientes[cli]["nome"]),
                "Faz aniversário hoje. %d pedidos na casa." % n, campanha="aniversario", favorito=fav, valor=gasto)

    # --- combo ao vivo: pedido em andamento sem o complemento de costume
    for ab in D.get("abertos") or []:
        cli = ab["cli"]
        if ab.get("status") not in STATUS_ABERTO or cli < 0 or not tem_whats(cli):
            continue
        r = recs.get(cli)
        comp = next((x for x in (r or {}).get("recs", []) if x["tipo"] == "complemento"), None)
        if not comp:
            continue
        base, sug = comp.get("base", ""), comp.get("sugestao", "")
        tem_base = any(base.lower() in i.lower() for i in ab["itens"])
        tem_sug = any(sug.lower() in i.lower() for i in ab["itens"] + ab.get("extras", []))
        if tem_base and not tem_sug and (agora - _pd(ab["dt"])).total_seconds() < 50 * 60:
            nome = _primeiro_nome(clientes[cli]["nome"])
            add("combo", 1, cli, ab["id"], "%s acabou de pedir sem %s" % (nome, sug),
                "Pedido em preparo com %s e sem %s, que ele costuma levar. Dá tempo de oferecer antes de sair para entrega." % (base, sug),
                campanha=None, favorito=base, sugestao=sug, pedido=ab["id"],
                mensagem="Oi %s! Seu pedido já está sendo preparado 😊 Quer que eu inclua %s junto? Sai na mesma entrega!" % (nome, sug),
                valor=r.get("gasto", 0) if r else 0)

    # --- avaliações ruins recentes
    for av in D.get("avaliacoes") or []:
        quando, cli, nota, coment, baixas, oid = av
        try:
            dt = _pd(quando)
        except ValueError:
            continue
        if nota is None or nota > 3 or (agora - dt).total_seconds() > 48 * 3600:
            continue
        if cli is not None and cli >= 0 and contatado(cli, 3):
            continue
        nome = _primeiro_nome(clientes[cli]["nome"]) if cli is not None and cli >= 0 else "Cliente"
        add("avaliacao", 1, cli if cli is not None else -1, oid, "Avaliação %s de %s" % (str(nota).replace(".", ","), nome),
            "%s%s" % (("Notas baixas: %s. " % baixas) if baixas else "", ('"%s"' % coment) if coment else "Sem comentário."),
            campanha=None, mensagem="Oi %s! Aqui é do Mariá. Vi sua avaliação e queria entender o que aconteceu para a gente melhorar. Pode me contar?" % nome,
            valor=0)

    # limita os "se afastando" aos mais valiosos
    afast = sorted([a for a in out if a["tipo"] == "afastando"], key=lambda a: -a["valor"])
    corte = set(id(a) for a in afast[lim["afastando_max"]:])
    out = [a for a in out if id(a) not in corte]
    out.sort(key=lambda a: (a["prio"], -a["valor"]))
    return out
