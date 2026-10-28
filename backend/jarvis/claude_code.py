"""
Ponte JARVIS -> Claude Code: execução de tarefas de programação em modo headless.

Quando o cérebro identifica que um pedido é uma AÇÃO de código (criar/editar
arquivos, refatorar, corrigir bug, montar projeto…), ele aciona esta ponte, que
roda o `claude` em modo não-interativo:

    claude -p "<tarefa>" --output-format json --dangerously-skip-permissions --model <m>

O `--dangerously-skip-permissions` é intencional: o usuário quer que o JARVIS aja
sem perguntar "yes/no" a cada alteração. O escopo é limitado a uma pasta-base
(`~/projetos` por padrão) para o agente não sair passeando pelo sistema.

O modelo é configurável e "grudento": o usuário pode pedir um modelo na própria
mensagem ("use o Opus 4.8…") e ele vira o novo padrão até ser trocado de novo.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
from pathlib import Path

from jarvis.config import save_config

# Famílias de modelo que reconhecemos numa fala do usuário.
_FAMILIES = ("opus", "sonnet", "haiku", "fable")


def normalize_model(texto: str) -> str | None:
    """
    Converte um jeito humano de nomear o modelo no `--model` do Claude Code.
    Ex.: 'Opus 4.8' -> 'claude-opus-4-8'; 'Sonnet 4.6' -> 'claude-sonnet-4-6';
    'haiku' (sem versão) -> 'haiku' (alias válido). Retorna None se não achar
    nenhuma família conhecida.
    """
    if not texto:
        return None
    s = texto.lower()
    familia = next((f for f in _FAMILIES if f in s), None)
    if not familia:
        return None
    m = re.search(r"(\d+)\s*[.\-]\s*(\d+)", s)  # "4.8", "4-6", "4 . 5"
    if m:
        return f"claude-{familia}-{m.group(1)}-{m.group(2)}"
    return familia  # alias puro (Claude Code resolve para a versão mais recente)


class ClaudeCoder:
    """Executa tarefas de código via Claude Code headless, com escopo e modelo."""

    def __init__(
        self,
        base_dir: str,
        model: str,
        timeout: int = 600,
        config: dict | None = None,
    ) -> None:
        self.base_dir = Path(base_dir).expanduser()
        self.model = model
        self.timeout = timeout
        self._config = config  # para persistir a troca de modelo padrão
        self.bin = shutil.which("claude")

    def available(self) -> bool:
        return self.bin is not None

    def set_default_model(self, pedido: str) -> str:
        """Troca o modelo padrão (persistindo na config) e devolve o valor final."""
        norm = normalize_model(pedido) or (pedido or "").strip()
        if not norm:
            return self.model
        self.model = norm
        if self._config is not None:
            self._config["claude_code_model"] = norm
            try:
                save_config(self._config)
            except Exception as exc:  # persistir é best-effort
                print(f"[claude-code] falha ao salvar modelo padrão: {exc!r}", flush=True)
        return norm

    def _resolve_cwd(self, projeto: str) -> Path:
        """Resolve a pasta de trabalho, mantendo tudo dentro da base (segurança)."""
        base = self.base_dir.resolve()
        if not projeto:
            return base
        alvo = (self.base_dir / projeto).resolve()
        if (alvo == base or base in alvo.parents) and alvo.exists():
            return alvo
        return base  # projeto inexistente ou fora da base -> cai na base

    async def run(self, tarefa: str, projeto: str = "") -> dict:
        """Roda a tarefa no Claude Code e devolve um dict com o resultado."""
        tarefa = (tarefa or "").strip()
        if not tarefa:
            return {"ok": False, "summary": "Nenhuma tarefa informada."}
        if not self.bin:
            return {
                "ok": False,
                "summary": "Claude Code não encontrado (comando 'claude' fora do PATH).",
            }

        cwd = self._resolve_cwd(projeto)
        cmd = [
            self.bin, "-p", tarefa,
            "--output-format", "json",
            "--dangerously-skip-permissions",
        ]
        if self.model:
            cmd += ["--model", self.model]

        print(
            f"[claude-code] {self.model} em {cwd}: {tarefa[:80]}"
            f"{'…' if len(tarefa) > 80 else ''}",
            flush=True,
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, err = await asyncio.wait_for(
                proc.communicate(), timeout=self.timeout
            )
        except asyncio.TimeoutError:
            return {
                "ok": False,
                "summary": f"A tarefa ultrapassou {self.timeout}s e foi interrompida.",
            }
        except Exception as exc:
            return {"ok": False, "summary": f"Falha ao executar o Claude Code: {exc!r}"}

        raw = (out or b"").decode("utf-8", "replace").strip()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            msg = raw or (err or b"").decode("utf-8", "replace").strip()
            return {"ok": proc.returncode == 0, "summary": msg[-1500:] or "Sem saída."}

        resultado = (data.get("result") or "").strip()
        return {
            "ok": (not data.get("is_error", False)) and proc.returncode == 0,
            "summary": resultado or "(sem resumo do Claude Code)",
            "modelo": self.model,
            "projeto": projeto or self.base_dir.name,
            "custo_usd": data.get("total_cost_usd"),
            "num_turns": data.get("num_turns"),
        }

    @classmethod
    def from_config(cls, config: dict) -> "ClaudeCoder | None":
        """Constrói a partir da config. None se a integração estiver desligada."""
        if not config.get("claude_code_enabled", True):
            return None
        return cls(
            base_dir=config.get("claude_code_base_dir", "~/projetos"),
            model=config.get("claude_code_model", "claude-sonnet-4-6"),
            timeout=int(config.get("claude_code_timeout", 600)),
            config=config,
        )
