# Sdd-Orchestrator-Telegram-Bot — Documentación Completa

---

## Índice / Tabla de Contenidos

1. [Visión General](#-visión-general)
2. [Arquitectura (Post-Refactor)](#-arquitectura-post-refactor)
3. [El Refactor de 5 Semanas](#-el-refactor-de-5-semanas)
4. [Estructura del Proyecto](#-estructura-del-proyecto)
5. [Capa de Servicios](#-capa-de-servicios)
6. [Handlers](#-handlers)
7. [Comandos del Bot](#-comandos-del-bot)
8. [Flujo de Trabajo SDD](#-flujo-de-trabajo-sdd)
9. [Sistema i18n (locales/)](#-sistema-i18n-locales)
10. [Formateo de Output](#-formateo-de-output)
11. [Mecanismo de Auto-Restart](#-mecanismo-de-auto-restart)
12. [CI/CD Pipeline](#-cicd-pipeline)
13. [Testing](#-testing)
14. [Configuración](#-configuración)
15. [Seguridad](#-seguridad)
16. [Problemas Conocidos y Soluciones](#-problemas-conocidos-y-soluciones)
17. [Guía de Instalación](#-guía-de-instalación)
18. [Evolución del Proyecto](#-evolución-del-proyecto)
19. [Roadmap / Mejoras Futuras](#-roadmap--mejoras-futuras)

---

## Visión General

### Qué es el proyecto

El **SDD Orchestrator Telegram Bot** es un bot de Telegram que actúa como puente entre un usuario móvil y **OpenCode CLI**, el orquestador SDD (Spec-Driven Development). Convierte Telegram en una terminal completa para ejecutar prompts de desarrollo con acceso a MCPs (Model Context Protocols): **Context7**, **Engram** y **Notion**.

Tras 5 semanas de refactor arquitectónico (v2.0), el código pasó de ser un monolito de ~1500 líneas con deuda técnica creciente a una arquitectura limpia con **inyección de dependencias**, **protocolos abstractos**, **i18n centralizado** y **0 variables globales mutables**.

### Caso de uso principal

El usuario es un **repartidor de Uber** (o cualquier desarrollador en movimiento) que necesita gestionar proyectos de software desde el celular:

- Ejecuta prompts SDD completos sin tocar una laptop
- Navega entre múltiples sesiones con `/session switch`
- Crea PRs en GitHub desde Telegram (`/pr`)
- Reinicia el bot remotamente (`/update`)
- Configura modelo, timeout y workdir por chat (`/config`)
- Consulta documentación vía Context7, persiste decisiones en Engram, lee Notion
- Todo con respuestas formateadas en MarkdownV2

### Problema que resuelve

OpenCode CLI solo se ejecuta en terminal de escritorio. Este bot expone **el 100% de la funcionalidad del orquestador SDD** a través de Telegram, permitiendo desarrollo remoto real desde cualquier lugar con señal móvil.

---

## Arquitectura (Post-Refactor)

### Diagrama de flujo

```
  Telegram App                         Windows PC (daemon)
  ─────────────                        ────────────────────
  ┌──────────┐     HTTP/API            ┌──────────────────────┐
  │ Usuario  │ ───────────────────────>│ python-telegram-bot  │
  │ (móvil)  │ <───────────────────────│ (v22+, asyncio)      │
  └──────────┘     Mensajes            └──────────┬───────────┘
                                                  │
                                   AppContainer (DI)
                                                  │
                          ┌───────────────────────┼───────────────────────┐
                          │                       │                       │
                   PromptService           SessionStore            MessageSender
                   (orquestación)          (sessions.json)         (unificado)
                          │
                  AIProviderFactory
                          │
                   OpenCodeCLIBackend
                   (AIBackend Protocol)
                          │
                 subprocess.Popen
                          │
                   OpenCode CLI ──── Context7, Engram, Notion
```

### Componentes principales

| Componente | Rol | Ubicación |
|---|---|---|
| **bot.py** | Punto de entrada, construye AppContainer, registra handlers | Raíz |
| **AppContainer** | Contenedor DI — mantiene referencias a todos los servicios | `services/container.py` |
| **SessionStore** | Persistencia thread-safe de sesiones en JSON | `services/session_store.py` |
| **MessageSender** | Envío unificado de mensajes (MarkdownV2 + fallback) | `services/message_sender.py` |
| **PromptService** | Orquestación de prompts: sesión, ejecución, limpieza, split | `services/prompt_service.py` |
| **AIBackend (Protocol)** | Interfaz abstracta para backends de IA | `services/ai_backend.py` |
| **BotPort (Protocol)** | Interfaz abstracta para bots de mensajería | `services/bot_port.py` |
| **AIProviderFactory** | Registry de backends (actualmente: opencode) | `services/ai_provider_factory.py` |
| **OpenCodeCLIBackend** | Implementación real vía subprocess | `services/opencode_cli_backend.py` |
| **TelegramAdapter** | Implementación real de BotPort para Telegram | `services/telegram_adapter.py` |
| **locales/** | Sistema i18n — strings centralizadas | `locales/es.py` |
| **formatting/markdown.py** | Transformación pura de texto (sin I/O) | `formatting/` |
| **handlers/** | Handlers delgados — solo delegan a servicios | `handlers/` |

### El patrón de Inyección de Dependencias

```python
# En bot.py — run_bot():
container = AppContainer(
    session_store=session_store,
    message_sender=message_sender,
    prompt_service=prompt_service,
    provider_factory=provider_factory,
    bot_port=bot_port,
    start_time=start_time,
    allowed_chat_ids=ALLOWED_CHAT_IDS,
    default_model=DEFAULT_MODEL,
)
app.bot_data["container"] = container

# En handlers — acceso limpio:
container = _get_container(context)
await container.prompt_service.execute(update, context, text)
```

**Beneficios:**
- **0 imports circulares** — antes había 16 `import bot` dispersos
- **Testeable** — cada servicio se puede mockear independientemente
- **Reemplazable** — cambiá OpenCode por otra IA, o Telegram por Discord, implementando el Protocol

### Protocolos (interfaces abstractas)

#### AIBackend Protocol
```python
class AIBackend(Protocol):
    async def execute(self, prompt: str, *, model: str, session_id: str | None,
                      workdir: str, timeout: int, chat_id: int) -> AIResult: ...
    async def cancel(self, chat_id: int) -> bool: ...
```

**Vendor lock anterior:** `bot.py` llamaba directamente a `subprocess.Popen`. Ahora, `AIBackend` es una interfaz. Si mañana querés usar la API de OpenAI en vez de OpenCode CLI, implementás el protocolo y lo registrás en el factory. El resto del código no se entera.

#### BotPort Protocol
```python
class BotPort(Protocol):
    async def send_message(self, chat_id: int, text: str, *,
                           parse_mode: str | None = None) -> Message: ...
    async def edit_message_text(self, chat_id: int, message_id: int,
                                text: str) -> None: ...
    async def send_chat_action(self, chat_id: int, action: str) -> None: ...
```

**Vendor lock anterior:** `context.bot.send_message()` directo. Ahora, `MessageSender` usa `BotPort`. Si quisieras migrar a Discord, implementás `DiscordAdapter` y el resto del código sigue igual.

---

## El Refactor de 5 Semanas

### Semana 1: Extracción de servicios
- `SessionStore` extraído de `bot.py` — thread-safe con `threading.Lock`
- `MessageSender` unifica 3 patrones de envío en 1
- `PromptService` absorbe `_process_prompt` (273 líneas → método enfocado)

### Semana 2: Protocolos y DI
- `AIBackend` Protocol rompe vendor lock a `subprocess`
- `BotPort` Protocol rompe vendor lock a `telegram.Bot`
- `AppContainer` como contenedor DI central
- `AIProviderFactory` como registry de backends

### Semana 3: i18n y handlers
- `locales/es.py` — 93 strings hardcodeadas extraídas y centralizadas
- `locales/__init__.py` — loader con fallback a español
- Handlers refactorizados: adelgazados a simples funciones que delegan
- `@authorized` decorator para auth en todos los handlers

### Semana 4: Nuevos comandos y CI/CD
- `/pr` — crea PRs en GitHub desde Telegram
- `/update` — auto-restart con `os._exit(42)` + `launcher.bat`
- `/config` — configuración por chat (modelo, timeout, workdir, provider)
- `.github/workflows/ci.yml` — ruff + mypy + pytest
- `.pre-commit-config.yaml` — hooks locales

### Semana 5: Testing, tooling y pulido
- `pyproject.toml` — ruff, mypy strict, pytest config
- `test_session_store.py` — 6 tests nuevos para SessionStore
- `sessions.example.json` — template para nuevos contribuidores
- `.gitattributes` — LF/CRLF consistency
- `launcher.bat` — babysitter con git pull + auto-restart
- Health Score de 5.5 → portfolio-ready

### Antes vs Después

| Métrica | Antes (v1.8.1) | Después (v2.0) |
|---|---|---|
| Archivos Python | ~3 | 18+ |
| Líneas en bot.py | ~1500 | ~398 |
| Dicts globales mutables | 5 | 0 |
| `import bot` circulares | 16 | 0 |
| Patrones de envío de mensajes | 3 | 1 (MessageSender) |
| Strings hardcodeadas | ~93 | 0 (i18n) |
| Vendor locks | 2 (subprocess, telegram.Bot) | 0 (Protocols) |
| Tests | 25 | 31 |
| mypy strict | ❌ | ✅ |
| CI/CD | ❌ | ✅ (GitHub Actions) |

---

## Estructura del Proyecto

```
Sdd-Orchestrator-Telegram-Bot/
├── bot.py                      # Punto de entrada (~398 líneas, era ~1500)
├── config.py                   # Configuración, constantes, logger
├── pyproject.toml              # ruff, mypy strict, pytest
├── requirements.txt            # Dependencias Python
├── launcher.bat                # Babysitter: git pull + auto-restart en /update
├── run_bot.bat                 # Launcher simple (legacy)
├── get_chat_id.py              # Utilidad para obtener chat ID
├── sessions.json               # Persistencia de sesiones (fuente de verdad)
├── sessions.example.json       # Template para nuevos contribuidores
│
├── services/                   # Capa de servicios (DI)
│   ├── __init__.py
│   ├── container.py            # AppContainer
│   ├── session_store.py        # SessionStore (thread-safe JSON)
│   ├── message_sender.py       # MessageSender (MarkdownV2 + fallback)
│   ├── prompt_service.py       # PromptService (orquestación)
│   ├── ai_backend.py           # AIBackend Protocol
│   ├── bot_port.py             # BotPort Protocol
│   ├── ai_provider_factory.py  # AIProviderFactory (registry)
│   ├── opencode_cli_backend.py # OpenCodeCLIBackend
│   └── telegram_adapter.py     # TelegramAdapter
│
├── handlers/                   # Handlers delgados
│   ├── __init__.py
│   ├── commands.py             # /start, /help, /status, /new, /model, /cancel, /open, /config
│   ├── sessions.py             # /session new|list|switch|delete|info|discover|adopt
│   ├── messages.py             # Mensajes de texto y voz
│   ├── admin.py                # /test_md, /session_preview
│   └── ci.py                   # /pr, /update
│
├── locales/                    # i18n
│   ├── __init__.py             # get_strings() loader
│   └── es.py                   # Strings en español
│
├── formatting/                 # Transformación de texto (sin I/O)
│   ├── __init__.py
│   └── markdown.py             # clean, telegramify, split, tool traces
│
├── utils/                      # Utilidades puras
│   ├── __init__.py
│   ├── logging.py              # Configuración de logging
│   └── time_formatting.py      # relative_time
│
├── tests/                      # Tests (31 tests)
│   ├── __init__.py
│   ├── conftest.py
│   ├── test_session_store.py   # 6 tests de SessionStore
│   ├── test_session_parse.py   # Tests de parseo
│   ├── test_persistence.py     # Tests de persistencia
│   └── test_utils.py           # Tests de utilidades
│
├── persistence/                # Acceso a OpenCode (legacy bridge)
│   └── sessions.py             # fetch_opencode_sessions
│
├── opencode/                   # Cliente de BD OpenCode (legacy bridge)
│   └── client.py               # query_opencode_db
│
├── .github/workflows/ci.yml    # CI/CD
├── .pre-commit-config.yaml     # Hooks pre-commit
└── .gitattributes              # LF/CRLF consistency
```

---

## Capa de Servicios

### SessionStore (`services/session_store.py`)

**Antes:** `active_sessions` (dict global), `load_session_map()`, `save_session_map()` — funciones sueltas en `bot.py` accediendo a estado global.

**Ahora:** Clase thread-safe con `threading.Lock`:

```python
class SessionStore:
    def __init__(self, path: str) -> None: ...
    def get_active(self, chat_id: int) -> str: ...
    def get_session(self, chat_id: int, name: str) -> dict | None: ...
    def get_id(self, chat_id: int, name: str) -> str | None: ...
    def set_id(self, chat_id: int, name: str, session_id: str | None) -> None: ...
    def increment_prompt_count(self, chat_id: int, name: str) -> int: ...
    def list_sessions(self, chat_id: int) -> dict: ...
    def create_session(self, chat_id: int, name: str) -> bool: ...
    def delete_session(self, chat_id: int, name: str) -> bool: ...
    def switch_session(self, chat_id: int, name: str) -> bool: ...
    def clear_id(self, chat_id: int, name: str) -> None: ...
```

Todas las operaciones de lectura/escritura a `sessions.json` pasan por esta clase. El lock previene condiciones de carrera entre handlers concurrentes.

### MessageSender (`services/message_sender.py`)

**Antes:** 3 patrones distintos para enviar mensajes:
1. `context.bot.send_message(chat_id, text, parse_mode=ParseMode.MARKDOWN_V2)` directo
2. Try/except duplicado en cada handler
3. Lógica de split y MarkdownV2 fallback repetida

**Ahora:** Un solo punto de entrada:

```python
class MessageSender:
    async def send(self, chat_id: int, text: str, *,
                   parse_mode: str = "MarkdownV2") -> list[Message]: ...
    async def send_plain(self, chat_id: int, text: str) -> list[Message]: ...
    async def edit(self, chat_id: int, message_id: int, text: str) -> None: ...
```

- `send()`: aplica `telegramify_markdown()`, `split_message()`, intenta MarkdownV2, fallback a texto plano
- `send_plain()`: envía sin formato (para mensajes de sistema)
- `edit()`: edita mensaje existente (usado por el contador de progreso)

### PromptService (`services/prompt_service.py`)

**Antes:** `_process_prompt()` — 273 líneas en `bot.py` haciendo todo: sesiones, ejecución, limpieza, split, envío, progreso.

**Ahora:** Clase enfocada con dependencias inyectadas:

```python
class PromptService:
    def __init__(self, session_store: SessionStore,
                 message_sender: MessageSender,
                 provider_factory: AIProviderFactory) -> None: ...
    
    async def execute(self, update: Update, context: ContextTypes.DEFAULT_TYPE,
                      text: str) -> None: ...
```

Flujo interno:
1. Verifica autorización
2. Lee sesión activa del `SessionStore`
3. Construye comando (modelo, session_id, --continue)
4. Envía mensaje "Procesando..." + lanza progress_updater
5. Ejecuta vía `AIBackend.execute()`
6. Captura ID real en primera ejecución
7. Limpia output (`clean_opencode_output`)
8. Convierte tablas (`telegramify_markdown`)
9. Parte en chunks (`split_message`)
10. Envía con MarkdownV2 + fallback
11. Actualiza prompt_count

### AIBackend Protocol y OpenCodeCLIBackend

```python
# services/ai_backend.py — Interfaz abstracta
class AIBackend(Protocol):
    async def execute(self, prompt: str, *, model: str, session_id: str | None,
                      workdir: str, timeout: int, chat_id: int) -> AIResult: ...
    async def cancel(self, chat_id: int) -> bool: ...

# services/opencode_cli_backend.py — Implementación real
class OpenCodeCLIBackend:
    def __init__(self, opencode_cmd: str, workdir: str, timeout: int) -> None: ...
    async def execute(self, prompt: str, *, model: str, ...) -> AIResult: ...
    async def cancel(self, chat_id: int) -> bool: ...
```

`AIResult` es un dataclass con `stdout`, `stderr`, `exit_code`, `timed_out`.

El `AIProviderFactory` mantiene un registry de backends por nombre:

```python
factory = AIProviderFactory(default_provider="opencode")
factory.register("opencode", OpenCodeCLIBackend)
backend = factory.get("opencode", opencode_cmd="...", workdir="...", timeout=600)
```

---

## Handlers

Todos los handlers son **funciones delgadas** que obtienen el container del context y delegan:

```python
# handlers/commands.py
async def model_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    container = _get_container(context)
    # lógica mínima, delega a servicios
    ...

# handlers/ci.py
async def pr_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    container = _get_container(context)
    # crea PR vía gh CLI
    ...

# handlers/messages.py — el handler principal
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    container = _get_container(context)
    text = update.message.text.strip()
    await container.prompt_service.execute(update, context, text)
```

El decorador `@authorized` (definido en `handlers/__init__.py`) verifica el chat_id contra la whitelist antes de ejecutar cualquier handler.

### Registro de handlers (`bot.py:build_application()`)

```python
application.add_handler(CommandHandler("start", start_command))
application.add_handler(CommandHandler("help", help_command))
application.add_handler(CommandHandler("status", status_command))
application.add_handler(CommandHandler("new", new_command))
application.add_handler(CommandHandler("model", model_command))
application.add_handler(CommandHandler("config", config_command))
application.add_handler(CommandHandler("cancel", cancel_command))
application.add_handler(CommandHandler("session", session_command))
application.add_handler(CommandHandler("open", open_command))
application.add_handler(CommandHandler("pr", pr_command))
application.add_handler(CommandHandler("update", update_command))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
application.add_handler(MessageHandler(filters.VOICE, handle_voice))
```

---

## Comandos del Bot

### Tabla completa

| Comando | Handler | Descripción |
|---|---|---|
| `/start` | `commands.py` | Inicia el bot, mensaje de bienvenida |
| `/help` | `commands.py` | Lista todos los comandos |
| `/status` | `commands.py` | Estado: sesión, modelo, uptime, prompt count |
| `/new` | `commands.py` | Reinicia sesión activa (limpia ID) |
| `/model pro\|flash` | `commands.py` | Cambia modelo deepseek-v4-pro o flash |
| `/config` | `commands.py` | Configuración por chat (modelo, timeout, workdir, provider) |
| `/cancel` | `commands.py` | Cancela prompt en ejecución |
| `/open <prompt>` | `commands.py` | Prompt explícito |
| `/session new\|list\|switch\|delete\|info\|discover\|adopt` | `sessions.py` | Gestión multi-sesión |
| `/session_preview` | `admin.py` | Atajo de diagnóstico |
| `/test_md` | `admin.py` | Test de MarkdownV2 |
| `/pr <título>` | `ci.py` | Crea PR en GitHub desde Telegram |
| `/update` | `ci.py` | Auto-restart del bot |
| _(texto libre)_ | `messages.py` | Prompt directo al orquestador |

### `/config` — Configuración por chat

Permite ver y modificar configuración sin tocar `.env`:

```
/config                          → Muestra la configuración actual
/config model pro                → Cambia a deepseek-v4-pro
/config model flash              → Cambia a deepseek-v4-flash
/config timeout 900              → Timeout de 15 minutos
/config workdir C:\proyecto      → Cambia el workdir
/config provider opencode        → Selecciona el backend
```

La configuración persiste en `sessions.json` por chat_id.

### `/pr <título>` — Crear PR desde Telegram

Flujo:
1. Lee `CHANGELOG.md` del workdir del chat para usarlo como body del PR
2. Ejecuta `gh pr create --title "<título>" --body "<changelog>"`
3. Devuelve la URL del PR creado

Requiere que `gh` CLI esté instalado y autenticado en la máquina donde corre el bot.

### `/update` — Auto-restart

Flujo:
1. El handler envía mensaje de confirmación
2. Ejecuta `os._exit(42)` — código de salida especial
3. `launcher.bat` detecta el exit code 42
4. Ejecuta `git pull origin main`
5. Reinicia el bot con `python -m bot`

El bot vuelve online en ~10-15 segundos. Cero intervención manual.

---

## Flujo de Trabajo SDD

### Cómo se mantienen las sesiones

El sistema usa `SessionStore` (thread-safe) que persiste en `sessions.json`:

```json
{
  "8664220427": {
    "active": "bot_telegram",
    "sessions": {
      "bot_telegram": {
        "id": "ses_20477ac36ffec2aYyBiD359Zfp",
        "title": "OpenCode remoto con bot de Telegram",
        "created": "2026-05-14T06:39:51+00:00",
        "last_used": "2026-05-14T16:16:07+00:00",
        "prompt_count": 11
      }
    }
  }
}
```

### Continuidad entre mensajes

`PromptService.execute()` decide si continuar o iniciar nueva sesión:

1. Lee `sessions.json` vía `SessionStore` → obtiene nombre activo + ID real
2. ¿No hay ID real? → **NUEVA sesión** (`opencode run "<prompt>"`)
3. ¿Sesión expirada? (>30 min sin uso) → **NUEVA sesión**
4. Sesión activa con ID real → **CONTINUAR** (`opencode run --continue --session <id> "<prompt>"`)

### Captura de IDs reales

Cuando una sesión es nueva, el bridge **no inventa IDs**. En lugar de eso:

1. Ejecuta `opencode run "<prompt>"` sin flags `--continue --session`
2. OpenCode crea sesión internamente con ID real (`ses_xxx`)
3. Al terminar, el backend ejecuta `opencode session list` y captura el ID
4. Guarda el ID en `SessionStore` para futuros `--continue`

### Navegación multi-sesión

```
/session new api        → Crea sesión "api" (lazy, id=null)
/open crea endpoint     → Captura ID real, prompt #1 en "api"
/session switch docs    → Cambia a "docs"
/open documenta JWT     → Prompt en "docs"
/session switch api     → Vuelve a "api"
/status                 → Muestra prompt_count en "api"
```

---

## Sistema i18n (locales/)

**Antes:** ~93 strings en español hardcodeadas en `bot.py` y handlers. Ejemplo:

```python
await update.message.reply_text("❌ No autorizado. Este bot es privado.")
await update.message.reply_text("⏳ OpenCode procesando...")
```

**Ahora:** Todas las strings en `locales/es.py`:

```python
# locales/es.py
STRINGS = {
    "unauthorized": "❌ No autorizado. Este bot es privado, carnal.",
    "processing": "⏳ OpenCode procesando...",
    "completed": "✅ {name} Completado ({duration}s)",
    # ... ~90 strings más
}
```

Acceso vía `get_strings()`:

```python
# locales/__init__.py
def get_strings(lang: str = "es") -> dict:
    if lang == "es":
        from locales.es import STRINGS
        return STRINGS
    # fallback a español
    from locales.es import STRINGS
    return STRINGS
```

Para agregar inglés: creás `locales/en.py` con las mismas keys. El loader detecta el idioma automáticamente.

---

## Formateo de Output

El módulo `formatting/markdown.py` contiene **funciones puras** (sin I/O, sin dependencias externas):

### `clean_opencode_output(text: str) -> str`
- Elimina ANSI escape codes
- Elimina líneas de build (`> build · deepseek-v4-pro`)
- Elimina tool traces (con tracking de `{ }` para JSON multilínea)
- Elimina líneas de auto-rechazo de permisos
- Colapsa líneas vacías múltiples

### `telegramify_markdown(text: str) -> str`
- Convierte tablas `|col1|col2|` a bloques ``` monospace
- Porque MarkdownV2 de Telegram **no soporta tablas**
- Usa caracteres de caja (│) para simular bordes

### `split_message(text: str, limit: int = 4000) -> list[str]`
- Parte respuestas largas en chunks de 4000 caracteres
- Intenta cortar en: `\n\n` → `. ` → `\n` → espacio → corte forzado
- Numera partes: `(parte 1/3)`, `(parte 2/3)`, etc.

### `_remove_tool_traces(text: str) -> str`
- Tracking de llaves `{ }` para filtrar JSON multilínea de tool calls
- Detecta inicio por `⚙` (o su versión corrupta por cp1252: `âš™`)
- Acumula brace_depth hasta que vuelve a 0

---

## Mecanismo de Auto-Restart

### `launcher.bat` — El babysitter

```batch
@echo off
:loop
    python -m bot
    if %errorlevel% equ 42 (
        echo [UPDATE] Pulling latest changes...
        git pull origin main
        echo [UPDATE] Restarting...
        goto loop
    )
    echo Bot stopped with code %errorlevel%
    pause
```

### Flujo completo

```
1. Usuario envía /update desde Telegram
2. Handlers/ci.py → update_command()
3. Envía "🔄 Reiniciando..." al usuario
4. os._exit(42)
5. launcher.bat recibe exit code 42
6. git pull origin main
7. python -m bot (reinicio limpio)
8. Bot vuelve online en ~10-15 segundos
```

**¿Por qué exit code 42?** Es el "Answer to the Ultimate Question of Life, the Universe, and Everything" según Douglas Adams. Y porque ningún otro código de salida lo usa, así no hay ambigüedad.

---

## CI/CD Pipeline

### GitHub Actions (`.github/workflows/ci.yml`)

```yaml
on: [push, pull_request]
jobs:
  lint-and-test:
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
      - run: pip install -e ".[dev]"
      - run: ruff check .
      - run: mypy .
      - run: pytest
```

Cada push y PR ejecuta:
1. **ruff** — linting con reglas estrictas (E, F, I, N, W, UP, B, C4, SIM, T20)
2. **mypy strict** — type checking sin concesiones
3. **pytest** — 31 tests

### Pre-commit hooks (`.pre-commit-config.yaml`)

```yaml
repos:
  - repo: local
    hooks:
      - id: ruff
        name: ruff
        entry: ruff check . --fix
      - id: mypy
        name: mypy
        entry: mypy .
```

Se ejecutan antes de cada commit. Si algo falla, el commit se rechaza.

### Desde la calle (Telegram-based CI/CD)

| Comando | Qué hace |
|---|---|
| `/pr <título>` | Crea PR en GitHub → el CI de GitHub Actions corre automáticamente |
| `/update` | git pull + restart → despliegue continuo desde el celular |

---

## Testing

### Suite actual: 31 tests

| Archivo | Tests | Qué prueba |
|---|---|---|
| `test_session_store.py` | 6 | SessionStore: crear, listar, switch, delete, thread safety, persistencia |
| `test_session_parse.py` | ~8 | Parseo de output de `opencode session list` |
| `test_persistence.py` | ~10 | Persistencia en sessions.json, load/save, IDs reales |
| `test_utils.py` | ~7 | clean_opencode_output, split_message, telegramify_markdown, relative_time |

### Nuevos tests (post-refactor)

`test_session_store.py` agrega 6 tests específicos para la clase SessionStore:

1. **test_create_session** — crea sesión y verifica que existe en el store
2. **test_list_sessions** — lista todas las sesiones de un chat
3. **test_switch_session** — cambia sesión activa y verifica
4. **test_delete_session** — elimina y verifica que ya no existe
5. **test_concurrent_access** — thread safety con múltiples hilos
6. **test_persistence** — guarda, crea nueva instancia, verifica que los datos persisten

### Ejecutar tests

```bash
# Todos los tests
pytest

# Solo SessionStore
pytest tests/test_session_store.py -v

# Con coverage
pytest --cov=. --cov-report=term-missing
```

---

## Configuración

### Variables de entorno (`.env`)

```bash
# ──── Telegram ────
TELEGRAM_BOT_TOKEN=<token_del_bot_de_BotFather>
ALLOWED_CHAT_IDS=<chat_id_1>,<chat_id_2>

# ──── OpenCode CLI ────
OPENCODE_WORKDIR=C:\Users\marie\Desktop\mono\python
OPENCODE_TIMEOUT=600
OPENCODE_CMD=                              # (opcional, auto-detectado)

# ──── OpenAI (voice transcription) ────
OPENAI_API_KEY=                            # (opcional)
```

| Variable | Requerida | Default | Descripción |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | **Sí** | — | Token de @BotFather |
| `ALLOWED_CHAT_IDS` | **Sí** | `""` | Chat IDs separados por coma (whitelist) |
| `OPENCODE_WORKDIR` | No | Raíz del proyecto | Directorio donde se ejecuta OpenCode |
| `OPENCODE_TIMEOUT` | No | `300` | Timeout en segundos por prompt |
| `OPENCODE_CMD` | No | Auto-detect | Ruta al ejecutable de OpenCode |
| `OPENAI_API_KEY` | No | — | API key para transcripción de voz |

### `/config` — Configuración por chat

El comando `/config` permite cambiar settings **sin tocar `.env`**:

```
/config                    → Ver configuración
/config model pro          → deepseek-v4-pro
/config model flash        → deepseek-v4-flash
/config timeout 900        → 15 minutos
/config workdir <path>     → Workdir personalizado
/config provider opencode  → Backend (actualmente solo opencode)
```

La config se guarda en `sessions.json` bajo `chat_id.config`.

---

## Seguridad

### Whitelist por chat_id

Cada mensaje pasa por el decorador `@authorized`:

```python
def authorized(func):
    async def wrapper(update, context):
        if update.effective_chat.id not in ALLOWED_CHAT_IDS:
            return  # Silencio absoluto
        return await func(update, context)
    return wrapper
```

### Protección contra ejecución paralela

`PromptService` rechaza prompts adicionales mientras hay uno en ejecución:

```python
if chat_id in self._active_processes:
    await self._message_sender.send_plain(
        chat_id, "⏳ Ya hay un prompt en proceso. Usá /cancel para cancelarlo."
    )
    return
```

### SQL Injection prevention

Las consultas a la BD de OpenCode validan el `session_id` con regex:

```python
if oc_id and re.match(r'^[a-zA-Z0-9_]+$', oc_id):
    rows = await query_opencode_db(...)
```

### Ofuscación de chat IDs en logs

```python
def mask_chat_id(chat_id: int) -> str:
    s = str(chat_id)
    return s[:2] + "*" * (len(s) - 4) + s[-2:]  # 8664220427 → 86******27
```

---

## Problemas Conocidos y Soluciones

### 1. ANSI escape codes en respuestas
**Solución:** `clean_opencode_output()` con regex ANSI + filtros adicionales.

### 2. Tool traces multilínea
**Solución:** `_remove_tool_traces()` con tracking de `{ }` brace_depth.

### 3. Encoding UTF-8 en Windows
**Solución:** `encoding="utf-8"` explícito en `Popen`.

### 4. Tablas en MarkdownV2
**Solución:** `telegramify_markdown()` convierte tablas a bloques ``` monospace.

### 5. MarkdownV2 parse failure
**Solución:** `MessageSender.send()` intenta MarkdownV2, si falla reenvía en texto plano.

### 6. Contador asincrónico de progreso
**Solución:** `progress_updater()` con `asyncio.Event`, actualiza cada 5s.

### 7. Event loop anidado
**Solución:** `nest_asyncio.apply()` + `run_in_executor` para no bloquear el loop.

### 8. Procesos zombie en timeout
**Solución:** `taskkill /F /T /PID` para matar el árbol completo.

### 9. Mensajes largos (>4096 chars)
**Solución:** `split_message()` con cortes inteligentes (4000 chars + margen).

### 10. IDs de sesión inventados
**Solución:** Captura de IDs reales (`ses_xxx`) vía `opencode session list`.

---

## Guía de Instalación

### Requisitos

- **Windows 10/11** (o Linux/Mac con ajustes)
- **Python 3.11+**
- **Node.js** (para OpenCode CLI)
- **OpenCode CLI** (`npm install -g @anthropic/opencode`)
- **Git**
- **GitHub CLI** (`gh`) — solo para el comando `/pr`

### Paso a paso

#### 1. Clonar e instalar dependencias

```bash
git clone <repo-url>
cd Sdd-Orchestrator-Telegram-Bot
python -m venv .venv
.venv\Scripts\activate    # Windows
pip install -r requirements.txt
pip install -e ".[dev]"   # ruff, mypy, pytest
```

#### 2. Instalar OpenCode CLI

```bash
npm install -g @anthropic/opencode
opencode --version
```

#### 3. Crear el bot de Telegram

1. Buscá **@BotFather** en Telegram
2. `/newbot` → seguí las instrucciones
3. Guardá el token

#### 4. Obtener tu chat ID

```bash
python get_chat_id.py
```

Enviá `/start` a tu bot desde Telegram. El script imprime tu chat ID.

#### 5. Configurar `.env`

```bash
TELEGRAM_BOT_TOKEN=123456:ABC-DEF1234gh...
ALLOWED_CHAT_IDS=8664220427
OPENCODE_WORKDIR=C:\Users\marie\Desktop\mono\python
OPENCODE_TIMEOUT=600
```

#### 6. Iniciar el bot

```powershell
# Con babysitter (recomendado para produccion)
.\launcher.bat

# Directo (desarrollo)
python -m bot
```

#### 7. Verificar

Enviá `/start` desde Telegram. Deberías recibir el mensaje de bienvenida.

---

## Evolución del Proyecto

### v1.0 — Prototipo inicial
Bot básico con `subprocess.run(["opencode", "run", prompt])`. Sin sesiones, sin limpieza de output.

### v1.1 — Resolución de ruta y timeout
`resolve_opencode_cmd()` con 4 fallbacks. Timeout configurable.

### v1.2 — Manejo de sesiones SDD
`--continue --session <id>`, expiración a 30 min, comando `/new`.

### v1.3 — Limpieza de output
ANSI escape codes, filtrado stderr, `split_message()`.

### v1.4 — Cambio de modelo y cancelación
`/model pro|flash`, `/cancel` con taskkill.

### v1.5 — Comando /status
Panel con modelo, sesión, tiempos relativos, uptime.

### v1.6 — Robustez
`nest_asyncio`, `run_in_executor`, logs rotativos, mask_chat_id.

### v1.7 — Gestión multi-sesión
`sessions.json`, 7 subcomandos `/session`, lazy creation, adopción.

### v1.8 — UX Layer (Phase 0)
Contador asincrónico, MarkdownV2 fallback, UTF-8 fix, tool traces.

### v1.8.1 — SQLite Metadata
`query_opencode_db()`, schema discovery, session info enriquecido.

### v2.0 — Refactor Arquitectónico (5 semanas)
- **Servicios**: SessionStore, MessageSender, PromptService, AIBackend, BotPort
- **DI**: AppContainer, AIProviderFactory
- **Protocolos**: AIBackend, BotPort (0 vendor locks)
- **i18n**: locales/es.py (0 strings hardcodeadas)
- **Comandos nuevos**: /pr, /update, /config
- **CI/CD**: GitHub Actions + pre-commit hooks
- **Testing**: 25 → 31 tests, mypy strict
- **Tooling**: pyproject.toml, launcher.bat, sessions.example.json

---

## Roadmap / Mejoras Futuras

### Corto plazo

- [ ] **Soporte multi-usuario con cola de trabajos** — semáforo global
- [ ] **Comando /retry** — reenviar último prompt
- [ ] **Adjuntar archivos** — screenshots, PDFs, código

### Mediano plazo

- [ ] **Notificaciones proactivas** — "terminó el build", "error en logs"
- [ ] **Dashboard web simple** — logs, sesiones, stats
- [ ] **Voice messages** — transcripción con Whisper

### Largo plazo

- [ ] **Modo "drive-through"** — optimizado para usar manejando
- [ ] **Auto-resumen de sesión** — guardar en Engram al expirar
- [ ] **Rate limiting** — límites por minuto

### Completado

- [x] **Phase 0** — UX Layer
- [x] **Phase 1** — Session Management
- [x] **Phase 2** — Session Commands
- [x] **Phase 3** — Session Adoption
- [x] **Phase 4** — SQLite Metadata
- [x] **v2.0 Refactor** — Arquitectura limpia, DI, protocolos, i18n, CI/CD, 31 tests

---

## Apéndice: Flujo de Ejecución Detallado

### Camino completo de un mensaje (post-refactor)

```
1. Usuario envía "Hola" por Telegram
2. Telegram API → Update al polling del bot
3. python-telegram-bot recibe el Update
4. MessageHandler(filters.TEXT & ~filters.COMMAND) → handle_message()
5. @authorized verifica chat_id en whitelist
6. _get_container(context) → obtiene AppContainer del bot_data
7. container.prompt_service.execute(update, context, "Hola")
8. PromptService:
   a. Verifica que no haya proceso activo (current_process)
   b. SessionStore.get_active() → nombre de sesión
   c. SessionStore.get_id() → ID real o None
   d. Construye comando: opencode run --model <m> [--continue --session <id>] "Hola"
   e. MessageSender.send_plain("⏳ Procesando...") + progress_updater task
   f. AIBackend.execute(prompt, model=..., session_id=..., ...)
   g. OpenCodeCLIBackend → subprocess.Popen en run_in_executor
   h. Captura ID real si es sesión nueva (opencode session list)
   i. clean_opencode_output() → ANSI, build lines, tool traces
   j. telegramify_markdown() → tablas a monospace
   k. split_message() → chunks de 4000 chars
   l. MessageSender.send() → MarkdownV2 con fallback
   m. SessionStore.increment_prompt_count()
   n. stop_event.set() → detener progress_updater
9. Usuario recibe respuesta formateada en Telegram
```

---

*Documentación generada y actualizada post-refactor v2.0. Si algo no cuadra, `/update` y pa'lante, carnal.*
