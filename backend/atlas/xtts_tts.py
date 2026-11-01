"""
Voz do ATLAS via XTTS-v2 (Coqui) — TTS local de alta naturalidade em PT-BR.

Qualidade excelente e voz nativa em português, MAS pesado: roda numa venv isolada
(`.venv-xtts`, com PyTorch) e a inferência na CPU é lenta — tipicamente perto do
tempo real (uma frase de ~10s leva ~10s+). Para um assistente ao vivo isso é
bastante latência; o Kokoro é muito mais rápido. Fica como opção para quem prioriza
naturalidade sobre velocidade.

Mesma interface `synthesize(text) -> bytes` (WAV). Fala com um worker persistente
(`xtts_worker.py`) por subprocesso, para carregar o modelo só uma vez. Serializa
os pedidos (um worker) e tem `fallback` (Piper) para nunca ficar mudo.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent  # .../backend
_XTTS_PYTHON = _BACKEND / ".venv-xtts" / "bin" / "python"
_WORKER = Path(__file__).resolve().parent / "xtts_worker.py"


class XTTSSpeaker:
    """Sintetizador via XTTS-v2 num worker persistente (venv isolada)."""

    def __init__(self, speaker: str, language: str = "pt", fallback=None) -> None:
        self.speaker = speaker
        self.language = language
        self._fallback = fallback
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()

    @property
    def label(self) -> str:
        return f"xtts {self.speaker} ({self.language})"

    def _ensure_worker(self) -> None:
        """Sobe o worker (carrega o modelo, ~15s) se ainda não estiver vivo."""
        if self._proc is not None and self._proc.poll() is None:
            return
        env = {**os.environ, "COQUI_TOS_AGREED": "1"}
        self._proc = subprocess.Popen(
            [str(_XTTS_PYTHON), "-u", str(_WORKER)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, env=env, text=True,
        )
        # Espera o 'READY' (ignora logs da lib no meio).
        while True:
            line = self._proc.stdout.readline()
            if not line:  # worker morreu ao carregar
                raise RuntimeError("worker do XTTS não inicializou")
            if line.strip() == "READY":
                break

    def synthesize(self, text: str) -> bytes:
        text = (text or "").strip()
        if not text:
            return b""
        try:
            with self._lock:  # um worker só — serializa os pedidos
                self._ensure_worker()
                out = Path(tempfile.mkstemp(prefix="atlas-xtts-", suffix=".wav")[1])
                req = {"text": text, "speaker": self.speaker,
                       "language": self.language, "out": str(out)}
                self._proc.stdin.write(json.dumps(req) + "\n")
                self._proc.stdin.flush()
                status = ""
                while True:
                    line = self._proc.stdout.readline()
                    if not line:
                        raise RuntimeError("worker do XTTS encerrou")
                    line = line.strip()
                    if line == "OK" or line.startswith("ERR"):
                        status = line
                        break
                if status != "OK":
                    raise RuntimeError(status)
                data = out.read_bytes()
                out.unlink(missing_ok=True)
                return data
        except Exception as exc:
            print(f"[xtts] falha ({exc!r}); usando fallback.", flush=True)
            return self._fallback.synthesize(text) if self._fallback else b""

    @classmethod
    def from_config(cls, config: dict, fallback=None) -> "XTTSSpeaker | None":
        """Disponível só se a venv isolada existir (senão None → usa fallback)."""
        if not _XTTS_PYTHON.exists() or not _WORKER.exists():
            return None
        return cls(
            speaker=config.get("xtts_speaker", "Royston Min"),
            language=config.get("xtts_language", "pt"),
            fallback=fallback,
        )
