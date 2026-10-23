# Sdd-Orchestrator-Telegram-Bot

Telegram bridge for OpenCode CLI. Ejecuta prompts de desarrollo contra múltiples proveedores de IA (DeepSeek, MiniMax) directamente desde una conversación de Telegram, con persistencia de sesiones, internacionalización centralizada y arranque desatendido vía `/update`.

## Descripción

`Sdd-Orchestrator-Telegram-Bot` es una aplicación Python que conecta Telegram con el ejecutable de OpenCode CLI para habilitar desarrollo asistido por IA desde dispositivos móviles. El bot recibe mensajes de texto o voz en un chat autorizado, los reenvía al backend de IA configurado y devuelve la respuesta formateada a Telegram.

El proyecto existe para提供一个 punto de acceso móvil a flujos de trabajo de Spec-Driven Development. En lugar de depender de una terminal local, el usuario envía prompts desde el teléfono, gestiona sesiones por chat, cambia de modelo, crea Pull Requests y actualiza el bot sin abrir una laptop.

La arquitectura sigue una separación clara por capas: handlers de comandos, servicios con inyección de dependencias, y adaptadores intercambiables. El protocolo `AIBackend` abstrae OpenCode CLI, mientras `BotPort` abstrae `python-telegram-bot`. Esto permite sustituir el backend de IA o la plataforma de mensajería sin modificar la lógica de negocio.

## Características

- Múltiples sesiones por chat, con persistencia en `sessions.json` y cambio explícito entre sesiones activas
- Multi-proveedor mediante `AIProviderFactory` (DeepSeek `pro`/`flash`, MiniMax `m3`/`m27`/`m27-fast`)
- Comandos de CI/CD desde Telegram: `/pr` crea un Pull Request con `gh`, `/update` reinicia el bot con la última versión
- i18n centralizada en `locales/es.py`, accesible vía `get_strings()`
- `SessionStore` thread-safe como única fuente de verdad para el estado
- Auto-restart con `launcher.bat` y código de salida `42` para `/update`
- Protocolos abstractos (`AIBackend`, `BotPort`) que desacoplan la integración
- Inyección de dependencias a través de `AppContainer`; cero variables globales mutables
- Calidad de código con `ruff`, `mypy --strict` y `pytest`

## Requisitos

- **Python 3.11 o superior**
- **Node.js** y **OpenCode CLI** instalados y disponibles en `PATH`
- **Token de Telegram Bot** obtenido desde [@BotFather](https://t.me/BotFather)
- **(Opcional) `gh` CLI** autenticado, requerido para el comando `/pr`
- **(Opcional) `git`**, requerido para el flujo de auto-restart con `launcher.bat`
- **(Opcional) Clave de API de OpenAI**, necesaria únicamente para transcripción de notas de voz

## Instalación

### 1. Clonar el repositorio

```bash
git clone https://github.com/<owner>/Sdd-Orchestrator-Telegram-Bot.git
cd Sdd-Orchestrator-Telegram-Bot
```

### 2. Crear y activar un entorno virtual

```bash
python -m venv .venv
```

En Windows (PowerShell):

```powershell
.\.venv\Scripts\Activate.ps1
```

En Linux o macOS:

```bash
source .venv/bin/activate
```

### 3. Instalar dependencias

```bash
pip install -e ".[dev]"
```

Este comando instala las dependencias de runtime y de desarrollo (ruff, mypy, pytest, pytest-asyncio).

### 4. Instalar OpenCode CLI

```bash
npm install -g @anthropic/opencode
```

Verifica la instalación con `opencode --version`.

### 5. Crear el bot en Telegram

Habla con [@BotFather](https://t.me/BotFather), ejecuta `/newbot` y guarda el token entregado.

### 6. Obtener el identificador del chat

Ejecuta el script auxiliar y envía cualquier mensaje al bot desde el chat que se desea autorizar:

```bash
python get_chat_id.py
```

El script imprime el `chat_id` que debe agregarse a la variable `ALLOWED_CHAT_IDS`.

### 7. Configurar variables de entorno

Crea un archivo `.env` en la raíz del proyecto (ver la sección [Configuración](#configuración) para más detalle).

### 8. Iniciar el bot

Con auto-restart y `git pull` automático (recomendado en producción):

```powershell
.\launcher.bat
```

Ejecución directa sin babysitter:

```bash
python -m bot
```

## Configuración

La plantilla completa, comentada y con los valores por defecto es [`.env.example`](.env.example). Cópiala a `.env` y completa los valores:

```powershell
Copy-Item .env.example .env
```

`config.py` carga `.env` al importarse, pero importar el módulo no arranca nada ni valida el servidor: las comprobaciones viven en el arranque real.

| Variable | Obligatoria | Default | Notas |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Sí | — | Token de @BotFather |
| `ALLOWED_CHAT_IDS` | Sí | — | Lista CSV; una lista vacía es fatal |
| `OPENCODE_BACKEND` | No | `server` | `server` o `cli` (rollback) |
| `OPENCODE_SERVER_PASSWORD` | Sí en modo `server` | — | Basic auth; nunca se registra |
| `OPENCODE_TIMEOUT` | No | `300` | Timeout del prompt; se suspende con un permiso pendiente |
| `OPENCODE_PERMISSION_TIMEOUT` | No | `0` | `0` = esperar al humano sin límite |

El resto (`OPENCODE_WORKDIR`, `OPENCODE_CMD`, `OPENCODE_SERVER_HOST`, `OPENCODE_SERVER_PORT`, `OPENCODE_SERVER_BINARY`, `OPENCODE_SERVER_ALLOW_REMOTE`, `OPENAI_API_KEY`, `BOT_LOG_FORMAT`, `BOT_LOG_FILE`, `EXPLORE_DB`) está documentado en `.env.example`.

Además, la configuración puede ajustarse por chat mediante el comando `/config` sin tocar `.env`.

### Modo servidor vs. modo CLI

- **`server` (default):** el bot lanza o adopta un proceso `opencode serve` y habla con él por HTTP/SSE. Es el único modo con **permisos interactivos** (los diálogos llegan a Telegram o a la GUI) y **streaming en vivo** del progreso.
- **`cli` (rollback):** el bot ejecuta `opencode run --format json` por prompt. No necesita `OPENCODE_SERVER_PASSWORD` ni ofrece diálogos: OpenCode auto-rechaza los permisos en modo no interactivo. Úsalo si el servidor no está disponible, no si necesitas aprobar acciones.

El servidor escucha solo en loopback (`127.0.0.1`, `::1`, `localhost`) porque el bot es su único cliente. Un host distinto falla al arrancar salvo `OPENCODE_SERVER_ALLOW_REMOTE=1`, que existe únicamente para cuando el bot y el agente viven en máquinas diferentes; nunca para clientes como el APK.

### Permisos

La política vive en la configuración del agente de OpenCode (`opencode.json`): cada clave (`bash`, `edit`, `read`, `external_directory`, …) se resuelve a `allow`, `ask` o `deny`. La última regla que coincide gana, así que pon los comodines primero y las excepciones después.

- Un `ask` se convierte en un diálogo que el bot reenvía a Telegram (botones inline) o a la GUI de Flet.
- `external_directory` controla cualquier lectura o escritura fuera del worktree; ponlo en `ask` si quieres aprobar esos accesos uno por uno.
- `tools` está deprecado: `tools: { "bash": true }` equivale a `{"*": "allow"}` y **omite el diálogo**. Prefiere `permission` para conservar las aprobaciones.
- `OPENCODE_PERMISSION_TIMEOUT=0` (default) espera al humano indefinidamente, incluso horas. Un valor `> 0` auto-rechaza tras N segundos como red de seguridad. Mientras hay un diálogo abierto, `OPENCODE_TIMEOUT` se suspende: la espera humana no consume el presupuesto del prompt.

### GUI Flet

```bash
pip install -e ".[gui]"
python -m flet_app.main
```

La GUI comparte el mismo servidor que el bot: si ya hay uno sano lo adopta, y al cerrarse no mata un proceso que no lanzó.

### Solución de problemas

- **La respuesta nunca llega / el stream SSE queda mudo:** falta o es incorrecto el `?directory=` de la conversación. Un directorio equivocado conecta el stream sin error HTTP pero no emite eventos; verifica `OPENCODE_WORKDIR` y el workdir del chat (`/config workdir`).
- **El bot sale al arrancar con un mensaje sobre `OPENCODE_SERVER_PASSWORD`:** el modo `server` exige Basic auth. Define la variable (`openssl rand -hex 16`) o arranca con `OPENCODE_BACKEND=cli`.
- **«OPENCODE_SERVER_HOST no es una dirección loopback»:** el servidor solo acepta loopback. Usa `127.0.0.1`, `::1` o `localhost`; si de verdad el agente vive en otro host, define `OPENCODE_SERVER_ALLOW_REMOTE=1` y protégelo con TLS y firewall.
- **¿Quién es dueño del servidor?** El bot adopta un servidor sano y solo detiene el que él mismo lanzó. Bot y GUI pueden compartirlo.


## Uso

Una vez iniciado, el bot responde en los chats autorizados. Ejemplos de uso habitual:

```text
/start
/help
/status

/model pro
/model m3

/session new mi_feature
/session list
/session switch mi_feature
/session delete vieja

/open "explica este código"

/pr "Fix: corrige timeout en Windows"

/update
```

Cualquier mensaje de texto que no comience con `/` se interpreta como un prompt directo y se envía al backend configurado en la sesión activa.

## Arquitectura
El proyecto sigue una arquitectura por capas con inyección de dependencias. `bot.py` actúa como punto de entrada y construye un `AppContainer` que provee los servicios a los handlers mediante `context.bot_data`.

```text
bot.py → AppContainer (DI)
           ├── AIProviderFactory → OpenCodeCLIBackend (AIBackend Protocol)
           ├── TelegramAdapter (BotPort Protocol)
           ├── SessionStore → sessions.json
           ├── MessageSender (mensajería unificada)
           └── PromptService (orquestación)

handlers → container.prompt_service.execute()
          → locales.get_strings() para i18n
          → @authorized decorator para autenticación
```

Componentes principales:

| Capa | Archivo | Responsabilidad |
|---|---|---|
| Presentación | `handlers/` | Comandos de Telegram, autorización, parsing de argumentos |
| Servicios | `services/container.py`, `services/prompt_service.py` | Orquestación y composición de dependencias |
| Datos | `services/session_store.py` | Persistencia thread-safe de sesiones en `sessions.json` |
| Adaptadores | `services/opencode_cli_backend.py`, `services/telegram_adapter.py` | Implementaciones concretas detrás de los protocolos |
| Formato | `formatting/markdown.py` | Limpieza y troceo de la salida para Telegram |
| i18n | `locales/es.py` | Strings de usuario centralizadas |

Para una descripción detallada, consultar [DOCUMENTACION.md](DOCUMENTACION.md).

## Comandos

| Comando | Descripción |
|---|---|
| `/start` | Mensaje de bienvenida |
| `/help` | Lista de comandos disponibles |
| `/status` | Estado de la sesión, modelo, uptime |
| `/new` | Reinicia la sesión activa |
| `/model <alias>` | Cambia el modelo (por ejemplo `pro`, `flash`, `m3`, `m27`) |
| `/config` | Ajusta modelo, timeout, workdir y provider por chat |
| `/cancel` | Cancela el prompt en ejecución |
| `/open <prompt>` | Envía un prompt explícito al backend |
| `/session new <nombre>` | Crea una sesión nombrada |
| `/session list` | Lista las sesiones del chat |
| `/session switch <nombre>` | Cambia la sesión activa |
| `/session delete <nombre>` | Elimina una sesión |
| `/session info [nombre]` | Muestra detalles de una sesión |
| `/session discover` | Descubre sesiones existentes en OpenCode |
| `/session adopt <id> <nombre>` | Adopta una sesión por identificador real |
| `/pr <título>` | Crea un Pull Request en GitHub desde Telegram |
| `/update` | Reinicia el bot tras `git pull` |
| _(texto libre)_ | Prompt directo al backend en la sesión activa |

## Testing y calidad

```bash
# Ejecutar la suite de pruebas
pytest

# Linting
ruff check .

# Type checking en modo strict
mypy .

# Pre-commit hooks (ruff, mypy, formateo)
pre-commit run --all-files
```

El repositorio incluye configuración para `ruff` (reglas `E, F, I, N, W, UP, B, C4, SIM, T20`), `mypy --strict` y `pytest` con modo asyncio automático. CI ejecuta los tres pasos en cada push y Pull Request.

## CI/CD

El pipeline en `.github/workflows/ci.yml` corre `ruff`, `mypy --strict` y `pytest` sobre Python 3.11, 3.12 y 3.13 en cada push a `main` y en cada Pull Request.

Comandos equivalentes disponibles desde Telegram:

| Comando | Efecto |
|---|---|
| `/pr <título>` | Lee el `CHANGELOG.md` del workdir del chat y crea un PR con `gh pr create` |
| `/update` | Ejecuta `os._exit(42)`; `launcher.bat` detecta el código y corre `git pull` seguido de un reinicio limpio |
Flujo de auto-restart:

```text
Usuario → /update → bot ejecuta os._exit(42)
                          ↓
launcher.bat detecta el código de salida 42
                          ↓
git pull origin main
                          ↓
python -m bot (reinicio limpio)
```

## Internacionalización

Todos los strings visibles para el usuario final están centralizados en `locales/es.py` y se acceden mediante `get_strings()`:

```python
from locales import get_strings

strings = get_strings()
await update.message.reply_text(strings["welcome"])
```

Para agregar un nuevo idioma, crea un archivo `locales/<codigo>.py` con las mismas claves que `es.py` y ajusta el loader en `locales/__init__.py`.

## Contribuir

1. Realiza un fork del repositorio y crea una rama a partir de `main`.
2. Implementa los cambios siguiendo el estilo existente (ruff, mypy strict).
3. Añade o actualiza pruebas en `tests/` para cubrir el cambio.
4. Ejecuta localmente `ruff check .`, `mypy .` y `pytest` antes de abrir el Pull Request.
5. Actualiza la documentación (README, DOCUMENTACION.md) si la modificación afecta al uso o a la arquitectura.
6. Abre un Pull Request describiendo el cambio y referenciando el issue asociado, si aplica.

## Licencia

Este proyecto no incluye un archivo de licencia en el repositorio. Los términos de uso se acuerdan con el autor; contacta al mantenedor antes de redistribuir o utilizar el código en producción.
