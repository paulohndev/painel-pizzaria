"""Envio de mensagens pelo WhatsApp por um serviço intermediário (Z-API ou Evolution API).

Estes serviços conectam o WhatsApp da loja como "aparelho conectado" (igual ao WhatsApp Web).
Não são a API oficial da Meta: o WhatsApp pode restringir números que mandam muitas mensagens
para quem não espera recebê-las. Por isso o painel envia devagar, em horário comercial e só para
clientes dentro do limite de frequência.
"""
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

URL_ZAPI = os.environ.get("PIZZARIA_ZAPI_URL", "https://api.z-api.io")


class ErroWhats(Exception):
    def __init__(self, texto, permanente=False):
        super().__init__(texto)
        self.permanente = permanente  # True = problema de configuração/conexão: pausa a fila


def provedor_de(t):
    t = (t or "").strip().lower().replace("-", "").replace(" ", "")
    if t in ("zapi", "z"):
        return "zapi"
    if t in ("evolution", "evolutionapi", "evo"):
        return "evolution"
    return ""


def numero_br(tel, ddi="55"):
    d = re.sub(r"\D", "", tel or "")
    if not d:
        return ""
    ddi = re.sub(r"\D", "", ddi or "55") or "55"
    if d.startswith(ddi) and len(d) > 11:
        return d
    return ddi + d


class Whats:
    def __init__(self, cfg):
        w = cfg.get("whatsapp") or {}
        self.provedor = provedor_de(w.get("provedor"))
        self.instancia = (w.get("instancia") or "").strip()
        self.token = (w.get("token") or "").strip()
        self.client_token = (w.get("client_token") or "").strip()
        self.url = (w.get("url") or "").strip().rstrip("/")
        self.apikey = (w.get("apikey") or "").strip()

    @property
    def ativo(self):
        if self.provedor == "zapi":
            return bool(self.instancia and self.token)
        if self.provedor == "evolution":
            return bool(self.url and self.instancia and self.apikey)
        return False

    def nome(self):
        return {"zapi": "Z-API", "evolution": "Evolution API"}.get(self.provedor, "")

    # ------------------------------------------------------------ HTTP
    def _req(self, metodo, url, corpo=None, cab=None):
        dados = json.dumps(corpo).encode("utf-8") if corpo is not None else None
        req = urllib.request.Request(url, data=dados, method=metodo,
                                     headers=dict({"Content-Type": "application/json", "Accept": "application/json"}, **(cab or {})))
        try:
            with urllib.request.urlopen(req, timeout=40) as r:
                txt = r.read().decode("utf-8", "replace")
                return json.loads(txt) if txt.strip() else {}
        except urllib.error.HTTPError as e:
            try:
                det = e.read().decode("utf-8", "replace")[:300]
            except Exception:
                det = ""
            if e.code in (401, 403):
                raise ErroWhats("O serviço de WhatsApp recusou o acesso (erro %s). Confira os dados da seção [whatsapp] do config.ini." % e.code, True)
            if e.code == 404:
                raise ErroWhats("Instância não encontrada no serviço de WhatsApp (erro 404). Confira a instância no config.ini.", True)
            raise ErroWhats("O serviço de WhatsApp respondeu com erro %s. %s" % (e.code, det), e.code >= 500)
        except (urllib.error.URLError, OSError) as e:
            raise ErroWhats("Sem conexão com o serviço de WhatsApp (%s)." % getattr(e, "reason", e), True)

    def _zapi(self, caminho):
        return "%s/instances/%s/token/%s/%s" % (URL_ZAPI, urllib.parse.quote(self.instancia), urllib.parse.quote(self.token), caminho)

    def _cab_zapi(self):
        return {"Client-Token": self.client_token} if self.client_token else {}

    # ------------------------------------------------------------ operações
    def conectado(self):
        """(True/False, texto) — se o WhatsApp está conectado no serviço."""
        if not self.ativo:
            return False, "Envio automático desligado (seção [whatsapp] do config.ini vazia)."
        try:
            if self.provedor == "zapi":
                r = self._req("GET", self._zapi("status"), cab=self._cab_zapi())
                ok = bool(r.get("connected"))
                return ok, "Conectado" if ok else ("WhatsApp desconectado na Z-API: %s" % (r.get("error") or "abra o painel da Z-API e leia o QR code de novo."))
            r = self._req("GET", "%s/instance/connectionState/%s" % (self.url, urllib.parse.quote(self.instancia)), cab={"apikey": self.apikey})
            st = ((r.get("instance") or {}).get("state") or r.get("state") or "").lower()
            ok = st == "open"
            return ok, "Conectado" if ok else "WhatsApp desconectado na Evolution API (estado: %s)." % (st or "?")
        except ErroWhats as e:
            return False, str(e)

    def enviar(self, numero, texto):
        """Envia texto. Devolve o id da mensagem (ou "")."""
        if not self.ativo:
            raise ErroWhats("Envio automático desligado.", True)
        if not numero:
            raise ErroWhats("Cliente sem telefone.")
        if self.provedor == "zapi":
            r = self._req("POST", self._zapi("send-text"), {"phone": numero, "message": texto}, self._cab_zapi())
            if r.get("error"):
                raise ErroWhats("Z-API: %s" % r.get("error"))
            return str(r.get("messageId") or r.get("zaapId") or r.get("id") or "")
        r = self._req("POST", "%s/message/sendText/%s" % (self.url, urllib.parse.quote(self.instancia)),
                      {"number": numero, "text": texto}, {"apikey": self.apikey})
        return str(((r.get("key") or {}).get("id")) or "")
