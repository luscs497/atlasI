"""Testes de unidade: wake word, VAD, áudio, texto falado, memória, conversas."""

from __future__ import annotations

import json

import numpy as np
import pytest

from helmet.audio.resample import pcm_to_wav, resample_pcm, wav_to_pcm
from helmet.audio.stt import is_hallucination
from helmet.audio.tts import clean_for_speech, split_sentences
from helmet.audio.vad import Segmenter, SpeechEnd, SpeechStart
from helmet.conversations import ConversationService
from helmet.db import init_db, make_engine, make_sessionmaker
from tests.conftest import FakeSummarizer, energy_vad, silence, speech
from helmet.wake import find_wake, is_sleep_command, strip_leading_wake


# ---- wake word ------------------------------------------------------------

@pytest.mark.parametrize("text,command", [
    ("Jarvis", ""),
    ("Jarvis.", ""),
    ("Jarvis, que horas são?", "que horas são?"),
    ("ok jarvis abre o spotify", "abre o spotify"),
    ("Ei, Djarvis, tudo bem?", "tudo bem?"),
    ("Jarves, liga a luz", "liga a luz"),
    ("JARVIS! Qual a cotação do dólar", "Qual a cotação do dólar"),
    ("Jarbis me lembra de pagar a conta", "me lembra de pagar a conta"),
])
def test_wake_detecta(text, command):
    m = find_wake(text)
    assert m is not None and m.command == command


@pytest.mark.parametrize("text", [
    "que horas são", "o jardim está bonito", "pega a jarra", "Marcos", "", "jarros",
])
def test_wake_nao_detecta(text):
    assert find_wake(text) is None


def test_strip_leading_wake():
    assert strip_leading_wake("Jarvis, e amanhã?") == "e amanhã?"
    assert strip_leading_wake("e amanhã?") == "e amanhã?"
    # nome no meio de uma frase longa não é cortado
    assert strip_leading_wake("eu acho que o seu nome jarvis é legal") == \
        "eu acho que o seu nome jarvis é legal"


@pytest.mark.parametrize("text,ok", [
    ("pode desligar", True), ("Ok, pode dormir.", True), ("é só isso", True),
    ("só isso, obrigado", True), ("Valeu, pode desligar por agora", True),
    ("desligar a luz da sala", False), ("Podji desliga!", True), ("desliga a luz", False), ("vou dormir cedo hoje", False),
    ("que horas são", False),
])
def test_comando_dormir(text, ok):
    assert is_sleep_command(text) is ok


def test_alucinacoes():
    assert is_hallucination("Obrigado.")
    assert is_hallucination("Legendas pela comunidade Amara.org")
    assert not is_hallucination("Obrigado pela ajuda com a planilha")


# ---- VAD ------------------------------------------------------------------

def _seg(**kw) -> Segmenter:
    return Segmenter(classifier=energy_vad, start_ms=60, end_silence_ms=300,
                     preroll_ms=120, **kw)


def test_vad_um_enunciado_com_preroll():
    seg = _seg()
    evs = seg.feed(silence(500) + speech(600) + silence(500))
    assert [type(e) for e in evs] == [SpeechStart, SpeechEnd]
    end = evs[1]
    # fala (600) + pre-roll (~120) + folga final (~210) — nunca menos que a fala
    assert 600 <= end.duration_ms <= 1000
    assert not seg.in_speech


def test_vad_pedacos_irregulares_e_dois_enunciados():
    seg = _seg()
    pcm = speech(400) + silence(400) + speech(300) + silence(400)
    evs = []
    rng = np.random.default_rng(1)
    i = 0
    while i < len(pcm):  # pacotes de tamanho aleatório (como no Wi-Fi)
        n = int(rng.integers(1, 3000)) * 2
        evs += seg.feed(pcm[i:i + n])
        i += n
    assert [type(e) for e in evs] == [SpeechStart, SpeechEnd, SpeechStart, SpeechEnd]


def test_vad_ruido_curto_nao_abre():
    seg = _seg()
    assert seg.feed(silence(210) + speech(30) + silence(500)) == []  # 1 frame só


def test_vad_corta_enunciado_infinito():
    seg = _seg(max_utterance_s=1.0)
    evs = seg.feed(speech(2500))
    ends = [e for e in evs if isinstance(e, SpeechEnd)]
    assert ends and ends[0].forced


# ---- áudio / texto falado ---------------------------------------------------

def test_resample_e_wav():
    t = np.arange(24_000) / 24_000
    pcm = (np.sin(2 * np.pi * 440 * t) * 10_000).astype("<i2").tobytes()
    out = resample_pcm(pcm, 24_000, 16_000)
    assert len(out) == 16_000 * 2
    back, sr = wav_to_pcm(pcm_to_wav(out, 16_000))
    assert sr == 16_000 and back == out
    # Energia preservada (o filtro não destrói o 440 Hz)
    a = np.frombuffer(out, "<i2").astype(float)
    assert 6_000 < np.sqrt((a ** 2).mean()) < 8_000


def test_texto_para_fala():
    assert clean_for_speech("**Olá**, senhor! 😀 Veja https://x.com") == "Olá, senhor! Veja"
    # frase curta no fim gruda na anterior (prosódia); as longas ficam separadas
    frases = split_sentences("Certo. O dólar está a cinco reais e dez centavos hoje. Algo mais?")
    assert frases == ["Certo. O dólar está a cinco reais e dez centavos hoje. Algo mais?"]
    frases = split_sentences(
        "O dólar fechou a cinco reais e dez centavos. O euro subiu para seis reais. "
        "Quer que eu acompanhe a cotação durante o dia?"
    )
    assert len(frases) == 3 and frases[0].startswith("O dólar")
    assert split_sentences("") == []
    # frase gigante: 1º pedaço curto (latência) e quebras só em vírgulas
    longa = ("Uma curiosidade interessante é que o Homem de Ferro, criado por Stan Lee, "
             "Larry Lieber, Don Heck e Jack Kirby, estreou em mil novecentos e cinquenta e "
             "nove, mas só se tornou um grande sucesso após o filme de dois mil e oito.")
    frases = split_sentences(longa)
    assert len(frases[0]) <= 70 and len(frases) >= 2
    assert " ".join(frases) == longa
    sem_virgula = "A armadura Mark Três foi a primeira a usar a liga de titânio e ouro nos filmes."
    frases = split_sentences(sem_virgula)
    assert len(frases[0]) <= 70 and " ".join(frases) == sem_virgula


# ---- memória de fatos compartilhada ----------------------------------------

def test_fatos_mesclados_sem_tocar_historico_do_desktop(tmp_path):
    from helmet.brain import SharedFactsMemory

    path = tmp_path / "memoria.json"
    path.write_text(json.dumps({
        "fatos": [{"texto": "Mora em Porto Alegre", "ts": 1, "origem": "auto"}],
        "historico": [{"role": "user", "texto": "oi do desktop", "ts": 1}],
    }))
    mem = SharedFactsMemory(path)
    assert mem.historico == []  # não herda o histórico do desktop
    mem.add_turn("user", "fala do capacete")
    mem.save()  # só histórico mudou: não grava nada
    assert json.loads(path.read_text())["historico"][0]["texto"] == "oi do desktop"

    # O desktop grava um fato novo "por fora" enquanto o capacete roda.
    data = json.loads(path.read_text())
    data["fatos"].append({"texto": "Tem um gato", "ts": 2, "origem": "auto"})
    path.write_text(json.dumps(data))

    mem.add_fact("Está construindo um capacete do Homem de Ferro")
    mem.save()
    data = json.loads(path.read_text())
    textos = [f["texto"] for f in data["fatos"]]
    assert textos == ["Mora em Porto Alegre", "Tem um gato",
                      "Está construindo um capacete do Homem de Ferro"]
    assert data["historico"][0]["texto"] == "oi do desktop"

    mem.remove_facts("porto alegre")
    mem.save()
    textos = [f["texto"] for f in json.loads(path.read_text())["fatos"]]
    assert "Mora em Porto Alegre" not in textos and "Tem um gato" in textos


# ---- serviço de conversas (direto, sem rede) --------------------------------

@pytest.fixture
async def service(tmp_path):
    engine = make_engine(f"sqlite+aiosqlite:///{tmp_path / 'c.db'}")
    await init_db(engine)
    summ = FakeSummarizer()
    svc = ConversationService(make_sessionmaker(engine), max_bytes=100, summarizer=summ)
    svc.summ = summ
    yield svc
    await svc.drain()
    await engine.dispose()


async def test_recuperacao_apos_queda(service):
    o = await service.open_session("cap", "Jarvis")
    await service.record_interaction(o.session_id, "x" * 80, "y" * 80)
    # processo "morre" aqui: a escuta fica aberta. No boot seguinte:
    await service.recover()
    await service.drain()
    assert service.summ.calls  # conversa cheia foi encerrada e resumida
    o2 = await service.open_session("cap", "Jarvis")
    assert o2.new_conversation and o2.conversation_id != o.conversation_id


async def test_resumo_falho_e_retomado(service):
    service.summ.fail = True
    o = await service.open_session("cap", "Jarvis")
    await service.record_interaction(o.session_id, "a" * 90, "b" * 90)
    assert await service.close_session(o.session_id, "timeout") == o.conversation_id
    await service.drain()
    service.summ.fail = False
    await service.recover()  # retoma resumos com status failed
    await service.drain()
    assert len(service.summ.calls) == 2


async def test_conversas_por_dispositivo_sao_independentes(service):
    a = await service.open_session("cap-a", None)
    b = await service.open_session("cap-b", None)
    assert a.conversation_id != b.conversation_id


async def test_limite_reduzido_fecha_conversa_antiga_na_abertura(service):
    service.max_bytes = 10_000
    o = await service.open_session("cap", None)
    await service.record_interaction(o.session_id, "a" * 200, "b")
    await service.close_session(o.session_id, "timeout")  # abaixo de 10k: segue aberta
    service.max_bytes = 100  # limite reduzido na config
    o2 = await service.open_session("cap", None)
    assert o2.new_conversation


# ---- filtro passa-alta e piso de ruído adaptativo --------------------------

def _tone(hz: float, ms: int, amp: float) -> bytes:
    t = np.arange(16_000 * ms // 1000) / 16_000
    return (np.sin(2 * np.pi * hz * t) * amp).astype("<i2").tobytes()


def _rms(pcm: bytes) -> float:
    a = np.frombuffer(pcm, dtype="<i2").astype(float)
    return float(np.sqrt((a ** 2).mean()))


def test_passa_alta_tira_zumbido_e_mantem_voz():
    from helmet.audio.filters import HighPass

    hum, voice = _tone(120, 1000, 3000), _tone(1000, 1000, 3000)
    h1, h2 = HighPass(200, 16_000), HighPass(200, 16_000)
    # em pacotes de 20 ms, como chega do ESP32 (estado mantido entre eles)
    out_hum = b"".join(h1.process(hum[i:i + 640]) for i in range(0, len(hum), 640))
    out_voice = b"".join(h2.process(voice[i:i + 640]) for i in range(0, len(voice), 640))
    assert _rms(out_hum[3200:]) < 0.3 * _rms(hum)
    assert _rms(out_voice[3200:]) > 0.9 * _rms(voice)


def test_piso_de_ruido_ignora_chiado_constante_e_pega_voz():
    rng = np.random.default_rng(0)

    def noise(ms: int) -> bytes:
        return rng.normal(0, 400, 16 * ms).astype("<i2").tobytes()

    loud = lambda ms: (rng.normal(0, 4000, 16 * ms)).astype("<i2").tobytes()  # noqa: E731
    # Chiado que o classificador (aqui: "tudo é fala") confundiria com voz.
    seg = Segmenter(classifier=lambda f, sr: True, start_ms=90, end_silence_ms=300,
                    noise_factor=3.0)
    evs = seg.feed(noise(3000))  # 3 s só de chiado: aprende o piso
    evs = [e for e in evs if isinstance(e, SpeechEnd)]
    assert seg.noise_floor > 300
    assert not seg.in_speech and not [e for e in evs if e.duration_ms > 1000]
    out = seg.feed(loud(800) + noise(600))
    ends = [e for e in out if isinstance(e, SpeechEnd)]
    assert len(ends) == 1 and 700 <= ends[0].duration_ms <= 1300


# ---- inglês ---------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("That's all, thanks.", True), ("Go to sleep, Jarvis.", True), ("Stand down.", True),
    ("You can go to sleep", True), ("Goodbye!", True),
    ("You can return to sleep.", True), ("Go back to sleep, Jarvis", True),
    ("Shut down the computer", False), ("I need to sleep more", False),
])
def test_comando_de_dormir_em_ingles(text, expected):
    assert is_sleep_command(text) is expected


def test_wake_em_ingles():
    m = find_wake("Jarvis, what time is it?")
    assert m is not None and m.command == "what time is it?"


def test_alucinacoes_em_ingles():
    assert is_hallucination("Thank you.") and is_hallucination("Thanks for watching!")
    assert is_hallucination("A conversation in English with the personal assistant JARVIS. "
                            "Jarvis, what time is it?")
    assert not is_hallucination("What's the weather like?")


def test_persona_em_ingles():
    from helmet.brain import persona_addendum
    from helmet.display import DisplaySpec

    voz = persona_addendum(None, speaker=False, cap=60, lang="en")
    assert "ENGLISH" in voz and "VOICE" in voz
    tela = persona_addendum(DisplaySpec(16, 2), speaker=False, cap=60, lang="en")
    assert "32 characters" in tela and "Done." in tela and "Couldn't do it." in tela
    assert "DISPLAY" in persona_addendum(DisplaySpec(16, 2), speaker=False, cap=60)  # pt


def test_voz_padrao_por_idioma():
    from helmet.settings import Settings

    assert Settings(language="en", tts_voice="", tts_lang="").voice() == ("bm_daniel", "en-gb")
    assert Settings(language="en", tts_voice="bm_george", tts_lang="").voice() == ("bm_george", "en-gb")
    assert Settings(language="pt", tts_voice="", tts_lang="").voice() == ("", "")
    assert Settings(language="en", stt_language="").stt_lang == "en"
