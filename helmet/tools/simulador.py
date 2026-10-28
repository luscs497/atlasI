"""
Simulador do capacete (ESP32) — fala exatamente o protocolo do firmware.

Serve para testar o backend antes do hardware ficar pronto:

    # fala pelo microfone do PC (Ctrl+C sai)
    helmet/.venv/bin/python helmet/tools/simulador.py --mic

    # frases sintetizadas (sem precisar falar), com pausa entre elas
    helmet/.venv/bin/python helmet/tools/simulador.py --say "Jarvis." --say "Que horas são?"

    # arquivos WAV
    helmet/.venv/bin/python helmet/tools/simulador.py --wav pergunta.wav

Com `--speaker` o simulador declara alto-falante e o áudio da resposta vem pelo
WebSocket (como no capacete com amplificador I2S); é tocado no PC (`--play`) e/ou
salvo em WAV (`--save-audio`). Sem `--speaker`, o backend toca no PC sozinho.

Com `--display 16x2` (ou 20x4, 21x5…) o simulador declara um display I2C no
hello e desenha cada quadro `display` recebido numa caixinha ASCII:

    helmet/.venv/bin/python helmet/tools/simulador.py --display 16x2 --say "Jarvis, oi"
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import websockets  # noqa: E402

from helmet.audio.resample import pcm_to_wav, resample_pcm, wav_to_pcm  # noqa: E402

RATE = 16_000
CHUNK_MS = 40


def parse_display(raw: str | None) -> dict | None:
    if not raw:
        return None
    cols, _, rows = raw.lower().partition("x")
    return {"cols": int(cols), "rows": int(rows), "ascii": True}


def draw_box(lines: list[str], cols: int, rows: int) -> str:
    """O que o LCD/OLED mostraria: `rows` linhas de `cols` colunas."""
    body = [(lines[i] if i < len(lines) else "").ljust(cols) for i in range(rows)]
    bar = "+" + "-" * cols + "+"
    return "\n".join([bar, *(f"|{ln}|" for ln in body), bar])


def log(*a) -> None:  # noqa: ANN002
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def synth(text: str) -> bytes:
    from helmet.audio.tts import VoiceSynth

    global _VOICE
    try:
        _VOICE
    except NameError:
        from helmet.settings import Settings

        voice, lang = Settings().voice()  # mesma língua/voz do capacete (HELMET_LANGUAGE)
        _VOICE = VoiceSynth.from_jarvis_config(voice=voice, lang=lang)
    return _VOICE.synth_pcm(text, RATE)


NOISE_RMS = 0.0  # ruído branco somado a TUDO que é enviado (--noise)


def add_noise(pcm: bytes) -> bytes:
    if NOISE_RMS <= 0:
        return pcm
    import numpy as np

    a = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
    a += np.random.default_rng().normal(0, NOISE_RMS, a.size)
    return np.clip(a, -32768, 32767).astype("<i2").tobytes()


async def send_realtime(ws, pcm: bytes) -> None:  # noqa: ANN001
    """Manda em pacotes de 40 ms no ritmo real (como o I2S do ESP32)."""
    pcm = add_noise(pcm)
    step = RATE * 2 * CHUNK_MS // 1000
    t0 = time.monotonic()
    for n, i in enumerate(range(0, len(pcm), step)):
        await ws.send(pcm[i:i + step])
        delay = t0 + (n + 1) * CHUNK_MS / 1000 - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)


async def main(args) -> int:  # noqa: ANN001
    token = args.token or os.environ.get("HELMET_TOKEN", "")
    url = f"{args.url}/ws/device?device_id={args.device}" + (f"&token={token}" if token else "")
    received = bytearray()
    done = asyncio.Event()
    replies: list[dict] = []
    t_speech_end = [0.0]
    stats: list[dict] = []
    active = [False]
    awaiting = [False]  # chegou a resposta; falta o JARVIS terminar (falar no PC)
    display = parse_display(args.display)

    async with websockets.connect(url, max_size=None) as ws:
        await ws.send(json.dumps({
            "type": "hello", "fw": "simulador", "mic_rate": RATE,
            "speaker": args.speaker, "speaker_rate": args.speaker_rate,
            **({"display": display} if display else {}),
        }))

        async def reader() -> None:
            player = None
            first_audio = None
            async for raw in ws:
                if isinstance(raw, bytes):
                    if first_audio is None:
                        first_audio = time.monotonic()
                        lat = first_audio - t_speech_end[0]
                        stats.append({"reply_s": lat})
                        log(f"🔊 1º áudio em {lat:.2f}s após o fim da fala")
                    received.extend(raw)
                    if player:
                        player.stdin.write(raw)
                    continue
                m = json.loads(raw)
                kind = m.get("type")
                if kind == "tone":
                    log(f"🎵 bipe '{m['name']}'")
                elif kind == "state":
                    active[0] = m["active"]
                    if awaiting[0] and m["state"] in ("listening", "idle"):
                        awaiting[0] = False
                        done.set()
                    log(f"◆ estado {m['state']} (ativo={m['active']}, expira em {m['expires_in']})")
                elif kind == "transcript":
                    log(f"❯ você: {m['text']}")
                elif kind == "reply":
                    replies.append(m)
                    log(f"● JARVIS: {m['text']}")
                    if not args.speaker:
                        stats.append({"reply_s": time.monotonic() - t_speech_end[0]})
                        awaiting[0] = True  # espera o JARVIS terminar (mic mudo)
                elif kind == "audio_start":
                    first_audio = None
                    if args.play:
                        player = subprocess.Popen(
                            ["paplay", "--raw", "--format=s16le", f"--rate={m['rate']}", "--channels=1"],
                            stdin=subprocess.PIPE)
                elif kind == "audio_end":
                    log(f"🔇 fim do áudio ({m['duration_ms']} ms)")
                    if player:
                        player.stdin.close()
                        player.wait()
                        player = None
                    await ws.send(json.dumps({"type": "playback_done", "seq": m["seq"]}))
                    done.set()
                elif kind == "display":
                    d = display or {"cols": 16, "rows": 2}
                    log(f"🖵 display [{m['kind']}, ttl {m['ttl_ms']} ms] "
                        f"({len(m['text'])} chars)\n" + draw_box(m["lines"], d["cols"], d["rows"]))
                    if any(len(ln) > d["cols"] for ln in m["lines"]) or len(m["lines"]) > d["rows"]:
                        log("⚠ quadro NÃO cabe no display!")
                elif kind == "welcome":
                    log(f"✔ conectado: destino do áudio = {m['audio_target']}, "
                        f"modelos prontos = {m['models_ready']}, display = {m.get('display')}")
                else:
                    log("·", m)

        rd = asyncio.create_task(reader())
        await asyncio.sleep(0.5)

        if args.mic:
            log("🎤 microfone aberto — diga 'Jarvis…' (Ctrl+C para sair)")
            proc = await asyncio.create_subprocess_exec(
                "parecord", "--raw", "--format=s16le", f"--rate={RATE}", "--channels=1",
                "--latency-msec=40", stdout=asyncio.subprocess.PIPE)
            step = RATE * 2 * CHUNK_MS // 1000
            try:
                while chunk := await proc.stdout.readexactly(step):
                    await ws.send(chunk)
            except (asyncio.IncompleteReadError, KeyboardInterrupt):
                pass
            finally:
                proc.kill()
            return 0

        items: list[tuple[str, bytes]] = []
        for text in args.say:
            items.append((text, synth(text)))
        for path in args.wav:
            pcm, sr = wav_to_pcm(Path(path).read_bytes())
            items.append((path, resample_pcm(pcm, sr, RATE)))

        silence = b"\x00\x00" * (RATE * 900 // 1000)
        for label, pcm in items:
            done.clear()
            log(f"🗣  enviando: {label}")
            await send_realtime(ws, b"\x00\x00" * (RATE // 5) + pcm)
            t_speech_end[0] = time.monotonic()
            await send_realtime(ws, silence)  # silêncio p/ o VAD fechar o enunciado
            # Só espera resposta se houver pedido (o "Jarvis" sozinho só ativa).
            from helmet.wake import find_wake, is_sleep_command

            m = find_wake(label)
            if m is not None and not m.command:
                wait = 2.5  # "Jarvis" sozinho: só ativa
            elif is_sleep_command(label) or (m is None and not active[0]):
                wait = 3.0  # vai dormir / será ignorado: não há resposta
            else:
                wait = args.wait
            try:
                await asyncio.wait_for(done.wait(), timeout=wait)
            except asyncio.TimeoutError:
                pass
            await asyncio.sleep(args.gap)

        if args.idle:
            log(f"⏳ parado {args.idle}s (testando o AFK)…")
            end = time.monotonic() + args.idle
            while time.monotonic() < end:  # mantém o mic "aberto" mandando silêncio
                await send_realtime(ws, b"\x00\x00" * (RATE // 2))
        await asyncio.sleep(0.5)
        rd.cancel()

    if args.save_audio and received:
        Path(args.save_audio).write_bytes(pcm_to_wav(bytes(received), args.speaker_rate))
        log(f"áudio da resposta salvo em {args.save_audio}")
    if stats:
        log("tempos fim-da-fala → resposta:", ", ".join(f"{s['reply_s']:.2f}s" for s in stats))
    return 0 if replies or args.mic or not items else 1


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", default="ws://127.0.0.1:8770")
    p.add_argument("--device", default="capacete-sim")
    p.add_argument("--token", default=None, help="padrão: HELMET_TOKEN do ambiente/.env")
    p.add_argument("--mic", action="store_true", help="usa o microfone do PC")
    p.add_argument("--say", action="append", default=[], help="frase sintetizada (repita)")
    p.add_argument("--wav", action="append", default=[], help="arquivo WAV (repita)")
    p.add_argument("--speaker", action="store_true", help="declara alto-falante no capacete")
    p.add_argument("--speaker-rate", type=int, default=16_000)
    p.add_argument("--display", default=None, metavar="COLSxROWS",
                   help="declara display I2C (ex.: 16x2, 20x4, 21x5) e desenha os quadros")
    p.add_argument("--play", action="store_true", help="toca o áudio recebido no PC")
    p.add_argument("--save-audio", default=None)
    p.add_argument("--wait", type=float, default=180.0, help="espera máx. pela resposta inteira")
    p.add_argument("--gap", type=float, default=0.5, help="pausa entre frases")
    p.add_argument("--idle", type=float, default=0.0, help="fica calado N s no fim")
    p.add_argument("--noise", type=float, default=0.0,
                   help="ruído branco (RMS em int16; ex.: 300 = mic barulhento)")
    a = p.parse_args()
    NOISE_RMS = a.noise
    if not a.token:
        from dotenv import dotenv_values

        a.token = dotenv_values(ROOT.parent / ".env").get("HELMET_TOKEN", "")
    sys.exit(asyncio.run(main(a)))
