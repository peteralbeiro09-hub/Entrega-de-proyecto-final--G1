"""Router de EcoMarket: clasificar → enrutar → responder (datos exactos, RAG o agente) o escalar.

Proyecto Final: las devoluciones ya no se responden con un prompt, las atiende el agente (src/agente/), que
verifica la elegibilidad y genera la etiqueta con herramientas. El resto de rutas no cambia:
- La ruta "devolucion" va al agente aunque el sentimiento sea negativo: el agente puede resolver (reposición por
  defecto) y, si hace falta, escalar con un ticket. Las quejas siguen yendo directo a un humano.
- Ruta "pegajosa": si el agente le pidió un dato al cliente (le falta el pedido, el producto o el motivo), el
  siguiente mensaje vuelve al agente sin clasificar; "ECO-10001" a secas no se entendería fuera de contexto.
- `responder_devolucion_v4` (Taller 2) se conserva como línea base para comparar en la evaluación.

Cambios del Taller 2 respecto al Taller 1:
- Nueva intención "informacion" (y también "otro"): se responde con la cadena RAG (src/rag.py). Antes se
  escalaba todo a un humano; ahora el asistente responde si la base de conocimiento lo permite y, si no,
  dice con honestidad que no tiene la información.
- Devoluciones: el catálogo ya no se pega completo en el prompt v4; se recuperan los productos más parecidos al
  mensaje. La política se recupera completa por metadatos (ver contexto_devolucion).
- Pedidos: se pasan al prompt solo los pedidos cuyo número menciona el cliente (búsqueda exacta, sin RAG).
"""

from src import contexto, prompts, rag
from src.agente import agente
from src.config import cargar_toml
from src.llm import completar

INTENCIONES = {"pedido", "devolucion", "informacion", "queja", "otro"}

# Cuántos productos se recuperan para armar el contexto del prompt v4 de devoluciones.
K_CATALOGO = 3


def clasificar(mensaje: str) -> dict | None:
    """Pide al modelo la clasificación en JSON. Devuelve None si la respuesta no es interpretable."""
    config = cargar_toml("cadena")["clasificador"]
    mensajes = [
        {"role": "system", "content": config["system"]},
        {"role": "user", "content": config["user"].replace("{consulta}", mensaje)},
    ]
    clasificacion = prompts.extraer_json(completar(mensajes))
    if not clasificacion or clasificacion.get("intencion") not in INTENCIONES:
        return None
    return clasificacion


def _escalar(mensaje: str, clasificacion: dict | None, motivo: str) -> dict:
    datos = clasificacion or {}
    resumen = (
        f"Motivo del escalamiento: {motivo}\n"
        f"Intención: {datos.get('intencion', 'desconocida')} | "
        f"Sentimiento: {datos.get('sentimiento', 'desconocido')} | "
        f"Pedido: {datos.get('numero_seguimiento') or 'no indicado'} | "
        f"Producto: {datos.get('producto') or 'no indicado'}\n"
        f"Mensaje original: {mensaje}"
    )
    return {
        "ruta": f"humano ({motivo})",
        "clasificacion": clasificacion,
        "respuesta": cargar_toml("cadena")["escalamiento"]["cliente"],
        "resumen_agente": resumen,
        "fuentes": [],
    }


def contexto_devolucion(mensaje: str) -> tuple[dict, list[str]]:
    """Arma {politica, catalogo, pedidos} para el prompt v4 de devoluciones (recuperación híbrida).

    - Catálogo: búsqueda por similitud entre los productos (filtro tipo=producto, umbral 0 porque el
      clasificador ya decidió que es una devolución). Es la parte que crece con el negocio y la que de verdad
      hay que buscar; el producto correcto sale primero en todos los casos de prueba.
    - Política: se recuperan sus 7 secciones completas por metadatos, no por similitud. Medido en
      `python app.py experimento-recuperacion` §4: buscando las 4 secciones más parecidas solo llegaban las
      reglas necesarias en 2 de 8 casos, porque "quiero devolver el cepillo de dientes" no se parece a "Higiene
      personal: no se aceptan devoluciones" (hay que pasar por la categoría del producto). La política es corta
      (~600 tokens) y sus reglas dependen unas de otras (plazo, excepción por daño, no devolvibles).
    """
    politica = rag.fragmentos_de({"fuente": "politica_devoluciones.pdf"})
    catalogo = rag.recuperar(mensaje, k=K_CATALOGO, umbral=0, filtro={"tipo": "producto"})
    valores = {
        "politica": rag.formatear_contexto(politica),
        "catalogo": rag.formatear_contexto([d for d, _ in catalogo]),
        "pedidos": contexto.pedidos_texto(contexto.numeros_pedido(mensaje)),
    }
    fuentes = [f"politica_devoluciones.pdf completa ({len(politica)} secciones, por metadatos)"]
    fuentes += [f"{d.metadata['fuente']} › {d.metadata['seccion']} (sim. {s:.3f})" for d, s in catalogo]
    return valores, fuentes


def responder_devolucion_v4(mensaje: str) -> dict:
    """Ejecuta devoluciones v4 con contexto recuperado. El razonamiento se guarda para auditoría, nunca se muestra."""
    valores, fuentes = contexto_devolucion(mensaje)
    crudo = completar(prompts.construir_mensajes("devoluciones", "v4", mensaje, valores))
    datos = prompts.extraer_json(crudo)
    if not datos or not datos.get("respuesta_cliente"):
        return {"ok": False, "crudo": crudo, "fuentes": fuentes}
    return {
        "ok": True,
        "decision": datos.get("decision"),
        "razonamiento": datos.get("razonamiento"),
        "respuesta_cliente": datos["respuesta_cliente"],
        "fuentes": fuentes,
    }


def responder_pedido_v4(mensaje: str) -> dict:
    """Pedidos v4 del Taller 1 con búsqueda exacta: el modelo solo ve los pedidos que el cliente menciona."""
    valores = {"pedidos": contexto.pedidos_texto(contexto.numeros_pedido(mensaje))}
    respuesta = completar(prompts.construir_mensajes("pedidos", "v4", mensaje, valores))
    return {"respuesta": respuesta, "fuentes": ["pedidos.json (búsqueda exacta por número)"]}


def _con_agente(mensaje: str, historial: list[dict] | None, sesion: str, clasificacion: dict | None) -> dict:
    r = agente.responder(mensaje, historial, sesion)
    # El agente espera un dato del cliente si terminó con una pregunta o si respondió sin usar herramientas (lo que
    # hace cuando pide el pedido, el producto o el motivo, aunque no siempre lo redacte con "?"): el próximo
    # mensaje vuelve a él sin reclasificar.
    esperando = r["respuesta"].rstrip().endswith("?") or (r["ok"] and not r["herramientas"])
    return {"clasificacion": clasificacion, "fuentes": [], "continuar_con_agente": esperando, **r}


def responder(mensaje: str, historial: list[dict] | None = None, sesion: str = "cli",
              continuar_con_agente: bool = False) -> dict:
    """Clasifica, enruta y responde. Ante cualquier duda, escala a un agente humano.

    `historial` son los mensajes anteriores ({role, content}) y `continuar_con_agente` indica que el turno
    anterior fue del agente y quedó esperando un dato (ver "ruta pegajosa" arriba)."""
    if continuar_con_agente:
        return _con_agente(mensaje, historial, sesion, None)

    clasificacion = clasificar(mensaje)
    if clasificacion is None:
        return _escalar(mensaje, None, "clasificación no interpretable")

    intencion = clasificacion["intencion"]
    if intencion == "devolucion":
        return _con_agente(mensaje, historial, sesion, clasificacion)
    if clasificacion.get("sentimiento") == "negativo":
        return _escalar(mensaje, clasificacion, "sentimiento negativo")
    if intencion == "queja":
        return _escalar(mensaje, clasificacion, "intención queja")

    if intencion == "pedido":
        r = responder_pedido_v4(mensaje)
        return {"ruta": "pedidos v4", "clasificacion": clasificacion, **r}

    # informacion u otro: la base de conocimiento decide si hay respuesta.
    r = rag.responder(mensaje)
    return {"clasificacion": clasificacion, **r}
