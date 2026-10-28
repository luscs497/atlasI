"""Conversões PCM ⇄ WAV e reamostragem (numpy puro, sem scipy)."""

from __future__ import annotations

import io
import wave

import numpy as np


def wav_to_pcm(wav: bytes) -> tuple[bytes, int]:
    """WAV (s16 mono ou estéreo) -> (PCM s16le mono, sample_rate)."""
    with wave.open(io.BytesIO(wav), "rb") as wf:
        sr, ch, width = wf.getframerate(), wf.getnchannels(), wf.getsampwidth()
        data = wf.readframes(wf.getnframes())
    if width != 2:
        raise ValueError(f"WAV com {8 * width} bits não suportado")
    if ch > 1:
        a = np.frombuffer(data, dtype="<i2").reshape(-1, ch).mean(axis=1)
        data = a.astype("<i2").tobytes()
    return data, sr


def pcm_to_wav(pcm: bytes, sample_rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm)
    return buf.getvalue()


def _lowpass(x: np.ndarray, cutoff: float, taps: int = 63) -> np.ndarray:
    """FIR passa-baixa (sinc janelado); `cutoff` em fração de Nyquist (0..1)."""
    n = np.arange(taps) - (taps - 1) / 2
    h = cutoff * np.sinc(cutoff * n) * np.hamming(taps)
    h /= h.sum()
    return np.convolve(x, h, mode="same")


def resample_pcm(pcm: bytes, src_rate: int, dst_rate: int) -> bytes:
    if src_rate == dst_rate or not pcm:
        return pcm
    x = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
    if dst_rate < src_rate:  # anti-aliasing antes de descer a taxa
        x = _lowpass(x, dst_rate / src_rate * 0.95)
    n_out = int(round(len(x) * dst_rate / src_rate))
    t_out = np.arange(n_out) * (src_rate / dst_rate)
    y = np.interp(t_out, np.arange(len(x)), x)
    return np.clip(y, -32768, 32767).astype("<i2").tobytes()


def pcm_to_float(pcm: bytes) -> np.ndarray:
    return np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0


def duration_ms(pcm: bytes, sample_rate: int) -> int:
    return int(len(pcm) / 2 / sample_rate * 1000)
