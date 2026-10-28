"""
Controle de finanças pessoais do JARVIS.

O usuário registra gastos por voz e recebe um resumo por categoria:

    "Jarvis, gastei 50 reais no mercado"   -> registra o gasto
    "quanto gastei esse mês?"              -> relatório por categoria

Foco em simplicidade: um lançamento tem valor (R$), categoria e uma descrição
opcional. O relatório soma por categoria no período (mês atual por padrão, como
pede o escopo). As categorias são reaproveitadas (normalizadas em minúsculas)
para o resumo não fragmentar 'Mercado' e 'mercado'.

Persiste no banco compartilhado `~/.jarvis/jarvis.db` (ver `jarvis.db`), a mesma
fundação das tarefas e do rastreamento de tempo.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from jarvis.db import connect

PERIODOS = ("hoje", "ontem", "semana", "mes", "ano")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS expenses (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    valor     REAL NOT NULL CHECK (valor > 0),
    categoria TEXT NOT NULL,
    descricao TEXT,
    data      TEXT NOT NULL   -- ISO local
);
CREATE INDEX IF NOT EXISTS idx_exp_data ON expenses (data);
CREATE INDEX IF NOT EXISTS idx_exp_cat  ON expenses (categoria);
"""


def _now() -> datetime:
    return datetime.now().astimezone()


def _agora() -> str:
    return _now().isoformat(timespec="seconds")


def _to_valor(valor) -> float:
    """Converte um valor humano ('50', '12,90', 'R$ 8.50') em float positivo."""
    if isinstance(valor, (int, float)):
        v = float(valor)
    else:
        s = str(valor).strip().lower().replace("r$", "").replace(" ", "")
        # normaliza vírgula decimal: '1.234,56' -> '1234.56'; '12,90' -> '12.90'
        if "," in s:
            s = s.replace(".", "").replace(",", ".")
        try:
            v = float(s)
        except ValueError as exc:
            raise ValueError(f"valor inválido: {valor!r}") from exc
    if v <= 0:
        raise ValueError("valor deve ser positivo")
    return round(v, 2)


def _fmt_brl(v: float) -> str:
    """Formata em reais no padrão brasileiro: 1234.5 -> 'R$ 1.234,50'."""
    s = f"{v:,.2f}"  # '1,234.50'
    s = s.replace(",", "X").replace(".", ",").replace("X", ".")  # '1.234,50'
    return f"R$ {s}"


class FinanceStore:
    """Registro de gastos e relatório por categoria/período."""

    def __init__(self) -> None:
        with connect() as conn:
            conn.executescript(_SCHEMA)

    def available(self) -> bool:
        return True

    # ---- escrita ---------------------------------------------------------

    def add(self, valor, categoria: str, descricao: str | None = None) -> dict:
        """Registra um gasto. Categoria é normalizada em minúsculas."""
        v = _to_valor(valor)
        categoria = (categoria or "").strip().lower()
        if not categoria:
            raise ValueError("categoria vazia")
        descricao = (descricao or "").strip() or None
        with connect() as conn:
            cur = conn.execute(
                "INSERT INTO expenses (valor, categoria, descricao, data) "
                "VALUES (?, ?, ?, ?)",
                (v, categoria, descricao, _agora()),
            )
            return self._get(conn, cur.lastrowid)

    def remove(self, expense_id: int) -> bool:
        with connect() as conn:
            cur = conn.execute("DELETE FROM expenses WHERE id = ?", (expense_id,))
            return cur.rowcount > 0

    # ---- leitura ---------------------------------------------------------

    def list(self, periodo: str | None = None, categoria: str | None = None) -> list[dict]:
        """Lista lançamentos (mais recentes primeiro), com filtros opcionais."""
        where: list[str] = []
        params: list[object] = []
        if periodo:
            desde, ate, _ = self._range(periodo)
            where.append("data >= ? AND data < ?")
            params += [desde.isoformat(timespec="seconds"), ate.isoformat(timespec="seconds")]
        if categoria:
            where.append("categoria = ?")
            params.append(categoria.strip().lower())
        sql = "SELECT * FROM expenses"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY data DESC"
        with connect() as conn:
            return [self._row_to_dict(r) for r in conn.execute(sql, params)]

    def report(self, periodo: str = "mes") -> dict:
        """Soma os gastos por categoria no período (mês atual por padrão)."""
        desde, ate, label = self._range(periodo)
        with connect() as conn:
            rows = conn.execute(
                "SELECT categoria, SUM(valor) AS total, COUNT(*) AS n "
                "FROM expenses WHERE data >= ? AND data < ? "
                "GROUP BY categoria ORDER BY total DESC",
                (desde.isoformat(timespec="seconds"), ate.isoformat(timespec="seconds")),
            ).fetchall()
        por_categoria = [
            {
                "categoria": r["categoria"],
                "total": round(r["total"], 2),
                "total_fmt": _fmt_brl(r["total"]),
                "lancamentos": r["n"],
            }
            for r in rows
        ]
        total = round(sum(c["total"] for c in por_categoria), 2)
        return {
            "periodo": label,
            "por_categoria": por_categoria,
            "total": total,
            "total_fmt": _fmt_brl(total),
        }

    def categorias(self) -> list[str]:
        """Categorias já usadas (para reaproveitar nomes e não fragmentar)."""
        with connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT categoria FROM expenses ORDER BY categoria"
            )
            return [r["categoria"] for r in rows]

    def _range(self, periodo: str) -> tuple[datetime, datetime, str]:
        now = _now()
        hoje0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
        depois = now + timedelta(seconds=1)
        p = (periodo or "mes").strip().lower()
        if p in ("hoje", "today"):
            return hoje0, depois, "hoje"
        if p in ("ontem", "yesterday"):
            return hoje0 - timedelta(days=1), hoje0, "ontem"
        if p in ("semana", "week"):
            return hoje0 - timedelta(days=now.weekday()), depois, "esta semana"
        if p in ("ano", "year"):
            return hoje0.replace(month=1, day=1), depois, "este ano"
        return hoje0.replace(day=1), depois, "este mês"  # padrão: mês atual

    # ---- internos --------------------------------------------------------

    def _get(self, conn, expense_id: int) -> dict | None:
        row = conn.execute(
            "SELECT * FROM expenses WHERE id = ?", (expense_id,)
        ).fetchone()
        return self._row_to_dict(row) if row else None

    @staticmethod
    def _row_to_dict(row) -> dict:
        return {
            "id": row["id"],
            "valor": round(row["valor"], 2),
            "valor_fmt": _fmt_brl(row["valor"]),
            "categoria": row["categoria"],
            "descricao": row["descricao"],
            "data": row["data"],
        }
