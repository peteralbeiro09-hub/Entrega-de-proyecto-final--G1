"""Cliente único para cualquier proveedor compatible con la API de OpenAI (OpenRouter, Ollama, Groq, OpenAI)."""

import hashlib
import json
import re
import sys
import time
from pathlib import Path

import openai

from src.config import TEMPERATURA, obtener_config_llm


class ErrorLLM(Exception):
    """Error al comunicarse con el modelo, con un mensaje legible para el usuario."""


class RespuestaPendiente(Exception):
    """En modo solo caché, la respuesta todavía no se ha obtenido del modelo."""


_cliente = None
_config = None
_ruta_cache: Path | None = None
_solo_cache = False

# Esperas ante saturaciones (429), frecuentes en los planes gratuitos: ~2,5 minutos en total. En el Taller 2 eran
# 15–240 s (~8 min), pensadas para evaluaciones por lotes; con una interfaz conversacional se prefiere recuperar antes.
ESPERAS_REINTENTO_SEG = (5, 10, 20, 40, 80)


def _obtener_cliente():
    global _cliente, _config
    if _cliente is None:
        _config = obtener_config_llm()
        _cliente = openai.OpenAI(base_url=_config["base_url"], api_key=_config["api_key"])
    return _cliente, _config


def nombre_modelo() -> str:
    return _obtener_cliente()[1]["modelo"]


def activar_cache(ruta: Path, solo_cache: bool = False) -> None:
    """Guarda cada respuesta real apenas llega, para que una evaluación interrumpida se pueda reanudar
    sin repetir llamadas. Con temperatura 0, reutilizar la respuesta ya obtenida es equivalente a pedirla de nuevo.
    Con solo_cache=True no se llama al modelo: lo que no esté guardado lanza RespuestaPendiente."""
    global _ruta_cache, _solo_cache
    _ruta_cache = ruta
    _solo_cache = solo_cache


def _clave_cache(modelo: str, mensajes: list[dict]) -> str:
    return hashlib.sha256(json.dumps([modelo, mensajes], ensure_ascii=False).encode("utf-8")).hexdigest()


def _leer_cache() -> dict:
    if _ruta_cache and _ruta_cache.exists():
        return json.loads(_ruta_cache.read_text(encoding="utf-8"))
    return {}


def _crear(cliente, config, mensajes):
    """Llama al modelo reintentando ante saturaciones momentáneas del proveedor."""
    for espera in ESPERAS_REINTENTO_SEG:
        try:
            return cliente.chat.completions.create(model=config["modelo"], messages=mensajes, temperature=TEMPERATURA)
        except openai.RateLimitError as e:
            print(f"  (límite o saturación del proveedor; reintento en {espera} s) {str(e)[:300]}", file=sys.stderr)
            time.sleep(espera)
    return cliente.chat.completions.create(model=config["modelo"], messages=mensajes, temperature=TEMPERATURA)


def completar(mensajes: list[dict]) -> str:
    """Envía la lista de mensajes al modelo y devuelve el texto de la respuesta."""
    cliente, config = _obtener_cliente()
    clave = _clave_cache(config["modelo"], mensajes)
    cache = _leer_cache()
    if clave in cache:
        return cache[clave]
    if _solo_cache:
        raise RespuestaPendiente()

    try:
        respuesta = _crear(cliente, config, mensajes)
    except openai.AuthenticationError as e:
        raise ErrorLLM("La clave LLM_API_KEY fue rechazada por el proveedor. Revisa .env.") from e
    except openai.NotFoundError as e:
        raise ErrorLLM(f"El modelo '{config['modelo']}' no existe en el proveedor. Revisa LLM_MODEL.") from e
    except openai.RateLimitError as e:
        raise ErrorLLM(
            "Se alcanzó el límite de uso del proveedor. Los modelos gratuitos de OpenRouter permiten "
            "20 llamadas por minuto y 50 por día, y su capacidad compartida a veces se satura: espera y "
            "vuelve a ejecutar el comando (evaluar retoma donde quedó) o usa `evaluar --solo ...`."
        ) from e
    except openai.APIConnectionError as e:
        raise ErrorLLM(f"No se pudo conectar con {config['base_url']}. Revisa la red o LLM_BASE_URL.") from e
    except openai.APIError as e:
        raise ErrorLLM(f"Error del proveedor: {e}") from e
    finally:
        if config["pausa_seg"]:
            time.sleep(config["pausa_seg"])

    texto = respuesta.choices[0].message.content or ""
    # Algunos modelos (p. ej. Qwen 3) incluyen su razonamiento en <think>...</think>; nunca se muestra al cliente.
    texto = re.sub(r"<think>.*?</think>", "", texto, flags=re.DOTALL).strip()

    if _ruta_cache:
        cache[clave] = texto
        _ruta_cache.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
    return texto
