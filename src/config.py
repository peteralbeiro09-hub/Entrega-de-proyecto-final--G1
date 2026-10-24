"""Configuración del proyecto: variables de entorno, rutas, parámetros del RAG y carga de prompts TOML."""

import os
import tomllib
from pathlib import Path

from dotenv import load_dotenv

RAIZ = Path(__file__).resolve().parent.parent
DIR_DATOS = RAIZ / "data"
DIR_PROMPTS = RAIZ / "prompts"
DIR_RESULTADOS = RAIZ / "results"
DIR_INDICE = RAIZ / "chroma_db"

# Fecha fija para que plazos de devolución y fechas de entrega sean reproducibles (la misma del Taller 1).
FECHA_REFERENCIA = "2026-09-20"
TEMPERATURA = 0

load_dotenv(RAIZ / ".env")

# --- Parámetros del RAG (se pueden cambiar en .env sin tocar código) ---
MODELO_EMBEDDINGS = os.getenv("RAG_MODELO_EMBEDDINGS", "intfloat/multilingual-e5-small")
COLECCION = "ecomarket"
RAG_K = int(os.getenv("RAG_K", "4"))
# Similitud coseno mínima para considerar que un fragmento es relevante. Calibrada con `python app.py buscar`
# (ver docs/fase3_integracion.md §3): por debajo de este valor el asistente admite que no tiene la información.
RAG_UMBRAL = float(os.getenv("RAG_UMBRAL", "0.80"))
# 1000 caracteres: ninguna sección de las políticas lo supera, así que cada sección (incluida la tabla de zonas
# de envío) queda en un solo fragmento. Con 600 la tabla se partía (docs/fase2_base_conocimiento.md §4).
CHUNK_TAMANO = int(os.getenv("RAG_CHUNK_TAMANO", "1000"))
CHUNK_SOLAPE = int(os.getenv("RAG_CHUNK_SOLAPE", "80"))


class ErrorConfiguracion(Exception):
    """Falta una variable obligatoria en .env."""


def obtener_config_llm() -> dict:
    """Lee LLM_BASE_URL, LLM_API_KEY y LLM_MODEL; falla con un mensaje claro si falta alguna."""
    variables = ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL")
    faltantes = [v for v in variables if not os.getenv(v)]
    if faltantes:
        raise ErrorConfiguracion(
            f"Faltan variables en .env: {', '.join(faltantes)}. "
            "Copia .env.example como .env y complétalo."
        )
    return {
        "base_url": os.getenv("LLM_BASE_URL"),
        "api_key": os.getenv("LLM_API_KEY"),
        "modelo": os.getenv("LLM_MODEL"),
        "pausa_seg": float(os.getenv("LLM_PAUSA_SEG", "0")),
    }


def cargar_toml(nombre: str) -> dict:
    """Carga prompts/<nombre>.toml."""
    with open(DIR_PROMPTS / f"{nombre}.toml", "rb") as archivo:
        return tomllib.load(archivo)
