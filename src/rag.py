"""Recuperación y generación: la cadena RAG expresada con LangChain (LCEL).

    consulta ──► recuperar (embeddings + Chroma + umbral) ──┬─ sin fragmentos relevantes ─► respuesta fija
                                                            └─ con fragmentos ─► prompt ─► LLM ─► respuesta

El LLM es el mismo del Taller 1 (src/llm.py: OpenRouter/Ollama/Groq/OpenAI con caché y reintentos);
LangChain se usa para la parte nueva: carga, segmentación, embeddings, base vectorial y la composición de la cadena.
"""

from langchain_core.documents import Document
from langchain_core.runnables import Runnable, RunnableBranch, RunnableLambda

from src import prompts
from src.config import RAG_K, RAG_UMBRAL, cargar_toml
from src.indice import cargar_indice
from src.llm import completar

_bd = None


def _base_vectorial():
    global _bd
    if _bd is None:
        _bd = cargar_indice()
    return _bd


def recuperar(consulta: str, k: int = RAG_K, umbral: float = RAG_UMBRAL,
              filtro: dict | None = None) -> list[tuple[Document, float]]:
    """Los k fragmentos más parecidos a la consulta cuya similitud coseno supera el umbral.

    El umbral es lo que permite responder "no sé": si la pregunta no se parece a nada de la base de
    conocimiento, la lista queda vacía y el modelo nunca recibe un contexto irrelevante que lo invite a inventar.
    """
    pares = _base_vectorial().similarity_search_with_relevance_scores(consulta, k=k, filter=filtro)
    return [(doc, score) for doc, score in pares if score >= umbral]


def fragmentos_de(filtro: dict) -> list[Document]:
    """Todos los fragmentos que cumplen un filtro de metadatos, en el orden original del documento.

    Es recuperación por metadatos, no por similitud: se usa cuando un documento es corto y sus reglas
    dependen unas de otras, como la política de devoluciones (ver src/cadena.py).
    """
    datos = _base_vectorial().get(where=filtro)
    pares = sorted(zip(datos["ids"], datos["documents"], datos["metadatas"]))
    return [Document(page_content=texto, metadata=meta) for _, texto, meta in pares]


def formatear_contexto(docs: list[Document]) -> str:
    """Fragmentos numerados con su fuente, para que el modelo pueda citarlos como [1], [2]..."""
    return "\n\n".join(
        f"[{i}] (fuente: {doc.metadata['fuente']} › {doc.metadata['seccion']})\n{doc.page_content}"
        for i, doc in enumerate(docs, start=1)
    )


def _fuentes(pares: list[tuple[Document, float]]) -> list[str]:
    return [f"[{i}] {d.metadata['fuente']} › {d.metadata['seccion']} (sim. {s:.3f})" for i, (d, s) in enumerate(pares, 1)]


def construir_cadena(version: str = "v2", k: int = RAG_K, umbral: float = RAG_UMBRAL) -> Runnable:
    """Arma la cadena RAG con LCEL. Entrada: la consulta (str). Salida: dict con respuesta, ruta y fuentes."""
    paso_recuperar = RunnableLambda(lambda consulta: {"consulta": consulta, "pares": recuperar(consulta, k, umbral)})

    paso_prompt = RunnableLambda(lambda d: {
        **d,
        "mensajes": prompts.construir_mensajes("rag", version, d["consulta"],
                                               {"contexto": formatear_contexto([doc for doc, _ in d["pares"]])}),
    })

    paso_llm = RunnableLambda(lambda d: {
        "respuesta": completar(d["mensajes"]),
        "ruta": f"rag {version} ({len(d['pares'])} fragmentos)",
        "fuentes": _fuentes(d["pares"]),
    })

    sin_contexto = RunnableLambda(lambda d: {
        "respuesta": cargar_toml("rag")["sin_contexto"]["cliente"],
        "ruta": "rag: ningún fragmento supera el umbral (sin llamar al LLM)",
        "fuentes": [],
    })

    return paso_recuperar | RunnableBranch(
        (lambda d: not d["pares"], sin_contexto),
        paso_prompt | paso_llm,
    )


def responder_sin_rag(consulta: str) -> dict:
    """Línea base: el mismo LLM sin base de conocimiento, para comparar contra el RAG."""
    respuesta = completar(prompts.construir_mensajes("rag", "sin_rag", consulta))
    return {"respuesta": respuesta, "ruta": "sin RAG", "fuentes": []}


def responder(consulta: str, version: str = "v2", k: int = RAG_K, umbral: float = RAG_UMBRAL) -> dict:
    return construir_cadena(version, k, umbral).invoke(consulta)
