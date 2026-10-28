"""Fluxo completo pelo WebSocket: wake word, escuta, AFK, conversas, limite."""

from __future__ import annotations

import asyncio

import httpx
import pytest
import websockets

from tests.conftest import FakeBrain, FakeSummarizer

H = {"Authorization": "Bearer segredo"}


def api(srv, path: str, method: str = "GET", **kw):  # noqa: ANN001, ANN201
    r = httpx.request(method, srv.base + path, headers=H, timeout=10, **kw)
    r.raise_for_status()
    return r.json()


def conversations(srv) -> list[dict]:  # noqa: ANN001
    return api(srv, "/api/conversations")["items"]


async def until(fn, timeout: float = 3.0):  # noqa: ANN001, ANN201
    """A resposta vai ao capacete ANTES da gravação no banco: espera assentar."""
    t0 = asyncio.get_running_loop().time()
    while True:
        try:
            v = fn()
            if v:
                return v
        except (AssertionError, IndexError, KeyError):
            pass
        if asyncio.get_running_loop().time() - t0 > timeout:
            raise AssertionError("condição não satisfeita a tempo")
        await asyncio.sleep(0.05)


async def test_auth_rejeita_token_errado(make_server):
    srv = make_server()
    with pytest.raises(websockets.InvalidStatus):
        async with websockets.connect(srv.ws_url(token="errado")):
            pass
    assert httpx.get(srv.base + "/api/devices").status_code == 401
    assert httpx.get(srv.base + "/health").status_code == 200  # health é público


async def test_dormindo_ignora_fala_sem_jarvis(make_server, connect):
    srv = make_server()
    dev = await connect(srv.ws_url())
    srv.stt.queue.extend(["que horas são", "abre o navegador"])
    await dev.say()
    await dev.say()
    await asyncio.sleep(0.4)
    assert srv.stt.calls == 2  # transcreveu…
    assert srv.brain.inputs == []  # …mas não respondeu
    assert not dev.of("tone", name="wake")
    assert conversations(srv) == []  # nada gravado


async def test_jarvis_sozinho_ativa_e_proxima_fala_vira_pedido(make_server, connect):
    srv = make_server(active_timeout_s=3)
    dev = await connect(srv.ws_url())
    srv.stt.queue.extend(["Jarvis.", "quanto é dois mais dois?"])
    await dev.say()
    await dev.wait_for("tone", name="wake")
    await dev.wait_for("state", state="listening")
    await dev.say()
    rep = await dev.wait_for("reply")
    assert srv.brain.inputs == ["quanto é dois mais dois?"]
    assert "dois mais dois" in rep["text"]
    convs = await until(lambda: [c for c in conversations(srv) if c["interaction_count"] == 1])
    its = api(srv, f"/api/conversations/{convs[0]['id']}/interactions")
    assert its[0]["user"] == "quanto é dois mais dois?"
    assert its[0]["stt_ms"] is not None and its[0]["brain_ms"] is not None


async def test_jarvis_e_pedido_na_mesma_frase(make_server, connect):
    srv = make_server()
    dev = await connect(srv.ws_url())
    srv.stt.queue.append("Ok Jarvis, que horas são?")
    await dev.say()
    await dev.wait_for("reply")
    assert srv.brain.inputs == ["que horas são?"]
    assert dev.of("tone", name="wake")


async def test_afk_desativa_e_exige_jarvis_de_novo(make_server, connect):
    srv = make_server(active_timeout_s=0.8)
    dev = await connect(srv.ws_url())
    srv.stt.queue.append("Jarvis")
    await dev.say()
    await dev.wait_for("tone", name="wake")
    await dev.wait_for("tone", name="sleep", timeout=3)  # 0.8 s sem falar
    srv.stt.queue.append("que horas são")
    await dev.say()
    await asyncio.sleep(0.4)
    assert srv.brain.inputs == []  # dormindo de novo
    conv = conversations(srv)[0]
    sess = api(srv, f"/api/conversations/{conv['id']}")["sessions"]
    assert sess[0]["end_reason"] == "timeout"


async def test_qualquer_fala_renova_o_prazo(make_server, connect):
    srv = make_server(active_timeout_s=1.0)
    dev = await connect(srv.ws_url())
    srv.stt.queue.extend(["Jarvis", "um", "dois", "três", "quatro"])
    await dev.say()
    await dev.wait_for("tone", name="wake")
    for _ in range(4):  # ~0.6 s cada, total bem acima de 1 s
        await asyncio.sleep(0.3)
        await dev.say(200)
    await dev.wait_for("reply", count=4)
    assert not dev.of("tone", name="sleep")
    await dev.wait_for("tone", name="sleep", timeout=4)


async def test_ruido_sem_texto_nao_renova_o_prazo(make_server, connect):
    srv = make_server(active_timeout_s=1.0)
    dev = await connect(srv.ws_url())
    srv.stt.queue.append("Jarvis")
    await dev.say()
    await dev.wait_for("tone", name="wake")
    t0 = asyncio.get_running_loop().time()
    for _ in range(3):  # "fala" que o STT devolve vazia (ruído)
        await dev.say(150)
    await dev.wait_for("tone", name="sleep", timeout=4)
    assert asyncio.get_running_loop().time() - t0 < 2.6


async def test_prazo_pausa_enquanto_jarvis_pensa(make_server, connect):
    srv = make_server(brain=FakeBrain(delay=1.5), active_timeout_s=0.8)
    dev = await connect(srv.ws_url())
    srv.stt.queue.append("Jarvis, pesquisa algo demorado")
    await dev.say()
    await dev.wait_for("reply", timeout=5)
    # Durante os 1,5 s pensando não pode ter dormido.
    sleeps = dev.of("tone", name="sleep")
    assert not sleeps
    await dev.wait_for("tone", name="sleep", timeout=3)


async def test_comando_de_dormir(make_server, connect):
    srv = make_server(active_timeout_s=30)
    dev = await connect(srv.ws_url())
    srv.stt.queue.extend(["Jarvis", "pode desligar"])
    await dev.say()
    await dev.wait_for("tone", name="wake")
    await dev.say()
    await dev.wait_for("tone", name="sleep")
    assert srv.brain.inputs == []
    conv = conversations(srv)[0]
    assert api(srv, f"/api/conversations/{conv['id']}")["sessions"][0]["end_reason"] == "command"


async def test_conversa_continua_entre_escutas_abaixo_do_limite(make_server, connect):
    srv = make_server(active_timeout_s=0.6)
    dev = await connect(srv.ws_url())
    for pedido in ("primeiro", "segundo"):
        srv.stt.queue.append(f"Jarvis, {pedido}")
        await dev.say()
        await dev.wait_for("reply", text=f"Resposta para: {pedido}. Mais alguma coisa, senhor?")
        await dev.wait_for("tone", name="sleep", count=1 if pedido == "primeiro" else 2, timeout=3)
    convs = await until(lambda: [c for c in conversations(srv) if c["interaction_count"] == 2])
    assert len(conversations(srv)) == 1
    assert convs[0]["status"] == "open" and convs[0]["session_count"] == 2


async def test_limite_estourado_ao_vivo_mantem_conversa_ate_o_afk(make_server, connect):
    summ = FakeSummarizer()
    srv = make_server(summarizer=summ, conversation_max_bytes=80, active_timeout_s=0.8)
    dev = await connect(srv.ws_url())
    srv.stt.queue.extend(["Jarvis, primeira pergunta bem comprida", "segunda", "terceira"])
    await dev.say()
    await dev.wait_for("reply")
    c1 = await until(lambda: [c for c in conversations(srv) if c["size_bytes"]][0])
    assert c1["size_bytes"] >= 80  # já estourou na 1ª troca…
    await dev.say()
    await dev.wait_for("reply", count=2)
    await dev.say()
    await dev.wait_for("reply", count=3)
    convs = await until(lambda: [c for c in conversations(srv) if c["interaction_count"] == 3])
    assert len(conversations(srv)) == 1 and convs[0]["status"] == "open"  # …a mesma

    await dev.wait_for("tone", name="sleep", timeout=3)  # AFK -> encerra
    conv = await until(lambda: (c := api(srv, f"/api/conversations/{c1['id']}"))
                       ["summary_status"] == "done" and c)
    assert conv["status"] == "closed" and conv["close_reason"] == "limit"
    assert conv["title"] == "Título de teste" and conv["summary_status"] == "done"
    assert "primeira pergunta" in summ.calls[0] and "terceira" in summ.calls[0]

    # Próximo "Jarvis" abre conversa NOVA, e o cérebro recebe contexto limpo.
    srv.stt.queue.append("Jarvis, oi de novo")
    await dev.say()
    await dev.wait_for("reply", count=4)
    convs = await until(lambda: len(conversations(srv)) == 2 and conversations(srv))
    assert convs[0]["status"] == "open"
    assert srv.brain.contexts[-1] == []


async def test_contexto_da_conversa_e_recarregado(make_server, connect):
    srv = make_server(active_timeout_s=0.6)
    dev = await connect(srv.ws_url())
    srv.stt.queue.append("Jarvis, meu nome é Tony")
    await dev.say()
    await dev.wait_for("reply")
    await dev.wait_for("tone", name="sleep", timeout=3)
    assert srv.brain.contexts == [[]]  # 1ª carga: conversa nova

    # Reinicia o servidor (mesmo banco): o contexto volta do banco.
    srv2 = make_server(db_name="capacete.db", active_timeout_s=0.6)
    dev2 = await connect(srv2.ws_url())
    srv2.stt.queue.append("Jarvis, qual é meu nome?")
    await dev2.say()
    await dev2.wait_for("reply")
    assert srv2.brain.contexts[0][0][0] == "meu nome é Tony"


async def test_cerebro_falhando_responde_erro_e_grava(make_server, connect):
    srv = make_server(brain=FakeBrain(fail=True))
    dev = await connect(srv.ws_url())
    srv.stt.queue.append("Jarvis, oi")
    await dev.say()
    rep = await dev.wait_for("reply")
    assert "problema" in rep["text"]
    await dev.wait_for("tone", name="error")
    conv = conversations(srv)[0]
    its = await until(lambda: api(srv, f"/api/conversations/{conv['id']}/interactions"))
    assert "cérebro fora do ar" in its[0]["error"]


async def test_audio_de_resposta_vai_pro_capacete(make_server, connect):
    srv = make_server(reply_audio="auto", active_timeout_s=5)
    dev = await connect(srv.ws_url(), hello={"fw": "0.1.0", "speaker": True, "speaker_rate": 8000})
    await dev.wait_for("welcome", count=2)
    assert dev.of("welcome")[-1]["audio_target"] == "device"
    srv.stt.queue.append("Jarvis, fala uma frase longa. E depois outra frase para testar.")
    await dev.say()
    end = await dev.wait_for("audio_end", timeout=6)
    start = dev.of("audio_start")[0]
    assert start["rate"] == 8000 and start["seq"] == end["seq"]
    # FakeTTS: 100 ms por frase a 8 kHz s16 = 1600 bytes por frase.
    assert len(dev.audio) == 1600 * end["duration_ms"] // 100
    await dev.ws.send('{"type": "playback_done", "seq": %d}' % end["seq"])
    await dev.wait_for("state", state="listening", count=2)


async def test_mic_mudo_enquanto_jarvis_fala(make_server, connect):
    srv = make_server(reply_audio="device", active_timeout_s=5)
    dev = await connect(srv.ws_url(), hello={"speaker": True})
    srv.stt.queue.append("Jarvis, oi")
    await dev.say()
    await dev.wait_for("audio_start")
    calls = srv.stt.calls
    await dev.say(300)  # eco da própria voz — deve ser descartado
    await dev.wait_for("audio_end")
    assert srv.stt.calls == calls


async def test_botao_e_texto_e_api(make_server, connect):
    srv = make_server(active_timeout_s=5)
    dev = await connect(srv.ws_url())
    await dev.ws.send('{"type": "wake"}')
    await dev.wait_for("tone", name="wake")
    r = api(srv, "/api/devices/capacete-test/text", "POST", json={"text": "liga a luz"})
    assert r["status"] == "ok" and r["reply"].startswith("Resposta para: liga a luz")
    devs = api(srv, "/api/devices")
    assert devs[0]["active"] and devs[0]["connected"]
    api(srv, "/api/devices/capacete-test/sleep", "POST")
    await dev.wait_for("tone", name="sleep")
    conv = conversations(srv)[0]
    sess = api(srv, f"/api/conversations/{conv['id']}")["sessions"]
    assert sess[0]["end_reason"] == "manual"


async def test_reconexao_mantem_a_escuta(make_server, connect):
    srv = make_server(active_timeout_s=3)
    dev = await connect(srv.ws_url())
    srv.stt.queue.append("Jarvis")
    await dev.say()
    await dev.wait_for("tone", name="wake")
    await dev.close()  # Wi-Fi caiu
    await asyncio.sleep(0.3)
    dev2 = await connect(srv.ws_url())
    st = await dev2.wait_for("state")
    assert st["active"] is True  # continua ouvindo
    srv.stt.queue.append("ainda está aí?")
    await dev2.say()
    await dev2.wait_for("reply")
    assert len(conversations(srv)) == 1


async def test_finalizar_manual_e_conflito_ao_vivo(make_server, connect):
    srv = make_server(active_timeout_s=30)
    dev = await connect(srv.ws_url())
    srv.stt.queue.append("Jarvis, oi")
    await dev.say()
    await dev.wait_for("reply")
    cid = conversations(srv)[0]["id"]
    r = httpx.post(f"{srv.base}/api/conversations/{cid}/finalize", headers=H)
    assert r.status_code == 409  # escuta ao vivo
    api(srv, "/api/devices/capacete-test/sleep", "POST")
    assert api(srv, f"/api/conversations/{cid}/finalize", "POST") == {"finalized": True}
    await until(lambda: api(srv, f"/api/conversations/{cid}")["summary_status"] == "done")
