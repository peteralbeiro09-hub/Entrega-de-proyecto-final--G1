"""Registro de acciones del agente: cada llamada a una herramienta queda en logs/acciones.jsonl.

Una línea JSON por evento (hora, sesión, herramienta, argumentos, resultado, latencia), con dos usos:
- Auditoría: qué hizo el agente, con qué datos y qué le respondieron los sistemas.
- Métricas y alertas: `python app.py monitoreo` resume el archivo (docs/fase3_analisis_critico.md §3).

La traza del turno actual también se devuelve a la interfaz para mostrar "qué herramientas usó el agente".
"""

import contextvars
import functools
import json
import time
from datetime import datetime

from src.config import RAIZ

ARCHIVO_LOG = RAIZ / "logs" / "acciones.jsonl"

_sesion = contextvars.ContextVar("sesion", default="cli")
_traza = contextvars.ContextVar("traza", default=None)


def iniciar_turno(sesion: str) -> list[dict]:
    """Marca el inicio de un mensaje del cliente; devuelve la lista donde se acumulan las llamadas del turno."""
    traza: list[dict] = []
    _sesion.set(sesion)
    _traza.set(traza)
    return traza


def registrar_evento(tipo: str, **datos) -> None:
    """Escribe un evento en el log (también los del agente: respuesta final, error, escalamiento)."""
    evento = {"hora": datetime.now().isoformat(timespec="seconds"), "sesion": _sesion.get(), "tipo": tipo, **datos}
    ARCHIVO_LOG.parent.mkdir(exist_ok=True)
    with open(ARCHIVO_LOG, "a", encoding="utf-8") as archivo:
        archivo.write(json.dumps(evento, ensure_ascii=False) + "\n")


def observar(funcion):
    """Envuelve una herramienta: mide la latencia, registra argumentos y resultado, y convierte excepciones
    inesperadas en un resultado {"ok": False}, para que un fallo de un sistema nunca rompa la conversación."""

    @functools.wraps(funcion)
    def envoltura(**argumentos):
        inicio = time.perf_counter()
        try:
            resultado = funcion(**argumentos)
        except Exception as e:  # noqa: BLE001 — cualquier fallo del sistema simulado se informa al agente
            resultado = {"ok": False, "error": "ERROR_INTERNO", "mensaje": f"{type(e).__name__}: {e}"}
        latencia_ms = round((time.perf_counter() - inicio) * 1000)
        llamada = {"herramienta": funcion.__name__, "argumentos": argumentos, "resultado": resultado,
                   "latencia_ms": latencia_ms}
        registrar_evento("herramienta", **llamada)
        traza = _traza.get()
        if traza is not None:
            traza.append(llamada)
        return resultado

    return envoltura


def leer_log() -> list[dict]:
    if not ARCHIVO_LOG.exists():
        return []
    with open(ARCHIVO_LOG, encoding="utf-8") as archivo:
        return [json.loads(linea) for linea in archivo if linea.strip()]


# Umbrales de alerta (docs/fase3_analisis_critico.md §3). En producción vivirían en la configuración del monitoreo.
ALERTA_NO_ELEGIBLES_POR_SESION = 3     # insistencia: posible intento de forzar una devolución
ALERTA_ETIQUETAS_POR_SESION = 3        # muchas etiquetas en una conversación: posible abuso o bucle
ALERTA_FALLAS_TRANSPORTADORA = 3       # integración caída
ALERTA_TASA_ERROR_AGENTE = 0.10        # el LLM falla en más del 10 % de los turnos


def resumen_monitoreo(eventos: list[dict] | None = None) -> dict:
    """Métricas y alertas calculadas a partir del registro de acciones."""
    eventos = leer_log() if eventos is None else eventos
    llamadas = [e for e in eventos if e["tipo"] == "herramienta"]
    turnos = [e for e in eventos if e["tipo"] in ("respuesta", "error_agente")]
    errores_agente = [e for e in eventos if e["tipo"] == "error_agente"]

    por_herramienta: dict[str, dict] = {}
    for e in llamadas:
        m = por_herramienta.setdefault(e["herramienta"], {"llamadas": 0, "errores": {}, "latencia_ms": []})
        m["llamadas"] += 1
        m["latencia_ms"].append(e["latencia_ms"])
        if not e["resultado"].get("ok"):
            codigo = e["resultado"].get("error", "?")
            m["errores"][codigo] = m["errores"].get(codigo, 0) + 1
    for m in por_herramienta.values():
        m["latencia_ms"] = round(sum(m["latencia_ms"]) / len(m["latencia_ms"]))

    def por_sesion(condicion) -> dict[str, int]:
        conteo: dict[str, int] = {}
        for e in llamadas:
            if condicion(e):
                conteo[e["sesion"]] = conteo.get(e["sesion"], 0) + 1
        return conteo

    etiquetas = por_sesion(lambda e: e["herramienta"] == "generar_etiqueta_devolucion" and e["resultado"].get("ok")
                           and not e["resultado"].get("ya_existia"))
    no_elegibles = por_sesion(lambda e: e["herramienta"] == "verificar_elegibilidad_producto"
                              and e["resultado"].get("elegible") is False)
    fallas_transp = sum(1 for e in llamadas if e["resultado"].get("error") == "TRANSPORTADORA_NO_DISPONIBLE")

    alertas = []
    for sesion, n in no_elegibles.items():
        if n >= ALERTA_NO_ELEGIBLES_POR_SESION:
            alertas.append(f"Sesión {sesion}: {n} verificaciones no elegibles (posible intento de forzar una devolución).")
    for sesion, n in etiquetas.items():
        if n >= ALERTA_ETIQUETAS_POR_SESION:
            alertas.append(f"Sesión {sesion}: {n} etiquetas emitidas (revisar posible abuso).")
    if fallas_transp >= ALERTA_FALLAS_TRANSPORTADORA:
        alertas.append(f"{fallas_transp} fallas de la transportadora: revisar la integración.")
    if turnos and len(errores_agente) / len(turnos) > ALERTA_TASA_ERROR_AGENTE:
        alertas.append(f"El agente falló en {len(errores_agente)} de {len(turnos)} turnos (umbral "
                       f"{ALERTA_TASA_ERROR_AGENTE:.0%}).")

    validador = [e for e in eventos if e["tipo"] == "validador"]
    bloqueadas = sum(1 for e in validador if e.get("accion") == "bloqueada")
    if bloqueadas:
        alertas.append(f"{bloqueadas} respuesta(s) del agente bloqueadas por datos de etiqueta sin respaldo: "
                       "revisar el prompt o el modelo.")

    return {
        "turnos": len(turnos),
        "respuestas_corregidas": sum(1 for e in validador if e.get("accion") == "corregida"),
        "respuestas_bloqueadas": bloqueadas,
        "errores_agente": len(errores_agente),
        "llamadas_herramientas": len(llamadas),
        "etiquetas_emitidas": sum(etiquetas.values()),
        "no_elegibles": sum(no_elegibles.values()),
        "escalamientos": sum(1 for e in llamadas if e["herramienta"] == "escalar_a_humano"),
        "por_herramienta": por_herramienta,
        "alertas": alertas,
    }
