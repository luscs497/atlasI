"""
Segmentação do stream do microfone em ENUNCIADOS (detecção de fala / VAD).

O ESP32 manda PCM cru sem parar; aqui cortamos o fluxo em frames de 10–30 ms,
classificamos cada um (webrtcvad + piso de energia) e emitimos:

- `SpeechStart` quando há fala contínua por `start_ms`;
- `SpeechEnd(pcm)` quando há silêncio por `end_silence_ms` (ou estourou o máximo),
  com o áudio do enunciado inteiro (incluindo `preroll_ms` antes do início).

Piso de ruído ADAPTATIVO (`noise_factor`): mic barato/fios longos têm chiado e
zumbido que o webrtcvad confunde com voz (aí o enunciado nunca fecha e vira um
bloco de 30 s). O Segmenter mede o ruído de fundo (percentil 20 da energia dos
últimos ~5 s FORA de fala) e só conta como fala o quadro com energia
>= piso * `noise_factor`.

O classificador é injetável para testes.
"""

from __future__ import annotations

import collections
from dataclasses import dataclass
from typing import Callable

import numpy as np

Classifier = Callable[[bytes, int], bool]


@dataclass
class SpeechStart:
    pass


@dataclass
class SpeechEnd:
    pcm: bytes
    duration_ms: int
    forced: bool = False  # True = cortado por max_utterance


def rms_int16(frame: bytes) -> float:
    a = np.frombuffer(frame, dtype="<i2").astype(np.float32)
    return float(np.sqrt(np.mean(a * a))) if a.size else 0.0


def make_classifier(aggressiveness: int, min_rms: float) -> Classifier:
    try:
        import webrtcvad

        vad = webrtcvad.Vad(aggressiveness)

        def classify(frame: bytes, sr: int) -> bool:
            return rms_int16(frame) >= min_rms and vad.is_speech(frame, sr)

    except ImportError:  # sem webrtcvad: só energia (pior, mas funciona)
        threshold = max(min_rms, 500.0)

        def classify(frame: bytes, sr: int) -> bool:
            return rms_int16(frame) >= threshold

    return classify


class Segmenter:
    def __init__(
        self,
        sample_rate: int = 16_000,
        frame_ms: int = 30,
        classifier: Classifier | None = None,
        start_ms: int = 150,
        end_silence_ms: int = 700,
        preroll_ms: int = 300,
        max_utterance_s: float = 30.0,
        aggressiveness: int = 2,
        min_rms: float = 200.0,
        noise_factor: float = 0.0,
    ) -> None:
        if frame_ms not in (10, 20, 30):
            raise ValueError("frame_ms deve ser 10, 20 ou 30")
        self.sample_rate = sample_rate
        self.frame_ms = frame_ms
        self.frame_bytes = sample_rate * frame_ms // 1000 * 2
        self._classify = classifier or make_classifier(aggressiveness, min_rms)
        self._start_frames = max(1, start_ms // frame_ms)
        self._end_frames = max(1, end_silence_ms // frame_ms)
        self.noise_factor = noise_factor
        self._noise: collections.deque[float] = collections.deque(maxlen=max(10, 5000 // frame_ms))
        self.noise_floor = 0.0
        self._max_frames = int(max_utterance_s * 1000 // frame_ms)
        self._preroll: collections.deque[bytes] = collections.deque(
            maxlen=max(1, preroll_ms // frame_ms) + self._start_frames
        )
        self.reset()

    def reset(self) -> None:
        self._buf = bytearray()
        self._preroll.clear()
        self._voiced_run = 0
        self.in_speech = False
        self._frames: list[bytes] = []
        self._silence_run = 0

    def feed(self, pcm: bytes) -> list[SpeechStart | SpeechEnd]:
        events: list[SpeechStart | SpeechEnd] = []
        self._buf.extend(pcm)
        fb = self.frame_bytes
        while len(self._buf) >= fb:
            frame = bytes(self._buf[:fb])
            del self._buf[:fb]
            ev = self._process(frame)
            if ev is not None:
                events.append(ev)
        return events

    def _gate(self, frame: bytes) -> bool:
        """Energia acima do ruído de fundo medido (sempre True se desligado)."""
        if self.noise_factor <= 0:
            return True
        r = rms_int16(frame)
        if not self.in_speech:
            self._noise.append(r)
            if len(self._noise) >= 10 and len(self._noise) % 5 == 0:
                self.noise_floor = float(np.percentile(self._noise, 20))
        if len(self._noise) < 10:
            return False  # ~300 ms aprendendo o ruído de fundo (após conectar)
        return r >= self.noise_floor * self.noise_factor

    def _process(self, frame: bytes) -> SpeechStart | SpeechEnd | None:
        gate = self._gate(frame)
        voiced = gate and self._classify(frame, self.sample_rate)
        if not self.in_speech:
            self._preroll.append(frame)
            self._voiced_run = self._voiced_run + 1 if voiced else 0
            if self._voiced_run >= self._start_frames:
                self.in_speech = True
                self._frames = list(self._preroll)
                self._preroll.clear()
                self._silence_run = 0
                return SpeechStart()
            return None

        self._frames.append(frame)
        self._silence_run = 0 if voiced else self._silence_run + 1
        if self._silence_run >= self._end_frames:
            # Mantém ~200 ms do silêncio final (o Whisper gosta de uma folga).
            keep = len(self._frames) - self._silence_run + max(1, 200 // self.frame_ms)
            return self._finish(self._frames[:keep], forced=False)
        if len(self._frames) >= self._max_frames:
            return self._finish(self._frames, forced=True)
        return None

    def _finish(self, frames: list[bytes], forced: bool) -> SpeechEnd:
        pcm = b"".join(frames)
        self.in_speech = False
        self._frames = []
        self._voiced_run = 0
        self._silence_run = 0
        return SpeechEnd(pcm=pcm, duration_ms=len(frames) * self.frame_ms, forced=forced)
