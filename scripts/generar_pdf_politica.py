"""Genera data/politica_devoluciones.pdf a partir de data/fuentes/politica_devoluciones.md.

En una empresa real la política de devoluciones suele existir como PDF firmado por el área legal. Este script
simula ese documento para que la base de conocimiento incluya un tipo de archivo distinto (PDF) y el pipeline
de ingesta tenga que extraer texto de él con PyPDFLoader.

Uso: python scripts/generar_pdf_politica.py
"""

from pathlib import Path

from fpdf import FPDF

RAIZ = Path(__file__).resolve().parent.parent
ORIGEN = RAIZ / "data" / "fuentes" / "politica_devoluciones.md"
DESTINO = RAIZ / "data" / "politica_devoluciones.pdf"


def main():
    pdf = FPDF(format="A4")
    pdf.set_auto_page_break(auto=True, margin=20)
    pdf.add_page()
    pdf.set_margins(20, 20, 20)
    ancho = pdf.w - pdf.l_margin - pdf.r_margin

    for linea in ORIGEN.read_text(encoding="utf-8").splitlines():
        linea = linea.replace("`", "")
        if linea.startswith("# "):
            pdf.set_font("Helvetica", "B", 16)
            pdf.multi_cell(ancho, 9, linea[2:], new_x="LMARGIN", new_y="NEXT")
            pdf.ln(3)
        elif linea.startswith("## "):
            pdf.ln(2)
            pdf.set_font("Helvetica", "B", 12)
            pdf.multi_cell(ancho, 7, linea[3:], new_x="LMARGIN", new_y="NEXT")
        elif linea.strip():
            pdf.set_font("Helvetica", "", 10.5)
            # Las fuentes base de PDF solo cubren latin-1: las viñetas se escriben con guion.
            pdf.multi_cell(ancho, 5.5, linea.strip(), markdown=True, new_x="LMARGIN", new_y="NEXT")
        else:
            pdf.ln(1.5)

    pdf.output(str(DESTINO))
    print(f"Escrito {DESTINO.relative_to(RAIZ)}")


if __name__ == "__main__":
    main()
