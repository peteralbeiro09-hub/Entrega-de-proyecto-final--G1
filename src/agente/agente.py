"""Agente de devoluciones con LangChain: un LLM con tool calling que decide qué herramienta usar en cada paso.

    mensaje del cliente ──► LLM ──┬─ responde directo (saludo, falta un dato) ──────────────► respuesta
                                  └─ pide una herramienta ─► se ejecuta ─► resultado ─► LLM ─► ... ─► respuesta

`create_agent` (LangChain 1.x, construido sobre LangGraph) implementa ese ciclo ReAct. Aquí se definen:
- el modelo (cualquier proveedor compatible con la API de OpenAI; por defecto Groq),
- las herramientas, con un esquema Pydantic cuyas descripciones son lo que el LLM lee para decidir,
- el límite de pasos y la conversión de cualquier fallo en una respuesta amable para el cliente.
"""

import json
import os
import re
from typing import Literal
from urllib.parse import urlparse

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from src.agente import herramientas as h
from src.agente.observabilidad import iniciar_turno, observar, registrar_evento
from src.config import FECHA_REFERENCIA, ErrorConfiguracion, cargar_toml

# Máximo de pasos del grafo (cada llamada al LLM o a una herramienta cuenta uno). Un caso completo usa ~6.
LIMITE_PASOS = int(os.getenv("AGENTE_LIMITE_PASOS", "12"))


# ---------- Esquemas de entrada: lo que el LLM ve de cada herramienta ----------

class EntradaPedido(BaseModel):
    numero_seguimiento: str = Field(description="Número de seguimiento del pedido, formato ECO-XXXXX.")


class EntradaDevolucion(BaseModel):
    numero_seguimiento: str = Field(description="Número de seguimiento del pedido, formato ECO-XXXXX.")
    producto: str = Field(description="Producto a devolver, tal como lo nombra el cliente (p. ej. 'la botella').")
    motivo: Literal["retracto", "danado", "defecto"] = Field(
        description="'retracto' si ya no lo quiere o se arrepintió; 'danado' si llegó roto o dañado; "
                    "'defecto' si tiene una falla de fábrica.")
    tiene_foto: bool = Field(default=False,
                             description="True solo si el cliente dice que tiene o adjunta fotos del daño o defecto.")


class EntradaPregunta(BaseModel):
    pregunta: str = Field(description="La pregunta sobre políticas de EcoMarket, en español.")


class EntradaEscalamiento(BaseModel):
    motivo: str = Field(description="Por qué se escala (p. ej. 'cliente molesto', 'error de la transportadora').")
    resumen: str = Field(description="Resumen del caso para el agente humano: qué pide el cliente y qué se hizo.")
    numero_seguimiento: str | None = Field(default=None, description="Número de pedido, si se conoce.")


def construir_herramientas() -> list[StructuredTool]:
    definiciones = [
        (h.buscar_pedido, EntradaPedido,
         "Consulta un pedido por su número: estado (Entregado, En tránsito...), productos y fecha de entrega."),
        (h.verificar_elegibilidad_producto, EntradaDevolucion,
         "Aplica la política de devoluciones a un producto de un pedido. Devuelve si es elegible, la regla aplicada, "
         "la explicación y la alternativa. Úsala SIEMPRE antes de generar una etiqueta."),
        (h.generar_etiqueta_devolucion, EntradaDevolucion,
         "Genera la etiqueta (guía prepagada) de devolución de un producto elegible. Devuelve RMA, transportadora, "
         "número de guía, enlace y fecha límite. Si el producto no es elegible, la rechaza."),
        (h.consultar_politica, EntradaPregunta,
         "Busca en las políticas de devoluciones y envíos y en las preguntas frecuentes de EcoMarket."),
        (h.escalar_a_humano, EntradaEscalamiento,
         "Abre un ticket para que un agente humano atienda el caso. Devuelve el número de ticket."),
    ]
    return [StructuredTool.from_function(func=_como_json(observar(funcion)), name=funcion.__name__,
                                         description=descripcion, args_schema=esquema)
            for funcion, esquema, descripcion in definiciones]


def _como_json(funcion):
    """El LLM recibe el resultado de la herramienta como texto JSON (con tildes legibles)."""
    return lambda **argumentos: json.dumps(funcion(**argumentos), ensure_ascii=False)


# ---------- Modelo ----------

def config_modelo() -> dict:
    """El agente usa AGENTE_* si existen y, si no, el mismo LLM_* de los talleres anteriores."""
    valores = {clave: os.getenv(f"AGENTE_{clave}") or os.getenv(f"LLM_{clave}") for clave in ("BASE_URL", "API_KEY", "MODEL")}
    faltantes = [c for c, v in valores.items() if not v]
    if faltantes:
        raise ErrorConfiguracion(f"Faltan variables en .env: {', '.join('LLM_' + c for c in faltantes)}. "
                                 "Copia .env.example como .env y complétalo.")
    respaldo = os.getenv("AGENTE_MODELOS_RESPALDO", "openai/gpt-oss-20b,qwen/qwen3.8-27b")
    return {"base_url": valores["BASE_URL"], "api_key": valores["API_KEY"], "modelo": valores["MODEL"],
            "respaldo": [m.strip() for m in respaldo.split(",") if m.strip()]}


_agente = None


def obtener_agente():
    """Crea el agente una sola vez. Si el modelo principal falla (por ejemplo, se agotó su cuota diaria de tokens
    en el plan gratuito de Groq), ModelFallbackMiddleware reintenta la misma llamada con los modelos de respaldo,
    que tienen cuotas independientes. Así una cuota agotada no deja al cliente sin respuesta."""
    global _agente
    if _agente is None:
        from langchain.agents import create_agent
        from langchain.agents.middleware import ModelFallbackMiddleware
        from langchain_openai import ChatOpenAI

        cfg = config_modelo()

        def chat(nombre: str, reintentos: int):
            return ChatOpenAI(model=nombre, base_url=cfg["base_url"], api_key=cfg["api_key"],
                              temperature=0, timeout=60, max_retries=reintentos)

        # Un solo reintento en el principal: ante una cuota agotada conviene pasar rápido al respaldo.
        middleware = [ModelFallbackMiddleware(*[chat(m, 2) for m in cfg["respaldo"]])] if cfg["respaldo"] else []
        prompt = cargar_toml("agente")["agente"]["system"].replace("{fecha}", FECHA_REFERENCIA)
        _agente = create_agent(chat(cfg["modelo"], 1), tools=construir_herramientas(), system_prompt=prompt,
                               middleware=middleware)
    return _agente


# ---------- Validador de salida: ningún dato de etiqueta sin respaldo ----------

# Un código después de "RMA" ("RMA-74E93442", "RMA: ECO-RMA-10001-B1") y cualquier enlace.
PATRON_RMA = re.compile(r"RMA[\s:#*\-]*([A-Z0-9][A-Z0-9\-]{3,})")
PATRON_URL = re.compile(r"https?://[^\s)\]>*]+")
DOMINIOS_PERMITIDOS = {"etiquetas.ecomarket.example", "rastreo.ecomarket.example"}


def validar_respuesta(respuesta: str) -> list[str]:
    """Problemas de la respuesta: RMA o enlaces que no salieron de ninguna herramienta.

    Existe porque la regla del prompt ("nunca inventes un RMA") no bastó: en una prueba de la interfaz el agente
    verificó la elegibilidad, NO llamó a generar_etiqueta_devolucion y aun así respondió con un RMA, una
    transportadora, una guía y un enlace inventados (docs/img/hallazgo_etiqueta_inventada.png). La validación es
    contra el registro de etiquetas reales, así que también acepta un RMA emitido en un turno anterior.
    """
    reales = h.etiquetas_emitidas()
    urls_reales = {e["url_etiqueta"] for e in reales.values()}
    problemas = []
    for codigo in dict.fromkeys(PATRON_RMA.findall(respuesta)):
        codigo = codigo.rstrip("-")
        if codigo not in reales and f"RMA-{codigo}" not in reales:
            problemas.append(f"RMA sin respaldo: {codigo}")
    for url in dict.fromkeys(PATRON_URL.findall(respuesta)):
        url = url.rstrip(".,;")
        host = urlparse(url).hostname or ""
        if host not in DOMINIOS_PERMITIDOS:
            problemas.append(f"enlace no reconocido: {url}")
        elif host == "etiquetas.ecomarket.example" and url not in urls_reales:
            problemas.append(f"enlace de etiqueta sin respaldo: {url}")
    return problemas


# ---------- Ejecución de un turno ----------

def _a_mensajes(historial: list[dict]) -> list:
    """Historial de la interfaz ({role, content}) → mensajes de LangChain. Solo texto: las llamadas a
    herramientas de turnos anteriores no se reenvían, sus resultados ya están en las respuestas del agente."""
    tipos = {"user": HumanMessage, "assistant": AIMessage}
    return [tipos[m["role"]](content=m["content"]) for m in historial if m["role"] in tipos]


def responder(mensaje: str, historial: list[dict] | None = None, sesion: str = "cli") -> dict:
    """Ejecuta el agente para un mensaje. Nunca lanza excepciones: cualquier fallo se convierte en una
    respuesta amable y queda en el log. Devuelve {respuesta, ruta, herramientas, ok}."""
    from langgraph.errors import GraphRecursionError

    traza = iniciar_turno(sesion)
    errores = cargar_toml("agente")["errores"]
    mensajes = _a_mensajes(historial or []) + [HumanMessage(content=mensaje)]
    try:
        estado = obtener_agente().invoke({"messages": mensajes}, config={"recursion_limit": LIMITE_PASOS})
    except ErrorConfiguracion:
        raise
    except GraphRecursionError:
        ticket = observar(h.escalar_a_humano)(motivo="el agente superó el límite de pasos",
                                              resumen=f"Mensaje del cliente: {mensaje}")
        registrar_evento("error_agente", error="limite_de_pasos", ticket=ticket.get("ticket"))
        return {"ok": False, "respuesta": errores["iteraciones"] + f" Número de ticket: {ticket.get('ticket')}.",
                "ruta": "agente → límite de pasos → humano", "herramientas": traza}
    except Exception as e:  # noqa: BLE001 — red, proveedor, clave, cuota...
        registrar_evento("error_agente", error=f"{type(e).__name__}: {e}"[:500])
        return {"ok": False, "respuesta": errores["llm"], "ruta": f"agente → error del LLM ({type(e).__name__})",
                "herramientas": traza, "detalle_error": str(e)[:500]}

    respuesta = _ultima_respuesta(estado["messages"][len(mensajes):])

    # Validador de salida: si la respuesta trae un RMA o un enlace que ninguna herramienta emitió, se le devuelve
    # al agente con la corrección (un reintento); si insiste, se reemplaza por un mensaje seguro y se escala.
    problemas = validar_respuesta(respuesta)
    validacion = "sin observaciones"
    if problemas:
        registrar_evento("validador", accion="reintento", problemas=problemas)
        correccion = cargar_toml("agente")["validador"]["correccion"].replace("{problemas}", "; ".join(problemas))
        try:
            estado = obtener_agente().invoke({"messages": estado["messages"] + [HumanMessage(content=correccion)]},
                                             config={"recursion_limit": LIMITE_PASOS})
            respuesta = _ultima_respuesta(estado["messages"])
            problemas = validar_respuesta(respuesta)
        except Exception as e:  # noqa: BLE001 — si el reintento falla, se trata como no corregido
            problemas = problemas + [f"reintento fallido: {type(e).__name__}"]
        if problemas:
            ticket = observar(h.escalar_a_humano)(motivo="respuesta con datos de etiqueta sin respaldo",
                                                  resumen=f"Mensaje del cliente: {mensaje} | Problemas: {problemas}")
            registrar_evento("validador", accion="bloqueada", problemas=problemas)
            respuesta = errores["datos_sin_respaldo"] + f" Número de ticket: {ticket.get('ticket')}."
            validacion = "respuesta bloqueada"
        else:
            registrar_evento("validador", accion="corregida")
            validacion = "corregida tras un reintento"

    usadas = [t["herramienta"] for t in traza]
    nuevos = estado["messages"][len(mensajes):]
    modelos = list(dict.fromkeys(m.response_metadata.get("model_name", "?") for m in nuevos if isinstance(m, AIMessage)))
    registrar_evento("respuesta", herramientas=usadas, validacion=validacion, modelos=modelos,
                     resultados_tool=sum(isinstance(m, ToolMessage) for m in nuevos))
    ruta = "agente (respuesta directa)" if not usadas else "agente → " + " → ".join(usadas)
    if validacion != "sin observaciones":
        ruta += f" [validador: {validacion}]"
    principal = config_modelo()["modelo"]
    if any(m != principal for m in modelos):
        ruta += f" [modelo de respaldo: {', '.join(m for m in modelos if m != principal)}]"
    return {"ok": validacion != "respuesta bloqueada", "respuesta": respuesta, "ruta": ruta, "herramientas": traza,
            "validacion": validacion}


def _ultima_respuesta(mensajes: list) -> str:
    texto = next((m.content for m in reversed(mensajes) if isinstance(m, AIMessage) and m.content), "")
    return texto or cargar_toml("agente")["errores"]["llm"]
