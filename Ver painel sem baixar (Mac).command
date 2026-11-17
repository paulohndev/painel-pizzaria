#!/bin/bash
cd "$(dirname "$0")"
python3 iniciar.py --so-painel --sem-atualizar
echo
read -p "Pressione Enter para fechar..."
