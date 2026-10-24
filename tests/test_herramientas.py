"""Pruebas sin LLM: herramientas del agente (reglas de la política, errores, idempotencia) y validador de salida.

Fecha de referencia: 2026-09-20 (src/config.py). Ejecutar con: pytest
"""

import pytest

from src.agente import herramientas as h


@pytest.fixture(autouse=True)
def registros_temporales(tmp_path, monkeypatch):
    """Cada prueba escribe etiquetas, tickets y registro de acciones en una carpeta temporal, nunca en data/ ni
    logs/, y no necesita clave: las variables del LLM son ficticias porque ninguna prueba llama al modelo."""
    from src.agente import observabilidad

    monkeypatch.setattr(h, "DIR_REGISTROS", tmp_path)
    monkeypatch.setattr(observabilidad, "ARCHIVO_LOG", tmp_path / "acciones.jsonl")
    monkeypatch.delenv("SIMULAR_FALLA_TRANSPORTADORA", raising=False)
    for variable, valor in (("LLM_BASE_URL", "http://localhost"), ("LLM_API_KEY", "prueba"), ("LLM_MODEL", "prueba")):
        monkeypatch.setenv(variable, valor)
    monkeypatch.delenv("AGENTE_MODEL", raising=False)


# ---------- buscar_pedido ----------

def test_buscar_pedido_existente_sin_datos_personales():
    r = h.buscar_pedido("eco-10001")
    assert r["ok"] and r["numero_seguimiento"] == "ECO-10001" and r["estado"] == "Entregado"
    assert "cliente" not in r


@pytest.mark.parametrize("numero, codigo", [("ECO-99999", "PEDIDO_NO_ENCONTRADO"), ("10001", "FORMATO_INVALIDO"),
                                            ("", "FORMATO_INVALIDO")])
def test_buscar_pedido_errores(numero, codigo):
    assert h.buscar_pedido(numero)["error"] == codigo


# ---------- emparejar_producto ----------

@pytest.mark.parametrize("escrito, oficial", [
    ("la botella", "Botella reutilizable de acero inoxidable 750 ml"),
    ("cafe", "Café orgánico de origen colombiano 500 g"),
    ("termo", "Termo grabado con nombre"),
    ("cepillo de dientes", "Cepillo de dientes de bambú"),
])
def test_emparejar_producto(escrito, oficial):
    opciones = h._pedidos()["ECO-10001"]["productos"] + h._pedidos()["ECO-10003"]["productos"] \
        + h._pedidos()["ECO-10004"]["productos"]
    assert h.emparejar_producto(escrito, opciones) == oficial


def test_emparejar_producto_inexistente():
    assert h.emparejar_producto("cargador solar", h._pedidos()["ECO-10001"]["productos"]) is None


# ---------- verificar_elegibilidad_producto: casos D1–D8 del Taller 1 ----------

def test_d1_devolvible_dentro_de_plazo():
    r = h.verificar_elegibilidad_producto("ECO-10001", "botella", "retracto")
    assert r["elegible"] and r["dias_desde_entrega"] == 10 and "cliente" in r["envio_a_cargo_de"]


def test_d2_fuera_de_plazo():
    r = h.verificar_elegibilidad_producto("ECO-10002", "camiseta", "retracto")
    assert not r["elegible"] and r["regla_aplicada"].startswith("§1") and r["dias_desde_entrega"] == 47


@pytest.mark.parametrize("pedido, producto, categoria", [
    ("ECO-10001", "snacks", "Perecedero"),
    ("ECO-10004", "cepillo de dientes", "Higiene personal"),
    ("ECO-10004", "termo grabado", "Personalizado"),
])
def test_d3_d4_d6_no_devolvibles(pedido, producto, categoria):
    r = h.verificar_elegibilidad_producto(pedido, producto, "retracto")
    assert not r["elegible"] and r["regla_aplicada"].startswith("§3") and r["categoria"] == categoria


def test_d5_perecedero_danado_con_foto_dentro_de_48_horas():
    r = h.verificar_elegibilidad_producto("ECO-10003", "café", "dañado", tiene_foto=True)
    assert r["elegible"] and r["envio_a_cargo_de"] == "EcoMarket"


def test_d5_sin_foto_pide_la_foto():
    r = h.verificar_elegibilidad_producto("ECO-10003", "café", "dañado", tiene_foto=False)
    assert not r["elegible"] and r["requiere"] == "foto"


def test_d8_defecto_de_fabrica_en_devolvible():
    r = h.verificar_elegibilidad_producto("ECO-10001", "botella", "defecto")
    assert r["elegible"] and r["regla_aplicada"].startswith("§4")


def test_danado_fuera_de_48_horas():
    r = h.verificar_elegibilidad_producto("ECO-10004", "jabón", "danado", tiene_foto=True)
    assert not r["elegible"]


@pytest.mark.parametrize("pedido, producto, motivo, codigo", [
    ("ECO-10005", "mochila", "retracto", "PEDIDO_NO_ENTREGADO"),
    ("ECO-10015", "loncheras", "retracto", "YA_DEVUELTO"),
    ("ECO-10001", "cargador solar", "retracto", "PRODUCTO_NO_EN_PEDIDO"),
    ("ECO-99999", "botella", "retracto", "PEDIDO_NO_ENCONTRADO"),
    ("ECO-10001", "botella", "porque sí", "MOTIVO_INVALIDO"),
])
def test_elegibilidad_errores(pedido, producto, motivo, codigo):
    r = h.verificar_elegibilidad_producto(pedido, producto, motivo)
    assert not r["ok"] and r["error"] == codigo


# ---------- generar_etiqueta_devolucion ----------

def test_etiqueta_elegible_e_idempotente():
    primera = h.generar_etiqueta_devolucion("ECO-10001", "botella", "retracto")
    assert primera["ok"] and primera["rma"].startswith("RMA-") and not primera["ya_existia"]
    assert primera["valida_hasta"] == "2026-09-27"
    segunda = h.generar_etiqueta_devolucion("ECO-10001", "Botella reutilizable de acero inoxidable 750 ml", "retracto")
    assert segunda["ya_existia"] and segunda["rma"] == primera["rma"]


def test_etiqueta_no_elegible_aunque_el_agente_la_pida():
    """Defensa ante prompt injection: la herramienta vuelve a aplicar la política por su cuenta."""
    r = h.generar_etiqueta_devolucion("ECO-10004", "termo", "retracto")
    assert not r["ok"] and r["error"] == "NO_ELEGIBLE"


def test_etiqueta_falla_de_transportadora(monkeypatch):
    monkeypatch.setenv("SIMULAR_FALLA_TRANSPORTADORA", "EnvíaVerde")
    r = h.generar_etiqueta_devolucion("ECO-10001", "botella", "retracto")
    assert not r["ok"] and r["error"] == "TRANSPORTADORA_NO_DISPONIBLE" and r["reintentar"]
    assert not (h.DIR_REGISTROS / "etiquetas.json").exists()


# ---------- escalar_a_humano ----------

def test_escalar_crea_tickets_consecutivos():
    assert h.escalar_a_humano("cliente molesto", "resumen")["ticket"] == "TCK-0001"
    assert h.escalar_a_humano("otro", "resumen")["ticket"] == "TCK-0002"


# ---------- Validador de salida del agente (src/agente/agente.py) ----------

# Respuesta real del agente en una prueba de la interfaz (docs/img/hallazgo_etiqueta_inventada.png): verificó la
# elegibilidad, NO llamó a generar_etiqueta_devolucion e inventó todos los datos de la etiqueta.
RESPUESTA_INVENTADA = """¡Claro! Tu botella reutilizable está dentro del plazo y cumple con las condiciones.
- RMA: **ECO-RMA-10001-B1**
- Transportadora: EcoEnvios
- Número de guía: **ECOV-123456789**
- Enlace de la etiqueta: https://eco-market.com/etiqueta/ECO-RMA-10001-B1
- Fecha límite para la entrega: **2026-10-20**"""


def test_validador_detecta_etiqueta_inventada():
    from src.agente.agente import validar_respuesta

    problemas = validar_respuesta(RESPUESTA_INVENTADA)
    assert any("ECO-RMA-10001-B1" in p for p in problemas)
    assert any("eco-market.com" in p for p in problemas)


def test_validador_acepta_etiqueta_real():
    from src.agente.agente import validar_respuesta

    e = h.generar_etiqueta_devolucion("ECO-10001", "botella", "retracto")
    texto = (f"- **RMA:** {e['rma']}\n- **Número de guía:** {e['numero_guia']}\n"
             f"- **Enlace:** {e['url_etiqueta']}\nRastreo: https://rastreo.ecomarket.example/ECO-10001.")
    assert validar_respuesta(texto) == []


def test_validador_no_marca_textos_sin_datos_de_etiqueta():
    from src.agente.agente import validar_respuesta

    assert validar_respuesta("No se generó ninguna etiqueta (RMA) porque el termo no admite devolución.") == []


class _AgenteFalso:
    """Simula el grafo del agente: devuelve, en orden, las respuestas dadas (sin LLM)."""

    def __init__(self, *respuestas):
        self.respuestas = list(respuestas)

    def invoke(self, entrada, config=None):
        from langchain_core.messages import AIMessage

        return {"messages": entrada["messages"] + [AIMessage(content=self.respuestas.pop(0))]}


def test_responder_corrige_dato_inventado_en_el_reintento(monkeypatch):
    from src.agente import agente

    e = h.generar_etiqueta_devolucion("ECO-10001", "botella", "retracto")
    falso = _AgenteFalso(RESPUESTA_INVENTADA, f"Tu etiqueta es {e['rma']}: {e['url_etiqueta']}")
    monkeypatch.setattr(agente, "obtener_agente", lambda: falso)
    r = agente.responder("Quiero devolver la botella del pedido ECO-10001")
    assert r["ok"] and r["validacion"] == "corregida tras un reintento" and e["rma"] in r["respuesta"]


def test_responder_bloquea_si_el_agente_insiste(monkeypatch):
    from src.agente import agente

    monkeypatch.setattr(agente, "obtener_agente", lambda: _AgenteFalso(RESPUESTA_INVENTADA, RESPUESTA_INVENTADA))
    r = agente.responder("Quiero devolver la botella del pedido ECO-10001")
    assert not r["ok"] and r["validacion"] == "respuesta bloqueada"
    assert "ECO-RMA-10001-B1" not in r["respuesta"] and "TCK-" in r["respuesta"]
