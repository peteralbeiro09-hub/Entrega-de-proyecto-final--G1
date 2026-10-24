# Guion del video (máximo 10 minutos)

Grabación de pantalla con voz. No hacen falta diapositivas: se muestran el README, los diagramas de `docs/`, el código y la aplicación.

**Antes de grabar:**
1. Activar el entorno (`.venv\Scripts\activate`) y comprobar `python app.py probar`. **No correr `evaluar-agente` el mismo día de la grabación**: consume la cuota diaria de Groq. Si se agota, el agente responde con el modelo de respaldo y la ruta lo indica, así que la demo sigue funcionando.
2. Borrar las etiquetas de pruebas anteriores, para que la demo emita etiquetas nuevas: eliminar la carpeta `data\etiquetas`.
3. `streamlit run app_web.py` y abrir http://localhost:8501. Hacer una consulta de calentamiento y luego "🗑️ Nueva conversación".
4. Tener abiertos en el editor: `README.md`, `docs/fase1_arquitectura_agente.md`, `src/agente/herramientas.py`, `src/agente/agente.py` y `src/cadena.py`.

---

## Parte 1 — Fase 1: herramientas y arquitectura (2–3 min)

**Pantalla:** `docs/fase1_arquitectura_agente.md`, en la vista previa de Markdown para ver los diagramas.

1. **(20 s) Contexto.** "En el Taller 2 el asistente respondía con RAG. Ahora, para las devoluciones, el asistente **actúa**: verifica si el producto es elegible y genera la etiqueta de envío, en la misma conversación."
2. **(40 s) Arquitectura, diagrama §2.** "El router del Taller 2 sigue decidiendo la ruta: pedidos, RAG, humano y, ahora, el agente para las devoluciones. El RAG está en dos lugares: como ruta para preguntas generales y como herramienta del agente, para dudas de política en medio de una devolución."
3. **(60–80 s) Herramientas, tabla §3.1.** Recorrer las cinco:
   - `buscar_pedido`: consulta el pedido sin devolver datos personales.
   - `verificar_elegibilidad_producto`: aplica la política (plazo de 30 días, no devolvibles, excepción por daño de 48 h con foto).
   - `generar_etiqueta_devolucion`: emite RMA, guía y enlace. **Vuelve a verificar por su cuenta** y es idempotente.
   - `escalar_a_humano`: abre un ticket.
   - `consultar_politica`: el RAG del Taller 2.
   - Frase clave: *"el LLM decide qué hacer; el código decide qué está permitido"*. Mostrar el contrato `{"ok": …}` de errores.
4. **(20 s) Marco, §4.** "LangChain, porque el RAG ya estaba en LangChain y `create_agent` con esquemas Pydantic hace explícito lo que el modelo puede pedir."
5. **(20 s) Flujo, diagrama §5.** Señalar las decisiones: pedir los datos que faltan, ¿elegible?, ¿error de la transportadora?

## Parte 2 — Fase 2: arquitectura del agente, comportamiento y código (3–4 min)

**Pantalla:** el editor.

1. **(30 s) Estructura del repositorio.** Mostrar el árbol del README: `src/agente/` (nuevo) junto a lo heredado de los Talleres 1 y 2.
2. **(60 s) `src/agente/herramientas.py`.** Mostrar `verificar_elegibilidad_producto` (las reglas en Python) y, en `generar_etiqueta_devolucion`, la línea que repite la verificación y el registro idempotente.
3. **(60 s) `src/agente/agente.py`.**
   - `EntradaDevolucion`: el `Literal` del motivo y las descripciones que lee el modelo.
   - `create_agent(modelo, tools, system_prompt)`.
   - En `responder`: el `recursion_limit`, la escalada si se supera y el mensaje amable si falla el LLM.
4. **(30 s) `src/cadena.py`.** La ruta `devolucion` ahora va al agente, y la ruta pegajosa.
5. **(30 s) `prompts/agente.toml`.** Reglas de cuándo usar herramientas y cuándo responder directo.
6. **(30 s) Evidencia.** Abrir `results/resultados_agente.md` y mostrar la tabla resumen: ruta, herramientas esperadas frente a usadas, y si se emitió etiqueta. Mencionar `pytest` (32 pruebas sin LLM) y el validador de salida (`validar_respuesta`), agregado tras encontrar una etiqueta inventada en la prueba de la interfaz: es un buen ejemplo de por qué las reglas críticas van en código. Si hay tiempo, mostrar `docs/img/hallazgo_etiqueta_inventada.png`.

## Parte 3 — Fase 4: demostración funcional (3–4 min)

**Pantalla:** la aplicación en el navegador. Después de cada respuesta, abrir "🔧 Herramientas usadas".

| # | Acción | Qué decir |
|---|---|---|
| 1 | Clic en **✅ Devolución elegible** | "Usó dos herramientas: verificó (10 días, dentro del plazo) y generó la etiqueta. Todos los datos (RMA, guía y enlace) salen de la herramienta." |
| 2 | Clic en **⛔ No devolvible** | "Esta vez decidió **no** generar la etiqueta: el termo es personalizado (§3). Explica el motivo y ofrece una alternativa." |
| 3 | **🗑️ Nueva conversación** → **❓ Faltan datos** | "Responde **sin herramientas**: le faltan el pedido, el producto y el motivo." |
| 4 | Escribir *"Es la botella del pedido ECO-10001, ya no la necesito"* | "El segundo mensaje vuelve al agente gracias a la ruta pegajosa. Como esa etiqueta ya existía, devuelve **la misma**: no emite guías duplicadas." |
| 5 | Activar **Simular caída de la transportadora** → **📸 Producto dañado con foto** | "Es elegible por la excepción de daño, pero la transportadora falla. El agente lo explica sin tecnicismos y no inventa una guía." Desactivar el interruptor y repetir: ahora sí genera la etiqueta. |
| 6 | **📖 Pregunta de política** | "El router la manda al RAG del Taller 2, no al agente: la ruta lo muestra." |
| 7 | **🛡️ Intento de manipulación** | "Aunque le pidan saltarse las reglas, no hay etiqueta: y si el modelo lo intentara, la herramienta respondería NO_ELEGIBLE." |
| 8 | Abrir **📈 Monitoreo** | "Cada acción quedó registrada: etiquetas, negativas, escalamientos y alertas." |

**Cierre (10 s):** "En resumen: el agente automatiza la devolución de punta a punta, las reglas están en código, los errores se manejan y cada acción queda auditada."
