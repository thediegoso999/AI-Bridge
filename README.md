# AI-Bridge

AI-Bridge es una base repo-first para preparar y revisar proyectos de desarrollo usando GitHub Codespaces, GitHub Copilot y GitHub Actions. La V1 inspecciona un repositorio local y ofrece un resumen JSON; no requiere servicios externos, credenciales ni dependencias de runtime.

## Requisitos

- Python 3.11 o posterior.
- GitHub Codespaces es opcional; el proyecto tambien funciona localmente.

## Inicio rapido

No hace falta instalar paquetes. Desde la raiz del repositorio:

```bash
python -m unittest discover -s tests -v
python -m aibridge inspect .
```

El comando `inspect` informa el nombre y la ruta del proyecto, si hay README, configuracion de Codespaces y metadatos Python, ademas de los workflows y archivos de prueba encontrados. Acepta una ruta alternativa:

```bash
python -m aibridge inspect /ruta/al/repositorio
```

La inspeccion es de solo lectura y no realiza llamadas de red.

## Codespaces y Copilot

`.devcontainer/devcontainer.json` prepara Python 3.11 y ejecuta las pruebas al crear el entorno. Las extensiones de Copilot se ofrecen dentro de VS Code; su disponibilidad y cuotas dependen de la cuenta del usuario.

Las instrucciones de desarrollo para Copilot estan en `.github/copilot-instructions.md`. Copilot Free se usa como asistente de programacion en el editor, no como una API de inferencia que AI-Bridge invoque.

## Arquitectura

- `aibridge/inspection.py`: inspeccion local de solo lectura y modelo inmutable `RepositorySnapshot`.
- `aibridge/__main__.py`: interfaz CLI que serializa el resultado como JSON.
- `tests/`: pruebas `unittest`, sin acceso a red.
- `.github/workflows/ci.yml`: comprobacion de sintaxis y ejecucion obligatoria de pruebas.
- `.devcontainer/`: entorno reproducible para Codespaces.

La frontera de inspeccion queda separada de la CLI para que futuras funciones puedan anadirse como casos de uso y adaptadores independientes. Las siguientes integraciones previstas son lectura de proyectos y ejecuciones de Actions mediante la API oficial de GitHub, ejecucion de workflows solo con permisos autorizados y una vista de tareas/estados. La integracion con modelos de IA sera otro adaptador y solo usara APIs oficiales configuradas expresamente.

## GitHub Actions

El workflow se ejecuta en `push`, `pull_request` y manualmente. Tiene permiso `contents: read`, un limite de cinco minutos y falla si falta `tests/` o no se descubre ninguna prueba. No despliega ni instala servicios.

## Alcance de V1

Operativo: inspeccion local de repositorios, salida JSON, entorno Codespaces, instrucciones Copilot y CI con pruebas obligatorias.

Pendiente: autenticacion GitHub, consulta de repositorios remotos y resultados de Actions, despacho autorizado de workflows, persistencia o interfaz web, y cualquier proveedor de modelos. Estas funciones requeriran decisiones explicitas de permisos, cuotas y almacenamiento; no se solicitan credenciales para usar la V1.
