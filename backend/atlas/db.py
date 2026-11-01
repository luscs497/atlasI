"""
Fundação de dados persistente do ATLAS.

Um banco SQLite único em `~/.atlas/atlas.db` serve de base para as features
que precisam guardar estado estruturado por tempo indeterminado — tarefas,
finanças, hábitos, rastreamento de tempo, segundo cérebro. Cada feature dona de
suas tabelas as cria via seu próprio módulo (ex.: `tasks.TaskStore`), usando o
helper de conexão daqui.

Ao contrário da `Memory` (fatos/histórico da IA em JSON), este banco NÃO entra na
memória do modelo: é dado do usuário, manipulado só sob comando explícito e
persistido de forma determinística.

Assistente de usuário único, processo único: abrimos uma conexão nova por
operação (barato no SQLite) para não amarrar a conexão a uma thread — assim os
módulos podem rodar tanto no loop async quanto via `asyncio.to_thread` sem susto.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from atlas.config import ATLAS_DIR

DB_PATH: Path = ATLAS_DIR / "atlas.db"


def connect() -> sqlite3.Connection:
    """Abre uma conexão com o banco, criando `~/.atlas/` se preciso."""
    ATLAS_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row  # linhas acessíveis por nome de coluna
    conn.execute("PRAGMA journal_mode=WAL")  # leituras não bloqueiam escritas
    conn.execute("PRAGMA foreign_keys=ON")
    return conn
