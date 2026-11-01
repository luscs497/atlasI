"""
Sistema de tarefas do ATLAS.

Duas listas FIXAS, conforme o escopo do usuário:

- **Pessoal** — tarefas soltas, sem projeto.
- **Trabalho** — sempre dividida por PROJETO. Uma tarefa de trabalho pertence a
  um projeto (ex.: 'stamflow', 'atlasI').

Regras de convívio (a decisão de "onde encaixar" mora no cérebro/persona, não
aqui): o ATLAS identifica um projeto existente e adiciona nele; só cria projeto
novo quando o usuário mandar explicitamente; e, se ficar em dúvida, pergunta
antes de agir. Este módulo só oferece as operações determinísticas e uma forma
de listar os projetos existentes (para o cérebro decidir com base em fatos).

Persiste no banco compartilhado `~/.atlas/atlas.db` (ver `atlas.db`).
"""

from __future__ import annotations

from datetime import datetime

from atlas.db import connect

LISTAS = ("trabalho", "pessoal")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    lista        TEXT    NOT NULL CHECK (lista IN ('trabalho','pessoal')),
    projeto      TEXT,                       -- só em 'trabalho'; NULL em 'pessoal'
    titulo       TEXT    NOT NULL,
    concluida    INTEGER NOT NULL DEFAULT 0,
    criada_em    TEXT    NOT NULL,
    concluida_em TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_lista   ON tasks (lista, concluida);
CREATE INDEX IF NOT EXISTS idx_tasks_projeto ON tasks (projeto);
"""


def _agora() -> str:
    """Timestamp local legível (ISO, no fuso da máquina — Fortaleza)."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _norm_lista(lista: str) -> str:
    lista = (lista or "").strip().lower()
    if lista in ("trabalho", "work", "profissional"):
        return "trabalho"
    if lista in ("pessoal", "personal", "casa"):
        return "pessoal"
    raise ValueError(f"lista inválida: {lista!r} (use 'trabalho' ou 'pessoal')")


class TaskStore:
    """CRUD de tarefas nas listas fixas Trabalho (por projeto) e Pessoal."""

    def __init__(self) -> None:
        with connect() as conn:
            conn.executescript(_SCHEMA)

    def available(self) -> bool:
        return True

    # ---- escrita ---------------------------------------------------------

    def add(self, titulo: str, lista: str, projeto: str | None = None) -> dict:
        """Cria uma tarefa. Em 'pessoal' o projeto é ignorado (fica NULL)."""
        titulo = (titulo or "").strip()
        if not titulo:
            raise ValueError("título vazio")
        lista = _norm_lista(lista)
        projeto = (projeto or "").strip() or None
        if lista == "pessoal":
            projeto = None  # lista pessoal não tem projeto
        with connect() as conn:
            cur = conn.execute(
                "INSERT INTO tasks (lista, projeto, titulo, criada_em) "
                "VALUES (?, ?, ?, ?)",
                (lista, projeto, titulo, _agora()),
            )
            return self._get(conn, cur.lastrowid)

    def complete(self, task_id: int) -> dict | None:
        """Marca como concluída. Devolve a tarefa, ou None se não existir."""
        with connect() as conn:
            row = self._get(conn, task_id)
            if row is None:
                return None
            conn.execute(
                "UPDATE tasks SET concluida = 1, concluida_em = ? WHERE id = ?",
                (_agora(), task_id),
            )
            return self._get(conn, task_id)

    def remove(self, task_id: int) -> bool:
        """Apaga a tarefa. True se algo foi removido."""
        with connect() as conn:
            cur = conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
            return cur.rowcount > 0

    # ---- leitura ---------------------------------------------------------

    def list(
        self,
        lista: str | None = None,
        projeto: str | None = None,
        incluir_concluidas: bool = False,
    ) -> list[dict]:
        """Lista tarefas, por padrão só as pendentes (mais antigas primeiro)."""
        where: list[str] = []
        params: list[object] = []
        if lista:
            where.append("lista = ?")
            params.append(_norm_lista(lista))
        if projeto:
            where.append("projeto = ?")
            params.append(projeto.strip())
        if not incluir_concluidas:
            where.append("concluida = 0")
        sql = "SELECT * FROM tasks"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY concluida ASC, criada_em ASC"
        with connect() as conn:
            return [self._row_to_dict(r) for r in conn.execute(sql, params)]

    def projetos(self) -> list[str]:
        """Projetos existentes na lista Trabalho (com tarefas pendentes ou não)."""
        with connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT projeto FROM tasks "
                "WHERE lista = 'trabalho' AND projeto IS NOT NULL "
                "ORDER BY projeto"
            )
            return [r["projeto"] for r in rows]

    # ---- internos --------------------------------------------------------

    def _get(self, conn, task_id: int) -> dict | None:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return self._row_to_dict(row) if row else None

    @staticmethod
    def _row_to_dict(row) -> dict:
        return {
            "id": row["id"],
            "lista": row["lista"],
            "projeto": row["projeto"],
            "titulo": row["titulo"],
            "concluida": bool(row["concluida"]),
            "criada_em": row["criada_em"],
            "concluida_em": row["concluida_em"],
        }
