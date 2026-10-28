"""
Perplexity Sonar: fonte de dados em TEMPO REAL para o JARVIS.

Quando o usuário pergunta algo que muda com o tempo (cotações, clima, notícias,
resultados, preços, qualquer dado atual), o cérebro NÃO deve chutar, perguntar ao
usuário nem recorrer ao Claude — chama a API online da Perplexity (Sonar), que
pesquisa na web, e usa a resposta para responder com naturalidade (sem citar a
fonte).

A API da Perplexity é compatível com o formato Chat Completions da OpenAI. Aqui
usamos uma chamada HTTP direta (urllib em thread) para não depender de SDK, no
mesmo espírito do `elevenlabs_tts`. A chave vem do ambiente (PERPLEXITY_API_KEY).

Nota: os modelos `llama-3.1-sonar-*-online` foram descontinuados pela Perplexity
em 2025; os atuais são `sonar` e `sonar-pro`. O modelo é configurável.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import urllib.error
import urllib.request

_ENDPOINT = "https://api.perplexity.ai/chat/completions"

_SISTEMA = (
    "Você é um mecanismo de busca em tempo real. Responda em português do Brasil, "
    "de forma direta, factual e concisa, com os dados MAIS ATUAIS disponíveis. "
    "Inclua números/datas precisos quando fizer sentido (ex.: cotação com valor e "
    "data/hora). Não faça preâmbulo."
)

# Modo estruturado: para séries/listas (ex.: cotação por dia) que vão virar
# gráfico. Devolve só os dados crus, sem prosa, para o cérebro extrair os pontos.
_SISTEMA_ESTRUTURADO = (
    "Você é uma FONTE DE DADOS. Responda APENAS com os dados pedidos, crus e "
    "estruturados: uma entrada por linha, exatamente no formato solicitado (ex.: "
    "'AAAA-MM-DD: valor'). Use os dados reais e mais recentes disponíveis. SEM "
    "introdução, SEM conclusão, SEM comentários, SEM markdown. Se faltar algum dia, "
    "traga os dias que existirem — nunca invente, mas também nunca se recuse a "
    "responder: entregue o máximo de dados reais que conseguir."
)

# Remove blocos de raciocínio dos modelos 'sonar-reasoning*' (se configurados).
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)

# Linha de série no modo estruturado: "<rótulo>: <valor>" (ex.: "2026-07-10: 5.37").
_LINHA_RE = re.compile(r"^\s*[-*•]?\s*(.+?)\s*:\s*[^\d]*([\d][\d.,]*)\s*%?\s*\**\s*$")


def _parse_valor(s: str) -> float:
    s = s.strip()
    if "," in s and "." in s:  # 1.234,56 -> 1234.56
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:             # 5,37 -> 5.37
        s = s.replace(",", ".")
    return float(s)


def _parse_serie(texto: str, limite: int = 90) -> list[dict]:
    """Extrai pontos {rotulo, valor} das linhas 'rótulo: valor' (modo estruturado)."""
    pontos = []
    for linha in texto.splitlines():
        m = _LINHA_RE.match(linha)
        if not m:
            continue
        rotulo = m.group(1).strip().strip("*_`")
        try:
            valor = _parse_valor(m.group(2))
        except ValueError:
            continue
        pontos.append({"rotulo": rotulo, "valor": valor})
        if len(pontos) >= limite:
            break
    return pontos


class Perplexity:
    """Busca informações atuais via Perplexity Sonar (web em tempo real)."""

    def __init__(self, api_key: str, model: str, timeout: int = 30) -> None:
        self.api_key = (api_key or "").strip()
        self.model = model
        self.timeout = timeout

    def available(self) -> bool:
        return bool(self.api_key)

    async def search(self, consulta: str, estruturado: bool = False) -> dict:
        """Consulta a Perplexity e devolve {ok, resposta, citacoes, modelo}.

        `estruturado=True` pede dados crus (série/lista) para virar gráfico."""
        consulta = (consulta or "").strip()
        if not consulta:
            return {"ok": False, "resposta": "Nenhuma consulta informada."}
        if not self.api_key:
            return {"ok": False, "resposta": "Perplexity sem chave (PERPLEXITY_API_KEY)."}
        try:
            return await asyncio.to_thread(self._call, consulta, estruturado)
        except Exception as exc:  # rede/timeout — nunca derruba o cérebro
            return {"ok": False, "resposta": f"Falha ao consultar a Perplexity: {exc!r}"}

    def _call(self, consulta: str, estruturado: bool = False) -> dict:
        sistema = _SISTEMA_ESTRUTURADO if estruturado else _SISTEMA
        body = json.dumps({
            "model": self.model,
            "messages": [
                {"role": "system", "content": sistema},
                {"role": "user", "content": consulta},
            ],
        }).encode("utf-8")
        req = urllib.request.Request(
            _ENDPOINT,
            data=body,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        print(f"[perplexity] {self.model}: {consulta[:80]}"
              f"{'…' if len(consulta) > 80 else ''}", flush=True)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            detalhe = e.read().decode("utf-8", "replace")[:400]
            return {"ok": False,
                    "resposta": f"Perplexity HTTP {e.code}: {detalhe}", "http": e.code}

        resposta = ""
        try:
            resposta = (data["choices"][0]["message"]["content"] or "").strip()
        except (KeyError, IndexError, TypeError):
            pass
        resposta = _THINK_RE.sub("", resposta).strip()  # tira <think> de reasoning
        out = {
            "ok": bool(resposta),
            "resposta": resposta or "(sem resposta da Perplexity)",
            "citacoes": data.get("citations") or [],
            "modelo": self.model,
        }
        # No modo estruturado, já entrega a SÉRIE parseada (rótulo/valor) para o
        # cérebro montar o gráfico sem re-digitar ponto a ponto (evita truncar).
        if estruturado:
            serie = _parse_serie(resposta)
            if serie:
                out["serie"] = serie
                out["pontos"] = len(serie)
        return out

    @classmethod
    def from_config(cls, config: dict) -> "Perplexity | None":
        if not config.get("perplexity_enabled", True):
            return None
        api_key = os.environ.get("PERPLEXITY_API_KEY", "").strip()
        if not api_key:
            return None
        return cls(
            api_key=api_key,
            model=config.get("perplexity_model", "sonar-pro"),
            timeout=int(config.get("perplexity_timeout", 30)),
        )
