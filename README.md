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

El bot carga su configuración desde variables de entorno definidas en un archivo `.env`:

```bash
# Token entregado por @BotFather al crear el bot
TELEGRAM_BOT_TOKEN=123456789:ABCdefGHIjklMNOpqrsTUVwxyz

# Identificadores de chat autorizados, separados por coma.
# Cada chat_id puede usar su propio workdir, modelo y timeout.
ALLOWED_CHAT_IDS=123456789,-1001234567890

# Directorio de trabajo donde OpenCode CLI ejecutará los prompts.
# Puede ser sobreescrito por chat con /config.
OPENCODE_WORKDIR=C:/ruta/al/proyecto

# Timeout en segundos para la ejecución de un prompt.
OPENCODE_TIMEOUT=600

# Proveedor por defecto: opencode (único soportado actualmente).
AI_PROVIDER=opencode

# Clave de OpenAI para transcripción de notas de voz (opcional).
OPENAI_API_KEY=
```

Adicionalmente, la configuración puede ajustarse por chat mediante el comando `/config` sin necesidad de modificar el archivo `.env`.

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
