"""
Filtro passa-alta do microfone do capacete (com estado entre pacotes).

Tira o zumbido da rede elétrica (60/120 Hz) e o ronco grave que o INMP441 pega
pelos fios/alimentação. A voz útil p/ o Whisper está acima de ~150 Hz.
"""

from __future__ import annotations

import numpy as np
from scipy.signal import butter, sosfilt, sosfilt_zi


class HighPass:
    def __init__(self, cutoff_hz: float, sample_rate: int, order: int = 4) -> None:
        self._sos = butter(order, cutoff_hz, "highpass", fs=sample_rate, output="sos")
        self._zi = sosfilt_zi(self._sos) * 0.0

    def process(self, pcm: bytes) -> bytes:
        x = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
        if not x.size:
            return pcm
        y, self._zi = sosfilt(self._sos, x, zi=self._zi)
        return np.clip(y, -32768, 32767).astype("<i2").tobytes()
