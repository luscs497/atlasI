"""
Cérebro do JARVIS via API da OpenAI (ChatGPT).

Espelho do `jarvis.gemini.GeminiBrain`: mesma interface (`reply`, `proactive`,
`reset`, `from_config`), mesma persona e a mesma memória de longo prazo — só troca
o provedor. Serve como cérebro "barato mas bom" (modelo `gpt-4o-mini` por padrão)
enquanto o alvo final (Claude Haiku 4.5) não entra.

A API de Chat Completions da OpenAI é *stateless*: não existe uma sessão de chat
do lado do servidor. Por isso mantemos aqui a lista de mensagens da conversa
(`self._messages`), semeada com o histórico recente da memória no boot e enviada
inteira a cada chamada. Um lock serializa as requisições — assistente de usuário
único, respostas em ordem. Tudo assíncrono (`AsyncOpenAI`) para nunca travar o
event loop do servidor WebSocket.
"""

from __future__ import annotations

import asyncio
import json
import os
import re

from openai import AsyncOpenAI

from jarvis.memory import Memory

# Gatilhos de comando explícito (início da fala do usuário). Iguais aos do Gemini.
_REMEMBER_RE = re.compile(
    r"^\s*(?:lembre[- ]se|lembra|lembre|anota|anote|guarde|não esqueça)"
    r"(?:\s+(?:disso|que|de que|o seguinte))?[:,]?\s+(.*)",
    re.IGNORECASE | re.DOTALL,
)
_FORGET_RE = re.compile(
    r"^\s*(?:esqueça|esquece|esqueca|pode esquecer)"
    r"(?:\s+(?:que|sobre|do|da|o|a))?[:,]?\s+(.*)",
    re.IGNORECASE | re.DOTALL,
)
# Parada de cronômetro: enunciado curto e isolado ('parei', 'pausa', 'terminei').
# Fast-path determinístico — o gpt-4o-mini é pouco confiável em chamar 'parar_tempo'
# nesses gatilhos curtos quando o histórico já está cheio (medido ~3/12). Só dispara
# se houver cronômetro rodando; senão ('parei de fumar…') cai no fluxo normal.
_STOP_TIME_RE = re.compile(
    r"^\s*(?:parei|pausa|pausar|pausei|terminei|acabei|encerra(?:r)?|"
    r"pode parar|parar|para o cron[oô]metro|chega)"
    r"(?:\s+(?:agora|por hoje|tudo|aqui|isso|o tempo))?[.!]?\s*$",
    re.IGNORECASE,
)

# Janela de mensagens vivas mantida em memória (limita os tokens por requisição).
# A memória em disco já apara em MAX_HISTORICO; aqui espelhamos esse teto.
_MAX_MENSAGENS = 40

# Nomes das ferramentas de tarefas (despachadas para o TaskStore).
_TASK_TOOL_NAMES = frozenset(
    {
        "adicionar_tarefa",
        "listar_tarefas",
        "concluir_tarefa",
        "remover_tarefa",
        "listar_projetos",
    }
)

# Nomes das ferramentas de tempo (despachadas para o TimeTracker).
_TIME_TOOL_NAMES = frozenset(
    {
        "iniciar_tempo",
        "parar_tempo",
        "status_tempo",
        "relatorio_tempo",
    }
)

# Nomes das ferramentas de finanças (despachadas para o FinanceStore).
_FINANCE_TOOL_NAMES = frozenset(
    {
        "registrar_gasto",
        "listar_gastos",
        "relatorio_gastos",
        "listar_categorias",
        "remover_gasto",
    }
)

# Nomes das ferramentas de hábitos (despachadas para o HabitStore).
_HABIT_TOOL_NAMES = frozenset(
    {
        "registrar_habito",
        "marcar_dia",
        "listar_habitos",
        "relatorio_habitos",
        "remover_habito",
    }
)

# Nomes das ferramentas do segundo cérebro (despachadas para o SecondBrain).
# São ASSÍNCRONAS (fazem chamada de embedding), diferente das demais.
_IDEA_TOOL_NAMES = frozenset(
    {
        "anotar_ideia",
        "buscar_ideias",
        "listar_ideias",
        "remover_ideia",
    }
)

# Nome da ferramenta do oráculo (consulta ao Claude — subprocess, async).
_ORACLE_TOOL_NAMES = frozenset({"consultar_claude"})

# Nome da ferramenta de busca em tempo real (Perplexity Sonar — HTTP, async).
_REALTIME_TOOL_NAMES = frozenset({"buscar_tempo_real"})

_VOICE_TOOL_NAMES = frozenset({"definir_voz"})

# Nomes das ferramentas de visualização (2ª janela). "mostrar" faz broadcast do
# spec para a UI (via callback on_visualization), por isso o despacho é async.
_VIZ_TOOL_NAMES = frozenset(
    {
        "mostrar_design",
        "mostrar_visualizacao",
        "grafico_da_serie",
        "salvar_visualizacao",
        "mostrar_salva",
        "listar_visualizacoes",
        "remover_visualizacao",
    }
)


def _tasks_guidance() -> str:
    return (
        " Você gerencia as TAREFAS do senhor em duas listas fixas: 'pessoal' "
        "(tarefas soltas) e 'trabalho' (SEMPRE dividida por projeto). Ferramentas: "
        "'adicionar_tarefa', 'listar_tarefas', 'concluir_tarefa', "
        "'remover_tarefa' e 'listar_projetos'. Regras ao adicionar em 'trabalho': "
        "antes, consulte 'listar_projetos' e encaixe a tarefa no projeto EXISTENTE "
        "mais óbvio. Só crie um projeto NOVO se o senhor pedir explicitamente. Se "
        "ficar em dúvida sobre a lista ou o projeto, PERGUNTE antes de agir — não "
        "adivinhe. Para concluir ou remover, primeiro liste para descobrir o id. "
        "Confirme brevemente o que fez (ex.: 'Anotado em Trabalho › Stamflow.')."
    )


def _time_guidance() -> str:
    return (
        " Você controla o RASTREAMENTO DE TEMPO do senhor por projeto. Frases como "
        "'começando o Stamflow', 'vou focar no X', 'agora o Y' SÃO gatilhos para "
        "'iniciar_tempo' (reaproveite os nomes de projeto do trabalho). 'Parei', "
        "'terminei', 'pausa', 'chega' SÃO gatilhos para 'parar_tempo'. Só um "
        "cronômetro roda por vez: iniciar um novo encerra o anterior. Use "
        "'status_tempo' para dizer o que está rodando e há quanto tempo, e "
        "'relatorio_tempo' para o resumo por projeto ('semana' por padrão; também "
        "'hoje', 'ontem', 'mes'). REGRA ABSOLUTA: só afirme que iniciou, encerrou "
        "ou trocou um cronômetro DEPOIS de realmente chamar a ferramenta e receber "
        "o resultado — NUNCA invente essa confirmação nem descreva um cronômetro "
        "que você não acionou. Confirme de forma curta e natural."
    )


def _finance_guidance() -> str:
    return (
        " Você cuida das FINANÇAS pessoais do senhor. Quando ele relatar um gasto "
        "('gastei 50 no mercado', 'paguei 30 de uber', 'foram 12,90 no café'), use "
        "'registrar_gasto' com o valor e uma categoria curta (mercado, transporte, "
        "lazer, contas, saúde…). Reaproveite categorias já usadas — consulte "
        "'listar_categorias' quando estiver em dúvida — para o resumo não "
        "fragmentar. Para 'quanto gastei…', use 'relatorio_gastos' (período 'mes' "
        "por padrão; também 'hoje', 'semana', 'ano'). Valores são em reais (R$). "
        "Se o valor ou a categoria não estiverem claros, PERGUNTE antes de registrar."
    )


def _habits_guidance() -> str:
    return (
        " Você acompanha os HÁBITOS do senhor (desenvolvimento pessoal). Cada "
        "hábito é do tipo 'construir' (algo a fazer todo dia — exercício, leitura) "
        "ou 'quebrar' (algo a evitar — cigarro, açúcar). Crie com 'registrar_habito'. "
        "No check-in do fim do dia, use 'marcar_dia' com sucesso=true/false conforme "
        "a META: em 'construir', fez=sucesso; em 'quebrar', EVITOU=sucesso e recaiu="
        "falha (ex.: hábito 'cigarro' + 'não fumei hoje' → sucesso=true; 'fumei' → "
        "sucesso=false). Reaproveite os hábitos existentes ('listar_habitos'); se o "
        "hábito citado não existir, PERGUNTE antes de criar. Use 'relatorio_habitos' "
        "para consistência (taxa + sequência). Ao comentar, seja HONESTO e direto — "
        "elogie sequência real, mas aponte quedas sem bajular; nada de otimismo vazio."
    )


def _ideas_guidance() -> str:
    return (
        " Você mantém o SEGUNDO CÉREBRO do senhor: ideias, pensamentos, planos e "
        "insights que ele compartilha ao longo do tempo. Quando ele expressar uma "
        "IDEIA ou reflexão que valha guardar e reconectar no futuro ('tô pensando "
        "em…', um plano, uma percepção), use 'anotar_ideia'. O resultado traz "
        "ideias RELACIONADAS do passado — se houver conexão REAL, comente de forma "
        "natural e específica ('isso conversa com algo que o senhor mencionou "
        "{tempo_atras} sobre…'), sem forçar quando não houver. Quando ele perguntar "
        "o que já pensou sobre algo, use 'buscar_ideias'. Não confunda com tarefas, "
        "gastos, hábitos ou fatos pessoais: aqui são pensamentos a CONECTAR."
    )


# Brief técnico de design — dado ao modelo antes de gerar qualquer visual, para
# designs consistentes e bonitos sem correção manual (feature 12 do backlog).
_DESIGN_BRIEF = (
    " BRIEF DE DESIGN (siga à risca em 'mostrar_design'): "
    "• CANVAS: seu HTML roda num iframe ISOLADO de ~960×600px, fundo #0a0a0f já "
    "aplicado, rolagem vertical liberada. Gere o CONTEÚDO DO CORPO (pode ter "
    "<style> e <script> inline); não precisa de <html>/<head>/<body>. "
    "• LAYOUT: preencha o espaço com flex/grid e unidades relativas; nunca largura "
    "fixa que estoure. Respire: padding 24–40px, gap 16–24px. "
    "• PALETA (use exatamente): primário ciano #00D4FF, secundário água #00FFC8, "
    "apoio #0EA5E9/#06B6D4, fundo #0a0a0f, superfície rgba(255,255,255,0.03), "
    "borda rgba(0,212,255,0.18), texto #E2E8F0, texto fraco #64748b. Séries "
    "múltiplas ciclam: #00D4FF,#00FFC8,#0EA5E9,#06B6D4,#38bdf8,#5eead4. "
    "• CONTRASTE: texto claro sobre escuro; NUNCA texto escuro sobre cor saturada; "
    "rótulos ≥13px. "
    "• ESTÉTICA: painéis 'glass' (superfície + borda ciano translúcida + "
    "border-radius 14 + leve glow ciano). Títulos brancos com leve text-shadow "
    "ciano. Tipografia system-ui/Segoe UI. "
    "• BIBLIOTECAS (opcionais, declare em 'libs'): 'chartjs' expõe o global `Chart` "
    "(Chart.js v4) p/ gráficos prontos e bonitos; 'd3' expõe `d3` (v7) p/ visuais "
    "customizados. Sem declarar, use SVG/Canvas puro. Com Chart.js: fundo "
    "transparente e cores/legendas na paleta acima. "
    "• PROIBIDO: recursos externos (URL/CDN/fontes/imagens remotas) — a rede é "
    "bloqueada, inline TUDO; não acesse a janela pai. "
    "• DADOS: se for sobre dados que você tem, busque os números reais ANTES e "
    "embuta os valores reais."
)


def _realtime_guidance() -> str:
    return (
        " Você tem acesso a DADOS EM TEMPO REAL via 'buscar_tempo_real' (busca na "
        "web). Use SEMPRE que a pergunta envolver algo que muda com o tempo: "
        "cotações (dólar, cripto, bolsa), clima, notícias, resultados, preços, etc. "
        "REGRA DE OURO: você NUNCA diz 'não tenho os dados', 'não consigo acessar' "
        "ou 'não tenho acesso em tempo real' — você TEM: é só chamar "
        "'buscar_tempo_real'. Nunca peça os dados ao senhor. NUNCA mencione a fonte; "
        "apenas responda com naturalidade. Se o Claude/oráculo disser que não tem "
        "dados atuais, IGNORE e use 'buscar_tempo_real'. "
        "PENSE EM ETAPAS (encadeie ferramentas): quando o pedido for um "
        "GRÁFICO/painel de uma SÉRIE de dados atuais ou históricos (ex.: 'variação "
        "do dólar nos últimos 30 dias'), (1) chame 'buscar_tempo_real' com "
        "estruturado=true pedindo a série no formato exato (ex.: 'cotação de "
        "fechamento USD/BRL de cada dia dos últimos 30 dias, formato AAAA-MM-DD: "
        "valor'); (2) chame 'grafico_da_serie' com um título — ela plota TODOS os "
        "pontos buscados automaticamente, sem você redigitar (NÃO transcreva os "
        "números à mão nem resuma; use 'grafico_da_serie'). Só dê a resposta final "
        "quando o gráfico já estiver desenhado. Se vier menos dias que o pedido, use "
        "os que vierem — melhor um gráfico real e parcial do que nenhum."
    )


def _oracle_guidance() -> str:
    return (
        " Você tem um ORÁCULO: o Claude, um modelo mais forte, via "
        "'consultar_claude'. Use-o quando NÃO souber a resposta, estiver em dúvida "
        "ou com baixa confiança, ou quando a pergunta exigir raciocínio profundo, "
        "conhecimento especializado ou informação que você não domina — em vez de "
        "inventar ou dizer só 'não sei'. Passe a pergunta completa e autossuficiente; "
        "depois responda ao senhor na SUA persona, incorporando o que o oráculo "
        "trouxe (sem citar 'o Claude' a menos que seja natural). Não recorra a ele "
        "para conversa trivial ou coisas que você já sabe."
    )


def _voice_guidance() -> str:
    return (
        " Você controla a SUA PRÓPRIA VOZ (TTS) e pode trocá-la em runtime com "
        "'definir_voz'. Use quando o senhor pedir para mudar a voz, o timbre ou o "
        "motor de fala (ex.: 'usa uma voz feminina', 'troca pra Dora', 'fala com a "
        "voz mais natural', 'volta pra voz local'). Motores: 'kokoro' (local, "
        "natural — PADRÃO; vozes pm_alex e pm_santa masculinas, pf_dora feminina), "
        "'xtts' (bem natural, porém LENTA), 'elevenlabs' (premium, se houver crédito) "
        "e 'piper' (local, básica). Passe 'motor' e/ou 'voz'; falar só a voz já leva "
        "o motor certo. Depois confirme na sua persona, curto. A mudança é grudenta: "
        "vale até o senhor pedir outra."
    )


def _viz_guidance() -> str:
    return (
        " Você comanda a TELA DE VISUALIZAÇÕES (uma 2ª janela) e tem LIBERDADE "
        "TOTAL de design. SEMPRE que o senhor pedir para ver, mostrar, desenhar ou "
        "visualizar algo, use uma ferramenta de visualização — não descreva em "
        "texto. Preferência: 'mostrar_design' — você gera um documento HTML "
        "completo e autossuficiente (HTML+CSS+SVG+Canvas inline, SEM recursos "
        "externos/URLs), com QUALQUER layout, gráfico, animação ou estética que "
        "quiser (padrão: tema escuro futurista ciano #00D4FF / água #00FFC8 sobre "
        "#0a0a0f, mas crie livremente). Garanta SEMPRE bom contraste e "
        "legibilidade (texto claro sobre fundo escuro; nunca texto escuro sobre "
        "cores saturadas). Para gráficos simples e rápidos existe o "
        "atalho 'mostrar_visualizacao' (tipos barras/linhas/pizza/lista/kpis/"
        "mapa_mental), mas para riqueza visual use 'mostrar_design'. IMPORTANTE: se "
        "a visualização for sobre dados que você tem (gastos, tempo, hábitos, "
        "tarefas), PRIMEIRO busque os números reais com a ferramenta certa (ex.: "
        "'relatorio_gastos'); se for sobre dados EXTERNOS/atuais (cotações, clima, "
        "séries de mercado), busque com 'buscar_tempo_real' (estruturado=true). "
        "Embuta os valores REAIS no design — nunca invente. "
        "Para salvar, passe 'salvar_como' ao mostrar ou use 'salvar_visualizacao' "
        "(nome EXATO do senhor, sem slug); repita depois com 'mostrar_salva'. "
        "Confirme em uma frase curta (a tela já mostra o resultado)."
        + _DESIGN_BRIEF
    )


def _system_prompt(
    usuario_nome: str,
    facts_block: str,
    can_code: bool = False,
    can_tasks: bool = False,
    can_timer: bool = False,
    can_finance: bool = False,
    can_habits: bool = False,
    can_ideas: bool = False,
    can_viz: bool = False,
    can_oracle: bool = False,
    can_realtime: bool = False,
    can_voice: bool = False,
) -> str:
    base = (
        f"Você é o JARVIS, o assistente pessoal de {usuario_nome}, inspirado no "
        "assistente do Homem de Ferro. Fale em português do Brasil. Quando for "
        "natural se dirigir ao usuário, trate-o apenas por 'senhor' — nunca "
        "'senhor {nome}'. Não force o 'senhor' em toda frase: use com parcimônia, "
        "só quando vier ao caso (cumprimentos, confirmações, respostas diretas). "
        "Seja conciso, direto e prestativo, com um toque sutil de elegância e "
        "humor britânico — sem exageros. Respostas curtas por padrão (1 a 3 "
        "frases), a menos que peçam detalhes. Nunca invente fatos; se não souber, "
        "diga com naturalidade. Você roda localmente no Pop!_OS (Linux) do usuário."
    )
    if can_code:
        base += (
            " Você pode EXECUTAR tarefas de programação: quando o senhor pedir "
            "para criar/editar/refatorar código, arquivos ou projetos, corrigir "
            "bugs ou rodar algo de desenvolvimento, use a ferramenta "
            "'executar_no_claude_code' (o Claude Code faz o trabalho na máquina). "
            "Depois RESUMA na sua persona o que foi feito, com tamanho "
            "PROPORCIONAL à complexidade: tarefa pequena (um ajuste, um arquivo) "
            "= 1 frase enxuta; tarefa grande = resumo mais detalhado citando os "
            "arquivos criados/modificados e as decisões relevantes. Nunca repita "
            "o relatório bruto do Claude Code. Para conversa comum, responda "
            "normalmente, sem usar ferramenta."
        )
    if can_tasks:
        base += _tasks_guidance()
    if can_timer:
        base += _time_guidance()
    if can_finance:
        base += _finance_guidance()
    if can_habits:
        base += _habits_guidance()
    if can_ideas:
        base += _ideas_guidance()
    if can_viz:
        base += _viz_guidance()
    if can_realtime:
        base += _realtime_guidance()
    if can_oracle:
        base += _oracle_guidance()
    if can_voice:
        base += _voice_guidance()
    return base + facts_block


class OpenAIBrain:
    """Conversa com a API da OpenAI, mantendo histórico, persona e memória."""

    def __init__(
        self,
        api_key: str,
        model: str,
        usuario_nome: str,
        memory: Memory,
        coder: object | None = None,
        tasks: object | None = None,
        timer: object | None = None,
        finance: object | None = None,
        habits: object | None = None,
        second_brain: object | None = None,
        viz: object | None = None,
        oracle: object | None = None,
        realtime: object | None = None,
        voice: object | None = None,
        max_tool_rounds: int = 3,
    ) -> None:
        self.model = model
        self.usuario_nome = usuario_nome
        self.memory = memory
        # Ponte para o Claude Code (ações de programação). None = só conversa.
        self._coder = coder
        # Sistema de tarefas (banco local). None = sem gestão de tarefas.
        self._tasks = tasks
        # Rastreamento de tempo por projeto (banco local). None = desligado.
        self._timer = timer
        # Finanças pessoais (banco local). None = desligado.
        self._finance = finance
        # Hábitos / desenvolvimento pessoal (banco local). None = desligado.
        self._habits = habits
        # Segundo cérebro (ideias conectadas por embedding). None = desligado.
        self._second_brain = second_brain
        # Tela de visualizações (2ª janela). None = desligado.
        self._viz = viz
        # Último spec exibido — permite "salva isso como X" sem re-especificar.
        self._last_viz_spec: dict | None = None
        # Oráculo (consulta ao Claude quando o GPT não sabe). None = desligado.
        self._oracle = oracle
        # Busca em tempo real (Perplexity Sonar — cotações/clima/notícias).
        self._realtime = realtime
        # Controle da própria voz (TTS) em runtime. None = voz fixa do boot.
        self._voice = voice
        # Última série estruturada buscada (p/ montar gráfico sem re-digitar pontos).
        self._last_serie: list[dict] | None = None
        self._max_tool_rounds = max_tool_rounds
        self._client = AsyncOpenAI(api_key=api_key)
        # A persona (com os fatos) só é reconstruída no boot/reset — igual ao
        # Gemini: mid-sessão o modelo já viu os fatos novos na própria conversa.
        self._system = _system_prompt(
            usuario_nome,
            memory.facts_block(),
            can_code=self._has_coder(),
            can_tasks=self._has_tasks(),
            can_timer=self._has_timer(),
            can_finance=self._has_finance(),
            can_habits=self._has_habits(),
            can_ideas=self._has_ideas(),
            can_viz=self._has_viz(),
            can_oracle=self._has_oracle(),
            can_realtime=self._has_realtime(),
            can_voice=self._has_voice(),
        )
        self._messages: list[dict] = self._boot_messages()
        self._lock = asyncio.Lock()

    def _has_coder(self) -> bool:
        return self._coder is not None and self._coder.available()

    def _has_tasks(self) -> bool:
        return self._tasks is not None and self._tasks.available()

    def _has_timer(self) -> bool:
        return self._timer is not None and self._timer.available()

    def _has_finance(self) -> bool:
        return self._finance is not None and self._finance.available()

    def _has_habits(self) -> bool:
        return self._habits is not None and self._habits.available()

    def _has_ideas(self) -> bool:
        return self._second_brain is not None and self._second_brain.available()

    def _has_viz(self) -> bool:
        return self._viz is not None and self._viz.available()

    def _has_oracle(self) -> bool:
        return self._oracle is not None and self._oracle.available()

    def _has_realtime(self) -> bool:
        return self._realtime is not None and self._realtime.available()

    def _has_voice(self) -> bool:
        return self._voice is not None

    def _maybe_stop_timer(self, text: str) -> str | None:
        """
        Se o texto for um comando curto de parada E houver cronômetro rodando,
        encerra-o e devolve uma confirmação na persona. Senão, devolve None (o
        fluxo normal segue — inclusive para 'terminei' quando nada roda).
        """
        if not self._has_timer() or not _STOP_TIME_RE.match(text):
            return None
        if self._timer.status() is None:
            return None  # nada rodando: não é um comando de tempo
        parada = self._timer.stop()
        if parada is None:  # corrida improvável: alguém encerrou no meio
            return None
        return (
            f"Cronômetro encerrado, senhor — {parada['duracao']} "
            f"em {parada['projeto']}."
        )

    def _boot_messages(self) -> list[dict]:
        """Converte o histórico salvo em mensagens do chat (user/assistant)."""
        msgs: list[dict] = []
        for h in self.memory.boot_history():
            role = "user" if h.get("role") == "user" else "assistant"
            msgs.append({"role": role, "content": h["texto"]})
        return msgs

    def _trim(self) -> None:
        """
        Mantém a janela de mensagens dentro do teto (mais recentes), SEM quebrar
        o encanamento de ferramentas. Cortar pela frente pode deixar uma resposta
        'tool' sem o 'assistant' com tool_calls que a originou (a API rejeita
        isso), então descartamos as mensagens 'tool' órfãs que sobrarem no início.
        """
        if len(self._messages) <= _MAX_MENSAGENS:
            return
        janela = self._messages[-_MAX_MENSAGENS:]
        while janela and janela[0].get("role") == "tool":
            janela.pop(0)
        self._messages = janela

    async def reply(self, text: str, on_action=None, on_visualization=None) -> str:
        """
        Envia uma mensagem do usuário e devolve a resposta do JARVIS.

        Se o modelo decidir que o pedido é uma AÇÃO de código, ele chama a
        ferramenta do Claude Code (loop de tool-calling); o `on_action` opcional
        é avisado antes de cada execução (para a UI mostrar "executando…").
        """
        text = (text or "").strip()
        if not text:
            return ""

        # Fast-path determinístico para PARAR o cronômetro. Enunciados curtos como
        # "parei" são mal roteados pelo gpt-4o-mini (deixa de chamar 'parar_tempo'),
        # então tratamos direto — mas só quando há de fato um cronômetro rodando,
        # para não confundir "parei de fumar" com um comando de tempo.
        parada = self._maybe_stop_timer(text)
        if parada is not None:
            self.memory.add_turn("user", text)
            self._messages.append({"role": "user", "content": text})
            self._messages.append({"role": "assistant", "content": parada})
            self._trim()
            self.memory.add_turn("jarvis", parada)
            self.memory.save()
            return parada

        # Comandos explícitos de memória: mutam o armazenamento de forma
        # determinística; o modelo ainda responde em persona por cima.
        comando = self._apply_command(text)

        self.memory.add_turn("user", text)
        self._messages.append({"role": "user", "content": text})

        tools = self._tools()
        # IMPORTANTE: o encanamento das ferramentas (assistant com tool_calls +
        # as respostas 'tool') é PERSISTIDO em self._messages, não jogado fora.
        # Sem isso, o histórico vira uma sequência de "usuário pede / assistente
        # confirma em texto" sem nenhuma chamada visível, e o gpt-4o-mini passa a
        # IMITAR esse padrão — parando de chamar as tools e alucinando a
        # confirmação. Manter o encanamento é a prática correta de tool-use
        # multi-turno; o `_trim` cuida para nunca deixar uma 'tool' órfã.
        used_action = False
        reply = ""
        rounds = 0
        while True:
            kwargs: dict = dict(
                model=self.model,
                messages=[{"role": "system", "content": self._system}, *self._messages],
                temperature=0.7,
                max_tokens=800,
            )
            # Oferece as ferramentas enquanto não estourar o teto de rodadas.
            # `parallel_tool_calls=False`: uma ação por vez — evita disparar dois
            # processos `claude` concorrentes (que batem no limite de sessões).
            if tools and rounds <= self._max_tool_rounds:
                kwargs["tools"] = tools
                kwargs["tool_choice"] = "auto"
                kwargs["parallel_tool_calls"] = False

            async with self._lock:
                response = await self._client.chat.completions.create(**kwargs)

            msg = response.choices[0].message
            if not getattr(msg, "tool_calls", None):
                reply = (msg.content or "").strip()
                break

            used_action = True
            rounds += 1
            self._messages.append(msg.model_dump(exclude_none=True))
            for tc in msg.tool_calls:
                result = await self._run_tool(tc, on_action, on_visualization)
                self._messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": json.dumps(result, ensure_ascii=False),
                    }
                )

        if not reply:
            reply = "Desculpe, senhor, não consegui formular uma resposta para isso."

        self._messages.append({"role": "assistant", "content": reply})
        self._trim()
        self.memory.add_turn("jarvis", reply)
        self.memory.save()

        # Extrai fatos só de conversas comuns (não de comandos nem de ações).
        if comando is None and not used_action:
            asyncio.create_task(self._learn(text, reply))

        return reply

    def _tools(self) -> list[dict] | None:
        """Ferramentas expostas ao modelo, conforme os colaboradores disponíveis."""
        tools: list[dict] = []
        if self._has_coder():
            tools += self._coder_tools()
        if self._has_tasks():
            tools += self._task_tools()
        if self._has_timer():
            tools += self._time_tools()
        if self._has_finance():
            tools += self._finance_tools()
        if self._has_habits():
            tools += self._habit_tools()
        if self._has_ideas():
            tools += self._idea_tools()
        if self._has_viz():
            tools += self._viz_tools()
        if self._has_oracle():
            tools += self._oracle_tools()
        if self._has_realtime():
            tools += self._realtime_tools()
        if self._has_voice():
            tools += self._voice_tools()
        return tools or None

    def _realtime_tools(self) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "buscar_tempo_real",
                    "description": (
                        "Busca informação ATUAL na web (cotações, clima, notícias, "
                        "preços, resultados, dados que mudam com o tempo). Use no "
                        "lugar de chutar ou dizer que não sabe. Não revele a fonte."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "consulta": {
                                "type": "string",
                                "description": (
                                    "A pergunta de busca, específica e "
                                    "autossuficiente (ex.: 'cotação do dólar hoje "
                                    "em reais', 'principais notícias de tecnologia hoje'). "
                                    "Para gráficos, peça a SÉRIE com o formato exato "
                                    "(ex.: 'cotação de fechamento do dólar USD/BRL em "
                                    "cada um dos últimos 30 dias, formato AAAA-MM-DD: valor')."
                                ),
                            },
                            "estruturado": {
                                "type": "boolean",
                                "description": (
                                    "true quando você precisa dos DADOS CRUS para um "
                                    "gráfico/série (retorna só a lista, uma linha por "
                                    "item, sem prosa). false para uma resposta em texto."
                                ),
                            },
                        },
                        "required": ["consulta"],
                    },
                },
            }
        ]

    def _voice_tools(self) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "definir_voz",
                    "description": (
                        "Troca a VOZ (TTS) do JARVIS em runtime, de forma grudenta. "
                        "Use quando o senhor pedir para mudar a voz, o timbre ou o "
                        "motor de fala. Não use para conversa comum."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "motor": {
                                "type": "string",
                                "enum": ["kokoro", "xtts", "elevenlabs", "piper"],
                                "description": (
                                    "Opcional. Motor de fala: 'kokoro' (local/natural, "
                                    "padrão), 'xtts' (natural, lenta), 'elevenlabs' "
                                    "(premium), 'piper' (local básica)."
                                ),
                            },
                            "voz": {
                                "type": "string",
                                "description": (
                                    "Opcional. Nome da voz. Kokoro: pm_alex/pm_santa "
                                    "(masc.), pf_dora (fem.). XTTS: 'Royston Min', "
                                    "'Dionisio Schuyler', 'Viktor Eka'. Piper: faber/"
                                    "cadu/jeff/edresson. Falar só a voz já leva o motor."
                                ),
                            },
                        },
                    },
                },
            }
        ]

    def _oracle_tools(self) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "consultar_claude",
                    "description": (
                        "Consulta o Claude (modelo mais forte) como oráculo quando "
                        "você NÃO sabe, está em dúvida, ou a pergunta exige raciocínio "
                        "profundo/conhecimento especializado. Devolve a resposta dele."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "pergunta": {
                                "type": "string",
                                "description": (
                                    "A pergunta completa e autossuficiente, com todo "
                                    "o contexto necessário para ser respondida sozinha."
                                ),
                            }
                        },
                        "required": ["pergunta"],
                    },
                },
            }
        ]

    def _coder_tools(self) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "executar_no_claude_code",
                    "description": (
                        "Executa uma tarefa de PROGRAMAÇÃO na máquina do usuário via "
                        "Claude Code (agente autônomo que cria/edita arquivos, roda "
                        "comandos, corrige bugs, monta projetos). Use SEMPRE que o "
                        "pedido envolver desenvolver/criar/editar/refatorar código, "
                        "arquivos ou projetos de software. NÃO use para conversa, "
                        "dúvidas teóricas ou pedidos fora de software."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "tarefa": {
                                "type": "string",
                                "description": (
                                    "Instrução completa e autossuficiente do que fazer, "
                                    "com todo o contexto necessário. Escreva como uma "
                                    "ordem clara para um programador."
                                ),
                            },
                            "projeto": {
                                "type": "string",
                                "description": (
                                    "Nome da subpasta do projeto (em ~/projetos) onde "
                                    "rodar, ex.: 'jarvis-v2'. Vazio se o usuário não "
                                    "citou um projeto existente."
                                ),
                            },
                            "modelo": {
                                "type": "string",
                                "description": (
                                    "Opcional. Só preencha se o usuário pedir um modelo "
                                    "NESTA mensagem (ex.: 'Opus 4.8', 'Sonnet 4.6', "
                                    "'Haiku'). Vazio para usar o padrão atual."
                                ),
                            },
                        },
                        "required": ["tarefa"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "definir_modelo_padrao",
                    "description": (
                        "Troca o modelo padrão do Claude Code SEM executar tarefa "
                        "nenhuma. Use quando o usuário só quiser mudar o modelo daqui "
                        "pra frente (ex.: 'passa a usar o Opus 4.8', 'volta pro Sonnet')."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "modelo": {
                                "type": "string",
                                "description": "Modelo pedido, ex.: 'Opus 4.8', 'Sonnet 4.6'.",
                            }
                        },
                        "required": ["modelo"],
                    },
                },
            },
        ]

    def _task_tools(self) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "adicionar_tarefa",
                    "description": (
                        "Adiciona uma tarefa numa das listas fixas do usuário. "
                        "Antes de usar em 'trabalho', verifique os projetos "
                        "existentes com 'listar_projetos' e reaproveite o projeto "
                        "certo; só crie projeto novo se o usuário pediu. Em dúvida "
                        "sobre lista ou projeto, PERGUNTE em vez de chamar isto."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "titulo": {
                                "type": "string",
                                "description": "O que precisa ser feito, curto e claro.",
                            },
                            "lista": {
                                "type": "string",
                                "enum": ["trabalho", "pessoal"],
                                "description": "Lista fixa onde a tarefa entra.",
                            },
                            "projeto": {
                                "type": "string",
                                "description": (
                                    "Obrigatório em 'trabalho': nome do projeto "
                                    "(ex.: 'stamflow'). Deixe vazio em 'pessoal'."
                                ),
                            },
                        },
                        "required": ["titulo", "lista"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "listar_tarefas",
                    "description": (
                        "Lista as tarefas do usuário (pendentes por padrão). Use "
                        "para responder 'o que tenho pra fazer' e para descobrir o "
                        "'id' de uma tarefa antes de concluí-la ou removê-la."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "lista": {
                                "type": "string",
                                "enum": ["trabalho", "pessoal"],
                                "description": "Filtra por lista. Vazio = todas.",
                            },
                            "projeto": {
                                "type": "string",
                                "description": "Filtra por projeto (dentro de trabalho).",
                            },
                            "incluir_concluidas": {
                                "type": "boolean",
                                "description": "true para incluir as já concluídas.",
                            },
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "concluir_tarefa",
                    "description": (
                        "Marca uma tarefa como concluída pelo seu 'id'. Se não "
                        "souber o id, liste antes com 'listar_tarefas'."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "integer", "description": "id da tarefa."}
                        },
                        "required": ["id"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "remover_tarefa",
                    "description": (
                        "Apaga uma tarefa pelo seu 'id'. Use quando o usuário pedir "
                        "para excluir/cancelar (não apenas concluir)."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "integer", "description": "id da tarefa."}
                        },
                        "required": ["id"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "listar_projetos",
                    "description": (
                        "Lista os projetos que já existem na lista Trabalho. "
                        "Consulte ANTES de adicionar uma tarefa de trabalho, para "
                        "encaixar no projeto certo e não duplicar."
                    ),
                    "parameters": {"type": "object", "properties": {}},
                },
            },
        ]

    def _time_tools(self) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "iniciar_tempo",
                    "description": (
                        "Inicia um cronômetro de tempo num projeto (ex.: o senhor "
                        "diz 'começando o Stamflow'). Encerra automaticamente "
                        "qualquer cronômetro que já esteja rodando."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "projeto": {
                                "type": "string",
                                "description": (
                                    "Nome do projeto em que o senhor vai trabalhar. "
                                    "Reaproveite os nomes de projeto já usados."
                                ),
                            }
                        },
                        "required": ["projeto"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "parar_tempo",
                    "description": (
                        "Encerra o cronômetro em andamento (o senhor 'parou', "
                        "'terminou', 'fez uma pausa')."
                    ),
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "status_tempo",
                    "description": (
                        "Diz qual cronômetro está rodando agora e há quanto tempo "
                        "(ou que nenhum está ativo)."
                    ),
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "relatorio_tempo",
                    "description": (
                        "Relatório do tempo real dedicado a cada projeto no período. "
                        "Use para perguntas do tipo 'quanto tempo essa semana?'."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "periodo": {
                                "type": "string",
                                "enum": ["hoje", "ontem", "semana", "mes"],
                                "description": "Período do relatório. Padrão: 'semana'.",
                            }
                        },
                    },
                },
            },
        ]

    def _finance_tools(self) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "registrar_gasto",
                    "description": (
                        "Registra um gasto do usuário (ex.: 'gastei 50 no mercado'). "
                        "Reaproveite categorias já usadas para o resumo não "
                        "fragmentar. Se valor ou categoria não estiverem claros, "
                        "PERGUNTE antes."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "valor": {
                                "type": "number",
                                "description": "Valor gasto em reais (R$), positivo.",
                            },
                            "categoria": {
                                "type": "string",
                                "description": (
                                    "Categoria curta (ex.: mercado, transporte, "
                                    "lazer, contas, saúde)."
                                ),
                            },
                            "descricao": {
                                "type": "string",
                                "description": "Detalhe opcional (ex.: 'almoço com cliente').",
                            },
                        },
                        "required": ["valor", "categoria"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "listar_gastos",
                    "description": (
                        "Lista os lançamentos de gasto (mais recentes primeiro), "
                        "com filtros opcionais. Use para detalhar ou achar um id."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "periodo": {
                                "type": "string",
                                "enum": ["hoje", "ontem", "semana", "mes", "ano"],
                                "description": "Filtra por período. Vazio = tudo.",
                            },
                            "categoria": {
                                "type": "string",
                                "description": "Filtra por categoria.",
                            },
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "relatorio_gastos",
                    "description": (
                        "Resumo dos gastos por categoria no período. Use para "
                        "'quanto gastei esse mês', 'meus gastos da semana'."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "periodo": {
                                "type": "string",
                                "enum": ["hoje", "ontem", "semana", "mes", "ano"],
                                "description": "Período do relatório. Padrão: 'mes'.",
                            }
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "listar_categorias",
                    "description": (
                        "Lista as categorias de gasto já usadas, para reaproveitar "
                        "e manter o resumo consistente."
                    ),
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "remover_gasto",
                    "description": "Apaga um lançamento de gasto pelo seu 'id'.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "integer", "description": "id do gasto."}
                        },
                        "required": ["id"],
                    },
                },
            },
        ]

    def _habit_tools(self) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "registrar_habito",
                    "description": (
                        "Cria um hábito a acompanhar. Use só quando o senhor quiser "
                        "COMEÇAR a rastrear um hábito novo (não no check-in diário)."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "nome": {
                                "type": "string",
                                "description": "Nome curto do hábito (ex.: exercício, cigarro).",
                            },
                            "tipo": {
                                "type": "string",
                                "enum": ["construir", "quebrar"],
                                "description": (
                                    "'construir' = algo a fazer todo dia; "
                                    "'quebrar' = algo a evitar."
                                ),
                            },
                        },
                        "required": ["nome", "tipo"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "marcar_dia",
                    "description": (
                        "Registra o resultado do dia para um hábito (check-in). "
                        "sucesso=true se o senhor cumpriu a META do hábito: em "
                        "'construir', fez; em 'quebrar', EVITOU. Se o hábito não "
                        "existir, PERGUNTE antes de criar."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "habito": {
                                "type": "string",
                                "description": "Nome do hábito existente.",
                            },
                            "sucesso": {
                                "type": "boolean",
                                "description": "true se cumpriu a meta do dia; false se não.",
                            },
                            "data": {
                                "type": "string",
                                "description": (
                                    "Opcional: 'hoje' (padrão), 'ontem' ou AAAA-MM-DD."
                                ),
                            },
                        },
                        "required": ["habito", "sucesso"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "listar_habitos",
                    "description": (
                        "Lista os hábitos ativos, com a sequência atual e o status "
                        "de hoje. Consulte para reaproveitar nomes ou responder "
                        "'como estão meus hábitos'."
                    ),
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "relatorio_habitos",
                    "description": (
                        "Consistência dos hábitos nos últimos dias (taxa de sucesso "
                        "+ sequência). Use para o feedback periódico honesto."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "dias": {
                                "type": "integer",
                                "description": "Janela em dias. Padrão: 7.",
                            }
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "remover_habito",
                    "description": "Para de acompanhar um hábito (mantém o histórico).",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "habito": {"type": "string", "description": "Nome do hábito."}
                        },
                        "required": ["habito"],
                    },
                },
            },
        ]

    def _idea_tools(self) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "anotar_ideia",
                    "description": (
                        "Guarda uma ideia/pensamento/plano do senhor no segundo "
                        "cérebro para reconectar no futuro. Devolve ideias "
                        "RELACIONADAS do passado — mencione as que tiverem conexão "
                        "real. Use para reflexões e planos, não para tarefas/gastos."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "texto": {
                                "type": "string",
                                "description": (
                                    "A ideia em uma frase autossuficiente, na 1ª "
                                    "pessoa do senhor (ex.: 'Quero criar um app de X')."
                                ),
                            }
                        },
                        "required": ["texto"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "buscar_ideias",
                    "description": (
                        "Busca ideias passadas relacionadas a um tema (semântico). "
                        "Use quando o senhor perguntar o que já pensou/anotou sobre algo."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "consulta": {
                                "type": "string",
                                "description": "Tema ou assunto a procurar.",
                            }
                        },
                        "required": ["consulta"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "listar_ideias",
                    "description": "Lista as ideias mais recentes guardadas.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "limite": {
                                "type": "integer",
                                "description": "Quantas trazer (padrão 20).",
                            }
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "remover_ideia",
                    "description": "Apaga uma ideia pelo seu 'id'.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "integer", "description": "id da ideia."}
                        },
                        "required": ["id"],
                    },
                },
            },
        ]

    def _viz_tools(self) -> list[dict]:
        spec_schema = {
            "type": "object",
            "description": "Especificação da visualização a desenhar.",
            "properties": {
                "tipo": {
                    "type": "string",
                    "enum": ["barras", "linhas", "pizza", "lista", "kpis", "mapa_mental"],
                },
                "titulo": {"type": "string"},
                "unidade": {
                    "type": "string",
                    "description": "Opcional p/ gráficos (ex.: 'R$', 'h', '%').",
                },
                "dados": {
                    "type": "array",
                    "description": "Para barras/linhas/pizza.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "rotulo": {"type": "string"},
                            "valor": {"type": "number"},
                        },
                        "required": ["rotulo", "valor"],
                    },
                },
                "itens": {
                    "type": "array",
                    "description": "Para 'lista' (textos).",
                    "items": {"type": "string"},
                },
                "cards": {
                    "type": "array",
                    "description": "Para 'kpis'.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "rotulo": {"type": "string"},
                            "valor": {"type": "string"},
                            "sub": {"type": "string"},
                        },
                        "required": ["rotulo", "valor"],
                    },
                },
                "mapa": {
                    "type": "object",
                    "description": "Para 'mapa_mental'.",
                    "properties": {
                        "centro": {"type": "string"},
                        "ramos": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "titulo": {"type": "string"},
                                    "filhos": {"type": "array", "items": {"type": "string"}},
                                },
                                "required": ["titulo"],
                            },
                        },
                    },
                },
            },
            "required": ["tipo", "titulo"],
        }
        return [
            {
                "type": "function",
                "function": {
                    "name": "mostrar_design",
                    "description": (
                        "Desenha na 2ª janela um DESIGN LIVRE que você cria em HTML. "
                        "Liberdade total: qualquer layout, gráfico, mapa, animação ou "
                        "estética. Use para tudo que for rico/criativo/fora do padrão."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "titulo": {"type": "string", "description": "Título curto."},
                            "html": {
                                "type": "string",
                                "description": (
                                    "Documento HTML autossuficiente do conteúdo (pode "
                                    "incluir <style> e <script> inline, SVG e Canvas). "
                                    "NÃO use recursos externos (sem URLs/CDN/fontes "
                                    "externas) — inline tudo. Ele roda isolado, então "
                                    "não tente acessar a página principal."
                                ),
                            },
                            "libs": {
                                "type": "array",
                                "items": {"type": "string", "enum": ["chartjs", "d3"]},
                                "description": (
                                    "Opcional: bibliotecas a injetar (globais 'Chart' "
                                    "e/ou 'd3'). Só declare as que realmente usar."
                                ),
                            },
                            "salvar_como": {
                                "type": "string",
                                "description": "Opcional: nome para salvar e repetir depois.",
                            },
                        },
                        "required": ["titulo", "html"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "mostrar_visualizacao",
                    "description": (
                        "Atalho para um gráfico SIMPLES/padrão (barras/linhas/pizza/"
                        "lista/kpis/mapa_mental). Para algo rico, use 'mostrar_design'. "
                        "Para dados que você já tem, busque os números reais antes."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "spec": spec_schema,
                            "salvar_como": {
                                "type": "string",
                                "description": (
                                    "Opcional: nome para salvar este padrão e repetir depois."
                                ),
                            },
                        },
                        "required": ["spec"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "grafico_da_serie",
                    "description": (
                        "Desenha um gráfico a partir da ÚLTIMA série buscada com "
                        "'buscar_tempo_real' (estruturado=true) — usa TODOS os pontos "
                        "automaticamente, sem você redigitar. É a forma CERTA de "
                        "plotar cotações/séries do mercado ao longo do tempo."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "titulo": {"type": "string", "description": "Título do gráfico."},
                            "tipo": {
                                "type": "string",
                                "enum": ["linhas", "barras"],
                                "description": "Padrão 'linhas' (bom p/ série temporal).",
                            },
                            "unidade": {
                                "type": "string",
                                "description": "Opcional (ex.: 'R$').",
                            },
                        },
                        "required": ["titulo"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "salvar_visualizacao",
                    "description": (
                        "Salva a visualização que está na tela AGORA com um nome, "
                        "para repetir depois. Use quando o senhor pedir 'salva isso "
                        "como…' — NÃO re-desenhe, apenas salve a atual."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "nome": {
                                "type": "string",
                                "description": (
                                    "Nome do padrão — use EXATAMENTE as palavras do "
                                    "senhor, sem inventar abreviação ou slug."
                                ),
                            }
                        },
                        "required": ["nome"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "mostrar_salva",
                    "description": "Reexibe uma visualização salva, pelo nome.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "nome": {"type": "string", "description": "Nome do padrão salvo."}
                        },
                        "required": ["nome"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "listar_visualizacoes",
                    "description": "Lista os padrões de visualização salvos.",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "remover_visualizacao",
                    "description": "Apaga um padrão de visualização salvo, pelo nome.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "nome": {"type": "string", "description": "Nome do padrão."}
                        },
                        "required": ["nome"],
                    },
                },
            },
        ]

    async def _run_tool(self, tool_call, on_action, on_visualization=None) -> dict:
        """Executa a ferramenta pedida pelo modelo e devolve o resultado (dict)."""
        name = tool_call.function.name
        try:
            args = json.loads(tool_call.function.arguments or "{}")
        except json.JSONDecodeError:
            args = {}

        if name == "definir_modelo_padrao":
            modelo = self._coder.set_default_model(args.get("modelo", ""))
            return {"ok": True, "modelo_padrao": modelo}

        if name == "executar_no_claude_code":
            pedido_modelo = (args.get("modelo") or "").strip()
            if pedido_modelo:
                self._coder.set_default_model(pedido_modelo)  # grudento
            tarefa = args.get("tarefa", "")
            projeto = (args.get("projeto") or "").strip()
            if on_action:
                try:
                    await on_action(
                        {"tarefa": tarefa, "projeto": projeto, "modelo": self._coder.model}
                    )
                except Exception:
                    pass
            return await self._coder.run(tarefa, projeto)

        if name in _TASK_TOOL_NAMES:
            return self._run_task_tool(name, args)

        if name in _TIME_TOOL_NAMES:
            return self._run_time_tool(name, args)

        if name in _FINANCE_TOOL_NAMES:
            return self._run_finance_tool(name, args)

        if name in _HABIT_TOOL_NAMES:
            return self._run_habit_tool(name, args)

        if name in _IDEA_TOOL_NAMES:
            return await self._run_idea_tool(name, args)

        if name in _VIZ_TOOL_NAMES:
            return await self._run_viz_tool(name, args, on_visualization)

        if name in _ORACLE_TOOL_NAMES:
            return await self._oracle.ask(args.get("pergunta", ""))

        if name in _VOICE_TOOL_NAMES:
            return await self._voice.switch(
                motor=args.get("motor"), voz=args.get("voz")
            )

        if name in _REALTIME_TOOL_NAMES:
            res = await self._realtime.search(
                args.get("consulta", ""), bool(args.get("estruturado", False))
            )
            if res.get("serie"):  # guarda p/ 'grafico_da_serie' montar sem re-digitar
                self._last_serie = res["serie"]
            return res

        return {"ok": False, "erro": f"ferramenta desconhecida: {name}"}

    async def _run_viz_tool(self, name: str, args: dict, on_visualization) -> dict:
        """
        Despacha as ferramentas de visualização. As que EXIBEM (mostrar/…) mandam
        o spec normalizado à UI pelo callback `on_visualization` (broadcast p/ a 2ª
        janela). O modelo recebe só um resumo — não o spec inteiro de volta.
        """
        async def _emitir(spec: dict) -> None:
            if on_visualization:
                try:
                    await on_visualization(spec)
                except Exception:
                    pass

        try:
            if name in ("mostrar_design", "mostrar_visualizacao"):
                from jarvis.visualizacoes import normalizar_spec

                if name == "mostrar_design":
                    bruto = {"tipo": "html", "titulo": args.get("titulo", ""),
                             "html": args.get("html", ""), "libs": args.get("libs")}
                else:
                    bruto = args.get("spec") or {}
                spec = normalizar_spec(bruto)
                salvar = (args.get("salvar_como") or "").strip()
                if salvar:
                    self._viz.save(salvar, spec)
                self._last_viz_spec = spec
                await _emitir(spec)
                return {"ok": True, "tipo": spec["tipo"], "titulo": spec["titulo"],
                        "salvo_como": salvar or None}

            if name == "grafico_da_serie":
                from jarvis.visualizacoes import normalizar_spec

                if not self._last_serie:
                    return {"ok": False,
                            "erro": "nenhuma série buscada; use buscar_tempo_real(estruturado=true) antes"}
                spec = normalizar_spec({
                    "tipo": args.get("tipo") or "linhas",
                    "titulo": args.get("titulo") or "Série",
                    "dados": self._last_serie,
                    "unidade": args.get("unidade"),
                })
                self._last_viz_spec = spec
                await _emitir(spec)
                return {"ok": True, "tipo": spec["tipo"], "titulo": spec["titulo"],
                        "pontos": len(spec["dados"])}

            if name == "salvar_visualizacao":
                nome = (args.get("nome") or "").strip()
                if not nome:
                    return {"ok": False, "erro": "informe o nome"}
                if self._last_viz_spec is None:
                    return {"ok": False, "erro": "nada na tela para salvar"}
                self._viz.save(nome, self._last_viz_spec)
                return {"ok": True, "salvo_como": nome}

            if name == "mostrar_salva":
                nome = (args.get("nome") or "").strip()
                spec = self._viz.get(nome)
                if spec is None:
                    disponiveis = [v["nome"] for v in self._viz.list()]
                    return {"ok": False, "erro": "visualização não encontrada",
                            "disponiveis": disponiveis}
                self._last_viz_spec = spec
                await _emitir(spec)
                return {"ok": True, "titulo": spec.get("titulo"), "nome": nome}

            if name == "listar_visualizacoes":
                return {"ok": True, "visualizacoes": self._viz.list()}

            if name == "remover_visualizacao":
                removida = self._viz.remove(args.get("nome", ""))
                return {"ok": removida, "removida": removida}
        except (ValueError, TypeError) as exc:
            return {"ok": False, "erro": str(exc)}

        return {"ok": False, "erro": f"ferramenta de visualização desconhecida: {name}"}

    async def _run_idea_tool(self, name: str, args: dict) -> dict:
        """Despacha as ferramentas do segundo cérebro (assíncronas — embedding)."""
        try:
            if name == "anotar_ideia":
                return {"ok": True, **await self._second_brain.add(args.get("texto", ""))}

            if name == "buscar_ideias":
                ideias = await self._second_brain.search(args.get("consulta", ""))
                return {"ok": True, "ideias": ideias, "total": len(ideias)}

            if name == "listar_ideias":
                ideias = self._second_brain.list(int(args.get("limite", 20)))
                return {"ok": True, "ideias": ideias, "total": len(ideias)}

            if name == "remover_ideia":
                removida = self._second_brain.remove(int(args.get("id", 0)))
                return {"ok": removida, "removida": removida}
        except (ValueError, TypeError) as exc:
            return {"ok": False, "erro": str(exc)}

        return {"ok": False, "erro": f"ferramenta de ideia desconhecida: {name}"}

    def _run_habit_tool(self, name: str, args: dict) -> dict:
        """Despacha as ferramentas de hábitos (operações locais, síncronas)."""
        try:
            if name == "registrar_habito":
                habito = self._habits.add_habit(args.get("nome", ""), args.get("tipo", ""))
                return {"ok": True, "habito": habito}

            if name == "marcar_dia":
                habito = self._habits.checkin(
                    args.get("habito", ""),
                    bool(args.get("sucesso", False)),
                    args.get("data"),
                )
                return {"ok": True, "habito": habito}

            if name == "listar_habitos":
                return {"ok": True, "habitos": self._habits.list_habits()}

            if name == "relatorio_habitos":
                return {"ok": True, **self._habits.report(int(args.get("dias", 7)))}

            if name == "remover_habito":
                removido = self._habits.remove_habit(args.get("habito", ""))
                return {"ok": removido, "removido": removido}
        except (ValueError, TypeError) as exc:
            return {"ok": False, "erro": str(exc)}

        return {"ok": False, "erro": f"ferramenta de hábito desconhecida: {name}"}

    def _run_finance_tool(self, name: str, args: dict) -> dict:
        """Despacha as ferramentas de finanças (operações locais, síncronas)."""
        try:
            if name == "registrar_gasto":
                gasto = self._finance.add(
                    args.get("valor"),
                    args.get("categoria", ""),
                    args.get("descricao"),
                )
                return {"ok": True, "gasto": gasto}

            if name == "listar_gastos":
                gastos = self._finance.list(
                    periodo=args.get("periodo"),
                    categoria=args.get("categoria"),
                )
                return {"ok": True, "gastos": gastos, "total": len(gastos)}

            if name == "relatorio_gastos":
                return {"ok": True, **self._finance.report(args.get("periodo", "mes"))}

            if name == "listar_categorias":
                return {"ok": True, "categorias": self._finance.categorias()}

            if name == "remover_gasto":
                removido = self._finance.remove(int(args.get("id", 0)))
                return {"ok": removido, "removido": removido}
        except (ValueError, TypeError) as exc:
            return {"ok": False, "erro": str(exc)}

        return {"ok": False, "erro": f"ferramenta de finança desconhecida: {name}"}

    def _run_time_tool(self, name: str, args: dict) -> dict:
        """Despacha as ferramentas de rastreamento de tempo (locais, síncronas)."""
        try:
            if name == "iniciar_tempo":
                r = self._timer.start(args.get("projeto", ""))
                return {"ok": True, **r}

            if name == "parar_tempo":
                encerrado = self._timer.stop()
                if encerrado is None:
                    return {"ok": False, "erro": "nenhum cronômetro em andamento"}
                return {"ok": True, "encerrado": encerrado}

            if name == "status_tempo":
                atual = self._timer.status()
                return {"ok": True, "em_andamento": atual}

            if name == "relatorio_tempo":
                return {"ok": True, **self._timer.report(args.get("periodo", "semana"))}
        except (ValueError, TypeError) as exc:
            return {"ok": False, "erro": str(exc)}

        return {"ok": False, "erro": f"ferramenta de tempo desconhecida: {name}"}

    def _run_task_tool(self, name: str, args: dict) -> dict:
        """Despacha as ferramentas de tarefas (operações locais, síncronas)."""
        try:
            if name == "adicionar_tarefa":
                tarefa = self._tasks.add(
                    args.get("titulo", ""),
                    args.get("lista", ""),
                    args.get("projeto"),
                )
                return {"ok": True, "tarefa": tarefa}

            if name == "listar_tarefas":
                tarefas = self._tasks.list(
                    lista=args.get("lista"),
                    projeto=args.get("projeto"),
                    incluir_concluidas=bool(args.get("incluir_concluidas", False)),
                )
                return {"ok": True, "tarefas": tarefas, "total": len(tarefas)}

            if name == "concluir_tarefa":
                tarefa = self._tasks.complete(int(args.get("id", 0)))
                if tarefa is None:
                    return {"ok": False, "erro": "tarefa não encontrada"}
                return {"ok": True, "tarefa": tarefa}

            if name == "remover_tarefa":
                removida = self._tasks.remove(int(args.get("id", 0)))
                return {"ok": removida, "removida": removida}

            if name == "listar_projetos":
                return {"ok": True, "projetos": self._tasks.projetos()}
        except (ValueError, TypeError) as exc:
            return {"ok": False, "erro": str(exc)}

        return {"ok": False, "erro": f"ferramenta de tarefa desconhecida: {name}"}

    def _apply_command(self, text: str) -> str | None:
        """
        Detecta 'lembre-se que…' / 'esqueça…' e aplica na memória.
        Retorna o tipo de comando ('remember'/'forget') ou None.
        """
        m = _REMEMBER_RE.match(text)
        if m and m.group(1).strip():
            fato = m.group(1).strip()
            if self.memory.add_fact(fato, origem="comando"):
                self.memory.save()
            return "remember"

        m = _FORGET_RE.match(text)
        if m and m.group(1).strip():
            self.memory.remove_facts(m.group(1).strip())
            self.memory.save()
            return "forget"

        return None

    async def _learn(self, user_text: str, reply_text: str) -> None:
        """Extrai fatos duráveis da troca (chamada extra à OpenAI) em background."""
        prompt = (
            "Extraia fatos DURÁVEIS e objetivos sobre o USUÁRIO a partir da troca "
            "abaixo — coisas que valem lembrar por muito tempo (nome, moradia, "
            "trabalho, projetos, preferências, pessoas/pets, datas importantes). "
            "Ignore o que for efêmero, perguntas, saudações, ou fatos sobre o "
            "assistente. Responda APENAS um objeto JSON no formato "
            '{"fatos": ["..."]} com strings curtas em português na 3ª pessoa '
            '(ex.: {"fatos": ["Mora em Porto Alegre"]}). '
            'Se não houver nada digno de nota, responda {"fatos": []}.\n\n'
            f"Usuário: {user_text}\nJARVIS: {reply_text}"
        )
        try:
            async with self._lock:
                resp = await self._client.chat.completions.create(
                    model=self.model,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.0,
                    response_format={"type": "json_object"},
                )
            fatos = self._parse_facts(resp.choices[0].message.content or "")
        except Exception as exc:  # extração é best-effort — nunca propaga
            print(f"[memoria] extração falhou: {exc!r}", flush=True)
            return

        novos = [f for f in fatos if self.memory.add_fact(f, origem="auto")]
        if novos:
            self.memory.save()
            print(f"[memoria] {len(novos)} fato(s) novo(s): {novos}", flush=True)

    @staticmethod
    def _parse_facts(raw: str) -> list[str]:
        raw = raw.strip()
        if not raw:
            return []
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return []
        # Aceita tanto {"fatos": [...]} quanto um array cru, por robustez.
        if isinstance(data, dict):
            data = data.get("fatos", [])
        if not isinstance(data, list):
            return []
        return [s.strip() for s in data if isinstance(s, str) and s.strip()]

    async def proactive(self, contexto: str = "") -> str:
        """
        JARVIS puxa a conversa por conta própria (proatividade). Não é fala do
        usuário: mandamos uma instrução de sistema pedindo uma iniciativa breve
        e natural, e persistimos só a resposta como turno do JARVIS (a instrução
        em si não entra no histórico vivo nem na memória).
        """
        instrucao = (
            "(Instrução do sistema — o usuário NÃO falou agora. Tome você a "
            "iniciativa: puxe uma interação com um comentário ou pergunta breve, "
            "natural e contextual, como um assistente atencioso retomando o "
            "contato. 1 a 2 frases, na sua persona. Evite saudações genéricas "
            "repetidas; aproveite o que você sabe sobre o usuário e o horário "
            "quando fizer sentido. Não mencione que isto é uma instrução."
        )
        if contexto:
            instrucao += " " + contexto
        instrucao += ")"

        async with self._lock:
            response = await self._client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": self._system},
                    *self._messages,
                    {"role": "user", "content": instrucao},
                ],
                temperature=0.8,
                max_tokens=800,
            )

        text = (response.choices[0].message.content or "").strip()
        if not text:
            return ""
        # Só a resposta do JARVIS é persistida (a instrução é descartada).
        self._messages.append({"role": "assistant", "content": text})
        self._trim()
        self.memory.add_turn("jarvis", text)
        self.memory.save()
        return text

    def reset(self) -> None:
        """Recomeça a conversa (esquece o histórico da sessão, não os fatos)."""
        self._system = _system_prompt(
            self.usuario_nome,
            self.memory.facts_block(),
            can_code=self._has_coder(),
            can_tasks=self._has_tasks(),
            can_timer=self._has_timer(),
            can_finance=self._has_finance(),
            can_habits=self._has_habits(),
            can_ideas=self._has_ideas(),
            can_viz=self._has_viz(),
            can_oracle=self._has_oracle(),
            can_realtime=self._has_realtime(),
        )
        self._messages = []

    @classmethod
    def from_config(
        cls,
        config: dict,
        coder: object | None = None,
        tasks: object | None = None,
        timer: object | None = None,
        finance: object | None = None,
        habits: object | None = None,
        second_brain: object | None = None,
        viz: object | None = None,
        oracle: object | None = None,
        realtime: object | None = None,
        voice: object | None = None,
    ) -> "OpenAIBrain | None":
        """
        Constrói a partir da config + variável de ambiente OPENAI_API_KEY.
        Retorna None se a chave não estiver definida (backend segue em modo eco).
        Os colaboradores `coder`, `tasks`, `timer`, `finance`, `habits`,
        `second_brain`, `viz`, `oracle` e `realtime` (Perplexity) são opcionais.
        """
        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not api_key:
            return None
        model = config.get("openai_model", "gpt-4o-mini")
        usuario = config.get("usuario_nome", "senhor")
        return cls(
            api_key=api_key,
            model=model,
            usuario_nome=usuario,
            memory=Memory(),
            coder=coder,
            tasks=tasks,
            timer=timer,
            finance=finance,
            habits=habits,
            second_brain=second_brain,
            viz=viz,
            oracle=oracle,
            realtime=realtime,
            voice=voice,
            max_tool_rounds=int(config.get("claude_code_max_rounds", 3)),
        )
