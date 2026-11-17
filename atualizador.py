# -*- coding: utf-8 -*-
"""
Atualização automática do programa.

Toda vez que o painel roda, este módulo olha o arquivo versao.json no
repositório do GitHub configurado. Se tiver versão nova, baixa só os arquivos
do programa listados lá, confere cada um pelo código SHA-256 e troca.

NUNCA mexe no config.ini (onde fica a chave) nem na pasta "dados".
Se não tiver internet ou der qualquer erro, segue com a versão atual.
"""
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

PASTA = os.path.dirname(os.path.abspath(__file__))

# Repositório padrão (usuario/repositorio). Pode ser trocado no config.ini, seção [atualizacao].
REPO_PADRAO = "paulohndev/painel-pizzaria"
RAMO = "main"

# Só estes tipos de arquivo, só na pasta principal, e nunca o config.ini
PERMITIDO = re.compile(r"^[\w\-() .]+\.(py|html|bat|command|txt|ps1)$", re.UNICODE)
PROIBIDO = {"config.ini", "painel.html", "painel_teste.html"}


def _versao_tupla(v):
    return tuple(int(x) for x in re.findall(r"\d+", str(v or "0")))


def versao_local():
    try:
        with open(os.path.join(PASTA, "versao.json"), encoding="utf-8") as f:
            return json.load(f).get("versao", "0")
    except (OSError, ValueError):
        return "0"


def _baixar(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "painel-pizzaria-atualizador", "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def base_url(repo):
    custom = os.environ.get("PIZZARIA_UPDATE_URL")  # só para testes
    if custom:
        return custom.rstrip("/")
    return "https://raw.githubusercontent.com/%s/%s" % (repo, RAMO)


def verificar(repo, log=print):
    """Retorna True se atualizou algum arquivo (o programa deve reiniciar)."""
    repo = (repo or REPO_PADRAO or "").strip().strip("/")
    if not repo and not os.environ.get("PIZZARIA_UPDATE_URL"):
        return False
    base = base_url(repo)
    try:
        manifesto = json.loads(_baixar("%s/versao.json?t=%d" % (base, int(time.time())), timeout=10).decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code != 404:  # 404 = ainda não tem versão publicada: segue em silêncio
            log("(não consegui verificar atualização: %s)" % e)
        return False
    except Exception as e:  # sem internet, repositório fora do ar etc.
        log("(não consegui verificar atualização: %s)" % e)
        return False

    nova, atual = manifesto.get("versao", "0"), versao_local()
    if _versao_tupla(nova) <= _versao_tupla(atual):
        return False

    arquivos = manifesto.get("arquivos") or {}
    baixados = {}
    for nome, sha in arquivos.items():
        if nome.lower() in PROIBIDO or not PERMITIDO.match(nome) or "/" in nome or "\\" in nome or ".." in nome:
            log("(atualização ignorou um arquivo não permitido: %s)" % nome)
            continue
        caminho = os.path.join(PASTA, nome)
        try:
            with open(caminho, "rb") as f:
                if hashlib.sha256(f.read()).hexdigest() == sha:
                    continue  # já está igual
        except OSError:
            pass
        try:
            dado = _baixar("%s/%s?t=%d" % (base, urllib.parse.quote(nome), int(time.time())))
        except Exception as e:
            log("(atualização cancelada: não consegui baixar %s: %s)" % (nome, e))
            return False
        if hashlib.sha256(dado).hexdigest() != sha:
            log("(atualização cancelada: %s veio diferente do esperado; tento de novo na próxima vez)" % nome)
            return False
        baixados[nome] = dado

    # só troca depois que TUDO baixou certo
    for nome, dado in baixados.items():
        caminho = os.path.join(PASTA, nome)
        tmp = caminho + ".novo"
        with open(tmp, "wb") as f:
            f.write(dado)
        os.replace(tmp, caminho)
    with open(os.path.join(PASTA, "versao.json"), "w", encoding="utf-8") as f:
        json.dump({"versao": nova, "atualizado_em": time.strftime("%Y-%m-%d %H:%M"),
                   "notas": manifesto.get("notas", "")}, f, ensure_ascii=False, indent=1)
    log("Programa atualizado da versão %s para a %s." % (atual, nova))
    if manifesto.get("notas"):
        log("Novidades: %s" % manifesto["notas"])
    return bool(baixados) or True
