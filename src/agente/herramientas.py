"""Herramientas del agente de devoluciones: funciones simuladas y deterministas.

Representan los sistemas externos de EcoMarket (base de pedidos, motor de reglas de devolución, API de la
transportadora y mesa de ayuda). Toda la lógica de negocio vive aquí, en Python, y no en el LLM: el modelo decide
QUÉ herramienta llamar y con qué datos, pero la decisión de si un producto es devolvible la toma este código.

Contrato común: cada función devuelve un dict.
- Éxito:  {"ok": True, ...datos}
- Fallo:  {"ok": False, "error": "CODIGO", "mensaje": "explicación legible"}
Así el agente siempre recibe algo interpretable, también cuando el proceso falla.
"""

import csv
import difflib
import hashlib
import json
import os
import re
import unicodedata
from datetime import date, datetime, timedelta
from pathlib import Path

from src.config import DIR_DATOS, FECHA_REFERENCIA

HOY = date.fromisoformat(FECHA_REFERENCIA)
PLAZO_DEVOLUCION_DIAS = 30           # Política §1
PLAZO_REPORTE_DANO_HORAS = 48        # Política §4
VIGENCIA_ETIQUETA_DIAS = 7

# Registro de etiquetas y tickets emitidos. Se puede cambiar en las pruebas.
DIR_REGISTROS = DIR_DATOS / "etiquetas"

PATRON_PEDIDO = re.compile(r"^ECO-\d{5}$")

MOTIVOS = {
    "retracto": "retracto",        # cambio de opinión, no le quedó, ya no lo quiere
    "cambio de opinion": "retracto",
    "danado": "danado",            # llegó roto o dañado en el transporte
    "dano": "danado",
    "roto": "danado",
    "defecto": "defecto",          # falla de fábrica
    "defectuoso": "defecto",
}

POR_QUE_NO_DEVOLVIBLE = {
    "Perecedero": "es un producto perecedero (alimentos y bebidas)",
    "Higiene personal": "es un producto de higiene personal",
    "Personalizado": "es un producto personalizado, fabricado o grabado a pedido",
}


# ---------- Utilidades ----------

def _error(codigo: str, mensaje: str, **extra) -> dict:
    return {"ok": False, "error": codigo, "mensaje": mensaje, **extra}


def _normalizar(texto: str) -> str:
    """Minúsculas y sin tildes, para comparar nombres de productos escritos por el cliente."""
    sin_tildes = unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", sin_tildes.lower()).strip()


def _palabras(texto: str) -> set[str]:
    return {p for p in re.findall(r"[a-z0-9]+", _normalizar(texto)) if len(p) > 3}


def emparejar_producto(producto: str, opciones: list[str]) -> str | None:
    """El nombre oficial que corresponde a lo que escribió el cliente ("la botella" → "Botella reutilizable…").

    Primero por palabras en común (el cliente suele nombrar el producto con una o dos palabras clave) y, si
    ninguna coincide, por parecido de caracteres (errores de digitación). None si no hay una coincidencia clara.
    """
    buscadas = _palabras(producto)
    if buscadas:
        puntajes = [(len(buscadas & _palabras(o)) / len(buscadas), o) for o in opciones]
        mejor, nombre = max(puntajes)
        empatados = [o for p, o in puntajes if p == mejor]
        if mejor >= 0.5 and len(empatados) == 1:
            return nombre
    normalizadas = {_normalizar(o): o for o in opciones}
    parecidos = difflib.get_close_matches(_normalizar(producto), list(normalizadas), n=1, cutoff=0.6)
    return normalizadas[parecidos[0]] if parecidos else None


def _pedidos() -> dict[str, dict]:
    with open(DIR_DATOS / "pedidos.json", encoding="utf-8") as archivo:
        return {p["numero_seguimiento"]: p for p in json.load(archivo)}


def _catalogo() -> dict[str, dict]:
    with open(DIR_DATOS / "inventario.csv", encoding="utf-8") as archivo:
        return {fila["nombre"]: fila for fila in csv.DictReader(archivo)}


def _normalizar_motivo(motivo: str) -> str | None:
    clave = _normalizar(motivo).replace("_", " ")
    return MOTIVOS.get(clave)


def _registro(nombre: str) -> tuple[Path, dict]:
    ruta = DIR_REGISTROS / nombre
    datos = json.loads(ruta.read_text(encoding="utf-8")) if ruta.exists() else {}
    return ruta, datos


def _guardar(ruta: Path, datos: dict) -> None:
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_text(json.dumps(datos, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------- Herramienta 1: consultar la base de pedidos ----------

def buscar_pedido(numero_seguimiento: str) -> dict:
    """Estado, productos y fecha de entrega de un pedido. No devuelve datos personales del titular."""
    numero = (numero_seguimiento or "").strip().upper()
    if not PATRON_PEDIDO.match(numero):
        return _error("FORMATO_INVALIDO",
                      f"'{numero_seguimiento}' no es un número de seguimiento válido. El formato es ECO-XXXXX (5 dígitos).")
    pedido = _pedidos().get(numero)
    if pedido is None:
        return _error("PEDIDO_NO_ENCONTRADO", f"No existe ningún pedido con el número {numero}.")
    return {
        "ok": True,
        "numero_seguimiento": numero,
        "estado": pedido["estado"],
        "productos": pedido["productos"],
        "fecha_entrega": pedido["fecha_entrega_real"],
        "transportadora": pedido["transportadora"],
    }


# ---------- Herramienta 2: motor de reglas de la política de devoluciones ----------

def verificar_elegibilidad_producto(numero_seguimiento: str, producto: str, motivo: str,
                                    tiene_foto: bool = False) -> dict:
    """Aplica la política de devoluciones (§1 plazo, §3 no devolvibles, §4 excepción por daño, §7 costo del envío)."""
    motivo_norm = _normalizar_motivo(motivo)
    if motivo_norm is None:
        return _error("MOTIVO_INVALIDO",
                      f"Motivo '{motivo}' no reconocido. Usa 'retracto' (cambio de opinión), 'danado' (llegó dañado) "
                      "o 'defecto' (falla de fábrica).")

    pedido = buscar_pedido(numero_seguimiento)
    if not pedido["ok"]:
        return pedido
    numero = pedido["numero_seguimiento"]
    if pedido["estado"] == "Devuelto":
        return _error("YA_DEVUELTO", f"El pedido {numero} ya fue devuelto; no admite otra devolución.")
    if pedido["estado"] != "Entregado":
        return _error("PEDIDO_NO_ENTREGADO",
                      f"El pedido {numero} está '{pedido['estado']}'. Solo se pueden devolver productos de pedidos entregados.",
                      estado=pedido["estado"])

    nombre = emparejar_producto(producto, pedido["productos"])
    if nombre is None:
        return _error("PRODUCTO_NO_EN_PEDIDO",
                      f"'{producto}' no corresponde a ningún producto del pedido {numero}.",
                      productos_del_pedido=pedido["productos"])
    item = _catalogo().get(nombre)
    if item is None:
        return _error("PRODUCTO_NO_EN_CATALOGO", f"'{nombre}' no está en el catálogo de EcoMarket.")

    devolvible = item["devolvible"] == "si"
    dias = (HOY - date.fromisoformat(pedido["fecha_entrega"])).days
    base = {
        "ok": True,
        "numero_seguimiento": numero,
        "producto": nombre,
        "categoria": item["categoria"],
        "devolvible_por_politica": devolvible,
        "motivo": motivo_norm,
        "fecha_entrega": pedido["fecha_entrega"],
        "dias_desde_entrega": dias,
    }

    def resultado(elegible: bool, regla: str, explicacion: str, **extra) -> dict:
        return {**base, "elegible": elegible, "regla_aplicada": regla, "explicacion": explicacion, **extra}

    if motivo_norm in ("danado", "defecto"):
        if dias * 24 <= PLAZO_REPORTE_DANO_HORAS:
            if not tiene_foto:
                return resultado(False, "§4 Excepción por daño o defecto",
                                 "Aplica la excepción por daño, pero el cliente debe adjuntar al menos una foto del daño.",
                                 requiere="foto")
            return resultado(True, "§4 Excepción por daño o defecto",
                             f"Reportado {dias} día(s) después de la entrega (dentro de 48 horas) y con foto.",
                             resolucion="reposición o reembolso completo", envio_a_cargo_de="EcoMarket")
        if motivo_norm == "defecto" and devolvible and dias <= PLAZO_DEVOLUCION_DIAS:
            return resultado(True, "§4 Defecto de fábrica en producto devolvible",
                             f"Defecto reportado a los {dias} días de la entrega (dentro de 30 días).",
                             resolucion="reposición o reembolso completo", envio_a_cargo_de="EcoMarket")
        return resultado(False, "§4 Excepción por daño o defecto",
                         f"El daño se reporta {dias} días después de la entrega; el plazo de la excepción es de 48 horas.",
                         alternativa="Si el producto es devolvible y está sin uso, puede solicitarse por retracto dentro de 30 días.")

    # Retracto (cambio de opinión)
    if not devolvible:
        porque = POR_QUE_NO_DEVOLVIBLE.get(item["categoria"], "no admite devolución")
        return resultado(False, "§3 Productos que no admiten devolución",
                         f"{nombre} {porque}; por salud, seguridad e inocuidad no se acepta su devolución por retracto.",
                         alternativa="Si el producto llegó dañado o con defecto, aplica la excepción del §4 (48 horas y foto).")
    if dias > PLAZO_DEVOLUCION_DIAS:
        return resultado(False, "§1 Plazo de 30 días",
                         f"Entregado hace {dias} días; el plazo para devolver es de {PLAZO_DEVOLUCION_DIAS} días calendario.",
                         alternativa="Si el producto tiene un defecto de fábrica, el caso se puede revisar con un agente humano.")
    return resultado(True, "§1 Plazo y §2 Condiciones",
                     f"Entregado hace {dias} días (dentro de 30). El producto debe estar sin uso y en su empaque original.",
                     resolucion="reembolso al medio de pago original, cambio o saldo a favor",
                     envio_a_cargo_de="cliente (se descuenta del reembolso, §7)")


# ---------- Herramienta 3: API de la transportadora ----------

def _transportadora_caida(nombre: str) -> bool:
    """Falla simulada para demostrar el manejo de errores: SIMULAR_FALLA_TRANSPORTADORA=EnvíaVerde (o 'todas')."""
    valor = os.getenv("SIMULAR_FALLA_TRANSPORTADORA", "")
    caidas = {_normalizar(v) for v in valor.split(",") if v.strip()}
    return "todas" in caidas or _normalizar(nombre) in caidas


def generar_etiqueta_devolucion(numero_seguimiento: str, producto: str, motivo: str,
                                tiene_foto: bool = False) -> dict:
    """Emite la guía prepagada de devolución. Vuelve a verificar la elegibilidad: el agente no se la puede saltar."""
    verificacion = verificar_elegibilidad_producto(numero_seguimiento, producto, motivo, tiene_foto)
    if not verificacion["ok"]:
        return verificacion
    if not verificacion["elegible"]:
        return _error("NO_ELEGIBLE", f"No se puede generar la etiqueta: {verificacion['explicacion']}",
                      regla_aplicada=verificacion["regla_aplicada"])

    numero, nombre = verificacion["numero_seguimiento"], verificacion["producto"]
    clave = f"{numero}::{nombre}"
    ruta, etiquetas = _registro("etiquetas.json")
    if clave in etiquetas:
        return {**etiquetas[clave], "ok": True, "ya_existia": True}

    transportadora = _pedidos()[numero]["transportadora"]
    if _transportadora_caida(transportadora):
        return _error("TRANSPORTADORA_NO_DISPONIBLE",
                      f"El servicio de {transportadora} no responde en este momento; no se generó ninguna etiqueta.",
                      reintentar=True)

    huella = hashlib.sha1(clave.encode("utf-8")).hexdigest().upper()
    rma = f"RMA-{huella[:8]}"
    etiqueta = {
        "ok": True,
        "rma": rma,
        "numero_seguimiento": numero,
        "producto": nombre,
        "motivo": verificacion["motivo"],
        "transportadora": transportadora,
        "numero_guia": f"{_normalizar(transportadora)[:2].upper()}{int(huella[8:16], 16) % 10**10:010d}",
        "url_etiqueta": f"https://etiquetas.ecomarket.example/{rma}.pdf",
        "costo_envio_a_cargo_de": verificacion["envio_a_cargo_de"],
        "resolucion": verificacion["resolucion"],
        "valida_hasta": (HOY + timedelta(days=VIGENCIA_ETIQUETA_DIAS)).isoformat(),
        "instrucciones": [
            "Empaca el producto en su empaque original con todos sus accesorios.",
            "Imprime la etiqueta y pégala en el paquete.",
            f"Entrega el paquete en un punto de {transportadora} antes de la fecha de vencimiento.",
            "Cuando EcoMarket reciba y revise el producto, te confirmará por correo.",
        ],
        "ya_existia": False,
    }
    etiquetas[clave] = {k: v for k, v in etiqueta.items() if k not in ("ok", "ya_existia")}
    _guardar(ruta, etiquetas)
    return etiqueta


def etiquetas_emitidas() -> dict[str, dict]:
    """Etiquetas que de verdad existen, por RMA. Lo usa el validador de respuestas del agente (agente.py)."""
    _, etiquetas = _registro("etiquetas.json")
    return {e["rma"]: e for e in etiquetas.values()}


# ---------- Herramienta 4: mesa de ayuda (escalamiento a un humano) ----------

def escalar_a_humano(motivo: str, resumen: str, numero_seguimiento: str | None = None) -> dict:
    """Abre un ticket para un agente humano con el resumen del caso, para que el cliente no repita la historia."""
    ruta, tickets = _registro("tickets.json")
    ticket = f"TCK-{len(tickets) + 1:04d}"
    tickets[ticket] = {
        "motivo": motivo,
        "resumen": resumen,
        "numero_seguimiento": numero_seguimiento,
        "creado": datetime.now().isoformat(timespec="seconds"),
    }
    _guardar(ruta, tickets)
    return {"ok": True, "ticket": ticket, "tiempo_respuesta": "máximo 2 días hábiles (política §5)"}


# ---------- Herramienta 5: la base de conocimiento del Taller 2 (RAG) ----------

def consultar_politica(pregunta: str) -> dict:
    """Fragmentos de las políticas y la FAQ (RAG del Taller 2) que responden una pregunta, con su fuente."""
    from src import rag   # import diferido: carga el modelo de embeddings solo si se usa

    # k = RAG_K (4), igual que la cadena RAG del Taller 2. Con k = 3, la pregunta compuesta del caso A15
    # ("¿cuánto tiempo tengo y en cuánto me devuelven la plata?") dejaba fuera "6. Reembolso" y el modelo
    # completó el plazo de memoria (docs/fase2_implementacion.md §5).
    pares = rag.recuperar(pregunta)
    if not pares:
        return _error("SIN_EVIDENCIA", "La base de conocimiento no tiene información sobre esa pregunta.")
    return {
        "ok": True,
        "fragmentos": [
            {"fuente": f"{d.metadata['fuente']} › {d.metadata['seccion']}", "similitud": round(s, 3),
             "texto": d.page_content}
            for d, s in pares
        ],
    }
