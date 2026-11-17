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

ESTADO = {"html": None, "versao": 0, "alertas": [], "gerado_em": "", "trava": threading.Lock()}


class Vigia:
    def __init__(self, cfg, base, chave):
        self.cfg = cfg
        self.api = P.API(base, chave)
        self.db = conectar()
        self.nome_loja = P.meta_get(self.db, "nome_loja") or ""
        self.mudou = True
        self.prox = {"polling": 0, "recalculo": 0, "avaliacoes": time.time() + 120, "backlog": time.time() + 30,
                     "diario": time.time() + 60, "versao": time.time() + 3600}
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
        html = P.html_painel(D)
        with ESTADO["trava"]:
            ESTADO["html"], ESTADO["alertas"], ESTADO["gerado_em"] = html, ativos, D["gerado_em"]
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
                else:
                    db.execute("DELETE FROM contatos WHERE cliente_id=?", (cid,))
                db.commit()
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
            return self._json({"erro": "não encontrado"}, 404)
        finally:
            db.close()


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
    if "--abrir" in sys.argv:
        threading.Timer(3, lambda: webbrowser.open(ENDERECO)).start()
    v.rodar()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        log("vigia parou com erro:\n" + traceback.format_exc())
        raise
