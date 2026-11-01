"""
Segundo cérebro do ATLAS — ideias conectadas ao longo do tempo.

O usuário compartilha ideias, pensamentos, planos e insights; o ATLAS os guarda
e, quando um tema volta, resgata o que já foi pensado ("isso lembra algo que o
senhor mencionou há 3 semanas sobre X"). A conexão é SEMÂNTICA: cada ideia ganha
um embedding e a busca é por similaridade de cosseno — assim "meu app de gastos"
casa com "sistema de controle financeiro" mesmo sem palavras em comum.

Isto NÃO é a `Memory` (fatos duráveis auto-extraídos sobre o usuário): aqui são
pensamentos a CONECTAR, guardados sob comando do cérebro e resgatados por tema.

O embedder é injetado (async `callable(texto)->list[float]`), para o módulo não
depender de um provedor específico e ser testável com um embedder falso. Sem
embedder (ou se a API falhar), a busca cai para correspondência por palavra-chave.

Persiste no banco compartilhado `~/.atlas/atlas.db` (ver `atlas.db`).
"""

from __future__ import annotations

import json
import math
from datetime import datetime

from atlas.db import connect

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ideas (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    texto     TEXT NOT NULL,
    criado_em TEXT NOT NULL,          -- ISO local
    embedding TEXT                    -- JSON [float, ...] ou NULL
);
CREATE INDEX IF NOT EXISTS idx_ideas_data ON ideas (criado_em);
"""


def _agora() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _cosseno(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _tempo_atras(iso: str) -> str:
    """'há 3 semanas', 'ontem', 'há 5 meses' — o toque do 'segundo cérebro'."""
    try:
        criado = datetime.fromisoformat(iso)
    except ValueError:
        return "em algum momento"
    dias = (datetime.now().astimezone() - criado).days
    if dias <= 0:
        return "hoje"
    if dias == 1:
        return "ontem"
    if dias < 7:
        return f"há {dias} dias"
    if dias < 30:
        semanas = dias // 7
        return f"há {semanas} semana" + ("s" if semanas > 1 else "")
    if dias < 365:
        meses = dias // 30
        return f"há {meses} " + ("meses" if meses > 1 else "mês")
    anos = dias // 365
    return f"há {anos} ano" + ("s" if anos > 1 else "")


class SecondBrain:
    """Guarda ideias e as conecta por similaridade semântica ao longo do tempo."""

    def __init__(self, embedder=None, limiar: float = 0.35) -> None:
        self._embed = embedder  # async callable(texto)->list[float] | None
        self._limiar = limiar
        with connect() as conn:
            conn.executescript(_SCHEMA)

    def available(self) -> bool:
        return True

    # ---- escrita ---------------------------------------------------------

    async def add(self, texto: str, k: int = 3) -> dict:
        """Guarda uma ideia e devolve ideias RELACIONADAS já existentes."""
        texto = (texto or "").strip()
        if not texto:
            raise ValueError("ideia vazia")
        # Relacionadas são calculadas ANTES de inserir (para não casar consigo).
        emb = await self._embedding(texto)
        relacionadas = self._similares(emb, k=k) if emb else []
        with connect() as conn:
            cur = conn.execute(
                "INSERT INTO ideas (texto, criado_em, embedding) VALUES (?, ?, ?)",
                (texto, _agora(), json.dumps(emb) if emb else None),
            )
            ideia = self._get(conn, cur.lastrowid)
        return {"ideia": ideia, "relacionadas": relacionadas}

    def remove(self, ideia_id: int) -> bool:
        with connect() as conn:
            cur = conn.execute("DELETE FROM ideas WHERE id = ?", (ideia_id,))
            return cur.rowcount > 0

    # ---- leitura ---------------------------------------------------------

    async def search(self, consulta: str, k: int = 5) -> list[dict]:
        """Ideias mais relacionadas a uma consulta (semântica; senão, palavra-chave)."""
        consulta = (consulta or "").strip()
        if not consulta:
            return []
        emb = await self._embedding(consulta)
        if emb:
            return self._similares(emb, k=k)
        return self._por_palavra(consulta, k=k)

    def list(self, limite: int = 20) -> list[dict]:
        """Ideias mais recentes (sem ranqueamento semântico)."""
        with connect() as conn:
            rows = conn.execute(
                "SELECT * FROM ideas ORDER BY criado_em DESC LIMIT ?", (int(limite),)
            ).fetchall()
            return [self._row_to_dict(r) for r in rows]

    # ---- internos --------------------------------------------------------

    async def _embedding(self, texto: str) -> list[float] | None:
        if not self._embed:
            return None
        try:
            vec = await self._embed(texto)
            return list(vec) if vec else None
        except Exception as exc:  # embedding é best-effort — cai p/ palavra-chave
            print(f"[segundo-cerebro] embedding falhou: {exc!r}", flush=True)
            return None

    def _similares(self, emb: list[float], k: int, exclude_id: int | None = None) -> list[dict]:
        with connect() as conn:
            rows = conn.execute(
                "SELECT * FROM ideas WHERE embedding IS NOT NULL"
            ).fetchall()
        marcados = []
        for r in rows:
            if exclude_id is not None and r["id"] == exclude_id:
                continue
            try:
                vec = json.loads(r["embedding"])
            except (json.JSONDecodeError, TypeError):
                continue
            sim = _cosseno(emb, vec)
            if sim >= self._limiar:
                d = self._row_to_dict(r)
                d["similaridade"] = round(sim, 3)
                marcados.append(d)
        marcados.sort(key=lambda x: x["similaridade"], reverse=True)
        return marcados[:k]

    def _por_palavra(self, consulta: str, k: int) -> list[dict]:
        like = f"%{consulta}%"
        with connect() as conn:
            rows = conn.execute(
                "SELECT * FROM ideas WHERE texto LIKE ? ORDER BY criado_em DESC LIMIT ?",
                (like, int(k)),
            ).fetchall()
            return [self._row_to_dict(r) for r in rows]

    def _get(self, conn, ideia_id: int) -> dict | None:
        row = conn.execute("SELECT * FROM ideas WHERE id = ?", (ideia_id,)).fetchone()
        return self._row_to_dict(row) if row else None

    @staticmethod
    def _row_to_dict(row) -> dict:
        return {
            "id": row["id"],
            "texto": row["texto"],
            "criado_em": row["criado_em"],
            "tempo_atras": _tempo_atras(row["criado_em"]),
        }
