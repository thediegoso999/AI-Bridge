# AI-Bridge Control Center

AI-Bridge es una base repo-first para preparar y revisar proyectos de desarrollo usando GitHub Codespaces, GitHub Copilot y GitHub Actions. El panel mantiene el workspace en solo lectura y V3 añade gestión local de tareas de IA con aprobación explícita para las llamadas a proveedores. No requiere dependencias de runtime ni envia credenciales al navegador.

## Requisitos

- Python 3.11 o posterior.
- GitHub Codespaces es opcional; el proyecto tambien funciona localmente.

## Inicio rapido

No hace falta instalar paquetes. Desde la raiz del repositorio:

```bash
python -m unittest discover -s tests -v
python -m aibridge inspect .
python -m aibridge dashboard
```

En un Codespace, el puerto 8000 se reenvia como **Private** y VS Code abre el panel. Localmente, abre `http://127.0.0.1:8000`. Al arrancar, copia el código de acceso de esa sesión desde la terminal del servidor e introdúcelo en Agent workspace. Deten el servidor con `Ctrl+C`.

El comando `inspect` informa el nombre y la ruta del proyecto, si hay README, configuracion de Codespaces y metadatos Python, ademas de los workflows y archivos de prueba encontrados. Acepta una ruta alternativa:

```bash
python -m aibridge inspect /ruta/al/repositorio
```

La inspeccion es de solo lectura y no realiza llamadas de red.

## Panel y GitHub

El panel muestra el snapshot real del workspace, navega y previsualiza archivos locales de solo lectura, y consulta hasta 30 repositorios accesibles y sus 20 ejecuciones recientes. Los logs se solicitan al abrir una ejecucion. Las rutas de archivo permanecen dentro del workspace; se bloquea traversal, enlaces simbolicos, directorios de dependencias y nombres de archivos habituales para secretos. Las vistas previas se limitan a texto UTF-8 de hasta 160 KB.

El backend usa el GitHub CLI (`gh`) para la autenticacion existente y llama endpoints de lectura; no obtiene ni devuelve tokens y no invoca operaciones de escritura. En un entorno sin sesion, el panel marca GitHub como desconectado y explica que no hay datos disponibles. Para autenticarte desde la terminal del Codespace puedes usar `gh auth login --hostname github.com`; la cuenta debe tener acceso de lectura al repositorio y a Actions. No pegues tokens en el panel ni en el codigo.

La seccion Agent workspace permite guardar instrucciones en un historial SQLite local, revisarlas y aprobar su envio a un proveedor. Guardar una tarea no hace llamadas externas; solo la accion explicita **Approve & send to provider** las inicia. Las respuestas son texto: no se ejecutan comandos ni se aplican cambios en archivos.

Los adaptadores oficiales disponibles son OpenAI Responses API (con un modelo Codex habilitado para la cuenta), DeepSeek Chat Completions y Anthropic Messages API. El proveedor, modelo y clave se configuran solo en el entorno del servidor (`AI_BRIDGE_PROVIDER`, `OPENAI_MODEL`/`DEEPSEEK_MODEL`/`ANTHROPIC_MODEL` y la clave correspondiente `OPENAI_API_KEY`/`DEEPSEEK_API_KEY`/`ANTHROPIC_API_KEY`). No hay campos de credenciales en el navegador. Las llamadas permanecen deshabilitadas hasta configurar ademas `AI_BRIDGE_ALLOW_PAID_API=true`; pueden generar cargos y cada tarea requiere aprobacion expresa. Copilot Free no se trata como API de inferencia. El proveedor simulado existe solo en pruebas automatizadas.

El historial se guarda en el directorio de estado del usuario (`%LOCALAPPDATA%/AI-Bridge` en Windows; `$XDG_STATE_HOME/ai-bridge` o `~/.local/state/ai-bridge` en Linux), o en `AI_BRIDGE_STATE_DIR` si se configura. La interfaz nunca ejecuta acciones de escritura de proveedores y solo acepta respuestas de texto. Las instrucciones que incluyan una clave API configurada se rechazan y esas claves se redactan de respuestas antes de guardarlas. Crear o aprobar tareas requiere el código de acceso de la sesión; la sesión expira a las ocho horas y se valida además el origen, el host y el token CSRF. En Codespaces solo se acepta el host HTTPS exacto derivado de `CODESPACE_NAME`, el puerto del dashboard y `GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN`, además de localhost y loopback. El puerto reenviado debe mantenerse como **Private**; no se admiten dominios públicos arbitrarios ni `AI_BRIDGE_ALLOWED_ORIGINS`. En Windows y otros entornos locales se admiten localhost y las direcciones loopback.

## SMART AI ROUTER — modo gratuito estricto

V3 incorpora un router con selección automática o manual, clasificación heurística (programación, razonamiento o general), preferencias locales, tiempos/éxitos observados y enfriamiento tras fallos transitorios. Para programación y razonamiento prioriza modelos cuya documentación oficial declara esas capacidades; esta compatibilidad no es una medición comparativa. Las prioridades explícitas del usuario y la salud observada también afectan al orden. Se registra la explicación de selección junto a cada intento. El modo `FREE_ONLY = true` es constante y rechaza modelos que no correspondan a una ruta gratuita admitida.

Los conectores son opcionales y usan endpoints oficiales:

| Proveedor | Ruta/modelo admitidos por defecto | Configuración del servidor |
|---|---|---|
| Gemini Developer API | `gemini-3.8-flash` | `GEMINI_API_KEY`, `AI_BRIDGE_GEMINI_FREE_TIER_CONFIRMED=true` |
| GroqCloud | `openai/gpt-oss-20b` o `openai/gpt-oss-120b` | `GROQ_API_KEY`, `AI_BRIDGE_GROQ_FREE_TIER_CONFIRMED=true`; el modelo puede fijarse con `AI_BRIDGE_GROQ_MODEL` |
| OpenRouter | `openrouter/free` o un ID que termine exactamente en `:free` | `OPENROUTER_API_KEY`; opcionalmente `AI_BRIDGE_OPENROUTER_MODEL` |
| Cloudflare Workers AI | `@cf/meta/llama-3.2-1b-instruct` | `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`, `AI_BRIDGE_CLOUDFLARE_FREE_PLAN_CONFIRMED=true` y `AI_BRIDGE_CLOUDFLARE_DAILY_NEURON_BUDGET` entre 1 y 8000 |

Las confirmaciones Gemini/Groq son declaraciones explícitas del operador: AI-Bridge no puede consultar el plan de una cuenta ni su cuota oficial en tiempo real. Cloudflare se mantiene desactivado sin confirmación explícita de Workers Free y un presupuesto local diario; el presupuesto es una estimación con margen, no detecta consumo compartido del resto de la cuenta. No lo habilites si no verificaste el plan y la cuota en la consola del proveedor. OpenRouter se limita a su router gratuito o modelos `:free`; nunca se configura una lista de fallback con modelos de pago. Si no se puede verificar la gratuidad o la configuración, el proveedor queda desactivado.

Los topes locales son hasta 20 intentos y 20 000 tokens **estimados** por proveedor y día, hasta 8 000 caracteres de instrucción, un worker/concurrencia y un máximo de tres proveedores por tarea, con un timeout total de 60 segundos. No son las cuotas oficiales de esos servicios; las cuotas exactas pueden variar por cuenta/modelo. AI-Bridge no consulta cuotas mediante llamadas de sondeo. Los fallos temporales (429, 5xx, timeout/conexión o modelo no disponible) generan un fallback acotado y un registro local; credenciales inválidas, solicitudes inválidas y rechazos de seguridad no se reintentan en otro proveedor. Una tarea fallida permanece en el historial y requiere nueva aprobación.

La aprobación muestra qué proveedores podrían recibir la instrucción y solicita autorización expresa. No se envían archivos del workspace ni se ejecutan comandos, herramientas o cambios de archivos. Las claves solo se leen de variables del entorno servidor y no se guardan en SQLite ni se entregan al navegador. No configures `AI_BRIDGE_ALLOW_PAID_API` para el Smart Router: Codex, DeepSeek y Claude quedan fuera del modo gratuito estricto porque su elegibilidad gratuita no se puede verificar aquí. No se realizan compras ni se habilita facturación automáticamente.

Fuentes oficiales consultadas para modelos, precios y rutas gratuitas: [Gemini models](https://ai.google.dev/gemini-api/docs/models), [Gemini pricing](https://ai.google.dev/gemini-api/docs/pricing), [Groq models](https://console.groq.com/docs/models), [Groq rate limits](https://console.groq.com/docs/rate-limits), [OpenRouter free variant](https://openrouter.ai/docs/guides/routing/model-variants/free), [OpenRouter FAQ](https://openrouter.ai/docs/faq), [Cloudflare Workers AI models](https://developers.cloudflare.com/workers-ai/models/) y [Cloudflare pricing](https://developers.cloudflare.com/workers-ai/platform/pricing/).

## Codespaces y Copilot

`.devcontainer/devcontainer.json` prepara Python 3.11 y ejecuta las pruebas al crear el entorno. Las extensiones de Copilot se ofrecen dentro de VS Code; su disponibilidad y cuotas dependen de la cuenta del usuario.

Las instrucciones de desarrollo para Copilot estan en `.github/copilot-instructions.md`. Copilot Free se usa como asistente de programacion en el editor, no como una API de inferencia que AI-Bridge invoque.

## Arquitectura

- `aibridge/inspection.py`: inspeccion local de solo lectura y modelo inmutable `RepositorySnapshot`.
- `aibridge/github.py`: adaptador de solo lectura al GitHub CLI autenticado por el usuario.
- `aibridge/dashboard.py`: servidor HTTP local, explorador confinado al workspace y API JSON.
- `aibridge/agents.py`: motor de tareas, aprobacion explicita e historial SQLite local.
- `aibridge/providers.py`: interfaz y adaptadores optativos de proveedores oficiales.
- `aibridge/router.py` y `aibridge/smart_router.py`: selección free-only, cola, fallback acotado, recuperación e historial de cuotas locales.
- `aibridge/web/`: interfaz web sin bibliotecas externas.
- `aibridge/__main__.py`: subcomandos `inspect` y `dashboard`.
- `tests/`: pruebas `unittest`, sin acceso a red.
- `.github/workflows/ci.yml`: comprobacion de sintaxis y ejecucion obligatoria de pruebas.
- `.devcontainer/`: entorno reproducible para Codespaces.

El adaptador GitHub usa `gh api` y `gh run view` en el servidor; el cliente web solo consume endpoints locales. Las operaciones del explorador de V2 siguen siendo de solo lectura. El panel escucha en loopback en ejecucion local y en el contenedor Codespaces para permitir el reenvio privado configurado en `.devcontainer/devcontainer.json`; no se publica ni se configura un deploy. Los endpoints de tareas usan validacion de origen y un token CSRF de doble envio.

## GitHub Actions

El workflow se ejecuta en `push`, `pull_request` y manualmente. Tiene permiso `contents: read`, un limite de cinco minutos y falla si falta `tests/` o no se descubre ninguna prueba. No despliega ni instala servicios.

## Alcance actual y siguiente version

Operativo: inspeccion local, salida JSON, Control Center, exploracion local, listado de repositorios autorizados, consulta de runs y logs de Actions cuando `gh` esta autenticado, historial/aprobacion asíncrona de tareas y Smart AI Router free-only. La conexión real requiere claves y comprobación del plan en cada proveedor; sin ellas, el dashboard muestra explícitamente que no hay una ruta gratuita lista.

Pendiente: despacho de workflows y ejecución autónoma de cambios. Las integraciones actuales solo devuelven texto, no ejecutan herramientas ni modifican archivos. Cualquier ampliación deberá usar APIs oficiales con configuración explícita; Copilot Free no se trata como API de inferencia.
