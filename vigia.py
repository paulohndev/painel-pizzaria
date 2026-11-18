# -*- coding: utf-8 -*-
"""
Vigia do painel: fica rodando em segundo plano (sem janela) desde que o Windows liga.

  - a cada minuto busca no Cardápio Web os pedidos novos ou alterados;
  - aos poucos completa os detalhes que faltam e, uma vez por dia, atualiza
    histórico, clientes e avaliações (o que antes era a atualização das 6h);
  - recalcula os alertas (alertas.py) e, quando aparece algo urgente, mostra uma
    notificação do Windows; clicando nela o painel abre direto no alerta;
  - serve o painel sempre atualizado em http://127.0.0.1:8766 (só neste computador);
  - confere se tem versão nova do programa e se reinicia sozinho quando atualiza.

    pythonw vigia.py          (normal: sem janela, grava dados/vigia.log)
    python vigia.py --janela  (mostra o que está fazendo, para testes)
"""
import base64
import json
import re
import os
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import webbrowser
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PASTA = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PASTA)
os.makedirs(os.path.join(PASTA, "dados"), exist_ok=True)

JANELA = "--janela" in sys.argv
if not JANELA or sys.stdout is None:
    _cam = os.path.join(PASTA, "dados", "vigia.log")
    try:
        if os.path.getsize(_cam) > 3_000_000:
            with open(_cam, "rb") as _f:
                _f.seek(-600_000, 2)
                _resto = _f.read()
            with open(_cam, "wb") as _f:
                _f.write(_resto)
    except OSError:
        pass
    _log = open(os.path.join(PASTA, "dados", "vigia.log"), "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stderr = _log

import pizzaria as P  # noqa: E402
import alertas  # noqa: E402
import ia_painel  # noqa: E402
import envios as E  # noqa: E402
from whatsapp import Whats, ErroWhats, numero_br  # noqa: E402
from ia import IA, ErroIA  # noqa: E402

PORTA = int(os.environ.get("PIZZARIA_PORTA", "8766"))
ENDERECO = "http://127.0.0.1:%d/" % PORTA
INTERVALO_POLLING = int(os.environ.get("PIZZARIA_POLL", "60"))
INTERVALO_RECALCULO = 4 * 60
LOTE_DETALHES = 120          # detalhes atrasados baixados por rodada (~1,5 min)
STATUS_ABERTO = alertas.STATUS_ABERTO


def log(msg):
    print("%s  %s" % (datetime.now().strftime("%d/%m %H:%M:%S"), msg), flush=True)


# ------------------------------------------------------------------ banco

ESQUEMA_VIGIA = """
CREATE TABLE IF NOT EXISTS alertas (
    id TEXT PRIMARY KEY, tipo TEXT, prio INTEGER, json TEXT,
    criado TEXT, status TEXT DEFAULT 'nova', notificado INTEGER DEFAULT 0, resolvido_em TEXT
);
CREATE TABLE IF NOT EXISTS contatos (cliente_id TEXT PRIMARY KEY, data TEXT, campanha TEXT);
"""


def conectar():
    db = P.abrir_banco("loja.db")
    db.executescript(ESQUEMA_VIGIA)
    return db


def contatos_do_banco(db):
    out = {}
    for cid, data, camp in db.execute("SELECT cliente_id, data, campanha FROM contatos"):
        out[cid] = {"d": data, "c": camp}
    return out


# ------------------------------------------------------------------ notificação do Windows

APP_ID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"


def _xml(t):
    return (t or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def notificar(titulo, texto, url):
    if os.name != "nt":
        log("[notificação] %s | %s | %s" % (titulo, texto, url))
        return
    xml = ('<toast activationType="protocol" launch="%s" duration="long"><visual><binding template="ToastGeneric">'
           '<text>%s</text><text>%s</text><text placement="attribution">Painel Mariá</text></binding></visual>'
           '<actions><action content="Ver agora" activationType="protocol" arguments="%s"/></actions>'
           '<audio src="ms-winsoundevent:Notification.Default"/></toast>') % (_xml(url), _xml(titulo), _xml(texto[:180]), _xml(url))
    ps = ("[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null;"
          "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null;"
          "$x = New-Object Windows.Data.Xml.Dom.XmlDocument; $x.LoadXml('%s');"
          "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('%s').Show([Windows.UI.Notifications.ToastNotification]::new($x))"
          ) % (xml.replace("'", "''"), APP_ID)
    cod = base64.b64encode(ps.encode("utf-16-le")).decode("ascii")
    try:
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-EncodedCommand", cod],
                       creationflags=0x08000000, timeout=30, capture_output=True)
    except Exception as e:
        log("não consegui mostrar a notificação: %s" % e)


# ------------------------------------------------------------------ estado compartilhado com o servidor

ESTADO = {"html": None, "versao": 0, "alertas": [], "gerado_em": "", "D": None, "por_chave": {}, "trava": threading.Lock()}


class Vigia:
    def __init__(self, cfg, base, chave):
        self.cfg = cfg
        self.ia = IA(cfg)
        ESTADO["ia"] = self.ia
        self.api = P.API(base, chave)
        ESTADO["api_ia"] = P.API(base, chave)
        self.db = conectar()
        E.preparar(self.db)
        ESTADO["wh"] = Whats(cfg)
        self.nome_loja = P.meta_get(self.db, "nome_loja") or ""
        self.mudou = True
        self.prox = {"polling": 0, "recalculo": 0, "avaliacoes": time.time() + 120, "backlog": time.time() + 30,
                     "diario": time.time() + 60, "versao": time.time() + 3600, "loja": time.time() + 90}
        self.ultimo_toast = 0
        self.toasts_prio2_hoje = (datetime.now().date(), 0)

    # ---------- trava compartilhada com o "Atualizar painel" manual
    def trava_alheia(self):
        caminho = os.path.join(P.PASTA_DADOS, "rodando_loja.db.lock")
        if P.TRAVA["caminho"] or not os.path.exists(caminho):
            return False
        try:
            return time.time() - os.path.getmtime(caminho) < 20 * 60
        except OSError:
            return False

    def com_trava(self, func, *a):
        caminho = os.path.join(P.PASTA_DADOS, "rodando_loja.db.lock")
        P.TRAVA["caminho"], P.TRAVA["toque"] = caminho, 0
        P.tocar_trava()
        try:
            return func(*a)
        finally:
            P.TRAVA["caminho"] = None
            try:
                os.remove(caminho)
            except OSError:
                pass

    # ---------- pedidos novos (polling)
    def polling(self):
        desde = P.meta_get(self.db, "polling_desde")
        agora = datetime.now()
        params = {}
        if desde:
            d = max(P.parse_dt(desde) - timedelta(minutes=3), agora - timedelta(hours=7, minutes=50))
            params["updated_since"] = d.strftime("%Y-%m-%dT%H:%M:%S-03:00")
        resp = self.api.get("/orders", params)
        lista = resp if isinstance(resp, list) else (resp or {}).get("orders", [])
        novos = []
        for p in lista:
            cur = self.db.execute("SELECT status FROM pedidos WHERE id=?", (p["id"],)).fetchone()
            if cur is None:
                self.db.execute("INSERT INTO pedidos (id,status,order_type,order_timing,sales_channel,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                                (p["id"], p.get("status"), p.get("order_type"), p.get("order_timing"), p.get("sales_channel"),
                                 p.get("created_at"), p.get("updated_at")))
                novos.append(p["id"])
            elif cur[0] != p.get("status"):
                self.db.execute("UPDATE pedidos SET status=?, updated_at=?, detalhe_json=NULL WHERE id=?",
                                (p.get("status"), p.get("updated_at"), p["id"]))
                novos.append(p["id"])
        self.db.commit()
        P.meta_set(self.db, "polling_desde", agora.strftime("%Y-%m-%dT%H:%M:%S"))
        for pid in novos[:80]:
            d = self.api.get("/orders/%d" % pid)
            self.db.execute("UPDATE pedidos SET detalhe_json=?, detalhe_em=? WHERE id=?",
                            (json.dumps(d, ensure_ascii=False) if d else "{}", datetime.now().isoformat(timespec="seconds"), pid))
        self.db.commit()
        if novos:
            log("polling: %d pedido(s) novo(s) ou alterado(s)" % len(novos))
            self.mudou = True

    # ---------- detalhes que ficaram para trás (aos poucos)
    def backlog(self):
        if self.trava_alheia():
            return
        ordem = " ".join("WHEN '%s' THEN %d" % (c, i) for i, c in enumerate(P.PRIORIDADE_CANAL))
        ids = [r[0] for r in self.db.execute(
            "SELECT id FROM pedidos WHERE detalhe_json IS NULL AND status='closed' "
            "ORDER BY CASE sales_channel %s ELSE 99 END, created_at DESC LIMIT %d" % (ordem, LOTE_DETALHES))]
        if not ids:
            return

        def baixar():
            for pid in ids:
                d = self.api.get("/orders/%d" % pid)
                self.db.execute("UPDATE pedidos SET detalhe_json=?, detalhe_em=? WHERE id=?",
                                (json.dumps(d, ensure_ascii=False) if d else "{}", datetime.now().isoformat(timespec="seconds"), pid))
            self.db.commit()
        self.com_trava(baixar)
        resta = self.db.execute("SELECT COUNT(*) FROM pedidos WHERE detalhe_json IS NULL AND status='closed'").fetchone()[0]
        log("detalhes atrasados: +%d (faltam %d)" % (len(ids), resta))
        self.mudou = True

    # ---------- uma vez por dia: histórico, clientes, avaliações
    def diario(self):
        hoje = datetime.now().strftime("%Y-%m-%d")
        if P.meta_get(self.db, "diario_em") == hoje or datetime.now().hour < 6 or self.trava_alheia():
            return
        log("atualização diária: histórico, clientes e avaliações")

        def rodar():
            P.sincronizar_historico(self.api, self.db, self.cfg["meses"])
            P.sincronizar_clientes(self.api, self.db)
            P.sincronizar_avaliacoes(self.api, self.db, self.cfg["meses"])
        self.com_trava(rodar)
        P.meta_set(self.db, "diario_em", hoje)
        self.mudou = True

    def loja(self):
        """Cadastro da loja, formas de pagamento, cupons e cardápio (1x por dia)."""
        em = P.meta_get(self.db, "loja_em") or ""
        if em[:10] == datetime.now().strftime("%Y-%m-%d") or self.trava_alheia():
            return
        P.sincronizar_loja(self.api, self.db)
        log("dados da loja, cupons e cardápio atualizados")

    def avaliacoes(self):
        antes = self.db.execute("SELECT COUNT(*) FROM avaliacoes").fetchone()[0]
        P.sincronizar_avaliacoes(self.api, self.db, self.cfg["meses"])
        if self.db.execute("SELECT COUNT(*) FROM avaliacoes").fetchone()[0] != antes:
            self.mudou = True

    # ---------- recalcula painel e alertas
    def recalcular(self):
        D = P.montar_dados(self.db, self.cfg, self.nome_loja)
        cont = contatos_do_banco(self.db)
        cont_dt = {}
        for k, v in cont.items():
            try:
                cont_dt[k] = datetime.strptime(v["d"], "%Y-%m-%d")
            except (ValueError, TypeError):
                pass
        lista = alertas.gerar(D, cont_dt)
        agora_txt = datetime.now().strftime("%Y-%m-%d %H:%M")
        ativos = []
        for a in lista:
            row = self.db.execute("SELECT status, notificado, criado FROM alertas WHERE id=?", (a["id"],)).fetchone()
            if row is None:
                self.db.execute("INSERT INTO alertas (id,tipo,prio,json,criado) VALUES (?,?,?,?,?)",
                                (a["id"], a["tipo"], a["prio"], json.dumps(a, ensure_ascii=False), agora_txt))
                a["status"], a["notificado"], a["criado"] = "nova", 0, agora_txt
            else:
                a["status"], a["notificado"], a["criado"] = row[0], row[1], row[2]
                self.db.execute("UPDATE alertas SET json=? WHERE id=?", (json.dumps(a, ensure_ascii=False), a["id"]))
            if a["status"] == "nova":
                ativos.append(a)
        self.db.commit()
        D["alertas"] = ativos
        D["ao_vivo"] = True
        D["ia"] = {"ativa": self.ia.ativa, "nome": self.ia.nome()}
        regras = E.carregar_regras()
        D["envio"] = E.cadencia(D, self.db, regras)
        wh = ESTADO.get("wh")
        D["envio_cfg"] = {"ativo": bool(wh and wh.ativo), "nome": wh.nome() if wh else "", "regras": regras,
                          "perfis": [[k, n, x] for k, n, x in E.PERFIS]}
        html = P.html_painel(D)
        with ESTADO["trava"]:
            ESTADO["html"], ESTADO["alertas"], ESTADO["gerado_em"] = html, ativos, D["gerado_em"]
            ESTADO["D"] = D
            ESTADO["por_chave"] = {c.get("chave"): c.get("uniu_em", i) for i, c in enumerate(D["clientes"])}
            ESTADO["versao"] += 1
        try:
            P.gerar_painel(D, os.path.join(PASTA, "painel.html"))  # a versão em arquivo também fica atual
        except OSError:
            pass
        self.notificar_novos(ativos)

    def notificar_novos(self, ativos):
        c = self.cfg_alertas()
        if not c["ligado"]:
            return
        agora = datetime.now()
        m = agora.hour * 60 + agora.minute
        if not (c["ini"] <= m <= c["fim"]):
            return
        dia, qt2 = self.toasts_prio2_hoje
        if dia != agora.date():
            dia, qt2 = agora.date(), 0
        pend = []
        for a in ativos:
            if a["notificado"]:
                continue
            if a["prio"] == 1 or (a["prio"] == 2 and qt2 < 3):
                pend.append(a)
                if a["prio"] == 2:
                    qt2 += 1
        self.toasts_prio2_hoje = (dia, qt2)
        if not pend or time.time() - self.ultimo_toast < 45:
            return
        if len(pend) == 1:
            a = pend[0]
            notificar(a["titulo"], a["texto"], ENDERECO + "#alerta=" + urllib.parse.quote(a["id"]))
        else:
            vistos_n = []
            for a in pend:
                n = a["primeiro"] or "cliente"
                if n not in vistos_n:
                    vistos_n.append(n)
            nomes = ", ".join(vistos_n[:4])
            notificar("%d oportunidades agora" % len(pend), "%s%s Clique para ver." % (nomes, "…" if len(pend) > 4 else "."),
                      ENDERECO + "#alertas")
        self.ultimo_toast = time.time()
        for a in pend:
            self.db.execute("UPDATE alertas SET notificado=1 WHERE id=?", (a["id"],))
            a["notificado"] = 1
        self.db.commit()

    def cfg_alertas(self):
        import configparser
        cp = configparser.ConfigParser(interpolation=None)
        cp.read(os.path.join(PASTA, "config.ini"), encoding="utf-8")
        hor = cp.get("alertas", "horario", fallback="09:00-23:30")
        try:
            ini, fim = [P.hora_para_min(x) for x in hor.split("-")]
        except ValueError:
            ini, fim = 9 * 60, 23 * 60 + 30
        lig = cp.get("alertas", "notificacoes", fallback="sim").strip().lower() not in ("nao", "não", "0", "no")
        return {"ligado": lig, "ini": ini, "fim": fim}

    # ---------- versão nova do programa
    def checar_versao(self):
        try:
            import atualizador
            import configparser
            cp = configparser.ConfigParser(interpolation=None)
            cp.read(os.path.join(PASTA, "config.ini"), encoding="utf-8")
            if cp.get("atualizacao", "ativo", fallback="sim").strip().lower() in ("nao", "não", "0"):
                return False
            return atualizador.verificar(cp.get("atualizacao", "repositorio", fallback=""), log)
        except Exception as e:
            log("verificação de versão falhou: %s" % e)
            return False

    # ---------- laço principal
    def rodar(self):
        log("vigia ligado: %s" % ENDERECO)
        while True:
            agora = time.time()
            try:
                if agora >= self.prox["polling"]:
                    self.prox["polling"] = agora + INTERVALO_POLLING
                    self.polling()
                if agora >= self.prox["avaliacoes"]:
                    self.prox["avaliacoes"] = agora + 15 * 60
                    self.avaliacoes()
                if agora >= self.prox["loja"]:
                    self.prox["loja"] = agora + 3 * 3600
                    self.loja()
                if agora >= self.prox["diario"]:
                    self.prox["diario"] = agora + 15 * 60
                    self.diario()
                if agora >= self.prox["backlog"]:
                    self.prox["backlog"] = agora + 20
                    self.backlog()
                if self.mudou or agora >= self.prox["recalculo"]:
                    self.mudou = False
                    self.prox["recalculo"] = agora + INTERVALO_RECALCULO
                    self.recalcular()
                if agora >= self.prox["versao"]:
                    self.prox["versao"] = agora + 3 * 3600
                    if self.checar_versao():
                        log("programa atualizado: reiniciando o vigia")
                        reiniciar()
            except P.ErroAPI as e:
                log("API: %s" % e)
                self.prox["polling"] = time.time() + 180
            except Exception:
                log("erro inesperado:\n" + traceback.format_exc())
                time.sleep(30)
            time.sleep(3)


def reiniciar():
    try:
        SERVIDOR["s"].shutdown()
        SERVIDOR["s"].server_close()
    except Exception:
        pass
    flags = 0x00000008 | 0x08000000 if os.name == "nt" else 0  # DETACHED_PROCESS | CREATE_NO_WINDOW
    subprocess.Popen([sys.executable, os.path.abspath(__file__)] + [a for a in sys.argv[1:]], cwd=PASTA, creationflags=flags)
    os._exit(0)


# ------------------------------------------------------------------ servidor local

SERVIDOR = {}


class Painel(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _host_ok(self):
        return (self.headers.get("Host") or "").split(":")[0] in ("127.0.0.1", "localhost")

    def _json(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if not self._host_ok():
            return self._json({"erro": "host"}, 403)
        u = urllib.parse.urlparse(self.path)
        if u.path in ("/", "/index.html"):
            with ESTADO["trava"]:
                html = ESTADO["html"]
            if html is None:
                html = ("<html><head><meta charset='utf-8'><meta http-equiv='refresh' content='4'></head>"
                        "<body style='font-family:sans-serif;padding:40px;background:#FBF7F2'>Preparando o painel… "
                        "a página atualiza sozinha.</body></html>")
            b = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(b)
        elif u.path == "/api/estado":
            with ESTADO["trava"]:
                self._json({"versao": ESTADO["versao"], "alertas": ESTADO["alertas"], "gerado_em": ESTADO["gerado_em"]})
        elif u.path == "/api/ia/resumo":  # só lê o que já foi gerado (gerar custa crédito: só por POST)
            return self._json(*ia_resumo(gerar=False))
        elif u.path == "/api/envios":
            db = conectar()
            try:
                return self._json(E.resumo(db, E.carregar_regras()))
            finally:
                db.close()
        elif u.path == "/api/lembretes":
            db = conectar()
            try:
                return self._json({"lembretes": E.lembretes(db, (urllib.parse.parse_qs(u.query).get("todos") or [""])[0] == "1")})
            finally:
                db.close()
        elif u.path == "/api/envios/conexao":
            wh = ESTADO.get("wh")
            ok, txt = wh.conectado() if wh else (False, "desligado")
            return self._json({"ok": ok, "texto": txt, "ativo": bool(wh and wh.ativo), "nome": wh.nome() if wh else ""})
        elif u.path == "/api/envios/cliente":
            q = urllib.parse.parse_qs(u.query)
            db = conectar()
            try:
                return self._json(E.historico_cliente(db, (q.get("id") or [""])[0], (q.get("chave") or [""])[0]))
            finally:
                db.close()
        elif u.path == "/api/cliente":
            chave = (urllib.parse.parse_qs(u.query).get("chave") or [""])[0]
            return self._json(perfil_cliente(chave))
        elif u.path == "/api/contatos":
            db = conectar()
            try:
                self._json(contatos_do_banco(db))
            finally:
                db.close()
        else:
            self._json({"erro": "não encontrado"}, 404)

    def do_POST(self):
        # só aceita chamadas do próprio painel (cabeçalho próprio exige mesma origem)
        if not self._host_ok() or self.headers.get("X-Painel") != "1":
            return self._json({"erro": "proibido"}, 403)
        n = int(self.headers.get("Content-Length") or 0)
        try:
            dado = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        except ValueError:
            return self._json({"erro": "json"}, 400)
        u = urllib.parse.urlparse(self.path)
        db = conectar()
        try:
            if u.path == "/api/contato":
                cid = str(dado.get("id"))
                if dado.get("on"):
                    db.execute("INSERT OR REPLACE INTO contatos VALUES (?,?,?)", (cid, datetime.now().strftime("%Y-%m-%d"), dado.get("camp") or ""))
                    agora = E.agora_txt()
                    db.execute("INSERT INTO envios (cliente_id, campanha, criado, agendado, enviado, status, via) VALUES (?,?,?,?,?,?,?)",
                               (cid, dado.get("camp") or "", agora, agora, agora, "enviado", "manual"))
                else:
                    db.execute("DELETE FROM contatos WHERE cliente_id=?", (cid,))
                    db.execute("DELETE FROM envios WHERE id = (SELECT id FROM envios WHERE cliente_id=? AND via='manual' AND enviado >= ? "
                               "ORDER BY enviado DESC LIMIT 1)", (cid, datetime.now().strftime("%Y-%m-%d")))
                db.commit()
                pedir_recalculo()
                return self._json({"ok": True})
            if u.path == "/api/alerta":
                aid, acao = dado.get("id"), dado.get("acao")
                st = {"feito": "feita", "ignorar": "ignorada"}.get(acao)
                if not st:
                    return self._json({"erro": "acao"}, 400)
                db.execute("UPDATE alertas SET status=?, resolvido_em=? WHERE id=?", (st, datetime.now().strftime("%Y-%m-%d %H:%M"), aid))
                if acao == "feito" and dado.get("cliente_id") is not None:
                    db.execute("INSERT OR REPLACE INTO contatos VALUES (?,?,?)",
                               (str(dado["cliente_id"]), datetime.now().strftime("%Y-%m-%d"), dado.get("camp") or "alerta"))
                db.commit()
                with ESTADO["trava"]:
                    ESTADO["alertas"] = [a for a in ESTADO["alertas"] if a["id"] != aid]
                    ESTADO["versao"] += 1
                return self._json({"ok": True})
            if u.path.startswith("/api/ia/"):
                return self._json(*ia_post(u.path, dado))
            if u.path == "/api/lembretes":
                if not E.lembrete_acao(db, dado):
                    return self._json({"erro": "acao"}, 400)
                return self._json({"lembretes": E.lembretes(db)})
            if u.path.startswith("/api/envios/"):
                return self._json(*envios_post(db, u.path, dado))
            if u.path == "/api/promocoes_casa":
                lista = dado.get("promos")
                if not isinstance(lista, list) or len(lista) > 30:
                    return self._json({"erro": "dados"}, 400)
                limpa = []
                for p in lista:
                    if not isinstance(p, dict) or not str(p.get("nome") or "").strip():
                        continue
                    txt = lambda k, n=300: str(p.get(k) or "").strip()[:n]
                    lst = lambda k: [str(x).strip()[:60] for x in (p.get(k) or []) if str(x).strip()][:20]
                    limpa.append({"id": txt("id", 40) or re.sub(r"\W+", "_", txt("nome").lower())[:40], "nome": txt("nome", 60),
                                  "dias": sorted({int(x) for x in (p.get("dias") or []) if str(x).isdigit() and 0 <= int(x) <= 6}),
                                  "itens": lst("itens"), "cupom": txt("cupom", 40).upper(), "so_proprio": bool(p.get("so_proprio")),
                                  "sabores": lst("sabores"), "oferta": txt("oferta"), "msg": txt("msg", 600)})
                P.salvar_promos(limpa)
                v = ESTADO.get("vigia")
                if v is not None:
                    v.mudou = True
                return self._json({"ok": True})
            if u.path == "/api/vinculo":
                app, princ, dec = str(dado.get("app") or ""), str(dado.get("principal") or ""), dado.get("decisao")
                if not app or dec not in ("sim", "nao"):
                    return self._json({"erro": "dados"}, 400)
                db.execute("INSERT OR REPLACE INTO vinculos VALUES (?,?,?,?)", (app, princ, dec, datetime.now().strftime("%Y-%m-%d %H:%M")))
                db.commit()
                v = ESTADO.get("vigia")
                if v is not None:
                    v.mudou = True
                return self._json({"ok": True})
            return self._json({"erro": "não encontrado"}, 404)
        finally:
            db.close()


# ------------------------------------------------------------------ envios pelo WhatsApp

def pedir_recalculo():
    v = ESTADO.get("vigia")
    if v is not None:
        v.mudou = True


def envios_post(db, caminho, dado):
    regras = E.carregar_regras()
    if caminho == "/api/envios/agendar":
        wh = ESTADO.get("wh")
        if not (wh and wh.ativo):
            return {"erro": "O envio automático ainda não está ligado (seção [whatsapp] do config.ini)."}, 200
        with ESTADO["trava"]:
            D, idx = ESTADO["D"], ESTADO["por_chave"]
        if D is None:
            return {"erro": "O painel ainda está carregando."}, 200
        cad = E.cadencia(D, db, regras)
        forcar = bool(dado.get("forcar"))
        itens, pulados = [], []
        for m in (dado.get("mensagens") or [])[:500]:
            cli = idx.get(str(m.get("chave") or ""))
            texto = str(m.get("texto") or "").strip()[:2000]
            if cli is None or not texto:
                continue
            c = D["clientes"][cli]
            st = cad.get(cli)
            if not st:
                pulados.append({"nome": c.get("nome"), "motivo": "sem telefone"})
                continue
            if st[2] in ("bloqueado", "na_fila", "so_app") or (st[2] in ("aguardar", "pausado") and not forcar):
                pulados.append({"nome": c.get("nome"), "motivo": st[4]})
                continue
            itens.append({"cliente_id": str(c.get("id")), "chave": c.get("chave"), "nome": c.get("nome"),
                          "numero": numero_br(c.get("tel"), c.get("ddi")), "campanha": str(m.get("campanha") or "")[:60],
                          "texto": texto, "ia": bool(dado.get("ia"))})
        if not itens:
            return {"agendados": 0, "pulados": pulados}, 200
        lote, res = E.agendar_ja(db, itens[0], regras) if dado.get("imediato") and len(itens) == 1 else E.agendar(db, itens, regras)
        log("envios: %d mensagem(ns) agendada(s) (lote %s), de %s até %s" % (len(res), lote, res[0][1].strftime("%d/%m %H:%M"),
                                                                            res[-1][1].strftime("%d/%m %H:%M")))
        pedir_recalculo()
        return {"agendados": len(res), "lote": lote, "inicio": E.agora_txt(min(t for _, t in res))[:16],
                "fim": E.agora_txt(max(t for _, t in res))[:16], "pulados": pulados}, 200
    if caminho == "/api/envios/acao":
        acao = dado.get("acao")
        if acao == "pausar":
            P.meta_set(db, "fila_pausada", "pausada por você")
        elif acao == "retomar":
            P.meta_set(db, "fila_pausada", "")
            n = E.reagendar_pendentes(db, regras)
            log("envios: fila retomada (%d pendente(s) reespaçada(s))" % n)
        elif acao == "cancelar":
            db.execute("UPDATE envios SET status='cancelado', erro='cancelado por você' WHERE id=? AND status='fila'", (int(dado.get("id") or 0),))
        elif acao == "cancelar_lote":
            db.execute("UPDATE envios SET status='cancelado', erro='cancelado por você' WHERE lote=? AND status='fila'", (str(dado.get("lote") or ""),))
        elif acao == "cancelar_tudo":
            db.execute("UPDATE envios SET status='cancelado', erro='cancelado por você' WHERE status='fila'")
        else:
            return {"erro": "acao"}, 400
        db.commit()
        pedir_recalculo()
        return E.resumo(db, regras), 200
    if caminho == "/api/envios/optout":
        cid = str(dado.get("id") or "")
        if not cid:
            return {"erro": "cliente"}, 400
        if dado.get("bloquear"):
            db.execute("INSERT OR REPLACE INTO optout VALUES (?,?,?)", (cid, E.agora_txt(), "marcado no painel"))
            db.execute("UPDATE envios SET status='cancelado', erro='cliente não quer receber' WHERE cliente_id=? AND status='fila'", (cid,))
        else:
            db.execute("DELETE FROM optout WHERE cliente_id=?", (cid,))
        db.commit()
        pedir_recalculo()
        return {"ok": True}, 200
    if caminho == "/api/envios/regras":
        novo = dado.get("regras") or {}
        r = dict(regras)
        for k in r:
            if k in novo:
                r[k] = novo[k]
        E.salvar_regras(r)
        r = E.carregar_regras()
        E.salvar_regras(r)
        n = E.reagendar_pendentes(db, r) if db.execute("SELECT 1 FROM envios WHERE status='fila' LIMIT 1").fetchone() else 0
        log("envios: regras de frequência atualizadas%s" % (" (%d na fila reespaçadas)" % n if n else ""))
        pedir_recalculo()
        return {"ok": True, "regras": r}, 200
    return {"erro": "não encontrado"}, 404


class Despachante(threading.Thread):
    """Manda as mensagens da fila, uma por vez, no horário agendado e respeitando os limites."""
    def __init__(self):
        super().__init__(daemon=True)
        self.erros_seguidos = 0
        self.avisado = ""

    def pausar(self, db, motivo):
        P.meta_set(db, "fila_pausada", motivo)
        db.commit()
        log("envios: fila pausada — %s" % motivo)
        if self.avisado != motivo:
            self.avisado = motivo
            notificar("Envios pausados", motivo, ENDERECO + "#envios")

    def run(self):
        time.sleep(20)
        while True:
            try:
                self.passo()
            except Exception:
                log("envios: erro inesperado:\n" + traceback.format_exc())
                time.sleep(60)
            time.sleep(15)

    def avisar_lembretes(self):
        agora = datetime.now()
        if agora.hour < 9 or time.time() - getattr(self, "ult_lemb", 0) < 600:
            return
        self.ult_lemb = time.time()
        db = conectar()
        try:
            rows = E.lembretes_a_avisar(db)
        finally:
            db.close()
        if rows:
            nomes = ", ".join(((r[1] or "").split() or ["cliente"])[0] for r in rows[:4]) + (" e mais %d" % (len(rows) - 4) if len(rows) > 4 else "")
            txt = (rows[0][2] or "")[:90] if len(rows) == 1 else "Clique para ver a lista."
            notificar("📌 %d lembrete%s para hoje" % (len(rows), "" if len(rows) == 1 else "s"), "%s. %s" % (nomes, txt), ENDERECO + "#lembretes")

    def passo(self):
        self.avisar_lembretes()
        wh = ESTADO.get("wh")
        if not (wh and wh.ativo):
            return
        db = conectar()
        try:
            if P.meta_get(db, "fila_pausada"):
                return
            agora = datetime.now()
            item = db.execute("SELECT id, cliente_id, chave, numero, campanha, texto, criado, ia FROM envios "
                              "WHERE status='fila' AND agendado <= ? ORDER BY agendado LIMIT 1", (E.agora_txt(agora),)).fetchone()
            if not item:
                return
            eid, cid, chave, numero, camp, texto, criado, usar_ia = item
            regras = E.carregar_regras()
            ini, fim = E.janela(regras)
            m = agora.hour * 60 + agora.minute
            ult = db.execute("SELECT MAX(enviado) FROM envios WHERE via='auto' AND status='enviado'").fetchone()[0]
            ult = E._dt(ult)
            hora = db.execute("SELECT COUNT(*) FROM envios WHERE via='auto' AND status='enviado' AND enviado >= ?",
                              (E.agora_txt(agora - timedelta(hours=1)),)).fetchone()[0]
            dia = db.execute("SELECT COUNT(*) FROM envios WHERE via='auto' AND status='enviado' AND enviado >= ?",
                             (agora.strftime("%Y-%m-%d"),)).fetchone()[0]
            if not (ini <= m < fim) or hora >= regras["por_hora"] or dia >= E.limite_dia(db, regras, agora) or \
                    (ult and agora - ult < timedelta(minutes=regras["intervalo_min"])):
                if (ult and agora - ult < timedelta(minutes=regras["intervalo_min"])):
                    return  # só espera um pouco
                n = E.reagendar_pendentes(db, regras)
                log("envios: fora do horário ou no limite; %d pendente(s) reagendada(s)" % n)
                return
            if db.execute("SELECT 1 FROM optout WHERE cliente_id=?", (str(cid),)).fetchone():
                db.execute("UPDATE envios SET status='cancelado', erro='cliente não quer receber' WHERE id=?", (eid,))
                db.commit()
                return
            with ESTADO["trava"]:
                D, idx = ESTADO["D"], ESTADO["por_chave"]
            cli = idx.get(chave) if chave else None
            if D is not None and cli is not None and camp != "avaliacao":
                ja = [p for p in D["pedidos"] if p[5] == cli and not p[7] and p[0] > (criado or "")[:16]]
                if ja:
                    db.execute("UPDATE envios SET status='cancelado', erro='fez um pedido antes do envio' WHERE id=?", (eid,))
                    db.commit()
                    log("envios: cancelada (cliente pediu antes do envio)")
                    return
            ia = ESTADO.get("ia")
            if usar_ia and ia is not None and ia.ativa and D is not None and cli is not None:
                try:
                    r = ia_painel.gerar_mensagem(ia, D, cli, perfil_cliente(chave), "campanha: %s" % (camp or "-"), texto, "")
                    if (r.get("texto") or "").strip():
                        texto = r["texto"].strip()
                except Exception as e:
                    log("envios: IA não personalizou (%s); vai a mensagem original" % str(e)[:120])
            try:
                mid = wh.enviar(numero, texto)
            except ErroWhats as e:
                if e.permanente:
                    self.pausar(db, str(e))
                    return
                self.erros_seguidos += 1
                db.execute("UPDATE envios SET status='erro', erro=?, enviado=? WHERE id=?", (str(e)[:300], E.agora_txt(), eid))
                db.commit()
                log("envios: erro ao enviar: %s" % e)
                if self.erros_seguidos >= 3:
                    self.pausar(db, "3 erros seguidos no envio. Último: %s" % str(e)[:150])
                return
            self.erros_seguidos, self.avisado = 0, ""
            agora = datetime.now()
            db.execute("UPDATE envios SET status='enviado', enviado=?, texto=?, msg_id=? WHERE id=?", (E.agora_txt(agora), texto, mid, eid))
            db.execute("INSERT OR REPLACE INTO contatos VALUES (?,?,?)", (str(cid), agora.strftime("%Y-%m-%d"), camp or "envio"))
            db.commit()
            log("envios: mensagem enviada (%s)" % (camp or "-"))
            pedir_recalculo()
        finally:
            db.close()


# ------------------------------------------------------------------ IA

def _ia():
    ia = ESTADO.get("ia")
    if ia is None or not ia.ativa:
        raise ErroIA("A IA ainda não está ligada. Abra o config.ini, preencha a seção [ia] (provedor e chave) e reinicie o vigia.")
    with ESTADO["trava"]:
        D = ESTADO["D"]
    if D is None:
        raise ErroIA("O painel ainda está carregando os dados. Tente de novo em instantes.")
    return ia, D


def ia_resumo(gerar=False, novo=False):
    hoje = datetime.now().strftime("%Y-%m-%d")
    db = conectar()
    try:
        try:
            cache = json.loads(P.meta_get(db, "ia_resumo") or "{}")
        except ValueError:
            cache = {}
        if cache.get("data") == hoje and cache.get("texto") and not novo:
            return cache, 200
        if not gerar:
            ia = ESTADO.get("ia")
            return {"vazio": True, "ativa": bool(ia and ia.ativa)}, 200
        try:
            ia, D = _ia()
            texto = ia_painel.resumo_dia(ia, D)
        except ErroIA as e:
            log("IA: resumo falhou: %s" % e)
            return {"erro": str(e)}, 200
        cache = {"data": hoje, "texto": texto, "em": datetime.now().strftime("%H:%M")}
        P.meta_set(db, "ia_resumo", json.dumps(cache, ensure_ascii=False))
        db.commit()
        log("IA: resumo do dia gerado")
        return cache, 200
    finally:
        db.close()


def ia_post(caminho, dado):
    try:
        ia, D = _ia()
        if caminho == "/api/ia/resumo":
            return ia_resumo(gerar=True, novo=bool(dado.get("novo")))
        if caminho == "/api/ia/mensagem":
            chave = str(dado.get("chave") or "")
            with ESTADO["trava"]:
                cli = ESTADO["por_chave"].get(chave)
            if cli is None:
                return {"erro": "Cliente não encontrado."}, 200
            perfil = perfil_cliente(chave)
            r = ia_painel.gerar_mensagem(ia, D, cli, perfil, str(dado.get("motivo") or "")[:600],
                                         str(dado.get("referencia") or "")[:800], str(dado.get("tipo") or ""))
            return r, 200
        if caminho == "/api/ia/pergunta":
            db = conectar()
            try:
                with ESTADO["trava"]:
                    versao = ESTADO["gerado_em"]
                liberar = str((ESTADO["vigia"].cfg.get("ia") or {}).get("dados_pessoais", "")).strip().lower() in ("sim", "s", "yes", "1")
                ia_painel.montar_analise(db, D, versao, liberar)
            finally:
                db.close()
            r = ia_painel.perguntar(ia, D, dado.get("mensagens") or [], api=ESTADO.get("api_ia"), liberar=liberar)
            log("IA: pergunta respondida (%d consulta(s))" % r["consultas"])
            return r, 200
        return {"erro": "não encontrado"}, 404
    except ErroIA as e:
        log("IA: %s" % e)
        return {"erro": str(e)}, 200
    except Exception as e:  # nunca derrubar o vigia por causa da IA
        log("IA: erro inesperado: %s" % e)
        return {"erro": "Erro inesperado na IA: %s" % e}, 200


STATUS_PT = {"closed": "Concluído", "canceled": "Cancelado", "confirmed": "Em preparo", "waiting_confirmation": "Aguardando",
             "ready": "Pronto", "released": "Saiu p/ entrega", "delivered": "Entregue", "scheduled_confirmed": "Agendado",
             "pending_payment": "Pagamento pendente", "pending_online_payment": "Pagamento pendente", "waiting_to_catch": "Aguardando retirada"}
PAGTO_PT = {"money": "Dinheiro", "pix": "Pix", "credit_card": "Crédito", "debit_card": "Débito", "online_credit_card": "Crédito online",
            "voucher": "Vale-refeição", "meal_voucher": "Vale-refeição", "online_pix": "Pix online"}


def perfil_cliente(chave):
    with ESTADO["trava"]:
        D, idx = ESTADO["D"], ESTADO["por_chave"]
    if D is None or chave not in idx:
        return {"erro": "cliente não encontrado"}
    cli = idx[chave]
    c = D["clientes"][cli]
    pids = [p[11] for p in D["pedidos"] if p[5] == cli and len(p) > 11]
    db = conectar()
    try:
        cad = None
        if c.get("id") is not None:
            r = db.execute("SELECT nome, telefone, email, nascimento, cadastro, pontos, cashback FROM clientes WHERE id=?", (c["id"],)).fetchone()
            if r:
                cad = {"nome": r[0], "telefone": r[1], "email": r[2], "nascimento": r[3], "cadastro": (r[4] or "")[:10], "pontos": r[5], "cashback": r[6]}
        pedidos, enderecos = [], {}
        for i in range(0, len(pids), 400):
            lote = pids[i:i + 400]
            q = "SELECT id, status, sales_channel, order_type, created_at, detalhe_json FROM pedidos WHERE id IN (%s)" % ",".join("?" * len(lote))
            for pid, st, canal, tipo, criado, det in db.execute(q, lote):
                d = json.loads(det) if det else {}
                itens = []
                for it in d.get("items") or []:
                    ops = [o.get("name") for o in (it.get("options") or []) if o.get("name")]
                    itens.append({"n": (it.get("name") or "").strip(), "q": it.get("quantity") or 1, "v": it.get("total_price"),
                                  "o": ops[:8], "obs": it.get("observation")})
                end = d.get("delivery_address") or {}
                if end.get("street") or end.get("neighborhood"):
                    k = "%s, %s · %s" % (end.get("street") or "", end.get("number") or "s/n", end.get("neighborhood") or "")
                    e = enderecos.setdefault(k, {"texto": k, "complemento": end.get("complement") or "", "referencia": end.get("reference") or "",
                                                 "cidade": end.get("city") or "", "vezes": 0, "ultimo": ""})
                    e["vezes"] += 1
                    e["ultimo"] = max(e["ultimo"], (criado or "")[:10])
                end_p = {"texto": k, "complemento": end.get("complement") or "", "referencia": end.get("reference") or ""} if (end.get("street") or end.get("neighborhood")) else None
                pag = [PAGTO_PT.get(x.get("payment_method"), x.get("payment_method") or "") for x in (d.get("payments") or [])]
                cup = next((x.get("coupon_code") or x.get("coupon_name") for x in (d.get("discounts") or []) if x.get("coupon_code") or x.get("coupon_name")), None)
                pedidos.append({"id": pid, "num": d.get("display_id"), "data": (criado or "")[:16].replace("T", " "),
                                "status": STATUS_PT.get(st, st), "cancelado": st == "canceled", "canal": P.CANAIS.get(canal, canal),
                                "tipo": P.TIPOS.get(tipo, tipo), "total": d.get("total"), "taxa": d.get("delivery_fee"),
                                "desconto": sum((x.get("total") or 0) for x in (d.get("discounts") or [])), "cupom": cup,
                                "pagamento": ", ".join(x for x in pag if x), "obs": d.get("observation"), "itens": itens, "end": end_p})
        pedidos.sort(key=lambda x: x["data"], reverse=True)
        av = [{"data": a[0], "nota": a[2], "comentario": a[3], "baixas": a[4]} for a in D.get("avaliacoes") or [] if a[1] == cli]
        cont = db.execute("SELECT data, campanha FROM contatos WHERE cliente_id=?", (str(c.get("id")),)).fetchone()
    finally:
        db.close()
    return {"cliente": c, "cadastro": cad, "pedidos": pedidos, "enderecos": sorted(enderecos.values(), key=lambda e: -e["vezes"]),
            "avaliacoes": av, "contato": {"data": cont[0], "campanha": cont[1]} if cont else None}


def porta_em_uso():
    s = socket.socket()
    s.settimeout(1)
    try:
        return s.connect_ex(("127.0.0.1", PORTA)) == 0
    finally:
        s.close()


def main():
    if porta_em_uso():
        # já tem um vigia rodando: só abre o painel
        if "--abrir" in sys.argv:
            webbrowser.open(ENDERECO)
        return 0
    cfg = P.carregar_config()
    teste = "--teste" in sys.argv
    base = os.environ.get("PIZZARIA_URL") or (P.URLS["sandbox"] if teste else P.URLS["producao"])
    chave = P.CHAVE_TESTE if teste else cfg["chave"]
    if not chave:
        log("Falta a chave da API no config.ini. O vigia não pode começar.")
        return 2
    srv = ThreadingHTTPServer(("127.0.0.1", PORTA), Painel)
    SERVIDOR["s"] = srv
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    v = Vigia(cfg, base, chave)
    ESTADO["vigia"] = v
    Despachante().start()
    if "--abrir" in sys.argv:
        threading.Timer(3, lambda: webbrowser.open(ENDERECO)).start()
    v.rodar()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        log("vigia parou com erro:\n" + traceback.format_exc())
        raise
