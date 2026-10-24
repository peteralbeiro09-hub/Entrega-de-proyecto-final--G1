"""Fase 2 en código: carga los documentos de la base de conocimiento y los divide en fragmentos (chunks).

Documentos (cada uno de un tipo distinto, como llegarían en una empresa real):
    data/politica_devoluciones.pdf  PDF        → PyPDFLoader
    data/politica_envios.md         Markdown   → MarkdownHeaderTextSplitter
    data/faq.json                   JSON       → un Document por pregunta
    data/inventario.csv             CSV        → un Document por producto

Estrategias de segmentación (se comparan en `python app.py experimento-chunking`):
    "estructura"  La propuesta: respeta la estructura de cada tipo de documento (sección, pregunta, producto)
                  y solo aplica RecursiveCharacterTextSplitter si una sección supera el tamaño máximo.
    "parrafos"    Corta el texto plano de cada documento en cada línea en blanco.
    "fijo"        Corta el texto plano cada N caracteres, sin solape ni respeto por la estructura.
"""

import csv
import json
import re

from langchain_community.document_loaders import PyPDFLoader
from langchain_core.documents import Document
from langchain_text_splitters import (
    CharacterTextSplitter,
    MarkdownHeaderTextSplitter,
    RecursiveCharacterTextSplitter,
)

from src.config import CHUNK_SOLAPE, CHUNK_TAMANO, DIR_DATOS

ESTRATEGIAS = ("estructura", "parrafos", "fijo")

PDF_POLITICA = DIR_DATOS / "politica_devoluciones.pdf"
MD_ENVIOS = DIR_DATOS / "politica_envios.md"
JSON_FAQ = DIR_DATOS / "faq.json"
CSV_INVENTARIO = DIR_DATOS / "inventario.csv"


# ---------- Carga: de archivo a texto ----------

def _texto_pdf() -> str:
    """PyPDFLoader devuelve un Document por página; se unen para no cortar secciones entre páginas."""
    paginas = PyPDFLoader(str(PDF_POLITICA)).load()
    return "\n".join(p.page_content for p in paginas)


def _faq() -> list[dict]:
    return json.loads(JSON_FAQ.read_text(encoding="utf-8"))["preguntas"]


def _productos() -> list[dict]:
    with open(CSV_INVENTARIO, encoding="utf-8", newline="") as archivo:
        return list(csv.DictReader(archivo))


def _texto_faq(item: dict) -> str:
    return f"Pregunta frecuente ({item['tema']}): {item['pregunta']}\nRespuesta: {item['respuesta']}"


def _texto_producto(p: dict) -> str:
    """Convierte una fila del CSV en un párrafo en lenguaje natural: los embeddings se entrenaron con texto,
    no con filas separadas por comas, y así la pregunta "¿la botella va al lavavajillas?" se parece al fragmento."""
    precio = f"${int(p['precio_cop']):,} COP".replace(",", ".")
    stock = int(p["stock"])
    disponibilidad = f"disponible ({stock} unidades en inventario)" if stock > 0 else "AGOTADO (0 unidades en inventario)"
    devolvible = "sí admite devolución" if p["devolvible"] == "si" else "NO admite devolución (salvo defecto o daño)"
    return (
        f"Producto: {p['nombre']} (referencia {p['sku']}). Categoría: {p['categoria']}. Precio: {precio}. "
        f"Disponibilidad: {disponibilidad}. Devoluciones: {devolvible}.\n"
        f"Materiales: {p['materiales']}.\nDescripción: {p['descripcion']}\nCuidado: {p['cuidado']}"
    )


def documentos_completos() -> list[Document]:
    """Un Document por archivo con su texto plano. Es la entrada de las estrategias ingenuas (fijo, párrafos)."""
    return [
        Document(page_content=_texto_pdf(), metadata={"fuente": PDF_POLITICA.name, "tipo": "politica"}),
        Document(page_content=MD_ENVIOS.read_text(encoding="utf-8"), metadata={"fuente": MD_ENVIOS.name, "tipo": "politica"}),
        Document(page_content="\n\n".join(_texto_faq(f) for f in _faq()), metadata={"fuente": JSON_FAQ.name, "tipo": "faq"}),
        Document(page_content="\n\n".join(_texto_producto(p) for p in _productos()),
                 metadata={"fuente": CSV_INVENTARIO.name, "tipo": "producto"}),
    ]


# ---------- Segmentación por estructura (la propuesta) ----------

def _divisor_recursivo(tamano: int) -> RecursiveCharacterTextSplitter:
    """Para secciones largas: intenta cortar primero por párrafo, luego por línea y luego por oración."""
    return RecursiveCharacterTextSplitter(
        chunk_size=tamano, chunk_overlap=CHUNK_SOLAPE, separators=["\n\n", "\n", ". ", " ", ""]
    )


def _secciones(titulo_doc: str, secciones: list[tuple[str, str]], metadata: dict, tamano: int) -> list[Document]:
    """Un fragmento por sección (o varios si la sección es larga). El documento y la sección van en los
    metadatos, no en el texto que se vectoriza.

    Primera versión: el texto empezaba con "Política de envíos de EcoMarket — 3. Costo del envío". El
    experimento de recuperación mostró que ese encabezado repetido hace que todos los fragmentos de un
    documento se parezcan entre sí: "¿Hacen envíos a México?" bajaba al puesto 5 y quedaba fuera de k=4.
    Sin él, el acierto@3 pasa de 92 % a 100 % (docs/fase2_base_conocimiento.md §4). El modelo sigue viendo
    la procedencia porque formatear_contexto() antepone "fuente › sección" a cada fragmento.
    """
    divisor = _divisor_recursivo(tamano)
    fragmentos = []
    for seccion, cuerpo in secciones:
        for parte in divisor.split_text(cuerpo.strip()):
            fragmentos.append(Document(
                page_content=parte,
                metadata={**metadata, "documento": titulo_doc, "seccion": seccion},
            ))
    return fragmentos


def _chunks_politica_pdf(tamano: int) -> list[Document]:
    """El PDF pierde el formato Markdown, pero conserva los títulos numerados ("4. Excepción: ...").

    Un título es una línea "N. Texto" corta, sin punto final y con el número consecutivo al título anterior;
    así los pasos numerados dentro de la sección 5 ("1. Escribir a atención al cliente...") no se confunden
    con títulos de sección.
    """
    lineas = _texto_pdf().strip().splitlines()
    titulo_doc = lineas[0].strip()
    secciones: list[tuple[str, list[str]]] = []
    for linea in lineas[1:]:
        m = re.match(r"^(\d+)\. .+$", linea.strip())
        es_titulo = (m and int(m.group(1)) == len(secciones) + 1
                     and len(linea.strip()) <= 60 and not linea.strip().endswith("."))
        if es_titulo:
            secciones.append((linea.strip(), []))
        elif secciones:
            secciones[-1][1].append(linea)
    return _secciones(titulo_doc, [(s, "\n".join(c)) for s, c in secciones],
                     {"fuente": PDF_POLITICA.name, "tipo": "politica"}, tamano)


def _chunks_envios_md(tamano: int) -> list[Document]:
    divisor_md = MarkdownHeaderTextSplitter(headers_to_split_on=[("#", "documento"), ("##", "seccion")])
    bloques = divisor_md.split_text(MD_ENVIOS.read_text(encoding="utf-8"))
    titulo_doc = bloques[0].metadata.get("documento", "Política de envíos de EcoMarket")
    secciones = [(b.metadata["seccion"], b.page_content) for b in bloques if "seccion" in b.metadata]
    return _secciones(titulo_doc, secciones, {"fuente": MD_ENVIOS.name, "tipo": "politica"}, tamano)


def _chunks_faq() -> list[Document]:
    """Una pregunta con su respuesta es la unidad de sentido: nunca se parte."""
    return [
        Document(page_content=_texto_faq(f),
                 metadata={"fuente": JSON_FAQ.name, "tipo": "faq", "seccion": f"{f['id']} {f['pregunta']}", "tema": f["tema"]})
        for f in _faq()
    ]


def _chunks_inventario() -> list[Document]:
    """Un producto por fragmento: así una pregunta por un producto no recupera atributos de otro."""
    return [
        Document(page_content=_texto_producto(p),
                 metadata={"fuente": CSV_INVENTARIO.name, "tipo": "producto", "seccion": p["nombre"],
                           "sku": p["sku"], "categoria": p["categoria"], "devolvible": p["devolvible"] == "si"})
        for p in _productos()
    ]


# ---------- Punto de entrada ----------

def fragmentar(estrategia: str = "estructura", tamano: int = CHUNK_TAMANO) -> list[Document]:
    """Devuelve los fragmentos de toda la base de conocimiento según la estrategia, con un id estable en metadata."""
    if estrategia == "estructura":
        fragmentos = _chunks_politica_pdf(tamano) + _chunks_envios_md(tamano) + _chunks_faq() + _chunks_inventario()
    elif estrategia == "parrafos":
        divisor = CharacterTextSplitter(separator="\n\n", chunk_size=tamano, chunk_overlap=0)
        fragmentos = divisor.split_documents(documentos_completos())
    elif estrategia == "fijo":
        divisor = CharacterTextSplitter(separator="", chunk_size=tamano, chunk_overlap=0)
        fragmentos = divisor.split_documents(documentos_completos())
    else:
        raise ValueError(f"Estrategia desconocida: {estrategia}. Usa una de {ESTRATEGIAS}.")

    contador: dict[str, int] = {}
    for doc in fragmentos:
        fuente = doc.metadata["fuente"]
        contador[fuente] = contador.get(fuente, 0) + 1
        doc.metadata.setdefault("seccion", "(sin sección)")
        doc.metadata["id"] = f"{fuente}::{contador[fuente]:03d}"
    return fragmentos
