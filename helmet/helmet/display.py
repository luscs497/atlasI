"""
Display pequeno no capacete (LCD 16x2 / 20x4, OLED ~21x5) — formatação pura.

Tudo aqui é determinístico e sem I/O: recebe o texto do ATLAS e devolve as
linhas prontas p/ o firmware só desenhar (sem quebrar nada do lado do ESP32).

Regras:
- tira markdown, links, emojis; quebra POR PALAVRAS em `cols` x `rows`;
- `ascii=True` dobra acentos (á->a, ç->c) e troca aspas curvas/travessão/
  reticências pelos equivalentes ASCII (o ROM do HD44780 não tem acentos);
- se não couber (ou passar de `max_chars`), corta em FIM DE PALAVRA e põe "...";
  palavra maior que a linha inteira é a única coisa partida (não há outro jeito);
- números e horas ("14:30", "R$ 1.234,56") passam intactos.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from helmet.audio.tts import _GLUE

ELLIPSIS = "..."

_ASCII_MAP = str.maketrans({
    "‘": "'", "’": "'", "‚": "'", "‛": "'", "´": "'", "`": "'",
    "“": '"', "”": '"', "„": '"', "«": '"', "»": '"',
    "–": "-", "—": "-", "―": "-", "−": "-", "•": "-",
    " ": " ", " ": " ", " ": " ",
    "º": "o", "ª": "a", "°": "o",
    "→": "->", "×": "x", "€": "EUR",
})
_EMOJI = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U00002B00-\U00002BFF"
    "\U0000FE00-\U0000FE0F\U0000200D\U000020E3\U0001F3FB-\U0001F3FF]"
)


@dataclass(frozen=True)
class DisplaySpec:
    cols: int = 16
    rows: int = 2
    ascii: bool = True

    @property
    def capacity(self) -> int:
        return self.cols * self.rows

    @classmethod
    def parse(cls, raw: object) -> "DisplaySpec | None":
        """Campo `display` do hello -> spec (ou None = sem display). Valida limites."""
        if not isinstance(raw, dict):
            return None
        cols, rows = int(raw.get("cols", 16)), int(raw.get("rows", 2))
        if not (8 <= cols <= 64 and 1 <= rows <= 16):
            raise ValueError("display: cols deve estar em 8..64 e rows em 1..16")
        return cls(cols=cols, rows=rows, ascii=bool(raw.get("ascii", True)))


@dataclass(frozen=True)
class Frame:
    text: str
    lines: list[str]


def max_chars(spec: DisplaySpec, cap: int) -> int:
    """Limite de caracteres da resposta: cols*rows, com teto `cap`."""
    return min(spec.capacity, cap) if cap > 0 else spec.capacity


def clean_text(text: str) -> str:
    """Tira markdown/links/emojis e junta tudo numa linha só."""
    t = text or ""
    t = re.sub(r"```.*?```", " ", t, flags=re.S)
    t = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", t)  # [texto](url) -> texto
    t = re.sub(r"https?://\S+", "", t)
    t = _EMOJI.sub("", t)
    t = re.sub(r"^\s*#+\s*", "", t, flags=re.M)
    t = re.sub(r"^\s*[-*•>]\s+", "", t, flags=re.M)
    t = re.sub(r"^\s*\d+[.)]\s+", "", t, flags=re.M)
    t = re.sub(r"(\*\*|__|\*|~~|`)", "", t)
    t = re.sub(r"(?<!\w)_(?=\w)|(?<=\w)_(?!\w)", "", t)  # _itálico_
    t = t.replace("|", " ")
    return " ".join(t.split())


def to_ascii(text: str) -> str:
    """Dobra acentos e pontuação tipográfica p/ ASCII puro (o que sobrar some)."""
    t = text.replace("…", ELLIPSIS).translate(_ASCII_MAP)
    t = unicodedata.normalize("NFKD", t)
    t = "".join(c for c in t if not unicodedata.combining(c))
    return "".join(c for c in t if 32 <= ord(c) < 127)


def _tokens(text: str, cols: int) -> list[str]:
    """Palavras; só uma palavra maior que a linha inteira é partida."""
    out: list[str] = []
    for w in text.split():
        while len(w) > cols:
            out.append(w[:cols])
            w = w[cols:]
        if w:
            out.append(w)
    return out


def wrap(words: list[str], cols: int) -> list[str]:
    lines: list[str] = []
    cur = ""
    for w in words:
        if not cur:
            cur = w
        elif len(cur) + 1 + len(w) <= cols:
            cur = f"{cur} {w}"
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def _fits(lines: list[str], spec: DisplaySpec, limit: int) -> bool:
    return len(lines) <= spec.rows and len(" ".join(lines)) <= limit


def _with_ellipsis(words: list[str]) -> list[str]:
    last = words[-1].rstrip(".,;:!?-")
    return [*words[:-1], last + ELLIPSIS] if last else [*words[:-1], ELLIPSIS]


def fit(text: str, spec: DisplaySpec, limit: int | None = None) -> Frame:
    """Texto do ATLAS -> quadro que CABE em `spec` (e em `limit` caracteres)."""
    limit = spec.capacity if limit is None else min(limit, spec.capacity)
    t = clean_text(text)
    if spec.ascii:
        t = " ".join(to_ascii(t).split())
    words = _tokens(t, spec.cols)
    lines = wrap(words, spec.cols)
    if _fits(lines, spec, limit):
        return Frame(" ".join(lines), lines)

    # Não coube: maior prefixo de palavras que caiba COM "...". Evita terminar em
    # palavra de ligação ("comprar o..."), que lê mal.
    for k in range(len(words) - 1, 0, -1):
        if k > 1 and words[k - 1].lower().strip(".,;:!?") in _GLUE:
            continue
        cand = wrap(_with_ellipsis(words[:k]), spec.cols)
        if _fits(cand, spec, limit):
            return Frame(" ".join(cand), cand)
    # Nem uma palavra coube (palavra gigante): corta no caractere.
    room = max(0, min(limit, spec.cols) - len(ELLIPSIS))
    line = (words[0][:room] if words else "") + ELLIPSIS
    return Frame(line, [line])
