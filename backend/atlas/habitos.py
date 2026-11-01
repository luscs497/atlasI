"""
Agenda de desenvolvimento pessoal do ATLAS — hábitos diários.

O usuário define hábitos que quer **construir** (ex.: exercício, leitura) ou
**quebrar** (ex.: cigarro, açúcar), faz um check-in por voz no fim do dia, e o
ATLAS acompanha a sequência (streak) e dá feedback honesto sobre a consistência.

Modelo uniforme via `sucesso` (bool): o dia foi um SUCESSO em relação à meta do
hábito, independente do tipo. Cabe ao cérebro mapear a fala:
- hábito 'construir' (fazer algo): "fiz/treinei/li" → sucesso; "não fiz" → falha.
- hábito 'quebrar' (evitar algo): "não fumei/resisti" → sucesso; "fumei/furei" → falha.

Assim a sequência é sempre "dias consecutivos de sucesso", igual para os dois
tipos. Um check-in por hábito por dia (re-registrar sobrescreve).

Persiste no banco compartilhado `~/.atlas/atlas.db` (ver `atlas.db`).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from atlas.db import connect

TIPOS = ("construir", "quebrar")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS habits (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    nome      TEXT    NOT NULL,
    tipo      TEXT    NOT NULL CHECK (tipo IN ('construir','quebrar')),
    ativo     INTEGER NOT NULL DEFAULT 1,
    criado_em TEXT    NOT NULL
);
CREATE TABLE IF NOT EXISTS habit_checkins (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    habit_id      INTEGER NOT NULL REFERENCES habits(id) ON DELETE CASCADE,
    data          TEXT    NOT NULL,   -- YYYY-MM-DD (local)
    sucesso       INTEGER NOT NULL,
    registrado_em TEXT    NOT NULL,
    UNIQUE (habit_id, data)
);
CREATE INDEX IF NOT EXISTS idx_checkin_habit ON habit_checkins (habit_id, data);
"""


def _hoje() -> date:
    return datetime.now().astimezone().date()


def _norm_tipo(tipo: str) -> str:
    t = (tipo or "").strip().lower()
    if t in ("construir", "criar", "build", "manter", "adquirir"):
        return "construir"
    if t in ("quebrar", "largar", "parar", "break", "cortar", "evitar"):
        return "quebrar"
    raise ValueError(f"tipo inválido: {tipo!r} (use 'construir' ou 'quebrar')")


class HabitStore:
    """Hábitos (construir/quebrar), check-ins diários, sequência e consistência."""

    def __init__(self) -> None:
        with connect() as conn:
            conn.executescript(_SCHEMA)

    def available(self) -> bool:
        return True

    # ---- hábitos ---------------------------------------------------------

    def add_habit(self, nome: str, tipo: str) -> dict:
        """Cria um hábito. Se já existir um ativo com o mesmo nome, devolve-o."""
        nome = (nome or "").strip()
        if not nome:
            raise ValueError("nome do hábito vazio")
        tipo = _norm_tipo(tipo)
        existente = self.find_habit(nome)
        if existente is not None:
            return existente
        with connect() as conn:
            cur = conn.execute(
                "INSERT INTO habits (nome, tipo, criado_em) VALUES (?, ?, ?)",
                (nome, tipo, _hoje().isoformat()),
            )
            return self._habit_dict(conn, cur.lastrowid)

    def find_habit(self, nome: str) -> dict | None:
        """Acha um hábito ATIVO pelo nome (case-insensitive)."""
        nome = (nome or "").strip().lower()
        if not nome:
            return None
        with connect() as conn:
            row = conn.execute(
                "SELECT * FROM habits WHERE ativo = 1 AND LOWER(nome) = ?",
                (nome,),
            ).fetchone()
            return self._habit_dict(conn, row["id"]) if row else None

    def remove_habit(self, nome: str) -> bool:
        """Desativa um hábito (mantém o histórico). True se algo mudou."""
        with connect() as conn:
            cur = conn.execute(
                "UPDATE habits SET ativo = 0 WHERE ativo = 1 AND LOWER(nome) = ?",
                ((nome or "").strip().lower(),),
            )
            return cur.rowcount > 0

    def list_habits(self) -> list[dict]:
        """Hábitos ativos, cada um com sequência atual e status de hoje."""
        with connect() as conn:
            rows = conn.execute(
                "SELECT id FROM habits WHERE ativo = 1 ORDER BY criado_em"
            ).fetchall()
            return [self._habit_dict(conn, r["id"]) for r in rows]

    # ---- check-ins -------------------------------------------------------

    def checkin(self, nome: str, sucesso: bool, dia: str | None = None) -> dict:
        """Registra o resultado do dia para um hábito (sobrescreve se já houver)."""
        habito = self.find_habit(nome)
        if habito is None:
            existentes = [h["nome"] for h in self.list_habits()]
            raise ValueError(
                f"hábito não encontrado: {nome!r}. Ativos: {existentes or 'nenhum'}"
            )
        d = self._parse_dia(dia)
        with connect() as conn:
            conn.execute(
                "INSERT INTO habit_checkins (habit_id, data, sucesso, registrado_em) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT (habit_id, data) DO UPDATE SET "
                "sucesso = excluded.sucesso, registrado_em = excluded.registrado_em",
                (habito["id"], d.isoformat(), 1 if sucesso else 0,
                 datetime.now().astimezone().isoformat(timespec="seconds")),
            )
            return self._habit_dict(conn, habito["id"])

    # ---- relatório -------------------------------------------------------

    def report(self, dias: int = 7) -> dict:
        """Consistência de cada hábito nos últimos `dias` (taxa + sequência)."""
        dias = max(1, int(dias))
        desde = (_hoje() - timedelta(days=dias - 1)).isoformat()
        itens = []
        with connect() as conn:
            for h in conn.execute(
                "SELECT id, nome, tipo FROM habits WHERE ativo = 1 ORDER BY criado_em"
            ).fetchall():
                rows = conn.execute(
                    "SELECT sucesso FROM habit_checkins "
                    "WHERE habit_id = ? AND data >= ?",
                    (h["id"], desde),
                ).fetchall()
                total = len(rows)
                sucessos = sum(r["sucesso"] for r in rows)
                itens.append(
                    {
                        "habito": h["nome"],
                        "tipo": h["tipo"],
                        "dias_registrados": total,
                        "sucessos": sucessos,
                        "taxa": round(sucessos / total, 2) if total else None,
                        "taxa_pct": round(100 * sucessos / total) if total else None,
                        "sequencia": self._streak(conn, h["id"]),
                    }
                )
        return {"janela_dias": dias, "habitos": itens}

    # ---- internos --------------------------------------------------------

    def _parse_dia(self, dia: str | None) -> date:
        if not dia:
            return _hoje()
        s = str(dia).strip().lower()
        if s in ("hoje", "today"):
            return _hoje()
        if s in ("ontem", "yesterday"):
            return _hoje() - timedelta(days=1)
        try:
            return date.fromisoformat(s)
        except ValueError:
            return _hoje()

    def _streak(self, conn, habit_id: int) -> int:
        """Dias consecutivos de sucesso terminando no check-in mais recente."""
        rows = conn.execute(
            "SELECT data, sucesso FROM habit_checkins "
            "WHERE habit_id = ? ORDER BY data DESC",
            (habit_id,),
        ).fetchall()
        streak = 0
        prev: date | None = None
        for r in rows:
            if not r["sucesso"]:
                break
            d = date.fromisoformat(r["data"])
            if prev is not None and (prev - d).days != 1:
                break  # buraco de dias: sequência interrompida
            streak += 1
            prev = d
        return streak

    def _habit_dict(self, conn, habit_id: int) -> dict:
        h = conn.execute("SELECT * FROM habits WHERE id = ?", (habit_id,)).fetchone()
        hoje = conn.execute(
            "SELECT sucesso FROM habit_checkins WHERE habit_id = ? AND data = ?",
            (habit_id, _hoje().isoformat()),
        ).fetchone()
        return {
            "id": h["id"],
            "nome": h["nome"],
            "tipo": h["tipo"],
            "sequencia": self._streak(conn, habit_id),
            "hoje": None if hoje is None else bool(hoje["sucesso"]),
        }
