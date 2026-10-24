"""Arma la lista de mensajes para cada ejercicio y versión, y lee de forma robusta el JSON del modelo."""

import json
import re

from src.config import FECHA_REFERENCIA, cargar_toml


def _rellenar(plantilla: str, valores: dict) -> str:
    """Reemplaza solo los marcadores conocidos; así las llaves de los JSON de ejemplo no se tocan."""
    for clave, valor in valores.items():
        plantilla = plantilla.replace("{" + clave + "}", valor)
    return plantilla


def construir_mensajes(archivo: str, version: str, consulta: str, contexto: dict | None = None) -> list[dict]:
    """Devuelve los mensajes: un único system al inicio y luego user/assistant alternados.

    `contexto` trae los bloques que se inyectan en la plantilla ({pedidos}, {politica}, {catalogo},
    {contexto}...). En el Taller 1 eran los documentos completos; ahora los arma el RAG con los fragmentos
    recuperados.
    """
    prompt = cargar_toml(archivo)[version]
    valores = {"consulta": consulta, "fecha": FECHA_REFERENCIA, **(contexto or {})}

    mensajes = []
    if "system" in prompt:
        mensajes.append({"role": "system", "content": _rellenar(prompt["system"], valores)})
    for ejemplo in prompt.get("ejemplos", []):
        mensajes.append({"role": "user", "content": ejemplo["user"]})
        mensajes.append({"role": "assistant", "content": ejemplo["assistant"]})
    mensajes.append({"role": "user", "content": _rellenar(prompt["user"], valores)})

    validar_alternancia(mensajes)
    return mensajes


def validar_alternancia(mensajes: list[dict]) -> None:
    """Varias plantillas de modelos abiertos fallan con system intercalados o dos user seguidos."""
    roles = [m["role"] for m in mensajes]
    if "system" in roles[1:]:
        raise ValueError("El mensaje system solo puede ir al inicio.")
    conversacion = roles[1:] if roles[0] == "system" else roles
    for i, rol in enumerate(conversacion):
        esperado = "user" if i % 2 == 0 else "assistant"
        if rol != esperado:
            raise ValueError(f"Alternancia inválida en la posición {i}: {roles}")
    if conversacion[-1] != "user":
        raise ValueError("El último mensaje debe ser del usuario.")


def extraer_json(texto: str) -> dict | None:
    """Quita bloques de código y toma el primer objeto {...}. Devuelve None si no se puede interpretar."""
    limpio = re.sub(r"```(?:json)?", "", texto)
    inicio = limpio.find("{")
    if inicio == -1:
        return None
    try:
        objeto, _ = json.JSONDecoder().raw_decode(limpio[inicio:])
    except json.JSONDecodeError:
        return None
    return objeto if isinstance(objeto, dict) else None
