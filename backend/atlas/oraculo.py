"""
Oráculo: ATLAS consulta o Claude quando não sabe algo.

O cérebro de rotina é barato (gpt-4o-mini). Quando ele não tem uma informação,
fica em dúvida, ou a pergunta exige raciocínio mais profundo, ele encaminha a
questão ao **Claude** (via CLI headless `claude -p`) e usa a resposta. O melhor
dos dois mundos: GPT barato no dia a dia, Claude quando precisa pensar mais.

Diferente do `ClaudeCoder` (que EXECUTA tarefas de código numa pasta), aqui é só
uma pergunta → resposta. Roda numa pasta TEMPORÁRIA isolada (o oráculo não deve
mexer nos projetos) e pede uma resposta direta e concisa.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile

# Enquadra a pergunta para o Claude responder direto, ciente de que é consultado
# por outro assistente (evita preâmbulos do tipo "claro, aqui está…").
_MOLDURA = (
    "Você está sendo consultado como ORÁCULO por outro assistente de IA (o ATLAS), "
    "que encaminha o que não sabe. Responda de forma direta, precisa e concisa, sem "
    "preâmbulo e sem se apresentar. Se não tiver certeza, diga o que é provável e o "
    "que é incerto. Pergunta:\n\n{pergunta}"
)


class Oracle:
    """Consulta o Claude (headless) para perguntas fora do alcance do GPT."""

    def __init__(self, model: str, timeout: int = 120) -> None:
        self.model = model
        self.timeout = timeout
        self.bin = shutil.which("claude")

    def available(self) -> bool:
        return self.bin is not None

    async def ask(self, pergunta: str) -> dict:
        """Faz a pergunta ao Claude e devolve {ok, resposta, modelo, custo_usd}."""
        pergunta = (pergunta or "").strip()
        if not pergunta:
            return {"ok": False, "resposta": "Nenhuma pergunta informada."}
        if not self.bin:
            return {"ok": False, "resposta": "Claude não encontrado (fora do PATH)."}

        prompt = _MOLDURA.format(pergunta=pergunta)
        cmd = [self.bin, "-p", prompt, "--output-format", "json"]
        if self.model:
            cmd += ["--model", self.model]

        print(f"[oraculo] {self.model}: {pergunta[:80]}{'…' if len(pergunta) > 80 else ''}",
              flush=True)
        # Pasta temporária isolada — o oráculo não deve tocar nos projetos.
        with tempfile.TemporaryDirectory(prefix="atlas-oraculo-") as cwd:
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd, cwd=cwd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                out, err = await asyncio.wait_for(proc.communicate(), timeout=self.timeout)
            except asyncio.TimeoutError:
                return {"ok": False,
                        "resposta": f"O oráculo ultrapassou {self.timeout}s e foi interrompido."}
            except Exception as exc:
                return {"ok": False, "resposta": f"Falha ao consultar o Claude: {exc!r}"}

        raw = (out or b"").decode("utf-8", "replace").strip()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            msg = raw or (err or b"").decode("utf-8", "replace").strip()
            return {"ok": proc.returncode == 0, "resposta": msg[-2000:] or "Sem resposta."}

        resposta = (data.get("result") or "").strip()
        return {
            "ok": (not data.get("is_error", False)) and proc.returncode == 0,
            "resposta": resposta or "(sem resposta do oráculo)",
            "modelo": self.model,
            "custo_usd": data.get("total_cost_usd"),
        }

    @classmethod
    def from_config(cls, config: dict) -> "Oracle | None":
        if not config.get("oraculo_enabled", True):
            return None
        return cls(
            model=config.get("oraculo_model", "claude-sonnet-4-6"),
            timeout=int(config.get("oraculo_timeout", 120)),
        )
