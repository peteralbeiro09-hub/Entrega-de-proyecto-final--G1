# Fase 4 — Despliegue de la aplicación

## 1. Selección de la herramienta: Streamlit

| Criterio | Streamlit | Gradio |
|---|---|---|
| Componentes de chat | `st.chat_input` y `st.chat_message` nativos, con avatares | `gr.ChatInterface`, muy rápido de montar |
| Mostrar **qué hizo el agente** | `st.status` (progreso), `st.expander`, `st.json` y `st.code` para ver cada herramienta con sus argumentos y su resultado debajo de la respuesta | Posible con `gr.Blocks`, pero hay que construir el panel a mano; `ChatInterface` está pensado para texto o multimedia |
| Estado por usuario | `st.session_state`: historial, ruta "pegajosa" e id de sesión para el registro de acciones | `gr.State`, equivalente |
| Panel lateral (ejemplos, controles, monitoreo) | `st.sidebar` con botones, interruptores y `st.metric` | Requiere un diseño con `Blocks` |
| Recursos pesados compartidos | `@st.cache_resource` carga una sola vez el modelo de embeddings y ChromaDB | Variables globales |
| Modelo de ejecución | El script se re-ejecuta en cada interacción: simple de razonar, pero obliga a guardar todo en `session_state` | Basado en eventos y funciones |
| Caso de uso típico | Aplicaciones de datos y paneles internos | Demos de modelos, compartir rápido un enlace público (`share=True`) |
| Experiencia previa | Ya usado en otros proyectos del autor | — |

**Decisión: Streamlit.** La interfaz no debe mostrar solo la respuesta: para la demostración y para la auditoría (Fase 3 §3) también debe mostrar **por qué** el agente respondió así, es decir, qué herramientas llamó, con qué datos y qué le devolvieron. Streamlit permite poner ese detalle debajo de cada mensaje con expansores y JSON, más un panel lateral con ejemplos y métricas, sin escribir HTML. Gradio habría servido igual para un chat simple con una casilla de texto. Su ventaja principal, el enlace público inmediato, no hace falta para una demostración local grabada en video.

## 2. La interfaz (`app_web.py`)

```
┌───────────────────────┬──────────────────────────────────────────────────┐
│ 🌿 EcoMarket          │ Asistente de atención al cliente                 │
│                       │                                                  │
│ Prueba un ejemplo     │ 🧑 Quiero devolver la botella del ECO-10001…     │
│ [✅ Devolución eleg.] │ 🌿 ¡Listo! Tu etiqueta de devolución…            │
│ [📸 Dañado con foto ] │    RMA-74E93442 · EnvíaVerde · guía …            │
│ [⛔ No devolvible   ] │    🤖 Ruta: agente → verificar… → generar…       │
│ [❓ Faltan datos    ] │    ▸ 🔧 Herramientas usadas (2)                  │
│ [🚚 Sin entregar    ] │         ✅ verificar_elegibilidad_producto 2 ms  │
│ [📖 Pregunta polít. ] │            {argumentos} {resultado}              │
│ [🛡️ Manipulación    ] │                                                  │
│ ─────────────────     │                                                  │
│ ◉ Mostrar herramientas│                                                  │
│ ○ Simular caída       │                                                  │
│   transportadora      │                                                  │
│ [🗑️ Nueva convers.]   │                                                  │
│ ▸ 📈 Monitoreo        │                                                  │
│ Modelo: gpt-oss-120b  │ [ Escribe tu mensaje…                       ➤ ]  │
└───────────────────────┴──────────────────────────────────────────────────┘
```

| Elemento | Para qué |
|---|---|
| Campo de texto (`st.chat_input`) | El *prompt* del usuario, como pide el enunciado |
| Área de conversación | Respuesta del agente, con historial: el agente recuerda lo que el cliente ya dijo |
| Ruta (🤖 agente, 📦 pedidos, 📚 RAG, 🧑‍💼 humano) | Muestra la decisión del router |
| "🔧 Herramientas usadas" | Cada llamada: nombre, latencia, argumentos y resultado (✅ éxito, ⚠️ error) |
| Ejemplos de un clic | Cubren los comportamientos que hay que demostrar: éxito, negativa, datos faltantes, error, RAG e inyección |
| "Simular caída de la transportadora" | Activa `SIMULAR_FALLA_TRANSPORTADORA` para mostrar en vivo el manejo de errores |
| "Nueva conversación" | Reinicia el historial y la sesión |
| "📈 Monitoreo" | Métricas y alertas del registro de acciones (Fase 3 §3) |

**Manejo de errores en la interfaz.** Si falta la configuración (`.env`) o el índice (`chroma_db/`), se muestra un mensaje que dice qué hacer, en lugar de una traza de Python. Si falla el LLM durante el turno, el agente ya devuelve un mensaje amable (Fase 2). Los precios con `$` se escapan para que Markdown no los interprete como fórmulas LaTeX.

## 3. Cómo ejecutarla

```bash
# Desde la carpeta del repositorio, con el entorno virtual activo y .env configurado (ver README)
python app.py indexar          # solo la primera vez: crea chroma_db/
streamlit run app_web.py       # abre http://localhost:8501
```

Al abrir la página se carga una sola vez el modelo de embeddings y ChromaDB (unos 40–60 s en CPU, con un indicador de progreso). Después, las respuestas tardan lo que tarde el LLM: de 5 a 30 s según el uso reciente de la cuota por minuto de Groq. `.streamlit/config.toml` desactiva la vigilancia de archivos de Streamlit, que al iniciar inspeccionaba los módulos de `transformers` y llenaba la consola de avisos.

## 4. Demostración funcional

Recorrido de extremo a extremo con los ejemplos de la barra lateral. Las capturas están en [`docs/img/`](img/) y la evidencia textual de los mismos comportamientos, en [`results/resultados_agente.md`](../results/resultados_agente.md).

| # | Ejemplo | Qué demuestra | Herramientas |
|---|---|---|---|
| 1 | ✅ Devolución elegible | Flujo completo exitoso: verificar → generar → RMA, guía y pasos | verificar, generar |
| 2 | ⛔ No devolvible | Decide **no** actuar: explica la regla §3 y ofrece una alternativa | verificar |
| 3 | ❓ Faltan datos → "la botella del ECO-10001, ya no la necesito" | Responde **sin herramientas** pidiendo datos; la ruta pegajosa devuelve el segundo mensaje al agente, que **reutiliza** la etiqueta del ejemplo 1 (idempotencia) | ninguna → verificar, generar |
| 4 | Interruptor de caída + 📸 Dañado con foto | Manejo de errores: la transportadora falla y el agente lo explica sin inventar una guía | verificar, generar (⚠️) |
| 5 | 📖 Pregunta de política | El router la envía al RAG del Taller 2, no al agente | — (ruta RAG) |
| 6 | 🛡️ Intento de manipulación | La inyección no produce etiqueta. Según cómo la clasifique el router, la rechaza el agente o el RAG v2 | ninguna (o generar → NO_ELEGIBLE) |
| 7 | Panel 📈 Monitoreo | Las acciones quedaron registradas, con métricas y alertas | — |

### 4.1 Evidencia de la prueba en el navegador

La aplicación se probó de extremo a extremo en Chrome. Las capturas son de esa sesión:

| Captura | Qué muestra |
|---|---|
| [`01_ruta_pegajosa_y_etiqueta.jpg`](img/01_ruta_pegajosa_y_etiqueta.jpg) | Ejemplo 3. Primer turno: el agente pide los datos sin herramientas. Segundo turno: vuelve al agente (ruta pegajosa), que usa `buscar_pedido → verificar → generar` y entrega RMA, guía, enlace y pasos. La ruta indica que respondió el **modelo de respaldo**, porque el principal había agotado su cuota diaria |
| [`02_herramientas_usadas.jpg`](img/02_herramientas_usadas.jpg) | Ejemplo 1 con el panel "🔧 Herramientas usadas" abierto: argumentos y latencia de cada llamada |
| [`03_falla_transportadora.jpg`](img/03_falla_transportadora.jpg) | Ejemplo 4: el interruptor de caída está activo; el agente explica que Mensajería Andina no responde y ofrece reintentar o un ticket, sin inventar una guía |
| [`04_inyeccion_y_monitoreo.jpg`](img/04_inyeccion_y_monitoreo.jpg) | Ejemplo 6 (inyección rechazada) y el panel de monitoreo: turnos, etiquetas emitidas, no elegibles y escalamientos |
| [`hallazgo_etiqueta_inventada.png`](img/hallazgo_etiqueta_inventada.png) | El problema encontrado en esta prueba antes de agregar el validador de salida: una etiqueta inventada por el modelo ([Fase 3, R4](fase3_analisis_critico.md#r4-datos-alucinados-presentados-como-reales)) |

La prueba en el navegador encontró tres problemas que la evaluación automática no mostró: la etiqueta inventada, la ruta pegajosa que se perdía cuando el agente no terminaba con "?" y la cuota diaria agotada. También encontró dos detalles de la interfaz: el aviso inicial seguía visible mientras se procesaba el primer mensaje, y el modelo de embeddings se cargaba con el primer mensaje en vez de al abrir la página. Todos se corrigieron ([Fase 2 §5.4](fase2_implementacion.md#54-prueba-en-la-interfaz-dos-hallazgos-que-la-evaluación-no-mostró)).

## 5. Alcance del despliegue

Es un **despliegue local** (`localhost`), suficiente para la demostración funcional que pide el enunciado. Para publicarlo:

- **Streamlit Community Cloud:** se conecta el repositorio de GitHub, se configuran las claves en *Secrets* y `chroma_db/` se construye al arrancar (unos 54 fragmentos, menos de un minuto). La descarga de PyTorch y del modelo de embeddings hace lenta la primera carga.
- **Producción:** el interruptor de falla simulada y el panel de monitoreo no deben estar visibles para los clientes. El panel pasaría a una vista interna. Además, `SIMULAR_FALLA_TRANSPORTADORA` hoy es una variable de entorno del proceso, así que afectaría a todas las sesiones a la vez. También haría falta autenticación de clientes (Fase 3, R3).
