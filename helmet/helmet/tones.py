"""
Bipes dos buzzers do capacete.

O backend manda a "partitura" pronta — `notes: [[freq_hz, dur_ms], …]`, com
freq 0 = pausa — e o firmware só toca (LEDC/`tone()`). Assim dá pra mudar os
sons sem regravar o ESP32. `buzzer`: 0, 1 ou "both" (os dois em uníssono).
"""

from __future__ import annotations

TONES: dict[str, dict] = {
    # conectou no backend
    "ready": {"notes": [[1047, 60], [0, 40], [1568, 90]], "buzzer": "both"},
    # ouviu "Jarvis" — escuta ativa
    "wake": {"notes": [[880, 70], [0, 30], [1319, 70], [0, 30], [1760, 110]], "buzzer": "both"},
    # recebeu o pedido, pensando
    "ack": {"notes": [[1568, 45]], "buzzer": 0},
    # voltou a dormir (AFK / comando)
    "sleep": {"notes": [[1760, 70], [0, 30], [1319, 70], [0, 30], [880, 120]], "buzzer": "both"},
    # erro (cérebro/rede)
    "error": {"notes": [[330, 180], [0, 60], [262, 260]], "buzzer": "both"},
}


def tone_message(name: str) -> dict:
    t = TONES[name]
    return {"type": "tone", "name": name, "notes": t["notes"], "buzzer": t["buzzer"]}
