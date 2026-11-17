#!/bin/bash
cd "$(dirname "$0")"
python3 iniciar.py "$@"
echo
read -p "Pressione Enter para fechar..."
