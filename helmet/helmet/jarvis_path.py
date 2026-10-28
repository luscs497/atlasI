"""Torna o pacote `jarvis` (backend do desktop) importável aqui, sem copiá-lo."""

import sys

from helmet.settings import BACKEND_DIR

if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
