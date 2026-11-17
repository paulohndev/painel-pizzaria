# -*- coding: utf-8 -*-
"""
Ponto de partida do painel: primeiro verifica se tem versão nova do programa,
depois roda o pizzaria.py. Fica separado de propósito: mesmo que uma versão
nova do pizzaria.py tenha algum problema, este arquivo continua conseguindo
baixar a correção.

    python iniciar.py [mesmas opções do pizzaria.py]
    python iniciar.py --agendado      (usado pelo Agendador do Windows: sem janela, grava log)
"""
import configparser
import os
import subprocess
import sys
import time

PASTA = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PASTA)


def main():
    args = sys.argv[1:]
    agendado = "--agendado" in args
    saida = None
    if agendado:
        os.makedirs(os.path.join(PASTA, "dados"), exist_ok=True)
        caminho_log = os.path.join(PASTA, "dados", "execucao.log")
        # mantém o log pequeno: guarda só o final do arquivo anterior
        try:
            if os.path.getsize(caminho_log) > 2_000_000:
                with open(caminho_log, "rb") as f:
                    f.seek(-500_000, 2)
                    resto = f.read()
                with open(caminho_log, "wb") as f:
                    f.write(resto)
        except OSError:
            pass
        saida = open(caminho_log, "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = saida
        print("\n===== %s =====" % time.strftime("%d/%m/%Y %H:%M"))

    def log(msg=""):
        print(msg, flush=True)

    if "--sem-atualizar" not in args:
        repo = ""
        try:
            cp = configparser.ConfigParser(interpolation=None)
            cp.read(os.path.join(PASTA, "config.ini"), encoding="utf-8")
            if cp.get("atualizacao", "ativo", fallback="sim").strip().lower() in ("nao", "não", "no", "0"):
                repo = None
            else:
                repo = cp.get("atualizacao", "repositorio", fallback="").strip()
        except Exception:
            pass
        if repo is not None:
            try:
                import atualizador
                atualizador.verificar(repo, log)
            except Exception as e:
                log("(verificação de atualização falhou: %s)" % e)

    resto = [a for a in args if a not in ("--agendado", "--sem-atualizar")]
    if agendado:
        resto.append("--nao-abrir")
    cmd = [sys.executable, os.path.join(PASTA, "pizzaria.py")] + resto
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    if saida:
        return subprocess.call(cmd, stdout=saida, stderr=saida, env=env, cwd=PASTA)
    return subprocess.call(cmd, env=env, cwd=PASTA)


if __name__ == "__main__":
    sys.exit(main())
