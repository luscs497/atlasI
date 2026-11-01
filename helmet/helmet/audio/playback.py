"""Reprodução nas caixas do PC (PulseAudio/PipeWire via `paplay --raw`)."""

from __future__ import annotations

import asyncio
import shutil


class PcPlayer:
    """Um stream de PCM s16le mono tocado em tempo real (frase após frase)."""

    def __init__(self, sample_rate: int) -> None:
        self.sample_rate = sample_rate
        self._proc: asyncio.subprocess.Process | None = None

    @staticmethod
    def available() -> bool:
        return shutil.which("paplay") is not None

    async def start(self) -> None:
        self._proc = await asyncio.create_subprocess_exec(
            "paplay", "--raw", "--format=s16le", f"--rate={self.sample_rate}",
            "--channels=1", "--client-name=atlas-capacete",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )

    async def write(self, pcm: bytes) -> None:
        if self._proc is None or self._proc.stdin is None:
            return
        self._proc.stdin.write(pcm)
        await self._proc.stdin.drain()

    async def finish(self) -> None:
        """Fecha a entrada e espera o áudio terminar de tocar."""
        if self._proc is None:
            return
        try:
            if self._proc.stdin:
                self._proc.stdin.close()
            await asyncio.wait_for(self._proc.wait(), timeout=120)
        except (asyncio.TimeoutError, BrokenPipeError, ConnectionResetError):
            self._proc.kill()
        self._proc = None
