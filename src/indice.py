"""Indexación: convierte los fragmentos en vectores con el modelo de embeddings y los guarda en ChromaDB."""

import os
from pathlib import Path

from langchain_chroma import Chroma
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings

from src.config import COLECCION, DIR_INDICE, MODELO_EMBEDDINGS

# Sin esto, la librería de Hugging Face imprime avisos de telemetría y de enlaces simbólicos en Windows.
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")

_embeddings: Embeddings | None = None


class EmbeddingsE5(Embeddings):
    """Envuelve HuggingFaceEmbeddings para agregar los prefijos que exige la familia E5.

    Los modelos E5 se entrenaron con "query: " delante de las preguntas y "passage: " delante de los
    documentos; sin los prefijos la calidad de la búsqueda baja. Los vectores se normalizan para que el
    producto punto sea igual a la similitud coseno.
    """

    def __init__(self, modelo: str):
        from langchain_huggingface import HuggingFaceEmbeddings  # import diferido: carga torch solo si se usa

        self._base = HuggingFaceEmbeddings(model_name=modelo, encode_kwargs={"normalize_embeddings": True})
        self._prefijos = "e5" in modelo.lower()

    def embed_documents(self, textos: list[str]) -> list[list[float]]:
        if self._prefijos:
            textos = [f"passage: {t}" for t in textos]
        return self._base.embed_documents(textos)

    def embed_query(self, texto: str) -> list[float]:
        return self._base.embed_query(f"query: {texto}" if self._prefijos else texto)


def obtener_embeddings() -> Embeddings:
    """El modelo (~470 MB) se descarga la primera vez y queda en la caché de Hugging Face."""
    global _embeddings
    if _embeddings is None:
        _embeddings = EmbeddingsE5(MODELO_EMBEDDINGS)
    return _embeddings


def _abrir(directorio: Path | None, coleccion: str) -> Chroma:
    return Chroma(
        collection_name=coleccion,
        embedding_function=obtener_embeddings(),
        persist_directory=str(directorio) if directorio else None,  # None = solo en memoria (experimentos)
        collection_metadata={"hnsw:space": "cosine"},
    )


def construir_indice(fragmentos: list[Document], directorio: Path | None = DIR_INDICE,
                     coleccion: str = COLECCION) -> Chroma:
    """Borra la colección y la vuelve a crear. Los ids estables (fuente::NNN) evitan duplicados al reindexar."""
    bd = _abrir(directorio, coleccion)
    bd.reset_collection()
    bd.add_documents(fragmentos, ids=[f.metadata["id"] for f in fragmentos])
    return bd


def cargar_indice() -> Chroma:
    if not (DIR_INDICE / "chroma.sqlite3").exists():
        raise FileNotFoundError("No existe el índice vectorial. Ejecuta primero: python app.py indexar")
    return _abrir(DIR_INDICE, COLECCION)
