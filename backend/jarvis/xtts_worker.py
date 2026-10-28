"""
Worker persistente do XTTS-v2 (Coqui) — roda na venv ISOLADA `.venv-xtts`.

O XTTS depende de PyTorch e de um `transformers` antigo, que conflitam com o
ambiente principal (Whisper/numpy). Por isso vive numa venv separada e é falado
por subprocesso. Carregar o modelo custa ~15s, então este processo carrega UMA
vez e fica em loop lendo pedidos do stdin (uma linha JSON por pedido):

    {"text": "...", "speaker": "Royston Min", "language": "pt", "out": "/tmp/x.wav"}

Para cada pedido, sintetiza no arquivo `out` e responde no stdout com 'OK' ou
'ERR <msg>'. Linhas de log da lib também vão pro stdout; o lado de cá (XTTSSpeaker)
lê até encontrar 'OK'/'ERR', ignorando o resto.
"""

import json
import os
import sys
import warnings

warnings.filterwarnings("ignore")
os.environ.setdefault("COQUI_TOS_AGREED", "1")


def main() -> None:
    from TTS.api import TTS

    model = os.environ.get("XTTS_MODEL", "tts_models/multilingual/multi-dataset/xtts_v2")
    tts = TTS(model, progress_bar=False)
    print("READY", flush=True)

    for linha in sys.stdin:
        linha = linha.strip()
        if not linha:
            continue
        try:
            req = json.loads(linha)
            tts.tts_to_file(
                text=req["text"],
                speaker=req.get("speaker", "Royston Min"),
                language=req.get("language", "pt"),
                file_path=req["out"],
            )
            print("OK", flush=True)
        except Exception as exc:  # nunca derruba o worker
            print("ERR " + str(exc).replace("\n", " ")[:200], flush=True)


if __name__ == "__main__":
    main()
