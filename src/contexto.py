"""Datos estructurados de EcoMarket que NO pasan por el RAG: pedidos (búsqueda exacta) y casos de prueba.

Los pedidos son datos transaccionales y personales: se consultan por número exacto, como lo haría una
herramienta (tool calling) contra la base de datos de pedidos, y nunca se indexan en la base vectorial.
"""

import json
import re

from src.config import DIR_DATOS

PATRON_PEDIDO = re.compile(r"ECO-\d{5}", re.IGNORECASE)


def _leer_json(nombre: str):
    with open(DIR_DATOS / nombre, encoding="utf-8") as archivo:
        return json.load(archivo)


def numeros_pedido(texto: str) -> list[str]:
    """Números de seguimiento (ECO-XXXXX) mencionados en el mensaje, en mayúsculas y sin repetir."""
    return list(dict.fromkeys(n.upper() for n in PATRON_PEDIDO.findall(texto)))


def pedidos_texto(numeros: list[str] | None = None) -> str:
    """Pedidos en JSON legible. Con `numeros`, solo los pedidos mencionados por el cliente.

    Taller 1 inyectaba los 15 pedidos en cada prompt; aquí se filtran antes de llamar al modelo, así el LLM
    nunca ve datos personales de otros clientes y el prompt es más corto. Si el número no existe, la lista
    queda vacía y el prompt v4 responde que no encuentra el pedido.
    """
    pedidos = _leer_json("pedidos.json")
    if numeros is not None:
        pedidos = [p for p in pedidos if p["numero_seguimiento"] in numeros]
    return json.dumps(pedidos, ensure_ascii=False, indent=2)


def casos_prueba() -> dict:
    return _leer_json("casos_prueba.json")
