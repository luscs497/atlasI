"""
Rastreamento de tempo do ATLAS.

O usuário controla por voz quanto tempo dedica a cada projeto:

    "Atlas, começando o Stamflow"   -> inicia um cronômetro no projeto
    "parei" / "terminei"             -> encerra o cronômetro em andamento
    "quanto tempo essa semana?"      -> relatório de tempo real por projeto

Regra central: existe NO MÁXIMO um cronômetro ativo. Iniciar um novo encerra
automaticamente o anterior (é raro querer dois ao mesmo tempo, e evita timers
esquecidos rodando). O tempo "em andamento" conta até agora — o relatório soma o
tempo real por projeto no período (semana atual por padrão).

Persiste no banco compartilhado `~/.atlas/atlas.db` (ver `atlas.db`), a mesma
fundação das tarefas. Reaproveite os nomes de projeto conhecidos (Trabalho).
"""

from __future__ import annotations

from datetime import datetime, timedelta

from atlas.db import connect

_SCHEMA = """
CREATE TABLE IF NOT EXISTS time_entries (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    projeto TEXT NOT NULL,
    inicio  TEXT NOT NULL,   -- ISO local
    fim     TEXT             -- NULL enquanto o cronômetro roda
);
CREATE INDEX IF NOT EXISTS idx_time_projeto ON time_entries (projeto);
CREATE INDEX IF NOT EXISTS idx_time_inicio  ON time_entries (inicio);
"""

# Rótulos aceitos para o período do relatório.
PERIODOS = ("hoje", "ontem", "semana", "mes")


def _now() -> datetime:
    return datetime.now().astimezone()


def _agora() -> str:
    return _now().isoformat(timespec="seconds")


def _dur_seg(inicio: str, fim: str) -> int:
    """Duração em segundos entre dois timestamps ISO (nunca negativa)."""
    a = datetime.fromisoformat(inicio)
    b = datetime.fromisoformat(fim)
    return max(0, int((b - a).total_seconds()))


def _fmt(seg: int) -> str:
    """Formata segundos como '2h 15min' / '45min' / '30s'."""
    seg = int(seg)
    h, resto = divmod(seg, 3600)
    m, s = divmod(resto, 60)
    if h and m:
        return f"{h}h {m}min"
    if h:
        return f"{h}h"
    if m:
        return f"{m}min"
    return f"{s}s"


class TimeTracker:
    """Cronômetros de tempo por projeto, com relatório agregado por período."""

    def __init__(self) -> None:
        with connect() as conn:
            conn.executescript(_SCHEMA)

    def available(self) -> bool:
        return True

    # ---- controle do cronômetro -----------------------------------------

    def start(self, projeto: str) -> dict:
        """Inicia um cronômetro no projeto, encerrando qualquer um em andamento."""
        projeto = (projeto or "").strip()
        if not projeto:
            raise ValueError("projeto vazio")
        encerrado = self.stop()  # no máximo um ativo por vez
        with connect() as conn:
            cur = conn.execute(
                "INSERT INTO time_entries (projeto, inicio) VALUES (?, ?)",
                (projeto, _agora()),
            )
            iniciado = self._get(conn, cur.lastrowid)
        return {"iniciado": iniciado, "encerrado": encerrado}

    def stop(self) -> dict | None:
        """Encerra o cronômetro em andamento. Devolve a entrada, ou None."""
        with connect() as conn:
            row = conn.execute(
                "SELECT * FROM time_entries WHERE fim IS NULL "
                "ORDER BY inicio DESC LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "UPDATE time_entries SET fim = ? WHERE id = ?", (_agora(), row["id"])
            )
            return self._get(conn, row["id"])

    def status(self) -> dict | None:
        """Cronômetro em andamento (com o tempo já decorrido), ou None."""
        with connect() as conn:
            row = conn.execute(
                "SELECT * FROM time_entries WHERE fim IS NULL "
                "ORDER BY inicio DESC LIMIT 1"
            ).fetchone()
        return self._row_to_dict(row) if row else None

    # ---- relatório -------------------------------------------------------

    def report(self, periodo: str = "semana") -> dict:
        """Soma o tempo real por projeto no período (semana atual por padrão)."""
        desde, ate, label = self._range(periodo)
        with connect() as conn:
            rows = conn.execute(
                "SELECT * FROM time_entries WHERE inicio >= ? AND inicio < ? "
                "ORDER BY inicio",
                (desde.isoformat(timespec="seconds"), ate.isoformat(timespec="seconds")),
            ).fetchall()
        agg: dict[str, int] = {}
        for r in rows:
            fim = r["fim"] or _agora()  # cronômetro rodando conta até agora
            agg[r["projeto"]] = agg.get(r["projeto"], 0) + _dur_seg(r["inicio"], fim)
        por_projeto = [
            {"projeto": p, "segundos": s, "tempo": _fmt(s)}
            for p, s in sorted(agg.items(), key=lambda kv: -kv[1])
        ]
        total = sum(agg.values())
        return {
            "periodo": label,
            "por_projeto": por_projeto,
            "total_seg": total,
            "total": _fmt(total),
        }

    def _range(self, periodo: str) -> tuple[datetime, datetime, str]:
        """Intervalo [desde, ate) e um rótulo humano para o período pedido."""
        now = _now()
        hoje0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
        depois = now + timedelta(seconds=1)  # inclui entradas até 'agora'
        p = (periodo or "semana").strip().lower()
        if p in ("hoje", "today"):
            return hoje0, depois, "hoje"
        if p in ("ontem", "yesterday"):
            return hoje0 - timedelta(days=1), hoje0, "ontem"
        if p in ("mes", "mês", "month"):
            return hoje0.replace(day=1), depois, "este mês"
        # padrão: semana atual (segunda 00:00 até agora)
        segunda = hoje0 - timedelta(days=now.weekday())
        return segunda, depois, "esta semana"

    # ---- internos --------------------------------------------------------

    def _get(self, conn, entry_id: int) -> dict | None:
        row = conn.execute(
            "SELECT * FROM time_entries WHERE id = ?", (entry_id,)
        ).fetchone()
        return self._row_to_dict(row) if row else None

    @staticmethod
    def _row_to_dict(row) -> dict:
        inicio = row["inicio"]
        fim = row["fim"]
        dur = _dur_seg(inicio, fim or _agora())
        return {
            "id": row["id"],
            "projeto": row["projeto"],
            "inicio": inicio,
            "fim": fim,
            "em_andamento": fim is None,
            "duracao_seg": dur,
            "duracao": _fmt(dur),
        }
