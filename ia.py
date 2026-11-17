"""Inteligência artificial do painel (ChatGPT ou Claude, à escolha no config.ini).

Só usa a biblioteca padrão do Python. Nunca manda telefone nem endereço de cliente para a IA.
"""
import json
import os
import urllib.error
import urllib.request

URL_CLAUDE = os.environ.get("PIZZARIA_IA_URL_CLAUDE", "https://api.anthropic.com/v1/messages")
URL_OPENAI = os.environ.get("PIZZARIA_IA_URL_OPENAI", "https://api.openai.com/v1/chat/completions")

MODELOS = {
    "claude": ("claude-haiku-5-5", "claude-sonnet-5-5"),
    "openai": ("gpt-6-luna", "gpt-6-luna"),
}


class ErroIA(Exception):
    def __init__(self, texto, codigo=0, detalhe=""):
        super().__init__(texto)
        self.codigo, self.detalhe = codigo, detalhe


def provedor_de(texto):
    t = (texto or "").strip().lower()
    if t in ("openai", "chatgpt", "gpt"):
        return "openai"
    if t in ("claude", "anthropic"):
        return "claude"
    return ""


class IA:
    def __init__(self, cfg):
        ia = cfg.get("ia") or {}
        self.provedor = provedor_de(ia.get("provedor"))
        self.chave = (ia.get("chave") or "").strip()
        if not self.provedor and self.chave:
            self.provedor = "claude" if self.chave.startswith("sk-ant-") else "openai"
        self.desligar_raciocinio = True  # respostas diretas (sem "pensar" antes), mais rápidas e baratas
        self.ultimo_diag = ""
        rapido, forte = MODELOS.get(self.provedor, ("", ""))
        self.modelo = (ia.get("modelo") or "").strip() or rapido
        self.modelo_forte = (ia.get("modelo_forte") or "").strip() or forte

    @property
    def ativa(self):
        return bool(self.provedor and self.chave)

    def nome(self):
        return {"openai": "ChatGPT", "claude": "Claude"}.get(self.provedor, "")

    # ------------------------------------------------------------ HTTP
    def _post(self, url, corpo, cab):
        req = urllib.request.Request(url, data=json.dumps(corpo).encode("utf-8"), method="POST",
                                     headers=dict({"Content-Type": "application/json"}, **cab))
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read().decode("utf-8")).get("error") or {}
                msg = msg.get("message") if isinstance(msg, dict) else str(msg)
            except Exception:
                msg = ""
            if e.code in (401, 403):
                raise ErroIA("A chave da IA foi recusada. Confira a linha 'chave' na seção [ia] do config.ini.")
            if e.code == 429:
                raise ErroIA("A IA está sem crédito ou recebendo pedidos demais. Confira o saldo da conta e tente de novo.")
            raise ErroIA("A IA respondeu com erro %s. %s" % (e.code, msg or ""), e.code, msg or "")
        except (urllib.error.URLError, OSError) as e:
            raise ErroIA("Sem conexão com a IA (%s)." % getattr(e, "reason", e))

    # ------------------------------------------------------------ chamada simples
    def gerar(self, sistema, texto, max_tokens=500, forte=False):
        r, _ = self.conversar(sistema, [{"role": "user", "content": texto}], max_tokens=max_tokens, forte=forte)
        return r

    # ------------------------------------------------------------ conversa com ferramentas
    def conversar(self, sistema, mensagens, ferramentas=None, executar=None, max_tokens=1200, forte=False, voltas=8):
        """mensagens: [{"role": "user"|"assistant", "content": "texto"}]. ferramentas: [{"name","description","parameters"}].
        executar(nome, argumentos) -> texto. Devolve (resposta, lista de consultas feitas)."""
        if not self.ativa:
            raise ErroIA("IA não configurada.")
        modelo = self.modelo_forte if forte else self.modelo
        feitas = []
        if self.provedor == "claude":
            msgs = [{"role": m["role"], "content": m["content"]} for m in mensagens]
            tools = [{"name": f["name"], "description": f["description"], "input_schema": f["parameters"]} for f in ferramentas or []]
            for _ in range(voltas):
                corpo = {"model": modelo, "max_tokens": max(max_tokens, 2000), "system": sistema, "messages": msgs}
                if tools:
                    corpo["tools"] = tools
                if self.desligar_raciocinio:
                    corpo["thinking"] = {"type": "disabled"}
                try:
                    r = self._post(URL_CLAUDE, corpo, {"x-api-key": self.chave, "anthropic-version": "2023-06-01"})
                except ErroIA as e:
                    if e.codigo == 400 and "thinking" in (e.detalhe or "").lower() and self.desligar_raciocinio:
                        self.desligar_raciocinio = False
                        continue
                    raise
                blocos = r.get("content") or []
                usos = [b for b in blocos if b.get("type") == "tool_use"]
                if not usos or not executar:
                    txt = "".join(b.get("text", "") for b in blocos if b.get("type") == "text").strip()
                    if not txt:
                        self.ultimo_diag = "claude stop=%s blocos=%s" % (r.get("stop_reason"), [b.get("type") for b in blocos])
                        raise ErroIA("A IA não devolveu texto (%s). Tente de novo." % self.ultimo_diag)
                    return txt, feitas
                msgs.append({"role": "assistant", "content": blocos})
                res = []
                for u in usos:
                    saida = executar(u.get("name"), u.get("input") or {})
                    feitas.append(u.get("input") or {})
                    res.append({"type": "tool_result", "tool_use_id": u.get("id"), "content": saida})
                msgs.append({"role": "user", "content": res})
            raise ErroIA("A IA não chegou a uma resposta. Tente perguntar de outro jeito.")
        # OpenAI (Chat Completions)
        msgs = [{"role": "system", "content": sistema}] + [{"role": m["role"], "content": m["content"]} for m in mensagens]
        tools = [{"type": "function", "function": {"name": f["name"], "description": f["description"], "parameters": f["parameters"]}}
                 for f in ferramentas or []]
        for _ in range(voltas):
            corpo = {"model": modelo, "messages": msgs, "max_completion_tokens": max(max_tokens, 2000)}
            if tools:
                corpo["tools"] = tools
            if self.desligar_raciocinio and modelo.startswith(("gpt-5", "gpt-6", "o")):
                corpo["reasoning_effort"] = "none"  # resposta direta; exigido pela OpenAI para usar ferramentas no Luna
            try:
                r = self._post(URL_OPENAI, corpo, {"Authorization": "Bearer " + self.chave})
            except ErroIA as e:
                if e.codigo == 400 and "reasoning" in (e.detalhe or "").lower() and self.desligar_raciocinio:
                    self.desligar_raciocinio = False
                    continue
                raise
            ch0 = (r.get("choices") or [{}])[0]
            msg = ch0.get("message") or {}
            chamadas = msg.get("tool_calls") or []
            if not chamadas or not executar:
                txt = (msg.get("content") or "").strip()
                if not txt:
                    self.ultimo_diag = "openai finish=%s" % ch0.get("finish_reason")
                    raise ErroIA("A IA não devolveu texto (%s). Tente de novo." % self.ultimo_diag)
                return txt, feitas
            msgs.append({"role": "assistant", "content": msg.get("content"), "tool_calls": chamadas})
            for ch in chamadas:
                fn = ch.get("function") or {}
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except ValueError:
                    args = {}
                saida = executar(fn.get("name"), args)
                feitas.append(args)
                msgs.append({"role": "tool", "tool_call_id": ch.get("id"), "content": saida})
        raise ErroIA("A IA não chegou a uma resposta. Tente perguntar de outro jeito.")
