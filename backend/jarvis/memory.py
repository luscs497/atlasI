"""
Memória de longo prazo do JARVIS.

Dá continuidade entre sessões. Dois tipos de memória, ambos em
`~/.jarvis/memoria.json`:

- **Episódica** (`historico`): as últimas trocas da conversa. Recarregadas no
  boot para o JARVIS não "esquecer" o assunto ao reiniciar o backend.
- **Semântica** (`fatos`): fatos duráveis sobre o usuário (onde mora, projetos,
  preferências). Injetados na persona para o JARVIS lembrar entre sessões.

Escrita atômica (arquivo temporário + rename) para nunca corromper o JSON se o
processo cair no meio de um save. Assistente de usuário único: uma instância,
sem concorrência entre processos.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from jarvis.config import MEMORY_PATH

# Limites para o arquivo não crescer sem controle. O histórico é uma janela
# rolante; os fatos são poucos e curtos por natureza.
MAX_HISTORICO = 40  # ~20 trocas guardadas em disco
MAX_FATOS = 200
BOOT_HISTORICO = 12  # quantas mensagens recarregar no chat ao subir


def _now() -> float:
    return round(time.time(), 3)


def _normalizar(texto: str) -> str:
    """Chave de comparação para deduplicar/casar fatos (sem caixa/pontuação)."""
    return " ".join(texto.lower().split()).strip(" .!?,;:")


class Memory:
    """Persistência de histórico de conversa e fatos duráveis do usuário."""

    def __init__(self, path: Path = MEMORY_PATH) -> None:
        self.path = path
        self.fatos: list[dict] = []
        self.historico: list[dict] = []
        self._load()

    # ---- carga / persistência -------------------------------------------

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # Arquivo corrompido não deve derrubar o boot — começa vazio.
            return
        if isinstance(data, dict):
            self.fatos = data.get("fatos") or []
            self.historico = data.get("historico") or []

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {"fatos": self.fatos, "historico": self.historico}
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp, self.path)  # atômico no mesmo filesystem

    # ---- histórico (episódico) ------------------------------------------

    def add_turn(self, role: str, texto: str) -> None:
        """Registra uma fala (`role`: 'user' ou 'jarvis') e apara a janela."""
        texto = (texto or "").strip()
        if not texto:
            return
        self.historico.append({"role": role, "texto": texto, "ts": _now()})
        if len(self.historico) > MAX_HISTORICO:
            self.historico = self.historico[-MAX_HISTORICO:]

    def boot_history(self) -> list[dict]:
        """Últimas mensagens para recarregar no chat ao subir o backend."""
        return self.historico[-BOOT_HISTORICO:]

    # ---- fatos (semântico) ----------------------------------------------

    def add_fact(self, texto: str, origem: str = "auto") -> bool:
        """Adiciona um fato durável se ainda não existir. Retorna True se novo."""
        texto = " ".join((texto or "").split()).strip()
        if not texto:
            return False
        chave = _normalizar(texto)
        if any(_normalizar(f["texto"]) == chave for f in self.fatos):
            return False
        self.fatos.append({"texto": texto, "ts": _now(), "origem": origem})
        if len(self.fatos) > MAX_FATOS:
            self.fatos = self.fatos[-MAX_FATOS:]
        return True

    def remove_facts(self, termo: str) -> list[str]:
        """Remove fatos que contenham `termo`. Retorna os textos removidos."""
        alvo = _normalizar(termo)
        if not alvo:
            return []
        removidos = [f["texto"] for f in self.fatos if alvo in _normalizar(f["texto"])]
        if removidos:
            self.fatos = [
                f for f in self.fatos if alvo not in _normalizar(f["texto"])
            ]
        return removidos

    def facts_block(self) -> str:
        """Bloco de fatos para injetar na persona (vazio se não houver)."""
        if not self.fatos:
            return ""
        linhas = "\n".join(f"- {f['texto']}" for f in self.fatos)
        return (
            "\n\nO que você já sabe sobre o usuário (lembranças de conversas "
            f"anteriores; use quando for útil, sem repetir à toa):\n{linhas}"
        )
