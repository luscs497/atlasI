"""Display I2C no capacete: formatação pura, protocolo e escolha da persona."""

from __future__ import annotations

import asyncio

import pytest

from datetime import datetime

from helmet.brain import HELMET_PERSONA, AtlasBrain, clock_line, persona_addendum
from helmet.display import DisplaySpec, clean_text, fit, max_chars, to_ascii
from tests.conftest import FakeBrain

LCD16 = DisplaySpec(16, 2)
LCD20 = DisplaySpec(20, 4)
OLED = DisplaySpec(21, 5)


def assert_fits(frame, spec: DisplaySpec, limit: int | None = None) -> None:  # noqa: ANN001
    assert 1 <= len(frame.lines) <= spec.rows, frame
    assert all(len(ln) <= spec.cols for ln in frame.lines), frame
    assert frame.text == " ".join(frame.lines)
    if limit is not None:
        assert len(frame.text) <= limit
    if spec.ascii:
        assert frame.text.isascii()


# ---- formatação -----------------------------------------------------------

@pytest.mark.parametrize("text", ["Oi!", "Estou aqui.", "Enviado.", "Feito."])
def test_confirmacoes_curtas_passam_intactas(text):
    f = fit(text, LCD16)
    assert f.lines == [text] and f.text == text


def test_quebra_por_palavras():
    f = fit("Lembrete criado para amanha cedo", LCD16)
    assert f.lines == ["Lembrete criado", "para amanha cedo"]
    assert_fits(f, LCD16)


def test_acentos_e_pontuacao_viram_ascii():
    assert to_ascii("Ação às 9h — “ótimo”… ‘ok’ nº 5ª") == 'Acao as 9h - "otimo"... \'ok\' no 5a'
    f = fit("Não consegui.", LCD16)
    assert f.text == "Nao consegui."


def test_ascii_false_mantem_acentos():
    f = fit("Não consegui, ação.", DisplaySpec(16, 2, ascii=False))
    assert f.text == "Não consegui, ação."


def test_remove_emoji_e_markdown():
    assert clean_text("**Feito**! 🚀✅ Veja [o link](https://x.com) `cod` _ok_") == \
        "Feito! Veja o link cod ok"
    assert clean_text("# Título\n- item um\n1. item dois") == "Título item um item dois"
    assert "🚀" not in fit("Feito! 🚀", DisplaySpec(16, 2, ascii=False)).text


def test_corta_em_fim_de_palavra_com_reticencias():
    text = "Agora são exatamente 14:30 de quinta-feira, senhor. Mais alguma coisa?"
    for spec in (LCD16, LCD20, OLED):
        f = fit(text, spec)
        assert_fits(f, spec)
        if len(text) > spec.capacity:
            assert f.text.endswith("...")
            # todas as palavras do quadro são palavras INTEIRAS do original
            words = to_ascii(text).split()
            shown = f.text[:-3].split()
            assert shown == words[:len(shown)]


def test_corte_nao_termina_em_palavra_de_ligacao():
    f = fit("Vou comprar o pao e o leite amanha cedo", LCD16)
    assert f.text == "Vou comprar o pao e o leite..."
    # sem a regra, o melhor corte seria "...compramos o..."
    f = fit("Ontem a tarde compramos o presente do aniversario", LCD16)
    assert f.text == "Ontem a tarde compramos..."
    assert_fits(f, LCD16)


def test_limite_de_caracteres_menor_que_a_tela():
    text = "Uma frase bem comprida que caberia no OLED inteiro mas passa do teto de sessenta"
    f = fit(text, OLED, limit=60)
    assert_fits(f, OLED, 60)
    assert f.text.endswith("...")


def test_numeros_e_horas_intactos():
    f = fit("Saldo R$ 1.234,56 as 14:30 em 15/10", DisplaySpec(20, 2))
    for token in ("R$", "1.234,56", "14:30", "15/10"):
        assert token in f.lines[0].split() + f.lines[-1].split()


def test_palavra_maior_que_a_linha_e_partida():
    f = fit("Inconstitucionalissimamente", LCD16)
    assert_fits(f, LCD16)


def test_spec_parse_e_limites():
    assert DisplaySpec.parse(None) is None
    assert DisplaySpec.parse({"cols": 20, "rows": 4}) == DisplaySpec(20, 4, True)
    with pytest.raises(ValueError):
        DisplaySpec.parse({"cols": 200, "rows": 2})
    assert max_chars(LCD16, 60) == 32
    assert max_chars(OLED, 60) == 60


# ---- escolha da persona ---------------------------------------------------

def test_adendo_display_so_sem_alto_falante():
    a = persona_addendum(LCD16, speaker=False, cap=60)
    assert "DISPLAY" in a and "16x2" in a and "32 caracteres" in a
    assert "Feito." in a and "Não consegui." in a
    assert persona_addendum(OLED, speaker=False, cap=60).count("60 caracteres") == 1
    assert persona_addendum(LCD16, speaker=True, cap=60).startswith(HELMET_PERSONA)
    assert persona_addendum(None, speaker=False, cap=60).startswith(HELMET_PERSONA)


def test_adendo_traz_a_hora_atual():
    now = datetime(2026, 10, 15, 14, 30)
    assert clock_line(now) == "\nAGORA: quinta-feira, 15/10/2026, 14:30 (horário local do senhor)."
    assert persona_addendum(LCD16, speaker=False, cap=60, now=now).endswith(clock_line(now))


class _Inner:
    _system = "PERSONA BASE"
    _client = None


def test_atlas_brain_troca_o_canal_sem_acumular():
    inner = _Inner()
    brain = AtlasBrain(inner, api_key="sk-test")
    assert inner._system == "PERSONA BASE" + HELMET_PERSONA
    disp = persona_addendum(LCD16, speaker=False, cap=60)
    brain.set_channel(disp)
    brain.set_channel(disp)
    assert inner._system == "PERSONA BASE" + disp
    brain.set_channel(HELMET_PERSONA)
    assert inner._system == "PERSONA BASE" + HELMET_PERSONA


# ---- protocolo ------------------------------------------------------------

class ChannelBrain(FakeBrain):
    def __init__(self, **kw) -> None:  # noqa: ANN003
        super().__init__(**kw)
        self.channels: list[str] = []

    def set_channel(self, addendum: str) -> None:
        self.channels.append(addendum)


async def test_hello_sem_display_nada_muda(make_server, connect):
    srv = make_server(brain=ChannelBrain())
    dev = await connect(srv.ws_url(), hello={"fw": "0.1.0"})
    w = dev.of("welcome")[-1]
    assert w["display"] is None and w["caps"]["display"] is None
    srv.stt.queue.append("Atlas, que horas são?")
    await dev.say()
    await dev.wait_for("reply")
    await asyncio.sleep(0.2)
    assert dev.of("display") == []
    assert len(srv.brain.channels) == 1 and srv.brain.channels[0].startswith(HELMET_PERSONA)


async def test_hello_com_display_envia_quadros(make_server, connect):
    srv = make_server(brain=ChannelBrain(), active_timeout_s=0.8)
    dev = await connect(srv.ws_url(), hello={"display": {"cols": 16, "rows": 2, "ascii": True}})
    w = dev.of("welcome")[-1]
    assert w["display"] == {"cols": 16, "rows": 2, "ascii": True, "max_chars": 32,
                            "ttl_ms": 8000, "reply_style": "display"}
    srv.stt.queue.append("Atlas, que horas são?")
    await dev.say()
    rep = await dev.wait_for("reply")
    frame = await dev.wait_for("display", kind="reply")
    # "reply" continua com o texto COMPLETO; o display recebe a versão que cabe
    assert rep["text"] == "Resposta para: que horas são?. Mais alguma coisa, senhor?"
    assert frame["ttl_ms"] == 8000
    assert all(len(ln) <= 16 for ln in frame["lines"]) and len(frame["lines"]) <= 2
    assert frame["text"].endswith("...") and frame["text"].isascii()
    status = dev.of("display", kind="status")
    assert status[0]["text"] == "Ouvindo..." and status[0]["ttl_ms"] == 0
    assert "DISPLAY" in srv.brain.channels[-1]
    # AFK -> dorme -> quadro de status
    sleep = await dev.wait_for("display", kind="status", count=2, timeout=4)
    assert sleep["text"] == "Em espera."
    # o banco guarda o texto ORIGINAL completo
    from tests.test_flow import api, until
    convs = await until(lambda: api(srv, "/api/conversations")["items"])
    its = api(srv, f"/api/conversations/{convs[0]['id']}/interactions")
    assert its[0]["atlas"] == rep["text"]


async def test_display_com_alto_falante_usa_persona_de_voz(make_server, connect):
    srv = make_server(brain=ChannelBrain())
    dev = await connect(srv.ws_url(), hello={"speaker": True, "display": {"cols": 21, "rows": 5}})
    assert dev.of("welcome")[-1]["display"]["reply_style"] == "voice"
    srv.stt.queue.append("Atlas, oi")
    await dev.say()
    await dev.wait_for("display", kind="reply")
    assert srv.brain.channels[-1].startswith(HELMET_PERSONA)


async def test_cerebro_falhando_mostra_nao_consegui(make_server, connect):
    srv = make_server(brain=ChannelBrain(fail=True))
    dev = await connect(srv.ws_url(), hello={"display": {"cols": 16, "rows": 2}})
    srv.stt.queue.append("Atlas, manda a mensagem")
    await dev.say()
    frame = await dev.wait_for("display", kind="reply")
    assert frame["lines"] == ["Nao consegui."]


async def test_display_invalido_vira_sem_display(make_server, connect):
    srv = make_server()
    dev = await connect(srv.ws_url(), hello={"display": {"cols": 999, "rows": 2}})
    await dev.wait_for("error")
    assert dev.of("welcome")[-1]["display"] is None


async def test_capacete_em_ingles_quadros_e_erro(make_server, connect):
    srv = make_server(brain=ChannelBrain(fail=True), language="en")
    dev = await connect(srv.ws_url(), hello={"display": {"cols": 16, "rows": 2}})
    srv.stt.queue.append("Atlas, send the message")
    await dev.say()
    frame = await dev.wait_for("display", kind="reply")
    assert frame["lines"] == ["Couldn't do it."]
    assert dev.of("display", kind="status")[0]["text"] == "Listening..."
    rep = dev.of("reply")[-1]
    assert rep["text"].startswith("I'm afraid")
    assert "ENGLISH" in srv.brain.channels[-1]
