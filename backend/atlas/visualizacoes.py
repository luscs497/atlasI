"""
Tela de visualizações dinâmicas do ATLAS.

O usuário pede visuais por voz ("mostra meus gastos do mês em pizza", "faz um
dashboard do meu tempo por projeto") e uma 2ª janela dedicada os desenha em tempo
real. Padrões podem ser salvos com nome para repetir/atualizar depois.

Este módulo NÃO desenha nada: ele (a) normaliza/valida o "spec" de visualização
que o cérebro monta e (b) persiste os padrões salvos. O desenho é do front-end
(2ª janela Electron), que recebe o spec normalizado via WebSocket. O cérebro é
quem COMPÕE o spec — normalmente buscando os dados nas outras features (finanças,
tempo, hábitos) e passando os números aqui.

Tipos suportados (o front-end sabe desenhar cada um):
  barras | linhas | pizza  -> `dados`: [{rotulo, valor}]
  lista                    -> `itens`: [str]
  kpis                     -> `cards`: [{rotulo, valor, sub?}]
  mapa_mental              -> `mapa`: {centro, ramos: [{titulo, filhos: [str]}]}

Persiste no banco compartilhado `~/.atlas/atlas.db` (ver `atlas.db`).
"""

from __future__ import annotations

import json
from datetime import datetime

from atlas.db import connect

TIPOS = ("barras", "linhas", "pizza", "lista", "kpis", "mapa_mental", "html")

# Nome reservado (não aparece na lista de padrões salvos) para o último design.
_ULTIMA = "__ultima__"

# Sinônimos comuns que o modelo pode usar -> tipo canônico.
_ALIAS = {
    "bar": "barras", "barra": "barras", "coluna": "barras", "colunas": "barras",
    "line": "linhas", "linha": "linhas",
    "pie": "pizza", "torta": "pizza", "rosca": "pizza", "donut": "pizza",
    "list": "lista",
    "kpi": "kpis", "cards": "kpis", "cartoes": "kpis", "indicadores": "kpis",
    "mindmap": "mapa_mental", "mapa": "mapa_mental", "mapa-mental": "mapa_mental",
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS visualizations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    nome          TEXT NOT NULL UNIQUE,
    spec          TEXT NOT NULL,          -- JSON normalizado
    criado_em     TEXT NOT NULL,
    atualizado_em TEXT NOT NULL
);
"""


def _agora() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _tipo_canonico(tipo: str) -> str:
    t = (tipo or "").strip().lower().replace(" ", "_")
    t = _ALIAS.get(t, t)
    if t not in TIPOS:
        raise ValueError(f"tipo de visualização inválido: {tipo!r}")
    return t


def _num(v) -> float:
    """Coage um valor a float (aceita '1.234,56' / 'R$ 40')."""
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().lower().replace("r$", "").replace("%", "").replace(" ", "")
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError as exc:
        raise ValueError(f"valor numérico inválido: {v!r}") from exc


def normalizar_spec(spec: dict) -> dict:
    """Valida e normaliza o spec vindo do cérebro. Levanta ValueError se inválido."""
    if not isinstance(spec, dict):
        raise ValueError("spec deve ser um objeto")
    # Atalho: se veio HTML cru, é o modo "design livre" (gama infinita).
    if spec.get("html") and not spec.get("tipo"):
        spec = {**spec, "tipo": "html"}
    tipo = _tipo_canonico(spec.get("tipo", ""))
    titulo = (spec.get("titulo") or "").strip() or "Visualização"
    out: dict = {"tipo": tipo, "titulo": titulo}

    if tipo == "html":
        html = spec.get("html")
        if not isinstance(html, str) or not html.strip():
            raise ValueError("design 'html' sem conteúdo")
        out["html"] = html
        libs = [l for l in (spec.get("libs") or []) if l in ("chartjs", "d3")]
        if libs:
            out["libs"] = libs
        return out

    if tipo in ("barras", "linhas", "pizza"):
        dados = spec.get("dados") or spec.get("series") or []
        norm = []
        for d in dados:
            if not isinstance(d, dict):
                continue
            rotulo = (d.get("rotulo") or d.get("label") or "").strip()
            if not rotulo:
                continue
            norm.append({"rotulo": rotulo, "valor": _num(d.get("valor", d.get("value", 0)))})
        if not norm:
            raise ValueError("gráfico sem dados (informe 'dados': [{rotulo, valor}])")
        out["dados"] = norm
        if spec.get("unidade"):
            out["unidade"] = str(spec["unidade"]).strip()

    elif tipo == "lista":
        itens = spec.get("itens") or []
        norm = []
        for it in itens:
            if isinstance(it, str) and it.strip():
                norm.append({"texto": it.strip()})
            elif isinstance(it, dict) and (it.get("texto") or "").strip():
                norm.append({"texto": it["texto"].strip(),
                             "detalhe": (it.get("detalhe") or "").strip() or None})
        if not norm:
            raise ValueError("lista sem itens")
        out["itens"] = norm

    elif tipo == "kpis":
        cards = spec.get("cards") or []
        norm = []
        for c in cards:
            if not isinstance(c, dict):
                continue
            rotulo = (c.get("rotulo") or "").strip()
            valor = c.get("valor")
            if not rotulo or valor is None:
                continue
            norm.append({"rotulo": rotulo, "valor": str(valor).strip(),
                         "sub": (c.get("sub") or "").strip() or None})
        if not norm:
            raise ValueError("kpis sem cards")
        out["cards"] = norm

    elif tipo == "mapa_mental":
        mapa = spec.get("mapa") or {}
        centro = (mapa.get("centro") or titulo).strip()
        ramos = []
        for r in mapa.get("ramos") or []:
            if not isinstance(r, dict):
                continue
            t = (r.get("titulo") or "").strip()
            if not t:
                continue
            filhos = [str(f).strip() for f in (r.get("filhos") or []) if str(f).strip()]
            ramos.append({"titulo": t, "filhos": filhos})
        if not ramos:
            raise ValueError("mapa mental sem ramos")
        out["mapa"] = {"centro": centro, "ramos": ramos}

    return out


class VizStore:
    """Persiste padrões de visualização nomeados (salvar/recuperar/listar)."""

    def __init__(self) -> None:
        with connect() as conn:
            conn.executescript(_SCHEMA)

    def available(self) -> bool:
        return True

    def save(self, nome: str, spec: dict) -> dict:
        """Salva (ou atualiza) um padrão nomeado. Devolve o spec normalizado."""
        nome = (nome or "").strip()
        if not nome:
            raise ValueError("nome vazio")
        norm = normalizar_spec(spec)
        agora = _agora()
        with connect() as conn:
            conn.execute(
                "INSERT INTO visualizations (nome, spec, criado_em, atualizado_em) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT (nome) DO UPDATE SET spec = excluded.spec, "
                "atualizado_em = excluded.atualizado_em",
                (nome, json.dumps(norm, ensure_ascii=False), agora, agora),
            )
        return norm

    def get(self, nome: str) -> dict | None:
        """Recupera pelo nome. Voz é imprecisa: tenta exato e, se falhar, um
        casamento por conteúdo (o nome pedido está contido no salvo ou vice-versa),
        pegando o mais recente."""
        n = (nome or "").strip().lower()
        if not n:
            return None
        with connect() as conn:
            row = conn.execute(
                "SELECT spec FROM visualizations WHERE LOWER(nome) = ?", (n,)
            ).fetchone()
            if row is None:
                like = f"%{n}%"
                row = conn.execute(
                    "SELECT spec FROM visualizations "
                    "WHERE LOWER(nome) LIKE ? OR ? LIKE '%' || LOWER(nome) || '%' "
                    "ORDER BY atualizado_em DESC LIMIT 1",
                    (like, n),
                ).fetchone()
        return json.loads(row["spec"]) if row else None

    def set_last(self, spec: dict) -> None:
        """Persiste o último design exibido (nome reservado, fora da lista)."""
        agora = _agora()
        with connect() as conn:
            conn.execute(
                "INSERT INTO visualizations (nome, spec, criado_em, atualizado_em) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT (nome) DO UPDATE SET spec = excluded.spec, "
                "atualizado_em = excluded.atualizado_em",
                (_ULTIMA, json.dumps(spec, ensure_ascii=False), agora, agora),
            )

    def get_last(self) -> dict | None:
        with connect() as conn:
            row = conn.execute(
                "SELECT spec FROM visualizations WHERE nome = ?", (_ULTIMA,)
            ).fetchone()
        return json.loads(row["spec"]) if row else None

    def list(self) -> list[dict]:
        with connect() as conn:
            rows = conn.execute(
                "SELECT nome, spec, atualizado_em FROM visualizations "
                "WHERE nome NOT LIKE '\\_\\_%' ESCAPE '\\' "
                "ORDER BY atualizado_em DESC"
            ).fetchall()
        out = []
        for r in rows:
            try:
                spec = json.loads(r["spec"])
            except json.JSONDecodeError:
                spec = {}
            out.append({"nome": r["nome"], "tipo": spec.get("tipo"),
                        "titulo": spec.get("titulo"), "atualizado_em": r["atualizado_em"]})
        return out

    def remove(self, nome: str) -> bool:
        with connect() as conn:
            cur = conn.execute(
                "DELETE FROM visualizations WHERE LOWER(nome) = ?",
                ((nome or "").strip().lower(),),
            )
            return cur.rowcount > 0
