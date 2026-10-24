"""CLI del asistente de EcoMarket: RAG (Taller 2) + agente de devoluciones (Proyecto Final).

Uso:
    python app.py indexar [--estrategia estructura|parrafos|fijo]     Base de conocimiento (Taller 2)
    python app.py buscar "<texto>" [--k 4]                            Solo recuperación, sin LLM
    python app.py preguntar "<texto>" [--version v1|v2] [--sin-rag]   Cadena RAG
    python app.py agente "<texto>"                                    Solo el agente de devoluciones
    python app.py chat                                                Sistema completo (router + agente), con historial
    python app.py evaluar-agente [--casos A1,A14]                     Casos de data/casos_agente.json → results/
    python app.py monitoreo                                           Métricas y alertas de logs/acciones.jsonl
    python app.py probar

La interfaz web está en app_web.py: streamlit run app_web.py
"""

import argparse
import json
import os
import re
import shutil
import sys
from datetime import datetime

from src.config import (
    CHUNK_TAMANO, DIR_DATOS, DIR_RESULTADOS, FECHA_REFERENCIA, MODELO_EMBEDDINGS, RAG_K, RAG_UMBRAL,
    ErrorConfiguracion,
)
from src.llm import ErrorLLM, completar, nombre_modelo


def cmd_probar(_args):
    print(f"Modelo: {nombre_modelo()}")
    print(completar([{"role": "user", "content": "Hola"}]))


# ---------- Fase 2: base de conocimiento ----------

def cmd_indexar(args):
    from src.indice import construir_indice
    from src.ingesta import fragmentar

    fragmentos = fragmentar(args.estrategia)
    print(f"Modelo de embeddings: {MODELO_EMBEDDINGS} (la primera vez se descarga, ~470 MB)", file=sys.stderr)
    construir_indice(fragmentos)
    conteo: dict[str, int] = {}
    for f in fragmentos:
        conteo[f.metadata["fuente"]] = conteo.get(f.metadata["fuente"], 0) + 1
    print(f"Índice creado en chroma_db/ con {len(fragmentos)} fragmentos (estrategia: {args.estrategia}):")
    for fuente, n in conteo.items():
        print(f"  {fuente:<28} {n:>3} fragmentos")


def cmd_fragmentos(args):
    from src.ingesta import fragmentar

    for f in fragmentar(args.estrategia):
        if args.fuente and f.metadata["fuente"] != args.fuente:
            continue
        print(f"--- {f.metadata['id']} | {f.metadata['seccion']} | {len(f.page_content)} caracteres ---")
        print(f.page_content, "\n")


def cmd_buscar(args):
    """Solo recuperación (embeddings + Chroma), sin LLM: gratis y sirve para calibrar k y el umbral."""
    from src import rag

    for i, (doc, score) in enumerate(rag.recuperar(args.texto, k=args.k, umbral=0), start=1):
        marca = "✔" if score >= RAG_UMBRAL else "✘ bajo el umbral"
        print(f"[{i}] sim={score:.3f} {marca}  {doc.metadata['fuente']} › {doc.metadata['seccion']}")
        print("    " + doc.page_content.replace("\n", "\n    ")[:400], "\n")


# ---------- Fase 3: preguntar y conversar ----------

def _imprimir(resultado: dict):
    print(resultado["respuesta"])
    print(f"\n   [ruta: {resultado['ruta']}]")
    for fuente in resultado.get("fuentes", []):
        print(f"   {fuente}")
    for llamada in resultado.get("herramientas", []):
        print(f"   🔧 {_resumen_llamada(llamada)}")


def _resumen_llamada(llamada: dict) -> str:
    """Una línea por llamada a herramienta: nombre(argumentos) → resultado clave."""
    args = ", ".join(f"{k}={v!r}" for k, v in llamada["argumentos"].items())
    r = llamada["resultado"]
    if not r.get("ok"):
        salida = f"error {r.get('error')}"
    elif "elegible" in r:
        salida = f"elegible={r['elegible']} ({r['regla_aplicada']})"
    elif "rma" in r:
        salida = f"{r['rma']} · guía {r['numero_guia']}" + (" (ya existía)" if r.get("ya_existia") else "")
    elif "ticket" in r:
        salida = f"ticket {r['ticket']}"
    elif "fragmentos" in r:
        salida = "; ".join(f["fuente"] for f in r["fragmentos"])
    else:
        salida = f"estado {r.get('estado')}"
    return f"{llamada['herramienta']}({args}) → {salida}"


def cmd_preguntar(args):
    from src import prompts, rag

    if args.mostrar_prompt:
        if args.sin_rag:
            mensajes = prompts.construir_mensajes("rag", "sin_rag", args.texto)
        else:
            pares = rag.recuperar(args.texto, args.k, args.umbral)
            mensajes = prompts.construir_mensajes("rag", args.version, args.texto,
                                                  {"contexto": rag.formatear_contexto([d for d, _ in pares])})
        for m in mensajes:
            print(f"--- [{m['role']}] ---\n{m['content']}\n")
        return
    if args.sin_rag:
        _imprimir(rag.responder_sin_rag(args.texto))
    else:
        _imprimir(rag.responder(args.texto, args.version, args.k, args.umbral))


def cmd_agente(args):
    from src.agente import agente

    _imprimir(agente.responder(args.texto))


def cmd_chat(_args):
    from src import cadena

    print(f"Asistente virtual de EcoMarket: RAG + agente de devoluciones ({nombre_modelo()}). "
          "Escribe 'salir' para terminar.\n")
    historial: list[dict] = []
    continuar = False
    sesion = f"cli-{datetime.now():%H%M%S}"
    while True:
        try:
            mensaje = input("Tú: ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if mensaje.lower() in ("salir", "exit", "quit"):
            break
        if not mensaje:
            continue
        resultado = cadena.responder(mensaje, historial, sesion, continuar)
        continuar = resultado.get("continuar_con_agente", False)
        historial += [{"role": "user", "content": mensaje}, {"role": "assistant", "content": resultado["respuesta"]}]
        print("EcoMarket: ", end="")
        _imprimir(resultado)
        print()


# ---------- Tablas Markdown ----------

def _celda(texto) -> str:
    """Hace que un texto quepa en una celda de tabla Markdown."""
    if texto is None:
        return ""
    return str(texto).strip().replace("|", "\\|").replace("\r", "").replace("\n", "<br>")


def _encabezado(titulo: str, descripcion: str) -> list[str]:
    from src.agente.agente import config_modelo

    return [
        f"# {titulo}",
        "",
        f"- **LLM del agente:** `{config_modelo()['modelo']}` · **LLM del router, pedidos y RAG:** `{nombre_modelo()}` · "
        "**Temperatura:** 0",
        f"- **Embeddings:** `{MODELO_EMBEDDINGS}` · **k:** {RAG_K} · **Umbral:** {RAG_UMBRAL} · "
        f"**Fragmento máx.:** {CHUNK_TAMANO} caracteres",
        f"- **Ejecutado:** {datetime.now():%Y-%m-%d %H:%M} · **Fecha de referencia:** {FECHA_REFERENCIA}",
        "- Salidas reales generadas por `python app.py evaluar-agente`, sin edición manual.",
        "",
        descripcion,
        "",
    ]


def _escribir(nombre: str, lineas: list[str]):
    DIR_RESULTADOS.mkdir(exist_ok=True)
    archivo = DIR_RESULTADOS / nombre
    archivo.write_text("\n".join(lineas) + "\n", encoding="utf-8")
    print(f"Escrito results/{archivo.name}")


# ---------- Evaluación del agente ----------

def _categoria_ruta(ruta: str) -> str:
    for prefijo, categoria in (("agente", "agente"), ("pedidos", "pedidos"), ("rag", "rag"), ("humano", "humano")):
        if ruta.startswith(prefijo):
            return categoria
    return ruta


def _ejecutar_caso(caso: dict) -> list[dict]:
    """Ejecuta los turnos del caso como lo haría la interfaz (historial + ruta pegajosa). Devuelve un resultado por turno."""
    from src import cadena
    from src.agente import agente

    anteriores = {k: os.environ.get(k) for k in caso.get("env", {})}
    os.environ.update(caso.get("env", {}))
    historial, continuar, turnos = [], False, []
    try:
        for mensaje in caso["mensajes"]:
            if caso["entrada"] == "agente":
                r = agente.responder(mensaje, historial, f"eval-{caso['id']}")
            else:
                r = cadena.responder(mensaje, historial, f"eval-{caso['id']}", continuar)
            continuar = r.get("continuar_con_agente", False)
            historial += [{"role": "user", "content": mensaje}, {"role": "assistant", "content": r["respuesta"]}]
            turnos.append({"mensaje": mensaje, **r})
    finally:
        for clave, valor in anteriores.items():
            if valor is None:
                os.environ.pop(clave, None)
            else:
                os.environ[clave] = valor
    return turnos


def _verificar(caso: dict, turnos: list[dict]) -> dict:
    llamadas = [ll for t in turnos for ll in t.get("herramientas", [])]
    usadas = [ll["herramienta"] for ll in llamadas]
    etiqueta = any(ll["herramienta"] == "generar_etiqueta_devolucion" and ll["resultado"].get("ok") for ll in llamadas)
    ruta = _categoria_ruta(turnos[-1]["ruta"])
    checks = {
        "ruta": ruta == caso["ruta_esperada"],
        "herramientas": all(any(h in usadas for h in alternativas.split("|"))
                            for alternativas in caso["herramientas_esperadas"]),
        "etiqueta": etiqueta == caso["etiqueta"],
        "sin_error_llm": all(t.get("ok", True) for t in turnos),
    }
    # Contenido de la respuesta final: detecta datos inventados que la traza de herramientas no muestra.
    # Los modelos usan a veces guiones y espacios tipográficos (ECO‑10005, "10 días"): se normalizan antes de comparar.
    final = re.sub("[\u00a0\u2009\u202f]", " ", turnos[-1]["respuesta"].lower().replace("\u2011", "-"))
    if caso.get("debe_contener"):
        checks["contenido"] = all(any(a.lower() in final for a in t.split("|")) for t in caso["debe_contener"])
    if caso.get("no_debe_contener"):
        checks["sin_datos_indebidos"] = not any(t.lower() in final for t in caso["no_debe_contener"])
    return {"usadas": usadas, "etiqueta": etiqueta, "ruta": ruta, "checks": checks, "ok": all(checks.values())}


def cmd_evaluar_agente(args):
    from src.agente import herramientas
    from src.agente.observabilidad import leer_log, resumen_monitoreo

    casos = json.loads((DIR_DATOS / "casos_agente.json").read_text(encoding="utf-8"))["casos"]
    if args.casos:
        ids = {c.strip().upper() for c in args.casos.split(",")}
        casos = [c for c in casos if c["id"] in ids]
    # Las etiquetas y tickets de la evaluación van a una carpeta propia que se reinicia en cada corrida completa,
    # para que la idempotencia (A13 reutiliza la etiqueta de A1) se observe igual cada vez.
    herramientas.DIR_REGISTROS = DIR_RESULTADOS / ".registros_evaluacion"
    if not args.casos:
        shutil.rmtree(herramientas.DIR_REGISTROS, ignore_errors=True)
    # Precarga el índice y el modelo de embeddings: así la latencia registrada de consultar_politica es la de la
    # búsqueda y no la de cargar el modelo (unos 40 s la primera vez).
    from src import rag
    rag._base_vectorial()
    inicio_log = len(leer_log())

    resumen = ["## 1. Resumen", "",
               "| Caso | Descripción | Ruta esperada → obtenida | Herramientas esperadas | Herramientas usadas | "
               "¿Etiqueta? esperada → emitida | Resultado |", "|---|---|---|---|---|---|---|"]
    detalle = ["## 2. Detalle por caso", ""]
    aprobados = 0
    for caso in casos:
        print(f"  {caso['id']} {caso['caso']}...", file=sys.stderr)
        turnos = _ejecutar_caso(caso)
        v = _verificar(caso, turnos)
        aprobados += v["ok"]
        fallas = [nombre for nombre, ok in v["checks"].items() if not ok]
        resumen.append(
            f"| {caso['id']} | {_celda(caso['caso'])} | {caso['ruta_esperada']} → {v['ruta']} | "
            f"{', '.join(h.replace('|', ' o ') for h in caso['herramientas_esperadas']) or '—'} | "
            f"{', '.join(v['usadas']) or 'ninguna'} | "
            f"{'sí' if caso['etiqueta'] else 'no'} → {'sí' if v['etiqueta'] else 'no'} | "
            f"{'✅' if v['ok'] else '❌ ' + ', '.join(fallas)} |")
        detalle += [f"### {caso['id']} — {caso['caso']}", "", f"**Esperado:** {caso['esperado']}", ""]
        if caso.get("env"):
            detalle += [f"**Entorno del caso:** `{json.dumps(caso['env'], ensure_ascii=False)}`", ""]
        for i, t in enumerate(turnos, start=1):
            prefijo = f"Turno {i} · " if len(turnos) > 1 else ""
            detalle += [f"**{prefijo}Cliente:** {t['mensaje']}", "", f"**Ruta:** `{t['ruta']}`", ""]
            for llamada in t.get("herramientas", []):
                detalle.append(f"- 🔧 `{_resumen_llamada(llamada)}`")
            if t.get("herramientas"):
                detalle.append("")
            detalle += ["**Respuesta del asistente:**", "", "> " + t["respuesta"].strip().replace("\n", "\n> "), ""]

    eventos = leer_log()[inicio_log:]
    m = resumen_monitoreo(eventos)
    monitoreo = ["## 3. Monitoreo de esta corrida (logs/acciones.jsonl)", "",
                 f"- Turnos: **{m['turnos']}** · errores del LLM: **{m['errores_agente']}** · llamadas a herramientas: "
                 f"**{m['llamadas_herramientas']}** · etiquetas nuevas: **{m['etiquetas_emitidas']}** · "
                 f"verificaciones no elegibles: **{m['no_elegibles']}** · escalamientos: **{m['escalamientos']}**", "",
                 "| Herramienta | Llamadas | Errores por código | Latencia media (ms) |", "|---|---|---|---|"]
    for nombre, datos in sorted(m["por_herramienta"].items()):
        errores = ", ".join(f"{k}: {v}" for k, v in datos["errores"].items()) or "—"
        monitoreo.append(f"| {nombre} | {datos['llamadas']} | {errores} | {datos['latencia_ms']} |")
    monitoreo += ["", "**Alertas:** " + ("; ".join(m["alertas"]) if m["alertas"] else "ninguna.")]

    lineas = _encabezado(
        "Resultados — Agente de devoluciones",
        "Cada caso pasa por el sistema completo (router del Taller 2 → agente) o directamente por el agente, y se "
        "verifica de forma automática: **ruta** correcta, **herramientas** esperadas presentes en la traza, "
        "**etiqueta** emitida solo cuando corresponde y **sin error** del LLM. "
        f"Casos aprobados: **{aprobados} de {len(casos)}**.",
    )
    nombre = "resultados_agente.md" if not args.casos else "resultados_agente_parcial.md"
    _escribir(nombre, lineas + resumen + [""] + detalle + monitoreo)
    print(f"Aprobados: {aprobados} de {len(casos)}")


def cmd_monitoreo(_args):
    from src.agente.observabilidad import ARCHIVO_LOG, resumen_monitoreo

    m = resumen_monitoreo()
    print(f"Registro: {ARCHIVO_LOG}")
    print(f"Turnos del agente: {m['turnos']} · errores del LLM: {m['errores_agente']} · "
          f"llamadas a herramientas: {m['llamadas_herramientas']}")
    print(f"Etiquetas emitidas: {m['etiquetas_emitidas']} · no elegibles: {m['no_elegibles']} · "
          f"escalamientos: {m['escalamientos']}")
    print(f"Validador de salida: {m['respuestas_corregidas']} respuestas corregidas · "
          f"{m['respuestas_bloqueadas']} bloqueadas\n")
    for nombre, datos in sorted(m["por_herramienta"].items()):
        errores = ", ".join(f"{k}={v}" for k, v in datos["errores"].items()) or "sin errores"
        print(f"  {nombre:<34} {datos['llamadas']:>4} llamadas · {datos['latencia_ms']:>5} ms · {errores}")
    print("\nAlertas:" if m["alertas"] else "\nSin alertas.")
    for alerta in m["alertas"]:
        print(f"  ⚠ {alerta}")


def main():
    # La consola de Windows no usa UTF-8 por defecto; sin esto se dañan tildes y eñes.
    for flujo in (sys.stdout, sys.stderr, sys.stdin):
        flujo.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="Asistente de EcoMarket: RAG + agente de devoluciones")
    sub = parser.add_subparsers(dest="comando", required=True)
    sub.add_parser("probar", help="Envía 'Hola' al modelo configurado en .env").set_defaults(func=cmd_probar)

    p = sub.add_parser("indexar", help="Carga, segmenta, vectoriza y guarda la base de conocimiento en chroma_db/")
    p.add_argument("--estrategia", choices=("estructura", "parrafos", "fijo"), default="estructura")
    p.set_defaults(func=cmd_indexar)

    p = sub.add_parser("fragmentos", help="Muestra los fragmentos de una estrategia, sin calcular embeddings")
    p.add_argument("--estrategia", choices=("estructura", "parrafos", "fijo"), default="estructura")
    p.add_argument("--fuente", help="Filtra por archivo, p. ej. faq.json")
    p.set_defaults(func=cmd_fragmentos)

    p = sub.add_parser("buscar", help="Solo recuperación: muestra los fragmentos y su similitud (sin LLM)")
    p.add_argument("texto")
    p.add_argument("--k", type=int, default=RAG_K)
    p.set_defaults(func=cmd_buscar)

    p = sub.add_parser("preguntar", help="Responde una pregunta con la cadena RAG")
    p.add_argument("texto")
    p.add_argument("--version", choices=("v1", "v2"), default="v2")
    p.add_argument("--sin-rag", action="store_true", help="Responde sin base de conocimiento (línea base)")
    p.add_argument("--k", type=int, default=RAG_K)
    p.add_argument("--umbral", type=float, default=RAG_UMBRAL)
    p.add_argument("--mostrar-prompt", action="store_true", help="Muestra los mensajes sin llamar al modelo")
    p.set_defaults(func=cmd_preguntar)

    sub.add_parser("chat", help="Conversación con el sistema completo: router + pedidos + RAG + agente").set_defaults(func=cmd_chat)

    p = sub.add_parser("agente", help="Envía un mensaje directamente al agente de devoluciones")
    p.add_argument("texto")
    p.set_defaults(func=cmd_agente)

    p = sub.add_parser("evaluar-agente", help="Ejecuta data/casos_agente.json y escribe results/resultados_agente.md")
    p.add_argument("--casos", help="Solo estos ids, separados por coma (p. ej. A1,A14)")
    p.set_defaults(func=cmd_evaluar_agente)

    sub.add_parser("monitoreo", help="Métricas y alertas a partir de logs/acciones.jsonl").set_defaults(func=cmd_monitoreo)

    args = parser.parse_args()
    try:
        args.func(args)
    except (ErrorConfiguracion, ErrorLLM, FileNotFoundError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
