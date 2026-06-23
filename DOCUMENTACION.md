# Sdd-Orchestrator-Telegram-Bot — Documentación

Documentación técnica universal del proyecto. Esta guía está orientada a
desarrolladores que necesitan entender, mantener o extender el bot.

---

## Tabla de Contenidos

1. [Introducción](#1-introducción)
2. [Arquitectura](#2-arquitectura)
3. [Configuración](#3-configuración)
4. [Sistema de Modelos](#4-sistema-de-modelos)
5. [Sistema de Sesiones](#5-sistema-de-sesiones)
6. [Comandos del Bot](#6-comandos-del-bot)
7. [Sistema de CI/CD](#7-sistema-de-cicd)
8. [Internals para Desarrolladores](#8-internals-para-desarrolladores)
9. [Formateo de Output](#9-formateo-de-output)
10. [Desarrollo](#10-desarrollo)
11. [Decisiones de Diseño y Trade-offs](#11-decisiones-de-diseño-y-trade-offs)
12. [Seguridad](#12-seguridad)
13. [Troubleshooting](#13-troubleshooting)
14. [Apéndice: Referencia de Archivos](#14-apéndice-referencia-de-archivos)

---

## 1. Introducción

### 1.1 Propósito del Proyecto

**SDD Orchestrator Telegram Bot** es un bot de Telegram que actúa como puente
entre un dispositivo móvil y el **OpenCode CLI**, el orquestador SDD
(Spec-Driven Development). El proyecto expone la funcionalidad completa del
orquestador a través de una interfaz conversacional, permitiendo ejecutar
prompts de desarrollo, gestionar sesiones de código y crear Pull Requests
desde cualquier dispositivo con acceso a Telegram.

El bot integra tres Model Context Protocols (MCPs):

- **Context7** — consulta de documentación de librerías en tiempo real
- **Engram** — persistencia de memoria entre sesiones
- **Notion** — acceso a documentación y bases de conocimiento del proyecto

### 1.2 Casos de Uso Típicos

El bot está diseñado para desarrolladores que necesitan acceso móvil a flujos
de trabajo de desarrollo:

- Ejecutar prompts SDD completos desde un dispositivo móvil sin abrir una laptop
- Gestionar múltiples sesiones de código con el comando `/session`
- Crear Pull Requests en GitHub directamente desde Telegram
- Reiniciar el bot de forma remota para aplicar actualizaciones
- Configurar parámetros por chat (modelo, timeout, directorio de trabajo)
- Consultar documentación técnica sin abandonar la conversación
- Enviar prompts de texto o notas de voz

### 1.3 Stack Tecnológico

| Componente | Tecnología | Versión |
|---|---|---|
| Lenguaje | Python | 3.11+ |
| Framework de bot | python-telegram-bot | v22+ |
| Orquestador | OpenCode CLI | última estable |
| Linter | ruff | con reglas E, F, I, N, W, UP, B, C4, SIM, T20 |
| Type checker | mypy | strict mode |
| Tests | pytest | con cobertura |
| CI/CD | GitHub Actions | workflows automatizados |
| Transcripción de voz | OpenAI Whisper (opcional) | API |

---

## 2. Arquitectura

### 2.1 Diagrama de Capas

```text
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
```text
```

### 2.2 Principios SOLID Aplicados

| Principio | Aplicación |
|---|---|
| **S** — Single Responsibility | Cada servicio tiene una única responsabilidad: `SessionStore` persiste sesiones, `MessageSender` envía mensajes, `PromptService` orquesta prompts. |
| **O** — Open/Closed | Nuevos providers de IA se agregan registrando clases en `AIProviderFactory` sin modificar código existente. |
| **L** — Liskov Substitution | Cualquier implementación de `AIBackend` o `BotPort` es intercambiable. |
| **I** — Interface Segregation | Los protocolos son mínimos: `AIBackend` expone solo `execute` y `cancel`; `BotPort` expone solo operaciones de envío. |
| **D** — Dependency Inversion | Las dependencias apuntan a abstracciones (Protocols), no a concreciones. |

### 2.3 Patrones de Diseño

| Patrón | Implementación |
|---|---|
| **Dependency Injection** | `AppContainer` concentra todas las dependencias y se inyecta vía `bot_data`. |
| **Strategy** | Diferentes `AIBackend` (OpenCode, OpenAI API, etc.) seleccionables en runtime. |
| **Factory** | `AIProviderFactory` registra y crea backends por nombre. |
| **Adapter** | `TelegramAdapter` implementa `BotPort` sobre `python-telegram-bot`. |
| **Decorator** | `@authorized` añade verificación de chat_id sin modificar la lógica del handler. |
| **Protocol (Interface)** | `AIBackend` y `BotPort` definen contratos sin herencia rígida. |

### 2.4 Flujo de un Prompt End-to-End

```text
1. Usuario envía un mensaje por Telegram
2. Telegram API entrega el Update al polling del bot
3. python-telegram-bot dispatch el Update al MessageHandler correspondiente
4. El decorador @authorized valida el chat_id contra la whitelist
5. El handler obtiene AppContainer desde context.bot_data
6. PromptService.execute() coordina el flujo:
   a. Verifica que no haya un proceso activo
   b. Lee la sesión activa del SessionStore
   c. Construye el comando OpenCode apropiado
   d. Envía mensaje "Procesando..." + inicia progress_updater
   e. AIBackend.execute() lanza el subprocess
   f. Captura el ID real de sesión si es la primera ejecución
   g. Aplica clean_opencode_output() al resultado
   h. Convierte tablas con telegramify_markdown()
   i. Divide el texto con split_message() si excede 4000 chars
   j. Envía con MessageSender (MarkdownV2 + fallback)
   k. Incrementa el contador de prompts
7. El usuario recibe la respuesta formateada en Telegram
```text
```

### 2.5 Componentes Principales

| Componente | Rol | Ubicación |
|---|---|---|
| `bot.py` | Punto de entrada, construye AppContainer, registra handlers | Raíz |
| `AppContainer` | Contenedor DI — mantiene referencias a todos los servicios | `services/container.py` |
| `SessionStore` | Persistencia thread-safe de sesiones en JSON | `services/session_store.py` |
| `MessageSender` | Envío unificado de mensajes (MarkdownV2 + fallback) | `services/message_sender.py` |
| `PromptService` | Orquestación de prompts: sesión, ejecución, limpieza, split | `services/prompt_service.py` |
| `AIBackend` (Protocol) | Interfaz abstracta para backends de IA | `services/ai_backend.py` |
| `BotPort` (Protocol) | Interfaz abstracta para bots de mensajería | `services/bot_port.py` |
| `AIProviderFactory` | Registry de backends (actualmente: opencode) | `services/ai_provider_factory.py` |
| `OpenCodeCLIBackend` | Implementación real vía subprocess | `services/opencode_cli_backend.py` |
| `TelegramAdapter` | Implementación real de BotPort para Telegram | `services/telegram_adapter.py` |
| `locales/` | Sistema i18n — strings centralizadas | `locales/es.py` |
| `formatting/markdown.py` | Transformación pura de texto (sin I/O) | `formatting/` |
| `handlers/` | Handlers delgados — solo delegan a servicios | `handlers/` |

---

## 3. Configuración

### 3.1 Variables de Entorno

Las variables se cargan desde un archivo `.env` en la raíz del proyecto:

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
```bash
```

| Variable | Requerida | Default | Descripción |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Sí | — | Token otorgado por @BotFather |
| `ALLOWED_CHAT_IDS` | Sí | `""` | Chat IDs separados por coma (whitelist) |
| `OPENCODE_WORKDIR` | No | Raíz del proyecto | Directorio donde se ejecuta OpenCode |
| `OPENCODE_TIMEOUT` | No | `300` | Timeout en segundos por prompt |
| `OPENCODE_CMD` | No | Auto-detect | Ruta al ejecutable de OpenCode |
| `OPENAI_API_KEY` | No | — | API key para transcripción de voz |

### 3.2 Whitelist de Chat IDs

El bot solo responde a chats cuyo identificador esté en `ALLOWED_CHAT_IDS`.
Los mensajes de chats no autorizados se ignoran silenciosamente. Esto
proporciona control de acceso sin necesidad de autenticación adicional.

Para agregar un nuevo chat:

1. Iniciar una conversación con el bot desde el cliente de Telegram
2. Ejecutar `python get_chat_id.py` para obtener el identificador
3. Agregar el identificador a `ALLOWED_CHAT_IDS` en `.env`
4. Reiniciar el bot

### 3.3 Configuración por Chat

El comando `/config` permite modificar parámetros sin editar `.env`:

```text
/config                          → Muestra la configuración actual
/config model pro                → Cambia el modelo
/config model flash              → Cambia al modelo rápido
/config timeout 900              → Timeout de 15 minutos
/config workdir C:\proyecto      → Cambia el directorio de trabajo
/config provider opencode        → Selecciona el backend
```text
```

La configuración se persiste en `sessions.json` bajo la clave
`chat_id.config`, permitiendo que cada chat tenga ajustes independientes.

---

## 4. Sistema de Modelos

### 4.1 Modelos Disponibles

El bot soporta múltiples proveedores y modelos. La nomenclatura sigue el
formato `provider/model-name`:

#### DeepSeek

| Modelo | Uso | Velocidad |
|---|---|---|
| `deepseek/pro` | Tareas complejas, razonamiento profundo | Lenta |
| `deepseek/flash` | Respuestas rápidas, tareas simples | Rápida |

#### MiniMax

| Modelo | Descripción |
|---|---|
| `minimax/M2` | Segunda generación |
| `minimax/M2.1` | Segunda generación, revisión 1 |
| `minimax/M2.5` | Segunda generación, revisión 5 |
| `minimax/M2.5-highspeed` | Variante optimizada para velocidad |
| `minimax/M2.7` | Segunda generación, revisión 7 |
| `minimax/M2.7-highspeed` | Variante optimizada para velocidad |
| `minimax/M3` | Tercera generación (modelo MiniMax-M3) |

### 4.2 Formato de Identificación

El formato `provider/model-name` permite identificar unívocamente cada
modelo. Al enviar un prompt, el bot resuelve el provider, selecciona el
backend correspondiente y pasa el identificador completo al CLI.

### 4.3 Agregar un Nuevo Modelo

Para registrar un modelo adicional del mismo provider:

1. Verificar que el CLI de OpenCode soporte el modelo
2. No se requieren cambios de código: el modelo se selecciona vía `/config model <nombre>` o como argumento directo

### 4.4 Agregar un Nuevo Provider

Para integrar un provider completamente nuevo (por ejemplo, API de OpenAI):

1. Crear una clase que implemente el protocolo `AIBackend`
2. Registrar la clase en `AIProviderFactory` con un nombre identificador
3. Configurar las credenciales necesarias en `.env`
4. Actualizar la documentación

Ejemplo de implementación:

```python
from services.ai_backend import AIBackend, AIResult

class OpenAIAPIBackend:
    def __init__(self, api_key: str, timeout: int) -> None:
        self._api_key = api_key
        self._timeout = timeout

    async def execute(self, prompt: str, *, model: str,
                      session_id: str | None, workdir: str,
                      timeout: int, chat_id: int) -> AIResult:
        # Implementación específica del provider
        ...

    async def cancel(self, chat_id: int) -> bool:
        # Lógica de cancelación
        ...
```

Registro en el factory:

```python
factory.register("openai", OpenAIAPIBackend)
```

---

## 5. Sistema de Sesiones

### 5.1 Concepto de Sesión

Una sesión representa una conversación persistente con el modelo de IA. El
bot mantiene sesiones independientes por chat, permitiendo que cada
conversación tenga su propio contexto, historial y estado.

Cada chat puede tener múltiples sesiones con nombres arbitrarios. Solo una
sesión está activa a la vez, determinada por la clave `active` en el
almacenamiento.

### 5.2 SessionStore — Internals

`SessionStore` es una clase thread-safe que gestiona la persistencia de
sesiones. Utiliza un patrón de caché en memoria respaldado por un archivo
JSON:

- **Caché en memoria**: mantiene los datos en un diccionario para acceso
  rápido sin I/O de disco en cada operación
- **Lock (`threading.Lock`)**: previene condiciones de carrera cuando
  múltiples handlers acceden concurrentemente
- **Persistencia JSON**: escritura atómica al archivo `sessions.json`
  después de cada modificación

```python
class SessionStore:
    def __init__(self, path: str) -> None: ...
    def get_active(self, chat_id: int) -> str: ...
    def get_session(self, chat_id: int, name: str) -> dict | None: ...
    def get_id(self, chat_id: int, name: str) -> str | None: ...
    def set_id(self, chat_id: int, name: str,
               session_id: str | None) -> None: ...
    def increment_prompt_count(self, chat_id: int, name: str) -> int: ...
    def list_sessions(self, chat_id: int) -> dict: ...
    def create_session(self, chat_id: int, name: str) -> bool: ...
    def delete_session(self, chat_id: int, name: str) -> bool: ...
    def switch_session(self, chat_id: int, name: str) -> bool: ...
    def clear_id(self, chat_id: int, name: str) -> None: ...
```

### 5.3 Formato de `sessions.json`

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

| Campo | Descripción |
|---|---|
| `active` | Nombre de la sesión activa para este chat |
| `id` | ID real asignado por OpenCode (`ses_xxx`) |
| `title` | Descripción legible de la sesión |
| `created` | Timestamp ISO 8601 de creación |
| `last_used` | Timestamp ISO 8601 del último uso |
| `prompt_count` | Número de prompts ejecutados en esta sesión |

### 5.4 Captura de OpenCode Session ID

Cuando se crea una sesión nueva, el bot no inventa identificadores. En su
lugar:

1. Ejecuta `opencode run "<prompt>"` sin flags `--continue --session`
2. OpenCode crea la sesión internamente con un ID real (`ses_xxx`)
3. Al terminar la ejecución, el backend consulta `opencode session list`
4. El ID capturado se guarda en `SessionStore` para futuros `--continue`

Esto garantiza que los IDs de sesión sean siempre válidos y correspondan a
sesiones reales en el backend.

### 5.5 Session Discovery y Adoption

El bot ofrece dos comandos para integrar sesiones preexistentes:

- `/session discover` — lista todas las sesiones disponibles en OpenCode
  que aún no están registradas en el bot
- `/session adopt <id> <nombre>` — adopta una sesión existente,
  registrando su ID con un nombre local para uso futuro

Esto permite retomar trabajo iniciado desde la línea de comandos sin perder
contexto.

### 5.6 Continuidad entre Mensajes

`PromptService.execute()` decide si continuar o iniciar una nueva sesión:

1. Lee `sessions.json` vía `SessionStore` para obtener el nombre activo y
   el ID real
2. Si no hay ID real, ejecuta una **nueva sesión** (`opencode run "<prompt>"`)
3. Si la sesión expiró (más de 30 minutos sin uso), ejecuta una **nueva
   sesión**
4. Si hay sesión activa con ID real, **continúa** la sesión
   (`opencode run --continue --session <id> "<prompt>"`)

---

## 6. Comandos del Bot

### 6.1 Tabla Completa de Comandos

| Comando | Handler | Descripción |
|---|---|---|
| `/start` | `commands.py` | Inicia el bot, muestra mensaje de bienvenida |
| `/help` | `commands.py` | Lista todos los comandos disponibles |
| `/status` | `commands.py` | Estado actual: sesión, modelo, uptime, prompt count |
| `/new` | `commands.py` | Reinicia la sesión activa (limpia ID) |
| `/model <nombre>` | `commands.py` | Cambia el modelo activo |
| `/config` | `commands.py` | Configuración por chat (modelo, timeout, workdir, provider) |
| `/cancel` | `commands.py` | Cancela el prompt en ejecución |
| `/open <prompt>` | `commands.py` | Ejecuta un prompt explícito |
| `/session new\|list\|switch\|delete\|info\|discover\|adopt` | `sessions.py` | Gestión multi-sesión |
| `/session_preview` | `admin.py` | Atajo de diagnóstico de sesión |
| `/test_md` | `admin.py` | Test de renderizado MarkdownV2 |
| `/pr <título>` | `ci.py` | Crea PR en GitHub desde Telegram |
| `/update` | `ci.py` | Auto-restart del bot |
| _(texto libre)_ | `messages.py` | Prompt directo al orquestador |
| _(nota de voz)_ | `messages.py` | Transcripción + prompt |

### 6.2 `/config` — Configuración Detallada

El comando `/config` permite ver y modificar la configuración del chat
actual sin necesidad de editar el archivo `.env`:

| Subcomando | Efecto |
|---|---|
| `/config` | Muestra la configuración actual |
| `/config model pro` | Cambia a `deepseek/pro` |
| `/config model flash` | Cambia a `deepseek/flash` |
| `/config timeout 900` | Establece timeout de 15 minutos |
| `/config workdir <path>` | Cambia el directorio de trabajo |
| `/config provider opencode` | Selecciona el backend |

La configuración persiste en `sessions.json` bajo `chat_id.config`,
sobrevive reinicios y permite ajustes independientes por chat.

### 6.3 `/pr <título>` — Crear Pull Request

El comando `/pr` automatiza la creación de Pull Requests directamente desde
Telegram:

**Flujo de ejecución:**

1. Lee `CHANGELOG.md` del directorio de trabajo configurado para el chat
2. Usa el contenido como cuerpo (body) del Pull Request
3. Ejecuta `gh pr create --title "<título>" --body "<changelog>"`
4. Devuelve la URL del PR creado al usuario

**Requisitos:**

- `gh` CLI instalado en la máquina donde corre el bot
- `gh` CLI autenticado (`gh auth login`)
- Permisos de escritura sobre el repositorio

### 6.4 `/update` — Auto-Restart

El comando `/update` permite actualizar el bot de forma remota:

**Flujo de ejecución:**

1. El handler envía mensaje de confirmación al usuario
2. Ejecuta `os._exit(42)` — código de salida especial
3. `launcher.bat` detecta el exit code 42
4. Ejecuta `git pull origin main`
5. Reinicia el bot con `python -m bot`

El bot vuelve a estar disponible en pocos segundos sin intervención manual.

### 6.5 Gestión de Sesiones

El comando `/session` acepta los siguientes subcomandos:

| Subcomando | Efecto |
|---|---|
| `/session new <nombre>` | Crea una nueva sesión (lazy, id=null) |
| `/session list` | Lista todas las sesiones del chat |
| `/session switch <nombre>` | Cambia la sesión activa |
| `/session delete <nombre>` | Elimina una sesión |
| `/session info <nombre>` | Muestra detalles de una sesión |
| `/session discover` | Lista sesiones de OpenCode no registradas |
| `/session adopt <id> <nombre>` | Adopta una sesión existente |

---

## 7. Sistema de CI/CD

### 7.1 GitHub Actions

El proyecto incluye un workflow en `.github/workflows/ci.yml` que se
ejecuta en cada push y Pull Request:

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

Cada ejecución realiza tres verificaciones:

1. **ruff** — linting con reglas estrictas (E, F, I, N, W, UP, B, C4, SIM, T20)
2. **mypy strict** — verificación de tipos sin excepciones
3. **pytest** — ejecución de la suite completa de tests

### 7.2 Pre-commit Hooks

El proyecto incluye configuración de pre-commit en `.pre-commit-config.yaml`:

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

Los hooks se ejecutan automáticamente antes de cada commit. Si alguna
verificación falla, el commit es rechazado, garantizando que el código
en el repositorio cumpla con los estándares de calidad.

### 7.3 `launcher.bat` — Babysitter Process

El archivo `launcher.bat` actúa como proceso supervisor que mantiene el
bot en ejecución y gestiona los reinicios:

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

### 7.4 CI/CD desde Telegram

| Comando | Efecto |
|---|---|
| `/pr <título>` | Crea PR en GitHub → el CI de GitHub Actions ejecuta automáticamente |
| `/update` | git pull + restart → despliegue continuo desde el dispositivo móvil |

### 7.5 Exit Code 42

El bot utiliza el código de salida 42 para señalar al `launcher.bat` que
debe realizar una actualización y reinicio. Este código se eligió porque es
un valor no estándar y memorable, lo que facilita su detección sin
ambigüedad frente a otros códigos de salida de Python.

---

## 8. Internals para Desarrolladores

### 8.1 AppContainer — Dependency Injection

`AppContainer` es el contenedor central de dependencias. Se construye una
sola vez durante el arranque del bot y se almacena en `bot_data` para
acceso desde cualquier handler:

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

**Por qué este patrón:**

- **0 imports circulares** — antes había múltiples `import bot` dispersos
- **Testeable** — cada servicio se puede mockear independientemente
- **Reemplazable** — cambiar OpenCode por otra IA o Telegram por otro
  messenger solo requiere implementar el Protocol correspondiente

### 8.2 Protocols — Interfaces Abstractas

#### `AIBackend` Protocol

```python
class AIBackend(Protocol):
    async def execute(self, prompt: str, *, model: str,
                      session_id: str | None,
                      workdir: str, timeout: int,
                      chat_id: int) -> AIResult: ...
    async def cancel(self, chat_id: int) -> bool: ...
```

`AIBackend` define la interfaz para cualquier backend de IA. La
implementación actual (`OpenCodeCLIBackend`) utiliza `subprocess.Popen`,
pero el resto del código no depende de este detalle. Para usar la API de
OpenAI u otro proveedor, basta con implementar este protocolo y registrarlo
en el factory.

#### `BotPort` Protocol

```python
class BotPort(Protocol):
    async def send_message(self, chat_id: int, text: str, *,
                           parse_mode: str | None = None) -> Message: ...
    async def edit_message_text(self, chat_id: int, message_id: int,
                                text: str) -> None: ...
    async def send_chat_action(self, chat_id: int, action: str) -> None: ...
```

`BotPort` abstrae la plataforma de mensajería. La implementación actual
(`TelegramAdapter`) envuelve `python-telegram-bot`, pero migrar a Discord,
Slack u otra plataforma requiere únicamente una nueva implementación del
protocolo.

### 8.3 AIProviderFactory

`AIProviderFactory` mantiene un registro de backends disponibles:

```python
factory = AIProviderFactory(default_provider="opencode")
factory.register("opencode", OpenCodeCLIBackend)
backend = factory.get("opencode", opencode_cmd="...",
                      workdir="...", timeout=600)
```

El factory permite seleccionar el backend en tiempo de ejecución mediante
el comando `/config provider <nombre>`.

### 8.4 Flujo Detallado de un Prompt

El siguiente diagrama muestra el recorrido completo de un mensaje desde
su recepción hasta la entrega de la respuesta:

```text
1. Usuario envía "Hola" por Telegram
2. Telegram API entrega el Update al polling del bot
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
   e. MessageSender.send_plain("Procesando...") + progress_updater task
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

### 8.5 Flujo de un Mensaje de Voz

1. Telegram entrega el archivo de audio al bot
2. El handler descarga el archivo `.ogg` a un directorio temporal
3. Si `OPENAI_API_KEY` está configurada, se transcribe el audio con Whisper
4. El texto transcrito se procesa como un prompt normal
5. Si la transcripción falla, se notifica al usuario

### 8.6 Manejo de Errores

El sistema implementa varias estrategias de manejo de errores:

- **MarkdownV2 parse failure**: `MessageSender.send()` intenta enviar con
  formato; si falla por caracteres no escapados, reenvía en texto plano
- **Timeout**: `AIBackend` cancela el subprocess después del tiempo
  configurado y notifica al usuario
- **Sesión expirada**: el sistema crea una nueva sesión automáticamente
- **Proceso activo**: prompts adicionales son rechazados mientras hay uno
  en ejecución

### 8.7 Progress Counter

Durante la ejecución de prompts largos, el bot muestra un contador que se
actualiza cada 5 segundos:

- Se envía un mensaje inicial "Procesando..."
- Una tarea asyncio actualiza el mensaje cada 5 segundos con el tiempo
  transcurrido
- Cuando el prompt termina, el mensaje se reemplaza con la respuesta
  final o se envía como mensaje nuevo si la respuesta es muy larga

---

## 9. Formateo de Output

El módulo `formatting/markdown.py` contiene funciones puras (sin I/O, sin
dependencias externas) para transformar el output del orquestador en texto
apto para Telegram.

### 9.1 `clean_opencode_output(text: str) -> str`

Limpia el output crudo del CLI eliminando:

- Códigos de escape ANSI (regex)
- Líneas de build (`> build · deepseek-v4-pro`)
- Tool traces (con tracking de `{ }` para JSON multilínea)
- Líneas de auto-rechazo de permisos
- Líneas vacías múltiples (colapsadas a una)

### 9.2 `telegramify_markdown(text: str) -> str`

Convierte tablas Markdown a bloques de código monospace porque MarkdownV2
de Telegram no soporta tablas nativas. Utiliza caracteres de caja (│)
para simular bordes visuales.

**Entrada:**

```markdown
| Columna A | Columna B |
|---|---|
| Valor 1 | Valor 2 |
```

**Salida:**

```text
┌───────────┬───────────┐
│ Columna A │ Columna B │
├───────────┼───────────┤
│ Valor 1   │ Valor 2   │
└───────────┴───────────┘
```

### 9.3 `split_message(text: str, limit: int = 4000) -> list[str]`

Divide respuestas largas en chunks de máximo 4000 caracteres (Telegram
limita los mensajes a 4096). Intenta cortar en los siguientes puntos en
orden de preferencia:

1. Párrafo (`\n\n`)
2. Final de oración (`. `)
3. Salto de línea (`\n`)
4. Espacio
5. Corte forzado en el límite

Las partes se numeran: `(parte 1/3)`, `(parte 2/3)`, etc.

### 9.4 `_remove_tool_traces(text: str) -> str`

Elimina traces de tool calls que aparecen como JSON multilínea en el output.
Implementa un contador de profundidad de llaves (`brace_depth`) que detecta
el inicio por el carácter `⚙` (o su variante corrupta por cp1252: `âš™`),
acumula líneas hasta que la profundidad vuelve a cero, y descarta el bloque
completo.

---

## 10. Desarrollo

### 10.1 Setup del Entorno

#### Requisitos

- Python 3.11 o superior
- Node.js (para OpenCode CLI)
- OpenCode CLI: `npm install -g opencode-ai`
- Git
- GitHub CLI (`gh`) — solo para el comando `/pr`

#### Instalación

```bash
git clone <url-del-repositorio>
cd Sdd-Orchestrator-Telegram-Bot
python -m venv .venv

# Windows
.venv\Scripts\activate

# Linux/macOS
source .venv/bin/activate

pip install -r requirements.txt
pip install -e ".[dev]"
```

### 10.2 Testing

El proyecto utiliza pytest con fixtures compartidas en `conftest.py`.

```bash
# Ejecutar todos los tests
pytest

# Solo SessionStore (verbose)
pytest tests/test_session_store.py -v

# Con cobertura
pytest --cov=. --cov-report=term-missing
```

#### Estructura de Tests

| Archivo | Tests | Cobertura |
|---|---|---|
| `test_session_store.py` | 6 | SessionStore: crear, listar, switch, delete, thread safety, persistencia |
| `test_session_parse.py` | ~8 | Parseo de output de `opencode session list` |
| `test_persistence.py` | ~10 | Persistencia en sessions.json, load/save, IDs reales |
| `test_utils.py` | ~7 | clean_opencode_output, split_message, telegramify_markdown, relative_time |

#### Escribir Nuevos Tests

1. Crear el archivo en `tests/` con prefijo `test_`
2. Importar fixtures necesarios desde `conftest.py`
3. Usar `tmp_path` de pytest para archivos temporales
4. Seguir el patrón Arrange-Act-Assert
5. Verificar que el test falla antes de implementar la funcionalidad

### 10.3 Linting y Type Checking

#### ruff

```bash
# Verificar
ruff check .

# Corregir automáticamente
ruff check . --fix
```

Reglas activas: E, F, I, N, W, UP, B, C4, SIM, T20.

#### mypy

```bash
# Verificar tipos
mypy .

# Modo strict está habilitado en pyproject.toml
```

El proyecto utiliza mypy en modo strict sin excepciones.

### 10.4 Pre-commit Hooks

```bash
# Instalar hooks
pip install pre-commit
pre-commit install

# Ejecutar manualmente
pre-commit run --all-files
```

Los hooks ejecutan ruff y mypy antes de cada commit.

### 10.5 Agregar un Nuevo Handler

1. Crear la función async en el archivo apropiado bajo `handlers/`
2. Obtener el container: `container = _get_container(context)`
3. Aplicar el decorador `@authorized`
4. Delegar la lógica al servicio correspondiente
5. Registrar el handler en `bot.py:build_application()`

```python
# handlers/commands.py
async def mi_comando(update: Update,
                     context: ContextTypes.DEFAULT_TYPE) -> None:
    container = _get_container(context)
    # Lógica mínima, delegar a servicios
    ...

# bot.py
application.add_handler(CommandHandler("mi_comando", mi_comando))
```

### 10.6 Agregar un Nuevo Servicio

1. Crear la clase en `services/`
2. Definir las dependencias como parámetros del `__init__`
3. Agregar el tipo al `AppContainer`
4. Inyectar la instancia en `bot.py:run_bot()`

### 10.7 Agregar un Nuevo Comando

1. Definir las strings en `locales/es.py`
2. Crear el handler siguiendo el patrón existente
3. Registrar el `CommandHandler` en `bot.py`
4. Documentar el comando en esta guía

### 10.8 Agregar un Nuevo Idioma i18n

1. Crear `locales/en.py` (o el código deseado) exportando un dict `STRINGS`
   con las mismas claves que `locales/es.py`
2. Modificar `locales/__init__.py` para detectar el idioma y cargar el
   módulo correspondiente
3. Mantener `es.py` como fallback

---

## 11. Decisiones de Diseño y Trade-offs

### 11.1 Por qué Dependency Injection > Globals

**Decisión:** Utilizar `AppContainer` para centralizar dependencias en
lugar de variables globales.

**Justificación:**

- Elimina imports circulares entre módulos
- Permite mockear servicios individualmente en tests
- Hace explícitas las dependencias de cada componente
- Facilita el reemplazo de implementaciones

**Trade-off:** Mayor verbosidad en la construcción inicial; el container
debe configurarse explícitamente.

### 11.2 Por qué Protocols > ABCs

**Decisión:** Utilizar `typing.Protocol` en lugar de `abc.ABC`.

**Justificación:**

- Duck typing nativo de Python — las clases no necesitan heredar
- Menor acoplamiento — cualquier clase con la forma correcta cumple el
  protocolo
- Compatibilidad con type checkers sin overhead de runtime
- Facilita la implementación de mocks en tests

### 11.3 Por qué SessionStore con Cache + Lock

**Decisión:** Caché en memoria + `threading.Lock` para acceso al JSON.

**Justificación:**

- Reduce I/O de disco en operaciones frecuentes (cada prompt)
- El lock previene condiciones de carrera entre handlers concurrentes
- La escritura atómica al JSON garantiza consistencia ante crashes

**Trade-off:** En caso de crash entre la modificación de la caché y la
escritura al disco, se pueden perder cambios. Aceptable dado que la
fuente de verdad final es OpenCode.

### 11.4 Por qué Graceful Shutdown > os._exit

**Decisión:** Utilizar `os._exit(42)` para señalar actualización.

**Justificación:**

- El exit code 42 es un valor no estándar que el `launcher.bat` detecta
  inequívocamente
- Permite que `launcher.bat` ejecute `git pull` antes de reiniciar
- Simplifica el flujo de actualización remota desde Telegram

**Trade-off:** `os._exit` no ejecuta cleanup de asyncio ni cierra
websockets elegantemente. Aceptable porque el launcher relanza el proceso
inmediatamente.

### 11.5 Por qué CHANGELOG.md se Borra Después de /pr

**Decisión:** El comando `/pr` utiliza `CHANGELOG.md` como body del PR y
lo elimina después.

**Justificación:**

- El changelog sirve como descripción detallada de cambios
- Después de crear el PR, la información ya está en GitHub
- Borrarlo fuerza la regeneración para el próximo ciclo de desarrollo

**Trade-off:** El desarrollador debe regenerar el changelog si necesita
editar el PR. Aceptable dado que el flujo de trabajo asume este patrón.

### 11.6 Por qué CONTAINER_KEY como Constante

**Decisión:** La clave `"container"` en `bot_data` se almacena como
constante.

**Justificación:**

- Evita magic strings dispersos por el código
- Facilita la refactorización si se decide cambiar el nombre
- Permite detectar typos en tiempo de desarrollo

### 11.7 Code Smells Corregidos

Durante el refactor arquitectónico se abordaron varios code smells:

| Code Smell | Solución |
|---|---|
| Imports circulares entre `bot.py` y handlers | AppContainer como punto único de acceso |
| Dicts globales mutables | Inyección de dependencias en clases |
| Patrones de envío duplicados | MessageSender unificado |
| Strings hardcodeadas dispersas | Sistema i18n centralizado |
| Vendor lock a `subprocess` | Protocol AIBackend |
| Vendor lock a `telegram.Bot` | Protocol BotPort |
| Funciones enormes | Descomposición en servicios enfocados |
| Configuración mutable global | Configuración por chat persistida |

---

## 12. Seguridad

### 12.1 Whitelist por Chat ID

Cada mensaje pasa por el decorador `@authorized` antes de ejecutar
cualquier handler:

```python
def authorized(func):
    async def wrapper(update, context):
        if update.effective_chat.id not in ALLOWED_CHAT_IDS:
            return  # Silencio absoluto
        return await func(update, context)
    return wrapper
```

Los mensajes de chats no autorizados se ignoran silenciosamente, sin
revelar la existencia del bot.

### 12.2 Protección contra Ejecución Paralela

`PromptService` rechaza prompts adicionales mientras hay uno en ejecución
para el mismo chat:

```python
if chat_id in self._active_processes:
    await self._message_sender.send_plain(
        chat_id, "Ya hay un prompt en proceso. Use /cancel para cancelarlo."
    )
    return
```

### 12.3 Validación de Session ID

Las consultas a la base de datos de OpenCode validan el `session_id` con
una expresión regular antes de ejecutar SQL, previniendo inyecciones:

```python
if oc_id and re.match(r'^[a-zA-Z0-9_]+$', oc_id):
    rows = await query_opencode_db(...)
```

### 12.4 Ofuscación de Chat IDs en Logs

Para evitar exponer identificadores completos en logs, se aplica una
función de enmascaramiento:

```python
def mask_chat_id(chat_id: int) -> str:
    s = str(chat_id)
    return s[:2] + "*" * (len(s) - 4) + s[-2:]
    # 8664220427 → 86******27
```

### 12.5 Buenas Prácticas

- No incluir credenciales en el repositorio
- Utilizar variables de entorno para tokens y claves API
- Revisar los logs antes de compartirlos públicamente
- Mantener `ALLOWED_CHAT_IDS` actualizado
- Rotar el token del bot periódicamente

---

## 13. Troubleshooting

### 13.1 Errores Comunes

#### `gh: command not found`

**Causa:** GitHub CLI no está instalado o no está en el PATH.

**Solución:** Instalar `gh` desde [cli.github.com](https://cli.github.com/)
y autenticarse con `gh auth login`.

#### `gh: authentication failed`

**Causa:** El token de `gh` expiró o no tiene los scopes necesarios.

**Solución:** Ejecutar `gh auth login` nuevamente y verificar que el
repositorio sea accesible.

#### `Permission denied` al ejecutar comandos

**Causa:** El usuario del sistema no tiene permisos sobre el directorio de
trabajo o el repositorio Git.

**Solución:** Verificar permisos con `ls -la` y ajustar si es necesario.

#### PR con body vacío

**Causa:** El archivo `CHANGELOG.md` no existe en el directorio de trabajo.

**Solución:** Crear un `CHANGELOG.md` con al menos una línea de contenido
antes de ejecutar `/pr`.

### 13.2 Debugging con Logs

Los logs se emiten con el módulo estándar `logging` de Python. Para
aumentar el nivel de detalle:

```python
# config.py
logging.basicConfig(level=logging.DEBUG)
```

Los logs incluyen:

- Timestamp de cada operación
- Chat ID ofuscado
- Comando ejecutado
- Resultado (éxito o error)
- Duración de operaciones largas

### 13.3 Problemas de Conectividad

#### El bot no responde a mensajes

1. Verificar que `launcher.bat` esté en ejecución
2. Comprobar que el token en `.env` sea válido
3. Revisar los logs para errores de autenticación
4. Confirmar que el chat ID esté en `ALLOWED_CHAT_IDS`

#### Timeout en la ejecución de prompts

1. Aumentar `OPENCODE_TIMEOUT` en `.env`
2. Verificar que OpenCode CLI responda directamente
3. Comprobar el espacio en disco del directorio de trabajo

### 13.4 Problemas con OpenCode CLI

#### Error de formato de modelo

**Causa:** El modelo especificado no sigue el formato `provider/model-name`
o no está soportado por OpenCode.

**Solución:** Verificar la lista de modelos disponibles y usar el formato
correcto.

#### Session ID no capturado

**Causa:** El comando `opencode session list` falló o no devolvió el ID
esperado.

**Solución:** Ejecutar manualmente `opencode session list` para verificar
que el CLI funcione correctamente. Revisar la versión de OpenCode.

---

## 14. Apéndice: Referencia de Archivos

### 14.1 Estructura del Proyecto

```text
Sdd-Orchestrator-Telegram-Bot/
├── bot.py                      # Punto de entrada
├── config.py                   # Configuración, constantes, logger
├── pyproject.toml              # ruff, mypy strict, pytest
├── requirements.txt            # Dependencias Python
├── launcher.bat                # Babysitter: git pull + auto-restart
├── run_bot.bat                 # Launcher simple (legacy)
├── get_chat_id.py              # Utilidad para obtener chat ID
├── sessions.json               # Persistencia de sesiones
├── sessions.example.json       # Template para contribuidores
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
├── tests/                      # Tests automatizados
│   ├── __init__.py
│   ├── conftest.py
│   ├── test_session_store.py
│   ├── test_session_parse.py
│   ├── test_persistence.py
│   └── test_utils.py
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

### 14.2 Tabla de Archivos

| Archivo | Propósito | Líneas aprox. |
|---|---|---|
| `bot.py` | Punto de entrada, registro de handlers | ~400 |
| `config.py` | Constantes, configuración, logger | ~150 |
| `services/container.py` | AppContainer (DI) | ~80 |
| `services/session_store.py` | Persistencia thread-safe | ~200 |
| `services/message_sender.py` | Envío unificado de mensajes | ~150 |
| `services/prompt_service.py` | Orquestación de prompts | ~300 |
| `services/ai_backend.py` | Protocol AIBackend | ~30 |
| `services/bot_port.py` | Protocol BotPort | ~30 |
| `services/ai_provider_factory.py` | Factory de providers | ~60 |
| `services/opencode_cli_backend.py` | Backend OpenCode | ~200 |
| `services/telegram_adapter.py` | Adapter Telegram | ~80 |
| `handlers/commands.py` | Comandos principales | ~250 |
| `handlers/sessions.py` | Gestión de sesiones | ~300 |
| `handlers/messages.py` | Mensajes de texto y voz | ~150 |
| `handlers/admin.py` | Comandos administrativos | ~80 |
| `handlers/ci.py` | Comandos CI/CD | ~100 |
| `locales/es.py` | Strings de interfaz | ~150 |
| `formatting/markdown.py` | Transformación de texto | ~200 |
| `tests/` | Suite de tests | ~500 |

---

*Documentación técnica del proyecto Sdd-Orchestrator-Telegram-Bot.*
