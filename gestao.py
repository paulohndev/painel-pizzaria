# -*- coding: utf-8 -*-
"""
Mariá Gestão dentro do sistema.

O Gestão (despesas, fichas técnicas, equipe e folha, painel geral) continua sendo o mesmo app,
mas agora é aberto pelo vigia em http://127.0.0.1:8766/ junto com o painel de vendas.
Os dados continuam nos mesmos arquivos da pasta Database; quem lê e grava é o sistema
(antes o navegador pedia permissão para usar a pasta).

Também faz a ponte entre os dois lados:
  - o faturamento diário das Despesas vem sozinho do Cardápio Web;
  - a IA do painel enxerga despesas, fichas, insumos, equipe e folha.
"""
import datetime
import json
import os
import re
import threading

PASTA = os.path.dirname(os.path.abspath(__file__))
SUBPASTAS = {"despesas": "Despesas", "fichas": "Fichas Técnicas", "equipe": "Folha"}
ARQUIVOS = {"despesas": "despesas-maria-dados.json", "fichas": "fichas-maria-dados.json",
            "equipe": "equipe-maria-dados.json", "notas": "anotacoes-maria-dados.json"}
PASTA_DO_ARQ = {"despesas": "despesas", "fichas": "fichas", "equipe": "equipe", "notas": "despesas"}
TRAVA = threading.Lock()
LIMITE = 25 * 1024 * 1024  # um comprovante em PDF pode ser grande, mas não infinito


class ErroGestao(Exception):
    def __init__(self, msg, codigo=400):
        super().__init__(msg)
        self.codigo = codigo


def pasta_base(cfg):
    p = ((cfg or {}).get("gestao") or {}).get("pasta") or ""
    if p:
        return os.path.abspath(os.path.expanduser(p))
    casa = os.path.expanduser("~")
    for desk in ("Desktop", "Área de Trabalho", os.path.join("OneDrive", "Desktop"), os.path.join("OneDrive", "Área de Trabalho")):
        c = os.path.join(casa, desk, "Database")
        if os.path.isdir(c):
            return c
    return os.path.join(casa, "Desktop", "Database")


def caminho(cfg, app, rel):
    """Caminho real de um arquivo dentro da pasta do app (nunca sai dela)."""
    if app not in SUBPASTAS:
        raise ErroGestao("app desconhecido", 404)
    raiz = os.path.join(pasta_base(cfg), SUBPASTAS[app])
    partes = [x for x in re.split(r"[\\/]+", str(rel or "")) if x not in ("", ".")]
    for x in partes:
        if x == ".." or ":" in x or x.startswith("~") or len(x) > 120:
            raise ErroGestao("caminho inválido", 403)
    alvo = os.path.abspath(os.path.join(raiz, *partes)) if partes else os.path.abspath(raiz)
    if alvo != os.path.abspath(raiz) and not alvo.startswith(os.path.abspath(raiz) + os.sep):
        raise ErroGestao("caminho inválido", 403)
    return alvo


def info(cfg, app, rel):
    c = caminho(cfg, app, rel)
    if not os.path.exists(c):
        return {"existe": False}
    return {"existe": True, "pasta": os.path.isdir(c), "tam": os.path.getsize(c) if os.path.isfile(c) else 0,
            "mtime": int(os.path.getmtime(c) * 1000)}


def ler(cfg, app, rel):
    c = caminho(cfg, app, rel)
    if not os.path.isfile(c):
        raise ErroGestao("não encontrado", 404)
    with open(c, "rb") as f:
        return f.read(), int(os.path.getmtime(c) * 1000)


def gravar(cfg, app, rel, dados):
    if len(dados) > LIMITE:
        raise ErroGestao("arquivo grande demais", 413)
    c = caminho(cfg, app, rel)
    if c == os.path.abspath(os.path.join(pasta_base(cfg), SUBPASTAS[app])):
        raise ErroGestao("caminho inválido", 403)
    if c.lower().endswith(".json"):
        try:
            json.loads(dados.decode("utf-8"))  # nunca grava um arquivo de dados quebrado
        except ValueError:
            raise ErroGestao("conteúdo inválido", 400)
    with TRAVA:
        os.makedirs(os.path.dirname(c), exist_ok=True)
        tmp = c + ".gravando"
        with open(tmp, "wb") as f:
            f.write(dados)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, c)


def criar_pasta(cfg, app, rel):
    os.makedirs(caminho(cfg, app, rel), exist_ok=True)


def apagar(cfg, app, rel):
    c = caminho(cfg, app, rel)
    if not os.path.isfile(c):
        raise ErroGestao("não encontrado", 404)
    if c.lower().endswith(".json") and os.path.dirname(c) == os.path.abspath(os.path.join(pasta_base(cfg), SUBPASTAS[app])):
        raise ErroGestao("os arquivos de dados não podem ser apagados por aqui", 403)
    with TRAVA:
        os.remove(c)


def listar(cfg, app, rel):
    c = caminho(cfg, app, rel)
    if not os.path.isdir(c):
        return []
    out = []
    for n in sorted(os.listdir(c)):
        if n.endswith(".gravando"):
            continue
        p = os.path.join(c, n)
        out.append({"nome": n, "pasta": os.path.isdir(p)})
    return out


def _json_do_app(cfg, k):
    try:
        b, _ = ler(cfg, PASTA_DO_ARQ[k], ARQUIVOS[k])
        return json.loads(b.decode("utf-8"))
    except (ErroGestao, ValueError, OSError):
        return None


def dados(cfg):
    """Os quatro arquivos de dados, para o navegador começar já com a versão mais nova."""
    out = {k: _json_do_app(cfg, k) for k in ARQUIVOS}
    g = (cfg or {}).get("gestao") or {}
    out["cfg"] = {"turno_faturamento": g.get("turno_faturamento") or "noite",
                  "faturamento_automatico": (g.get("faturamento_automatico") or "sim").lower() not in ("nao", "não", "n", "0")}
    return out


# ------------------------------------------------------------------ vendas por dia (para o faturamento)

def vendas_por_dia(db, cfg, dias=120, parse_dt=None, turno_de=None):
    """Soma dos pedidos concluídos por dia de operação, só do turno configurado (padrão: noite = pizzaria).
    Um turno que passa da meia-noite conta no dia em que começou."""
    g = (cfg or {}).get("gestao") or {}
    alvo = (g.get("turno_faturamento") or "noite").strip().lower()
    turnos = cfg["turnos"]
    ti = next((i for i, t in enumerate(turnos) if t["id"] == alvo or t["nome"].lower() == alvo), None)
    desde = (datetime.date.today() - datetime.timedelta(days=int(dias) + 1)).isoformat()
    out = {}
    for status, created, det in db.execute(
            "SELECT status, created_at, detalhe_json FROM pedidos WHERE created_at >= ? AND status = 'closed'", (desde,)):
        dt = parse_dt(created)
        if dt is None or not det:
            continue
        i = turno_de(dt, turnos)
        if ti is not None and i != ti:
            continue
        dia = dt.date()
        if ti is not None:
            t = turnos[ti]
            if t["ini"] > t["fim"] and dt.hour * 60 + dt.minute < t["fim"]:
                dia -= datetime.timedelta(days=1)
        try:
            total = float(json.loads(det).get("total") or 0)
        except (ValueError, TypeError):
            continue
        x = out.setdefault(dia.isoformat(), {"valor": 0.0, "pedidos": 0})
        x["valor"] += total
        x["pedidos"] += 1
    for x in out.values():
        x["valor"] = round(x["valor"], 2)
    return {"turno": turnos[ti]["nome"] if ti is not None else "todos", "dias": out,
            "hoje": datetime.date.today().isoformat()}


# ------------------------------------------------------------------ páginas

ABAS_VENDAS = [["hoje", "Hoje"], ["reservas", "Reservas"], ["promocoes", "Promoções"], ["envios", "Envios"], ["listas", "Listas"], ["lembretes", "Lembretes"],
               ["resultados", "Resultados"],
               ["geral", "Visão geral"], ["clientes", "Clientes"], ["produtos", "Produtos"], ["bairros", "Bairros"]]
_CACHE = {}


def _arquivo(n):
    p = os.path.join(PASTA, n)
    m = os.path.getmtime(p)
    c = _CACHE.get(n)
    if not c or c[0] != m:
        with open(p, encoding="utf-8") as f:
            c = (m, f.read())
        _CACHE[n] = c
    return c[1]


def pagina():
    """A casca do Gestão com os apps dentro, mais o app de Vendas e clientes (o painel)."""
    casca = _arquivo("gestao_casca.html")
    apps = {"vendas": {"nome": "Vendas e clientes", "abas": ABAS_VENDAS, "src": "/painel"}}
    for k, nome in (("painel", None), ("despesas", None), ("fichas", None), ("equipe", None)):
        apps[k] = {"html": _arquivo("gestao_%s.html" % k)}
    meta = json.loads(_arquivo("gestao_apps.txt"))
    for k, v in meta.items():
        apps[k].update(v)
    js = json.dumps(apps, ensure_ascii=False).replace("</", "<\\/")
    return casca.replace("const APPS=__APPS__;", "const APPS=" + js + ";", 1)


# ------------------------------------------------------------------ para a IA

def assinatura(cfg):
    out = []
    for k in ("despesas", "fichas", "equipe"):
        try:
            out.append(int(os.path.getmtime(caminho(cfg, PASTA_DO_ARQ[k], ARQUIVOS[k]))))
        except (OSError, ErroGestao):
            out.append(0)
    return ",".join(map(str, out))


def tabelas_ia(cfg):
    """Linhas para a base de análise da IA (sem CPF, telefone, PIX)."""
    d = {k: ((_json_do_app(cfg, k) or {}).get("dados") or {}) for k in ("despesas", "fichas", "equipe")}
    t = {}
    de = d["despesas"]
    t["despesas"] = [(x.get("data"), x.get("descricao"), x.get("categoria"), x.get("valor"), 1 if x.get("pago") else 0,
                      x.get("vencimento") or None, x.get("pagamento"), x.get("obs") or None) for x in de.get("despesas") or []]
    t["faturamento_mensal_gestao"] = [(m, v, 1 if (de.get("fatAuto") or {}).get(m) else 0) for m, v in (de.get("faturamento") or {}).items()]
    t["desperdicio"] = [(x.get("data"), x.get("item"), x.get("qtd"), x.get("unidade"), x.get("custo"), x.get("motivo"), x.get("turno"))
                        for x in de.get("desperdicio") or []]
    t["orcamento"] = [(k, v) for k, v in (de.get("orcamento") or {}).items() if isinstance(v, (int, float))]
    fi = d["fichas"]
    t["insumos"] = [(x.get("cod"), x.get("nome"), x.get("pago"), x.get("vol"), x.get("ref"), x.get("un"),
                     round(x["pago"] / x["vol"] * (x.get("ref") or 1), 4) if x.get("pago") and x.get("vol") else None)
                    for x in fi.get("insumos") or []]
    ins = {x.get("cod"): x for x in fi.get("insumos") or []}
    t["fichas"] = [(x.get("id"), x.get("cod"), x.get("nome"), x.get("tipo"), x.get("categoria"), x.get("rend"), x.get("praticado"))
                   for x in fi.get("fichas") or []]
    t["fichas_itens"] = []
    for x in fi.get("fichas") or []:
        for it in x.get("itens") or []:
            t["fichas_itens"].append((x.get("id"), x.get("nome"), it.get("cod"), (ins.get(it.get("cod")) or {}).get("nome"),
                                      it.get("bruta"), it.get("liq"), it.get("ficha")))
    t["revenda"] = [(x.get("nome"), x.get("custo"), x.get("preco")) for x in fi.get("revenda") or []]
    t["premissas_fichas"] = [(k, json.dumps(v, ensure_ascii=False)) for k, v in (fi.get("prem") or {}).items()]
    eq = d["equipe"]
    t["funcionarios"] = [(x.get("id"), x.get("nome"), x.get("funcao"), x.get("vinculo"), x.get("admissao"), x.get("demissao") or None,
                          x.get("salario"), x.get("valorDia"), x.get("valorDiaria"), x.get("valorEntrega"))
                         for x in eq.get("funcionarios") or []]
    nomes = {x.get("id"): x.get("nome") for x in eq.get("funcionarios") or []}
    t["folha"] = []
    for mes, f in (eq.get("folhas") or {}).items():
        for fid, it in ((f or {}).get("itens") or {}).items():
            limpo = {k: v for k, v in (it or {}).items() if k not in ("cpf", "pix", "telefone")}
            t["folha"].append((mes, (f or {}).get("status"), fid, nomes.get(fid), json.dumps(limpo, ensure_ascii=False)[:4000]))
    return t


ESQUEMA_IA = {
    "despesas": "data TEXT, descricao TEXT, categoria TEXT, valor REAL, pago INTEGER, vencimento TEXT, pagamento TEXT, obs TEXT",
    "faturamento_mensal_gestao": "mes TEXT, valor REAL, soma_dos_dias INTEGER",
    "desperdicio": "data TEXT, item TEXT, qtd REAL, unidade TEXT, custo REAL, motivo TEXT, turno TEXT",
    "orcamento": "categoria TEXT, valor REAL",
    "insumos": "cod INTEGER, nome TEXT, preco_pago REAL, volume_compra REAL, ref REAL, unidade TEXT, preco_por_unidade REAL",
    "fichas": "id TEXT, cod INTEGER, nome TEXT, tipo TEXT, categoria TEXT, rendimento REAL, preco_praticado REAL",
    "fichas_itens": "ficha_id TEXT, ficha TEXT, insumo_cod INTEGER, insumo TEXT, qtd_bruta REAL, qtd_liquida REAL, sub_ficha TEXT",
    "revenda": "nome TEXT, custo REAL, preco REAL",
    "premissas_fichas": "chave TEXT, valor_json TEXT",
    "funcionarios": "id TEXT, nome TEXT, funcao TEXT, vinculo TEXT, admissao TEXT, demissao TEXT, salario REAL, valor_dia REAL, valor_diaria REAL, valor_entrega REAL",
    "folha": "mes TEXT, status TEXT, funcionario_id TEXT, funcionario TEXT, lancamentos_json TEXT",
}

DESCRICAO_IA = (
    "\nDADOS DO MARIÁ GESTÃO (lançados pelo dono; a pizzaria é o turno da noite, o almoço é o restaurante):\n"
    "- despesas(data, descricao, categoria, valor, pago, vencimento, pagamento, obs): despesas lançadas. "
    "CMV = categorias 'Ingredientes e alimentos' e 'Bebidas'.\n"
    "- faturamento_mensal_gestao(mes, valor, soma_dos_dias): faturamento da pizzaria usado no Gestão.\n"
    "- desperdicio(...), orcamento(categoria, valor).\n"
    "- insumos(cod, nome, preco_pago, volume_compra, ref, unidade, preco_por_unidade), fichas(id, cod, nome, tipo "
    "['preparo' = base usada em outras fichas, senão produto], categoria, rendimento, preco_praticado), "
    "fichas_itens(ficha_id, ficha, insumo_cod, insumo, qtd_bruta, qtd_liquida, sub_ficha), revenda(nome, custo, preco), "
    "premissas_fichas(chave, valor_json).\n"
    "- funcionarios(nome, funcao, vinculo, admissao, demissao, salario, ...), folha(mes, status, funcionario, lancamentos_json).\n"
    "Para custo exato de uma ficha, prefira o que o dono vê no app Fichas técnicas; aqui o cálculo é aproximado.\n")
