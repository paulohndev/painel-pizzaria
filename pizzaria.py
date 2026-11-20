# -*- coding: utf-8 -*-
"""
Painel de clientes e vendas - Cardápio Web
------------------------------------------
Baixa pedidos e clientes da API do Cardápio Web, guarda num banco local
(pasta "dados") e gera um painel (painel.html) que abre no navegador.

Uso:
    python pizzaria.py                -> atualiza os dados e abre o painel
    python pizzaria.py --teste        -> usa a loja de TESTE do Cardápio Web (não precisa de chave)
    python pizzaria.py --so-painel    -> só gera o painel com o que já foi baixado
    python pizzaria.py --sem-detalhes -> baixa só a lista de pedidos (rápido), sem os detalhes

Só usa bibliotecas que já vêm com o Python 3.8+.
"""
import argparse
import configparser
import json
import os
import re
import sqlite3
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from recomendacoes import recomendar, limpar, sabor_do_nome, classificar_opcao  # noqa: E402

PASTA = os.path.dirname(os.path.abspath(__file__))
if sys.stdout is None:  # rodando sem janela (pythonw) e sem log: não quebra nos prints
    sys.stdout = sys.stderr = open(os.devnull, "w", encoding="utf-8")
PASTA_DADOS = os.path.join(PASTA, "dados")

URLS = {
    "producao": "https://integracao.cardapioweb.com/api/partner/v1",
    "sandbox": "https://integracao.sandbox.cardapioweb.com/api/partner/v1",
}
# Chave pública da loja de teste, publicada na documentação oficial do Cardápio Web
CHAVE_TESTE = "7nSyGq49NVXuyZfgEQNPg3TdUqLNXTMNMNJwckvE"

# Limites da API (por estabelecimento):
#   histórico de pedidos: 5 por minuto  -> 1 a cada 13 s (com folga)
#   demais endpoints:     300 a cada 3 min -> 1 a cada 0,65 s (com folga)
INTERVALO_HISTORICO = 13.0
INTERVALO_PADRAO = 0.65
JANELA_DIAS = 170  # a API aceita no máximo 6 meses por consulta
if os.environ.get("PIZZARIA_RAPIDO"):  # só para testes com API simulada
    INTERVALO_HISTORICO = INTERVALO_PADRAO = 0.0


# ---------------------------------------------------------------- utilidades

def log(msg=""):
    print(msg, flush=True)


def parse_dt(texto):
    """'2026-06-01T14:25:04.413-03:00' -> datetime ingênuo no horário local da loja."""
    if not texto:
        return None
    m = re.match(r"(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})", texto)
    if not m:
        return None
    return datetime.strptime(m.group(1) + " " + m.group(2), "%Y-%m-%d %H:%M:%S")


def so_digitos(t):
    return re.sub(r"\D", "", t or "")


def hora_para_min(hhmm):
    h, m = hhmm.strip().split(":")
    return int(h) * 60 + int(m)


# ---------------------------------------------------------------- configuração

CONFIG_PADRAO = """\
; ============================================================
;  Configuração do painel - edite com o Bloco de Notas
; ============================================================

[api]
; Cole aqui a chave gerada no painel do Cardápio Web:
; Configurações > Integrações > API  (botão "Gerar novo token")
chave =

[historico]
; Quantos meses para trás baixar na primeira vez (máximo 36).
meses = 15

[turnos]
; Horário de início e fim de cada turno (formato HH:MM-HH:MM).
; Deixe vazio (ex.: "tarde =") para não usar um turno.
; Pedidos fora desses horários aparecem como "Outro horário".
almoco = 05:00-17:00
tarde =
noite = 17:00-05:00

[clientes]
; Cliente "ativo" = pediu nos últimos X dias
dias_ativo = 45
; Cliente "fiel" = tem pelo menos X pedidos (no turno escolhido)
pedidos_fiel = 4
; Mensagem padrão do WhatsApp. {nome} vira o primeiro nome do cliente.
mensagem = Oi {nome}! Tudo bem? Sentimos sua falta por aqui! Preparamos um mimo pro seu próximo pedido pelo nosso app 🍕

[alertas]
; Notificações no canto da tela (sim/nao) e em que horário elas podem aparecer.
notificacoes = sim
horario = 09:00-23:30

[ia]
; Inteligência artificial (opcional). provedor = chatgpt ou claude.
; Cole a chave da API (platform.openai.com ou platform.claude.com) na linha "chave".
provedor =
chave =
; Opcional: troque o modelo (deixe vazio para o padrão).
modelo =
modelo_forte =
; A IA vê nomes, pedidos, bairros e ruas. Telefone, e-mail, número da casa e
; complemento só vão para a IA se você trocar para "sim".
dados_pessoais = nao

[whatsapp]
; Envio automático de mensagens (opcional). provedor = zapi ou evolution.
; Z-API: preencha instancia, token e client_token (painel da Z-API).
; Evolution API: preencha url, instancia e apikey.
provedor =
instancia =
token =
client_token =
url =
apikey =

[gestao]
; Pasta onde o Mariá Gestão guarda despesas, fichas técnicas e folha.
; Vazio = Área de Trabalho > Database
pasta =
; Turno que é a pizzaria (o faturamento diário das Despesas vem dele, do Cardápio Web)
turno_faturamento = noite
; Preencher o faturamento diário sozinho (sim/nao)
faturamento_automatico = sim

[atualizacao]
; O programa se atualiza sozinho a partir deste repositório do GitHub.
; Para desligar, troque "sim" por "nao".
ativo = sim
repositorio = paulohndev/painel-pizzaria
"""


def carregar_config():
    caminho = os.path.join(PASTA, "config.ini")
    if not os.path.exists(caminho):
        with open(caminho, "w", encoding="utf-8") as f:
            f.write(CONFIG_PADRAO)
    cp = configparser.ConfigParser(interpolation=None)
    cp.read_string(CONFIG_PADRAO)
    cp.read(caminho, encoding="utf-8")
    turnos = []
    for chave, rotulo in (("almoco", "Almoço"), ("tarde", "Tarde"), ("noite", "Noite")):
        val = cp.get("turnos", chave, fallback="").strip()
        if not val:
            continue
        ini, fim = val.split("-")
        turnos.append({"id": chave, "nome": rotulo, "ini": hora_para_min(ini), "fim": hora_para_min(fim), "texto": val})
    return {
        "chave": cp.get("api", "chave", fallback="").strip(),
        "meses": max(1, min(36, cp.getint("historico", "meses", fallback=15))),
        "turnos": turnos,
        "dias_ativo": cp.getint("clientes", "dias_ativo", fallback=45),
        "pedidos_fiel": cp.getint("clientes", "pedidos_fiel", fallback=4),
        "mensagem": cp.get("clientes", "mensagem", fallback="").strip().replace("Aqui é da pizzaria. ", ""),
        "ia": {k: cp.get("ia", k, fallback="").strip() for k in ("provedor", "chave", "modelo", "modelo_forte", "dados_pessoais")},
        "whatsapp": {k: cp.get("whatsapp", k, fallback="").strip() for k in ("provedor", "instancia", "token", "client_token", "url", "apikey")},
        "gestao": {k: cp.get("gestao", k, fallback="").strip() for k in ("pasta", "turno_faturamento", "faturamento_automatico")},
    }


def turno_de(dt, turnos):
    m = dt.hour * 60 + dt.minute
    for i, t in enumerate(turnos):
        ini, fim = t["ini"], t["fim"]
        dentro = (ini <= m < fim) if ini < fim else (m >= ini or m < fim)
        if dentro:
            return i
    return len(turnos)  # "Outro horário"


# ---------------------------------------------------------------- cliente da API

TRAVA = {"caminho": None, "toque": 0}


def tocar_trava():
    """Mantém o arquivo de trava 'vivo' enquanto baixa (se o programa morrer, a trava expira em 20 min)."""
    if TRAVA["caminho"] and time.time() - TRAVA["toque"] > 60:
        try:
            with open(TRAVA["caminho"], "w") as f:
                f.write(str(os.getpid()))
            TRAVA["toque"] = time.time()
        except OSError:
            pass


class ErroAPI(Exception):
    pass


class API:
    def __init__(self, base, chave):
        self.base = base.rstrip("/")
        self.chave = chave
        self._ultimo = {}

    def _esperar(self, grupo, intervalo):
        tocar_trava()
        agora = time.monotonic()
        prox = self._ultimo.get(grupo, 0) + intervalo
        if prox > agora:
            time.sleep(prox - agora)
        self._ultimo[grupo] = time.monotonic()

    def get(self, caminho, params=None, grupo="padrao", intervalo=INTERVALO_PADRAO):
        url = self.base + caminho
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)
        tentativas = 0
        while True:
            tentativas += 1
            self._esperar(grupo, intervalo)
            req = urllib.request.Request(url, headers={
                "X-API-KEY": self.chave,
                "Accept": "application/json",
                "User-Agent": "painel-pizzaria/1.0",
            })
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    return json.loads(r.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                if e.code == 401:
                    raise ErroAPI("A chave da API foi recusada (erro 401). Confira a chave no arquivo config.ini.")
                if e.code == 404:
                    return None
                if e.code == 429:
                    espera = 65
                    try:
                        espera = max(5, int(e.headers.get("Retry-After", espera)))
                    except (TypeError, ValueError):
                        pass
                    log("   (limite de consultas da API atingido, aguardando %d s...)" % espera)
                    time.sleep(espera)
                    tentativas -= 1  # não conta como falha
                    continue
                if e.code >= 500 and tentativas < 5:
                    time.sleep(5 * tentativas)
                    continue
                corpo = ""
                try:
                    corpo = e.read().decode("utf-8")[:300]
                except Exception:
                    pass
                raise ErroAPI("Erro %d em %s: %s" % (e.code, caminho, corpo))
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                if tentativas < 5:
                    log("   (falha de conexão, tentando de novo: %s)" % e)
                    time.sleep(5 * tentativas)
                    continue
                raise ErroAPI("Sem conexão com a API do Cardápio Web: %s" % e)


# ---------------------------------------------------------------- banco local

ESQUEMA = """
CREATE TABLE IF NOT EXISTS pedidos (
    id INTEGER PRIMARY KEY,
    status TEXT, order_type TEXT, order_timing TEXT, sales_channel TEXT,
    created_at TEXT, updated_at TEXT,
    detalhe_json TEXT,          -- resposta completa de /orders/{id}
    detalhe_em TEXT
);
CREATE TABLE IF NOT EXISTS clientes (
    id INTEGER PRIMARY KEY,
    nome TEXT, telefone TEXT, ddi TEXT, email TEXT, nascimento TEXT,
    cadastro TEXT, pontos INTEGER, cashback REAL, json TEXT
);
CREATE TABLE IF NOT EXISTS meta (chave TEXT PRIMARY KEY, valor TEXT);
CREATE TABLE IF NOT EXISTS vinculos (chave_app TEXT PRIMARY KEY, chave_principal TEXT, decisao TEXT, em TEXT);
CREATE TABLE IF NOT EXISTS avaliacoes (
    id INTEGER PRIMARY KEY, order_id INTEGER, customer_id INTEGER,
    nota REAL, comentario TEXT, criada TEXT, json TEXT
);
"""


def abrir_banco(nome):
    os.makedirs(PASTA_DADOS, exist_ok=True)
    db = sqlite3.connect(os.path.join(PASTA_DADOS, nome), timeout=60)
    db.executescript(ESQUEMA)
    return db


def meta_get(db, k):
    r = db.execute("SELECT valor FROM meta WHERE chave=?", (k,)).fetchone()
    return r[0] if r else None


def meta_set(db, k, v):
    db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (k, v))
    db.commit()


# ---------------------------------------------------------------- sincronização

def fmt_tempo(seg):
    seg = int(seg)
    if seg < 90:
        return "%d s" % seg
    if seg < 3600:
        return "%d min" % round(seg / 60)
    return "%dh%02d" % (seg // 3600, (seg % 3600) // 60)


def sincronizar_historico(api, db, meses):
    agora = datetime.now()
    limite_api = agora - timedelta(days=3 * 365 - 2)
    ultimo = meta_get(db, "historico_ate")
    if ultimo:
        inicio = parse_dt(ultimo) - timedelta(days=3)  # margem p/ pedidos fechados com atraso
    else:
        inicio = agora - timedelta(days=int(meses * 30.5))
    inicio = max(inicio, limite_api)

    janelas = []
    a = inicio
    while a < agora:
        b = min(a + timedelta(days=JANELA_DIAS), agora)
        janelas.append((a, b))
        a = b
    log("1/3  Lista de pedidos de %s até hoje (%d consulta(s) de período)..." % (inicio.strftime("%d/%m/%Y"), len(janelas)))
    log("     A API libera só 5 consultas por minuto aqui, então essa parte é mais lenta.")

    novos = 0
    total_vistos = 0
    for (a, b) in janelas:
        pagina = 1
        while True:
            resp = api.get("/orders/history", {
                "start_date": a.strftime("%Y-%m-%dT%H:%M:%S-03:00"),
                "end_date": b.strftime("%Y-%m-%dT%H:%M:%S-03:00"),
                "page": pagina, "per_page": 100,
            }, grupo="historico", intervalo=INTERVALO_HISTORICO)
            if not resp:
                break
            pedidos = resp.get("orders", [])
            pag = resp.get("pagination", {}) or {}
            for p in pedidos:
                cur = db.execute("SELECT status FROM pedidos WHERE id=?", (p["id"],)).fetchone()
                if cur is None:
                    novos += 1
                    db.execute(
                        "INSERT INTO pedidos (id,status,order_type,order_timing,sales_channel,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                        (p["id"], p.get("status"), p.get("order_type"), p.get("order_timing"),
                         p.get("sales_channel"), p.get("created_at"), p.get("updated_at")))
                else:
                    # status mudou (ex.: cancelado depois) -> atualiza e força rebuscar o detalhe
                    db.execute(
                        "UPDATE pedidos SET status=?, updated_at=?, detalhe_json=CASE WHEN status<>? THEN NULL ELSE detalhe_json END WHERE id=?",
                        (p.get("status"), p.get("updated_at"), p.get("status"), p["id"]))
            db.commit()
            total_vistos += len(pedidos)
            tot_pag = pag.get("total_pages") or 1
            log("     %s a %s: página %d de %d (%d pedidos no período)" % (
                a.strftime("%d/%m/%y"), b.strftime("%d/%m/%y"), pagina, tot_pag, pag.get("total_orders", len(pedidos))))
            if pagina >= tot_pag or not pedidos:
                break
            pagina += 1
        # período completo: se for interrompido, da próxima vez continua daqui
        meta_set(db, "historico_ate", b.strftime("%Y-%m-%dT%H:%M:%S"))
    log("     OK: %d pedidos novos (%d conferidos)." % (novos, total_vistos))


def sincronizar_detalhes(api, db, ao_progresso=None):
    # primeiro os canais com cliente identificado (app, WhatsApp, 99), depois balcão e iFood
    ordem = " ".join("WHEN '%s' THEN %d" % (c, i) for i, c in enumerate(PRIORIDADE_CANAL))
    ids = [r[0] for r in db.execute(
        "SELECT id FROM pedidos WHERE detalhe_json IS NULL AND status='closed' "
        "ORDER BY CASE sales_channel %s ELSE 99 END, created_at DESC" % ordem)]
    if not ids:
        log("2/3  Detalhes dos pedidos: nada novo para baixar.")
        return
    log("2/3  Baixando detalhes de %d pedidos (cliente, itens, bairro). Estimativa: %s." % (
        len(ids), fmt_tempo(len(ids) * INTERVALO_PADRAO)))
    log("     Pode fechar a qualquer momento: da próxima vez continua de onde parou.")
    t0 = time.time()
    for i, pid in enumerate(ids, 1):
        d = api.get("/orders/%d" % pid)
        if d is not None:
            db.execute("UPDATE pedidos SET detalhe_json=?, detalhe_em=? WHERE id=?",
                       (json.dumps(d, ensure_ascii=False), datetime.now().isoformat(timespec="seconds"), pid))
        else:
            db.execute("UPDATE pedidos SET detalhe_json='{}' WHERE id=?", (pid,))
        if i % 50 == 0 or i == len(ids):
            db.commit()
            gasto = time.time() - t0
            resta = gasto / i * (len(ids) - i)
            log("     %d de %d  (falta ~%s)" % (i, len(ids), fmt_tempo(resta)))
            if ao_progresso and i % 1500 == 0:
                ao_progresso()
    db.commit()


def sincronizar_avaliacoes(api, db, meses):
    agora = datetime.now()
    ultimo = meta_get(db, "avaliacoes_ate")
    inicio = parse_dt(ultimo) - timedelta(days=2) if ultimo else agora - timedelta(days=int(min(meses, 35) * 30.5))
    inicio = max(inicio, agora - timedelta(days=3 * 365 - 2))
    total = 0
    a = inicio
    while a < agora:
        b = min(a + timedelta(days=JANELA_DIAS), agora)
        pagina = 1
        while True:
            resp = api.get("/merchant/reviews", {
                "start_date": a.strftime("%Y-%m-%dT%H:%M:%S-03:00"),
                "end_date": b.strftime("%Y-%m-%dT%H:%M:%S-03:00"),
                "page": pagina, "per_page": 100})
            if not resp:
                break
            for r in resp.get("reviews", []):
                db.execute("INSERT OR REPLACE INTO avaliacoes VALUES (?,?,?,?,?,?,?)", (
                    r["id"], r.get("order_id"), r.get("customer_id"), r.get("rating"),
                    r.get("comment") or "", r.get("created_at"), json.dumps(r, ensure_ascii=False)))
                total += 1
            db.commit()
            tp = (resp.get("pagination") or {}).get("total_pages") or 1
            if pagina >= tp or not resp.get("reviews"):
                break
            pagina += 1
        a = b
    meta_set(db, "avaliacoes_ate", agora.strftime("%Y-%m-%dT%H:%M:%S"))
    log("     Avaliações: %d baixadas/conferidas." % total)


def sincronizar_clientes(api, db):
    log("3/3  Atualizando cadastro de clientes...")
    pagina, total = 1, 0
    while True:
        resp = api.get("/merchant/customers", {"page": pagina, "per_page": 50})
        if not resp:
            break
        for c in resp.get("customers", []):
            db.execute("INSERT OR REPLACE INTO clientes VALUES (?,?,?,?,?,?,?,?,?,?)", (
                c["id"], c.get("name"), c.get("phone_number"), c.get("ddi"), c.get("email"),
                c.get("birth_date"), c.get("created_at"), c.get("loyalty_points") or 0,
                c.get("cashback_balance") or 0, json.dumps(c, ensure_ascii=False)))
            total += 1
        db.commit()
        tp = (resp.get("pagination") or {}).get("total_pages") or 1
        if pagina % 10 == 0:
            log("     página %d de %d" % (pagina, tp))
        if pagina >= tp or not resp.get("customers"):
            break
        pagina += 1
    log("     OK: %d clientes." % total)


def sincronizar_loja(api, db):
    """Dados cadastrais da loja: informações, formas de pagamento, cupons e cardápio (quando a chave permite)."""
    def todas_paginas(caminho):
        pagina, itens, tam = 1, [], 50
        while True:
            try:
                r = api.get(caminho, {"page": pagina, "per_page": tam})
            except ErroAPI as e:  # cada endpoint aceita um tamanho de página diferente
                if "per_page" in str(e) and tam > 10:
                    tam = 20 if tam > 20 else 10
                    continue
                raise
            if r is None:
                return None
            if isinstance(r, list):
                return r
            lst = next((v for v in r.values() if isinstance(v, list)), [])
            itens.extend(lst)
            tp = (r.get("pagination") or {}).get("total_pages") or 1
            if pagina >= tp or not lst:
                return itens
            pagina += 1
    for chave, caminho, pag in (("loja_json", "/merchant", False), ("pagamentos_json", "/merchant/payment_methods", False),
                                ("cupons_json", "/merchant/coupons", True), ("catalogo_json", "/catalog", False)):
        try:
            r = todas_paginas(caminho) if pag else api.get(caminho)
            if r is not None:
                meta_set(db, chave, json.dumps(r, ensure_ascii=False))
                db.commit()
        except ErroAPI as e:
            log("     (%s não disponível com esta chave: %s)" % (caminho, str(e)[:120]))
    meta_set(db, "loja_em", datetime.now().strftime("%Y-%m-%d %H:%M"))
    db.commit()


# ---------------------------------------------------------------- dados do painel

CANAIS = {
    "catalog": "App / cardápio digital",
    "store_front_catalog": "Balcão (cardápio)",
    "portal": "Lançado no portal",
    "whatsapp_extension": "WhatsApp",
    "ifood": "iFood",
    "food99": "99Food",
}
APPS_ENTREGA = {"ifood", "food99"}
PRIORIDADE_CANAL = ["catalog", "whatsapp_extension", "store_front_catalog", "food99", "portal", "ifood"]
TIPOS = {"delivery": "Entrega", "takeout": "Retirada", "onsite": "No local", "closed_table": "Mesa"}


def montar_dados(db, cfg, nome_loja):
    turnos = cfg["turnos"]
    nomes_turno = [t["nome"] for t in turnos] + ["Outro horário"]

    clientes_cad = {}
    for (cid, nome, tel, ddi, email, nasc, cad, pontos, cashback) in db.execute(
            "SELECT id,nome,telefone,ddi,email,nascimento,cadastro,pontos,cashback FROM clientes"):
        clientes_cad[cid] = {"nome": nome, "tel": tel, "ddi": ddi, "nasc": nasc, "cad": cad,
                             "pontos": pontos or 0, "cashback": cashback or 0}

    idx_cli, lista_cli = {}, []
    idx_bairro, lista_bairro = {}, []
    idx_canal, lista_canal = {}, []
    idx_tipo, lista_tipo = {}, []

    def idx(mapa, lista, chave):
        if chave not in mapa:
            mapa[chave] = len(lista)
            lista.append(chave)
        return mapa[chave]

    pedidos = []
    itens = {}     # (turno, canal, nome) -> [qtd, valor]
    sabores = {}   # (turno, canal, grupo, nome) -> [qtd, valor]
    itens_dia = {}     # (dia, turno, canal, nome) -> [qtd, valor]  (para filtrar por período)
    sabores_dia = {}   # (dia, turno, canal, grupo, nome) -> [qtd, valor]
    idx_nome, lista_nome = {}, []
    DIA0 = datetime(2000, 1, 1).date()
    sem_detalhe = 0
    compras = []   # pedidos concluídos com cliente, para as recomendações
    abertos = []   # pedidos em andamento (para o alerta de combo ao vivo)
    idx_cupom, lista_cupom = {}, []
    cid_cli = {}   # id do cliente no Cardápio Web -> índice no painel
    promos = carregar_promos()
    info_cli = {}  # índice -> {"ends": set(endereços), "canais": set()}

    for (pid, status, otype, canal, created, det) in db.execute(
            "SELECT id,status,order_type,sales_channel,created_at,detalhe_json FROM pedidos ORDER BY created_at"):
        dt = parse_dt(created)
        if dt is None:
            continue
        t = turno_de(dt, turnos)
        ci = idx(idx_canal, lista_canal, canal or "?")
        ti = idx(idx_tipo, lista_tipo, otype or "?")
        d = json.loads(det) if det else None
        if status == "closed" and not d:
            sem_detalhe += 1
        total, taxa, cli_i, bai_i, desc, cup_i = None, 0, -1, -1, 0, -1
        if d:
            total = d.get("total")
            taxa = d.get("delivery_fee") or 0
            desc = sum((x.get("total") or 0) for x in (d.get("discounts") or []))
            for x in d.get("discounts") or []:
                cod = (x.get("coupon_code") or x.get("coupon_name") or "").strip().upper()
                if cod:
                    cup_i = idx(idx_cupom, lista_cupom, cod)
                    break
            cust = d.get("customer") or {}
            cid = cust.get("id")
            if cid:
                cad = clientes_cad.get(cid, {})
                tel = so_digitos(cad.get("tel") or cust.get("phone"))
                if tel.startswith("0800") or tel.startswith("800") or len(set(tel)) <= 2:
                    tel = ""  # número de central (ex.: iFood), não é do cliente
                chave = ("t" + tel[-11:]) if len(tel) >= 10 else "c%s" % cid
                if chave not in idx_cli:
                    ddi = so_digitos(cad.get("ddi") or cust.get("ddi") or "55")
                    idx_cli[chave] = len(lista_cli)
                    lista_cli.append({
                        "id": cid, "chave": chave,
                        "nome": (cad.get("nome") or cust.get("name") or "").strip(),
                        "tel": tel, "ddi": ddi or "55",
                        "pontos": cad.get("pontos", 0), "cashback": cad.get("cashback", 0),
                        "nasc": cad.get("nasc"), "cad": (cad.get("cad") or "")[:10],
                        "ifood": canal in APPS_ENTREGA,
                        "app": CANAIS.get(canal, "") if canal in APPS_ENTREGA else "",
                    })
                cli_i = idx_cli[chave]
                cid_cli[cid] = cli_i
                reg = lista_cli[cli_i]
                if canal not in APPS_ENTREGA:
                    reg["ifood"] = False
                    reg["app"] = ""
                    if cad.get("nome") or (cust.get("name") and not reg["nome"]):
                        reg["nome"] = (cad.get("nome") or cust.get("name") or "").strip()
                    if cad:
                        reg.update({"pontos": cad.get("pontos", 0), "nasc": cad.get("nasc"), "id": cid})
                elif reg.get("app") and reg["app"] != CANAIS.get(canal):
                    reg["app"] = "iFood/99"
            end = d.get("delivery_address") or {}
            if cli_i >= 0:
                inf = info_cli.setdefault(cli_i, {"ends": set(), "canais": set()})
                inf["canais"].add(canal)
                ne = norm_endereco(end)
                if ne:
                    inf["ends"].add(ne)
                # posição no mapa: a do endereço de entrega mais recente (só coordenadas de verdade, na região)
                try:
                    la, lo = float(end.get("latitude") or 0), float(end.get("longitude") or 0)
                except (TypeError, ValueError):
                    la = lo = 0
                if -8.6 < la < -7.5 and -35.4 < lo < -34.7 and len(str(end.get("latitude"))) > 5 and (inf.get("gdt") is None or dt >= inf["gdt"]):
                    inf["gdt"] = dt
                    lista_cli[cli_i]["g"] = [round(la, 4), round(lo, 4)]
            bairro = (end.get("neighborhood") or "").strip().title()
            if bairro:
                bai_i = idx(idx_bairro, lista_bairro, bairro)
            if status == "closed":
                compra = {"cli": cli_i, "dt": dt, "t": t, "ifood": canal in APPS_ENTREGA, "total": total,
                          "itens": [], "sabores": [], "extras": []}
                for it in d.get("items") or []:
                    if it.get("status") not in (None, "ok"):
                        continue
                    nome_it = (it.get("name") or "?").strip()
                    compra["itens"].append(limpar(nome_it) or nome_it)
                    if (it.get("total_price") or 0) > compra.get("_preco", -1):
                        compra["_preco"] = it.get("total_price") or 0
                        compra["principal"] = compra["itens"][-1]
                    sab_nome = sabor_do_nome(nome_it)
                    if sab_nome:
                        compra["sabores"].append(sab_nome)
                    for op in it.get("options") or []:
                        nome_op = limpar(op.get("name") or "")
                        tipo_op = classificar_opcao(nome_it, op.get("option_group_name"), nome_op)
                        if tipo_op == "sabor" and nome_op:
                            compra["sabores"].append(nome_op)
                        elif tipo_op == "extra" and nome_op:
                            compra["extras"].append(nome_op)
                    k = (t, ci, (it.get("name") or "?").strip())
                    acc = itens.setdefault(k, [0, 0.0])
                    acc[0] += it.get("quantity") or 0
                    acc[1] += it.get("total_price") or 0
                    dia = (dt.date() - DIA0).days
                    accd = itens_dia.setdefault((dia, t, ci, idx(idx_nome, lista_nome, k[2])), [0, 0.0])
                    accd[0] += it.get("quantity") or 0
                    accd[1] += it.get("total_price") or 0
                    for op in it.get("options") or []:
                        k2 = (t, ci, (op.get("option_group_name") or "").strip(), (op.get("name") or "?").strip())
                        acc2 = sabores.setdefault(k2, [0, 0.0])
                        q = (op.get("quantity") or 1) * (it.get("quantity") or 1)
                        acc2[0] += q
                        acc2[1] += (op.get("unit_price") or 0) * q
                        acc3 = sabores_dia.setdefault((dia, t, ci, idx(idx_nome, lista_nome, k2[2]), idx(idx_nome, lista_nome, k2[3])), [0, 0.0])
                        acc3[0] += q
                        acc3[1] += (op.get("unit_price") or 0) * q
            if status == "closed" and cli_i >= 0:
                compras.append(compra)
            if status not in ("closed", "canceled") and cli_i >= 0 and (datetime.now() - dt).total_seconds() < 4 * 3600:
                abertos.append({"id": pid, "cli": cli_i, "dt": dt.strftime("%Y-%m-%d %H:%M"), "status": status, "canal": canal,
                                "itens": [limpar((it.get("name") or "").strip()) for it in (d.get("items") or [])],
                                "extras": [limpar(op.get("name") or "") for it in (d.get("items") or []) for op in (it.get("options") or [])
                                           if classificar_opcao(it.get("name"), op.get("option_group_name"), op.get("name")) == "extra"]})
        pedidos.append([
            dt.strftime("%Y-%m-%d %H:%M"), t, ci, ti,
            None if total is None else round(total, 2),
            cli_i, bai_i, 1 if status == "canceled" else 0,
            round(taxa, 2), round(desc, 2), cup_i, pid,
            0 if status in ("closed", "canceled") else 1,
            promos_do_pedido(promos, d) if d else 0,
        ])

    # ---- mesma pessoa em canais diferentes (iFood não passa telefone)
    uniao, sugestoes = achar_vinculos(db, lista_cli, info_cli)
    if uniao:
        for a, (p, origem) in uniao.items():
            ra, rp = lista_cli[a], lista_cli[p]
            ra["uniu_em"] = p
            rp.setdefault("unidos", []).append({"chave": ra["chave"], "nome": ra["nome"], "app": ra.get("app") or "iFood", "origem": origem})
            if rp.get("app") and ra.get("app") and rp["app"] != ra["app"]:
                rp["app"] = "iFood/99"
            if not rp.get("nome"):
                rp["nome"] = ra["nome"]
        m = lambda i: uniao[i][0] if i in uniao else i
        for row in pedidos:
            row[5] = m(row[5])
        for cp in compras:
            cp["cli"] = m(cp["cli"])
        for ab in abertos:
            ab["cli"] = m(ab["cli"])
        for k in list(cid_cli):
            cid_cli[k] = m(cid_cli[k])

    avaliacoes = []
    for (oid, cust, nota, coment, criada, js) in db.execute(
            "SELECT order_id, customer_id, nota, comentario, criada, json FROM avaliacoes ORDER BY criada DESC LIMIT 3000"):
        try:
            resp = [(a.get("question"), a.get("answer")) for a in (json.loads(js).get("answers") or [])]
        except ValueError:
            resp = []
        baixas = ", ".join("%s %s" % (q, r) for q, r in resp if str(r).strip().isdigit() and int(r) <= 3)
        avaliacoes.append([(criada or "")[:16].replace("T", " "), cid_cli.get(cust, -1), nota, coment or "", baixas, oid])

    try:
        promo = recomendar(compras, lista_cli, nomes_turno, cfg)
    except Exception as e:  # recomendação nunca deve impedir o painel de abrir
        log("Aviso: não consegui calcular as recomendações (%s)" % e)
        promo = {"clientes": [], "pares": []}

    return {
        "promo": promo,
        "gerado_em": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "loja": nome_loja,
        "turnos": nomes_turno,
        "turnos_horario": [t["texto"] for t in turnos] + [""],
        "canais": [{"id": c, "nome": CANAIS.get(c, c), "ifood": c in APPS_ENTREGA} for c in lista_canal],
        "tipos": [TIPOS.get(x, x) for x in lista_tipo],
        "bairros": lista_bairro,
        "clientes": lista_cli,
        "pedidos": pedidos,
        "itens": [[k[0], k[1], k[2], round(v[0], 2), round(v[1], 2)] for k, v in itens.items()],
        "sabores": [[k[0], k[1], k[2], k[3], round(v[0], 2), round(v[1], 2)] for k, v in sabores.items()],
        "nomes_it": lista_nome,
        "itens_dia": [list(k) + [round(v[0], 2), round(v[1], 2)] for k, v in itens_dia.items()],
        "sabores_dia": [list(k) + [round(v[0], 2), round(v[1], 2)] for k, v in sabores_dia.items()],
        "config": {"dias_ativo": cfg["dias_ativo"], "pedidos_fiel": cfg["pedidos_fiel"], "mensagem": cfg["mensagem"]},
        "sem_detalhe": sem_detalhe,
        "cupons": lista_cupom,
        "avaliacoes": avaliacoes,
        "abertos": abertos,
        "vinc_sug": sugestoes,
        "promos_casa": promos,
    }


# ---------------------------------------------------------------- promoções da casa

# dias: 0=domingo ... 6=sábado (vazio = todo dia). "itens": trechos do nome do item no Cardápio Web.
PROMOS_PADRAO = [
    {"id": "segunda_maluca", "nome": "Segunda Maluca", "dias": [1], "itens": ["segunda maluca"], "cupom": "", "so_proprio": False,
     "sabores": ["calabresa", "mussarela", "napolitana", "marguerita", "cartola"],
     "oferta": "pizza grande de calabresa, mussarela, napolitana, marguerita ou cartola por R$ 39,99",
     "msg": "Oi {nome}! Hoje é Segunda Maluca 🍕 {sabor_promo} sai por R$ 39,99. É só pedir pelo nosso app!"},
    {"id": "terca_off", "nome": "Terça Off", "dias": [2], "itens": [], "cupom": "TERCA15", "so_proprio": True, "sabores": [],
     "oferta": "15% de desconto em todo pedido pelo nosso app com o cupom TERCA15",
     "msg": "Oi {nome}! Hoje é Terça Off: pedindo pelo nosso app com o cupom TERCA15, você ganha 15% de desconto em tudo. Que tal {favorito} hoje? 😋"},
    {"id": "quinta_clone", "nome": "Quinta do Clone", "dias": [4], "itens": ["clone"], "cupom": "", "so_proprio": False, "sabores": [],
     "oferta": "clone de pizza (até 2 sabores) por R$ 69,99",
     "msg": "Oi {nome}! Hoje é Quinta do Clone 🍕🍕 Clone de pizza por R$ 69,99. Que tal dobrar {favorito}? Peça pelo nosso app!"},
    {"id": "combos", "nome": "Combos", "dias": [], "itens": ["combo"], "cupom": "", "so_proprio": False, "sabores": [],
     "oferta": "combos de pizza com refrigerante todos os dias", "msg": ""},
]


def carregar_promos():
    try:
        with open(os.path.join(PASTA_DADOS, "promocoes_casa.json"), encoding="utf-8") as f:
            v = json.load(f)
        if isinstance(v, list):
            return v
    except (OSError, ValueError):
        pass
    return PROMOS_PADRAO


def salvar_promos(promos):
    os.makedirs(PASTA_DADOS, exist_ok=True)
    tmp = os.path.join(PASTA_DADOS, "promocoes_casa.json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(promos, f, ensure_ascii=False, indent=1)
    os.replace(tmp, os.path.join(PASTA_DADOS, "promocoes_casa.json"))


def promos_do_pedido(promos, d):
    """Bits das promoções da casa usadas no pedido (pelo nome do item ou pelo cupom)."""
    nomes = " ".join(_sem_acento(i.get("name")) for i in (d.get("items") or []))
    cupons = {str(x.get(k) or "").strip().upper() for x in (d.get("discounts") or []) for k in ("coupon_code", "coupon_name")}
    m = 0
    for k, p in enumerate(promos[:30]):
        if any(t and _sem_acento(t) in nomes for t in p.get("itens") or []) or \
                (p.get("cupom") and p["cupom"].strip().upper() in cupons):
            m |= 1 << k
    return m


# ---------------------------------------------------------------- mesma pessoa em vários canais

_PALAVRAS_FORA = {"de", "da", "do", "das", "dos", "e"}
_NOMES_PROPRIOS = set("""joao jose maria ana pedro paulo carlos luiz luis antonio miguel rafael gabriel lucas mateus matheus marcelo
vitor victor henrique eduardo eduarda luiza luisa beatriz clara julia fernanda cristina cecilia alice sophia sofia vitoria heloisa
gustavo felipe augusto vinicius ricardo roberto fernando gabriela isabel isabela isabella carolina lucia helena laura manuela valentina
livia luana leticia rodrigo rosa rita francisco teresa tereza aparecida fatima conceicao carmen emilly emily vitoria arthur artur davi
daniel daniela guilherme bruno bruna camila amanda jessica larissa mariana marina raquel renata sandra simone tiago thiago diego
andre andrea andreia alexandre antonia marcos marco mario cesar caio enzo bernardo lorenzo heitor theo samuel benjamin nicolas joaquim
eduarda yasmin ester esther rebeca sara sarah luciana patricia adriana juliana silvia sergio jorge edson elias""".split())
_TIPOS_RUA = {"r", "rua", "av", "avenida", "tv", "travessa", "trav", "estrada", "est", "rod", "rodovia", "al", "alameda", "pc", "praca", "largo", "lg"}


def _sem_acento(t):
    return "".join(ch for ch in unicodedata.normalize("NFD", t or "") if unicodedata.category(ch) != "Mn").lower()


def norm_nome(n):
    return [w for w in re.findall(r"[a-z]+", _sem_acento(n)) if w not in _PALAVRAS_FORA and len(w) > 1]


def norm_endereco(e):
    rua = [w for w in re.findall(r"[a-z0-9]+", _sem_acento(e.get("street"))) if w not in _TIPOS_RUA and w not in _PALAVRAS_FORA]
    num = re.sub(r"\D", "", str(e.get("number") or ""))
    if not rua or not num:
        return ""
    return " ".join(rua) + "|" + num


def _primeiro_igual(a, b):
    if a == b:
        return True
    if min(len(a), len(b)) < 5 or abs(len(a) - len(b)) > 1:
        return False
    # diferença de uma letra (Andrea / Andreia, Thiago / Tiago)
    i = 0
    while i < min(len(a), len(b)) and a[i] == b[i]:
        i += 1
    return a[i + (len(a) > len(b)):] == b[i + (len(b) > len(a)):] or a[i + 1:] == b[i + 1:]


def nomes_compativeis(a, b):
    """Nome do app (às vezes abreviado) bate com o nome completo do cadastro."""
    if len(a) < 2 or len(b) < 2 or not _primeiro_igual(a[0], b[0]):
        return False
    curto, longo = (a, b) if len(a) <= len(b) else (b, a)
    return set(curto[1:]) <= set(longo[1:])


def achar_vinculos(db, clientes, info):
    """Liga clientes do iFood (sem telefone) ao cadastro com telefone da mesma pessoa.
    Junta sozinho quando nome e endereço batem; quando só o nome bate, vira sugestão para confirmar."""
    try:
        dec = {r[0]: (r[1], r[2]) for r in db.execute("SELECT chave_app, chave_principal, decisao FROM vinculos")}
    except sqlite3.Error:
        dec = {}
    por_chave = {c["chave"]: i for i, c in enumerate(clientes)}
    com_tel, por_primeiro = [], {}
    for i, c in enumerate(clientes):
        if c.get("tel"):
            nn = norm_nome(c.get("nome"))
            if len(nn) >= 2:
                com_tel.append(i)
                por_primeiro.setdefault(nn[0][:4], []).append((i, nn))
    # palavras que não identificam ninguém: nomes próprios (João Vitor, Maria Luiza) e sobrenomes muito comuns (Silva)
    freq, como_primeiro = {}, {}
    for c in clientes:
        nn = norm_nome(c.get("nome"))
        if nn:
            como_primeiro[nn[0]] = como_primeiro.get(nn[0], 0) + 1
        for w in set(nn):
            freq[w] = freq.get(w, 0) + 1
    limite = max(5, len(clientes) // 250)
    distintivo = lambda w: w not in _NOMES_PROPRIOS and como_primeiro.get(w, 0) < 2 and freq.get(w, 0) <= limite
    uniao, sugestoes = {}, []
    for i, c in enumerate(clientes):
        if c.get("tel") or i not in info:
            continue
        if not (info[i]["canais"] <= APPS_ENTREGA):
            continue
        d = dec.get(c["chave"])
        if d and d[1] == "sim" and d[0] in por_chave:
            uniao[i] = (por_chave[d[0]], "manual")
            continue
        nn = norm_nome(c.get("nome"))
        if len(nn) < 2:
            continue
        cands = [j for j, nj in por_primeiro.get(nn[0][:4], []) if nomes_compativeis(nn, nj)]
        if d and d[1] == "nao":
            cands = [j for j in cands if clientes[j]["chave"] != d[0]]
        if not cands:
            continue
        ends = info[i]["ends"]
        mesmo_end = [j for j in cands if ends & info.get(j, {}).get("ends", set())]
        if len(mesmo_end) == 1:
            uniao[i] = (mesmo_end[0], "auto")
        elif len(cands) == 1 and any(distintivo(w) for w in set(nn[1:]) & set(norm_nome(clientes[cands[0]].get("nome"))[1:])):
            sugestoes.append([i, cands[0], "mesmo nome"])
    return uniao, sugestoes


def html_painel(dados):
    with open(os.path.join(PASTA, "painel_modelo.html"), encoding="utf-8") as f:
        html = f.read()
    js = json.dumps(dados, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    return html.replace("/*__DADOS__*/null", js)


def gerar_painel(dados, destino):
    html = html_painel(dados)
    tmp = destino + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(html)
    os.replace(tmp, destino)


# ---------------------------------------------------------------- principal

def main():
    ap = argparse.ArgumentParser(description="Painel de clientes e vendas - Cardápio Web")
    ap.add_argument("--teste", action="store_true", help="usa a loja de teste do Cardápio Web")
    ap.add_argument("--so-painel", action="store_true", help="não baixa nada, só gera o painel")
    ap.add_argument("--sem-detalhes", action="store_true", help="não baixa os detalhes dos pedidos")
    ap.add_argument("--nao-abrir", action="store_true", help="não abre o navegador no final")
    ap.add_argument("--url", help=argparse.SUPPRESS)  # para testes
    args = ap.parse_args()

    cfg = carregar_config()
    if args.teste:
        base, chave, banco, painel = URLS["sandbox"], CHAVE_TESTE, "teste.db", "painel_TESTE.html"
        log(">> MODO TESTE: usando a loja de demonstração do Cardápio Web (dados fictícios).")
    else:
        base, chave, banco, painel = URLS["producao"], cfg["chave"], "loja.db", "painel.html"
    if args.url:
        base = args.url

    db = abrir_banco(banco)
    nome_loja = meta_get(db, "nome_loja") or ""

    trava = os.path.join(PASTA_DADOS, "rodando_%s.lock" % banco)
    if not args.so_painel and os.path.exists(trava):
        try:
            idade = time.time() - os.path.getmtime(trava)
        except OSError:
            idade = 0
        if idade < 20 * 60:
            log("Já tem uma atualização rodando (começou há %s). Vou só gerar o painel com o que já foi baixado." % fmt_tempo(idade))
            args.so_painel = True

    if not args.so_painel:
        if not chave:
            log("")
            log("Falta a chave da API!")
            log("1) No painel do Cardápio Web: Configurações > Integrações > API > Gerar novo token")
            log("2) Abra o arquivo config.ini (nesta pasta) com o Bloco de Notas")
            log("3) Cole a chave depois de 'chave =' e salve")
            log("")
            log("Para testar antes com dados de demonstração:  python pizzaria.py --teste")
            return 2
        api = API(base, chave)
        inicio = time.time()
        TRAVA["caminho"], TRAVA["toque"] = trava, 0
        tocar_trava()
        try:
            loja = api.get("/merchant", grupo="loja", intervalo=INTERVALO_HISTORICO)
            if loja:
                nome_loja = loja.get("name") or loja.get("trade_name") or nome_loja
                meta_set(db, "nome_loja", nome_loja)
                log("Conectado: %s" % (nome_loja or "loja"))
            sincronizar_historico(api, db, cfg["meses"])
            if not args.sem_detalhes:
                def parcial():
                    gerar_painel(montar_dados(db, cfg, nome_loja), os.path.join(PASTA, painel))
                    log("     (painel.html atualizado com o que já foi baixado; pode abrir enquanto continua)")
                sincronizar_detalhes(api, db, parcial)
            sincronizar_clientes(api, db)
            try:
                sincronizar_avaliacoes(api, db, cfg["meses"])
            except ErroAPI as e:
                log("     (não consegui baixar as avaliações: %s)" % e)
        except ErroAPI as e:
            log("")
            log("ERRO: %s" % e)
            log("O que já foi baixado ficou salvo. Vou gerar o painel com esses dados.")
        except KeyboardInterrupt:
            log("")
            log("Interrompido. O que já foi baixado ficou salvo.")
        finally:
            TRAVA["caminho"] = None
            try:
                os.remove(trava)
            except OSError:
                pass
        log("Tempo total: %s" % fmt_tempo(time.time() - inicio))

    dados = montar_dados(db, cfg, nome_loja)
    destino = os.path.join(PASTA, painel)
    gerar_painel(dados, destino)
    log("")
    log("Painel pronto: %s  (%d pedidos, %d clientes)" % (destino, len(dados["pedidos"]), len(dados["clientes"])))
    if dados["sem_detalhe"]:
        log("Atenção: %d pedidos ainda sem detalhes. Rode de novo para completar." % dados["sem_detalhe"])
    if not args.nao_abrir and dados["pedidos"]:
        from pathlib import Path
        webbrowser.open(Path(destino).resolve().as_uri())
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # mostra o erro sem fechar a janela de repente
        import traceback
        traceback.print_exc()
        log("")
        log("Deu um erro inesperado. Mande um print desta tela para quem te ajudou a configurar.")
        sys.exit(1)
