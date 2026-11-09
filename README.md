# AI-Bridge Control Center

AI-Bridge es una base repo-first para preparar y revisar proyectos de desarrollo usando GitHub Codespaces, GitHub Copilot y GitHub Actions. La V2 incorpora un panel web local de solo lectura para el workspace, repositorios y ejecuciones de Actions autorizados. No requiere dependencias de runtime ni envia credenciales al navegador.

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

En un Codespace, el puerto 8000 se reenvia como **Private** y VS Code abre el panel. Localmente, abre `http://127.0.0.1:8000`. Deten el servidor con `Ctrl+C`.

El comando `inspect` informa el nombre y la ruta del proyecto, si hay README, configuracion de Codespaces y metadatos Python, ademas de los workflows y archivos de prueba encontrados. Acepta una ruta alternativa:

```bash
python -m aibridge inspect /ruta/al/repositorio
```

La inspeccion es de solo lectura y no realiza llamadas de red.

## Panel y GitHub

El panel muestra el snapshot real del workspace, navega y previsualiza archivos locales de solo lectura, y consulta hasta 30 repositorios accesibles y sus 20 ejecuciones recientes. Los logs se solicitan al abrir una ejecucion. Las rutas de archivo permanecen dentro del workspace; se bloquea traversal, enlaces simbolicos, directorios de dependencias y nombres de archivos habituales para secretos. Las vistas previas se limitan a texto UTF-8 de hasta 160 KB.

El backend usa el GitHub CLI (`gh`) para la autenticacion existente y llama endpoints de lectura; no obtiene ni devuelve tokens y no invoca operaciones de escritura. En un entorno sin sesion, el panel marca GitHub como desconectado y explica que no hay datos disponibles. Para autenticarte desde la terminal del Codespace puedes usar `gh auth login --hostname github.com`; la cuenta debe tener acceso de lectura al repositorio y a Actions. No pegues tokens en el panel ni en el codigo.

La seccion Agent workspace informa que no hay proveedor conectado. No se simulan agentes ni se invoca Copilot Free como API.

## Codespaces y Copilot

`.devcontainer/devcontainer.json` prepara Python 3.11 y ejecuta las pruebas al crear el entorno. Las extensiones de Copilot se ofrecen dentro de VS Code; su disponibilidad y cuotas dependen de la cuenta del usuario.

Las instrucciones de desarrollo para Copilot estan en `.github/copilot-instructions.md`. Copilot Free se usa como asistente de programacion en el editor, no como una API de inferencia que AI-Bridge invoque.

## Arquitectura

- `aibridge/inspection.py`: inspeccion local de solo lectura y modelo inmutable `RepositorySnapshot`.
- `aibridge/github.py`: adaptador de solo lectura al GitHub CLI autenticado por el usuario.
- `aibridge/dashboard.py`: servidor HTTP local, explorador confinado al workspace y API JSON.
- `aibridge/web/`: interfaz web sin bibliotecas externas.
- `aibridge/__main__.py`: subcomandos `inspect` y `dashboard`.
- `tests/`: pruebas `unittest`, sin acceso a red.
- `.github/workflows/ci.yml`: comprobacion de sintaxis y ejecucion obligatoria de pruebas.
- `.devcontainer/`: entorno reproducible para Codespaces.

El adaptador GitHub usa `gh api` y `gh run view` en el servidor; el cliente web solo consume endpoints locales. Las operaciones de V2 son de lectura. El panel escucha en loopback en ejecucion local y en el contenedor Codespaces para permitir el reenvio privado configurado en `.devcontainer/devcontainer.json`; no se publica ni se configura un deploy.

## GitHub Actions

El workflow se ejecuta en `push`, `pull_request` y manualmente. Tiene permiso `contents: read`, un limite de cinco minutos y falla si falta `tests/` o no se descubre ninguna prueba. No despliega ni instala servicios.

## Alcance actual y siguiente version

Operativo: inspeccion local, salida JSON, Control Center, exploracion local, listado de repositorios autorizados, consulta de runs y logs de Actions cuando `gh` esta autenticado, entorno privado Codespaces e instrucciones Copilot.

Pendiente: despacho de workflows (no disponible en modo de solo lectura), persistencia, estado de tareas y cualquier runtime de agentes. Las futuras integraciones de modelos deberan usar APIs oficiales con configuracion explicita; Copilot Free no se trata como API de inferencia.
