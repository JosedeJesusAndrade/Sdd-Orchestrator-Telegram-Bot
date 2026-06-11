# 🤖 SDD Orchestrator Telegram Bot

**Telegram → OpenCode CLI bridge con esteroides.** 5 semanas de refactor, 0 deuda técnica, portfolio-ready.

¿Qué onda, Carnal? Este bot convierte tu Telegram en una terminal completa para **OpenCode CLI**, el orquestador SDD (Spec-Driven Development). Desde el celular, mientras repartes o te echas un taco, ejecutas prompts de desarrollo con acceso a todos los MCPs: **Context7**, **Engram**, **Notion**. Sin laptop, sin terminal, puro Telegram.

---

## 🏗️ Arquitectura (post-refactor)

```
bot.py → AppContainer (DI)
           ├── AIProviderFactory → OpenCodeCLIBackend (AIBackend Protocol)
           ├── TelegramAdapter (BotPort Protocol)
           ├── SessionStore → sessions.json
           ├── MessageSender (mensajería unificada)
           └── PromptService (orquestación)

handlers → _get_container(context).prompt_service.execute()
        → locales/get_strings() para i18n
        → @authorized decorator para auth
```

**Lo que cambió en 5 semanas:**
- 🧹 **5 dicts globales mutables → 0** — todo el estado en `SessionStore`
- 🧩 **`_process_prompt` de 273 líneas → `PromptService.execute()`**
- 📨 **3 patrones de envío de mensajes → 1 `MessageSender`**
- 🔌 **16 `import bot` → 0** — todo por inyección de dependencias
- 🔓 **Vendor lock a `subprocess` → `AIBackend` Protocol**
- 🔓 **Vendor lock a `telegram.Bot` → `BotPort` Protocol**
- 🌐 **~93 strings hardcodeadas en español → `locales/es.py`**
- 🧪 **25 tests → 31 tests**
- 💯 **Health Score: 5.5 → portfolio-ready**

---

## 📁 Estructura del proyecto

```
Sdd-Orchestrator-Telegram-Bot/
├── bot.py                      # Punto de entrada, AppContainer, handlers
├── config.py                   # Configuración, constantes, logger
├── pyproject.toml              # ruff, mypy strict, pytest
├── launcher.bat                # Babysitter con git pull + auto-restart
├── sessions.json               # Persistencia de sesiones (fuente de verdad)
├── sessions.example.json       # Template para nuevos contributors
│
├── services/                   # Capa de servicios (DI)
│   ├── container.py            # AppContainer (inyección de dependencias)
│   ├── session_store.py        # SessionStore (persistencia en JSON)
│   ├── message_sender.py       # MessageSender (mensajería unificada)
│   ├── prompt_service.py       # PromptService (orquestación de prompts)
│   ├── ai_backend.py           # AIBackend Protocol (interfaz abstracta)
│   ├── bot_port.py             # BotPort Protocol (interfaz abstracta)
│   ├── ai_provider_factory.py  # AIProviderFactory (registry de backends)
│   ├── opencode_cli_backend.py # OpenCodeCLIBackend (implementación real)
│   └── telegram_adapter.py     # TelegramAdapter (implementación real)
│
├── handlers/                   # Handlers de comandos
│   ├── commands.py             # /start, /help, /status, /new, /model, /cancel, /open, /config
│   ├── sessions.py             # /session new|list|switch|delete|info|discover|adopt
│   ├── messages.py             # Mensajes de texto y voz
│   ├── admin.py                # /test_md, /session_preview
│   └── ci.py                   # /pr (crear PR) y /update (auto-restart)
│
├── locales/                    # Internacionalización (i18n)
│   ├── __init__.py             # get_strings() loader
│   └── es.py                   # Strings en español (centralizados)
│
├── formatting/                 # Transformación de texto (sin I/O)
│   └── markdown.py             # clean_opencode_output, telegramify_markdown, split_message
│
├── utils/                      # Utilidades
│   ├── logging.py              # Configuración de logging
│   └── time_formatting.py      # relative_time (formato "hace X min")
│
├── tests/                      # Tests (31 tests)
│   ├── test_session_store.py   # Tests de SessionStore (6 nuevos)
│   ├── test_session_parse.py   # Tests de parseo de sesiones
│   ├── test_persistence.py     # Tests de persistencia
│   ├── test_utils.py           # Tests de utilidades
│   └── conftest.py             # Fixtures compartidos
│
├── .github/workflows/ci.yml    # CI/CD con GitHub Actions
├── .pre-commit-config.yaml     # Hooks: ruff + mypy
└── .gitattributes              # LF/CRLF consistency
```

---

## 📋 Comandos del bot

| Comando | Descripción | Ejemplo |
|---|---|---|
| `/start` | Inicia el bot, mensaje de bienvenida | `/start` |
| `/help` | Lista todos los comandos | `/help` |
| `/status` | Estado de sesión, modelo, uptime | `/status` |
| `/new` | Reinicia la sesión activa | `/new` |
| `/model pro` | Cambia a deepseek-v4-pro (razonamiento profundo) | `/model pro` |
| `/model flash` | Cambia a deepseek-v4-flash (rápido) | `/model flash` |
| `/config` | Configuración por chat (modelo, timeout, workdir, provider) | `/config` |
| `/cancel` | Cancela el prompt en ejecución | `/cancel` |
| `/open <prompt>` | Envía un prompt explícito | `/open explica este código` |
| `/session new <nombre>` | Crea una sesión nombrada (lazy) | `/session new mi_feature` |
| `/session list` | Lista todas las sesiones del chat | `/session list` |
| `/session switch <nombre>` | Cambia a otra sesión | `/session switch docs` |
| `/session delete <nombre>` | Elimina una sesión | `/session delete vieja` |
| `/session info [nombre]` | Detalles enriquecidos de sesión | `/session info` |
| `/session discover` | Descubre sesiones OpenCode existentes | `/session discover` |
| `/session adopt <id> <nombre>` | Adopta una sesión por ID real | `/session adopt ses_xxx mi_sesion` |
| `/pr <título>` | Crea un PR en GitHub desde Telegram | `/pr Fix: corrige timeout en Windows` |
| `/update` | Auto-restart del bot (git pull + restart) | `/update` |
| _(cualquier texto)_ | Prompt directo al orquestador SDD | `¿Qué es SDD?` |

### Nuevos comandos (post-refactor)

**`/config`** — Configuración por chat sin tocar `.env`:
- Modelo (`pro` / `flash`)
- Timeout de prompts
- Workdir personalizado
- Provider (`opencode`)

**`/pr <título>`** — Crea un Pull Request en GitHub desde Telegram. Lee `CHANGELOG.md` del workdir del chat, ejecuta `gh pr create` y te devuelve la URL del PR.

**`/update`** — Reinicia el bot automáticamente. Hace `os._exit(42)`, el `launcher.bat` detecta el código de salida y ejecuta `git pull` + restart. Cero intervención manual.

---

## 🚀 Cómo ejecutar

```powershell
# Usa el babysitter para auto-restart en /update (recomendado)
.\launcher.bat

# Ejecución directa (sin auto-restart)
python -m bot
```

### Requisitos

- **Python 3.11+**
- **Node.js** (para OpenCode CLI)
- **OpenCode CLI** instalado globalmente (`npm install -g @anthropic/opencode`)
- **Bot de Telegram** creado con @BotFather
- Variables en `.env`:
  ```bash
  TELEGRAM_BOT_TOKEN=<token>
  ALLOWED_CHAT_IDS=<chat_id_1>,<chat_id_2>
  OPENCODE_WORKDIR=C:\Users\marie\Desktop\mono\python
  OPENCODE_TIMEOUT=600
  ```

### Dependencias

```bash
pip install -r requirements.txt
pip install -e ".[dev]"  # ruff, mypy, pytest
```

---

## 🧪 Testing y calidad

```bash
# Ejecutar tests
pytest                          # 31 tests

# Linting y type checking
ruff check .
mypy .

# Pre-commit hooks
pre-commit run --all-files
```

**Métricas post-refactor:**
- **31 tests** (25 antes del refactor)
- **mypy strict** mode (cero `type: ignore`)
- **ruff** con reglas E, F, I, N, W, UP, B, C4, SIM, T20
- **CI/CD** vía GitHub Actions en cada push

---

## 🔄 CI/CD Pipeline

El pipeline corre en **GitHub Actions** (`.github/workflows/ci.yml`) y también desde la calle:

| Entorno | Qué hace |
|---|---|
| **GitHub Actions** | ruff lint, mypy strict, pytest en cada push |
| **Telegram `/pr`** | Crea PRs desde el celular (lee CHANGELOG, ejecuta gh pr create) |
| **Telegram `/update`** | Auto-restart: `os._exit(42)` → `launcher.bat` hace git pull + restart |

**Flujo de auto-restart (`/update`):**
```
Usuario → /update → bot hace os._exit(42)
                         ↓
launcher.bat detecta exit code 42
                         ↓
git pull origin main
                         ↓
python -m bot  (reinicio limpio)
```

---

## 🌐 i18n — Todos los strings en un solo lugar

Antes del refactor: ~93 strings en español hardcodeadas en `bot.py` y handlers.
Después del refactor: todas en `locales/es.py`, accedidas vía `get_strings()`.

```python
from locales import get_strings
strings = get_strings()
await update.message.reply_text(strings["welcome"])
```

¿Querés agregar inglés? Creás `locales/en.py` con las mismas keys y el loader lo detecta automático. Así de limpio, Carnal.

---

## 🧠 Por qué este refactor importa

| Antes | Después |
|---|---|
| 1 archivo de ~1500 líneas (`bot.py`) | 18 archivos organizados por responsabilidad |
| 5 dicts globales mutables | 0 — `SessionStore` thread-safe |
| `import bot` circular por todos lados | 0 — DI con `AppContainer` |
| Dependencia directa de `subprocess` | `AIBackend` Protocol (cambiable) |
| Dependencia directa de `telegram.Bot` | `BotPort` Protocol (testeable) |
| Strings en español dispersas | `locales/es.py` centralizado |
| 25 tests | 31 tests |
| Deuda técnica creciente | Deuda técnica en cero |

El código ahora es **testeable**, **extensible** y **mantenible**. Podés cambiar OpenCode por otro backend, cambiar Telegram por Discord, o agregar inglés en minutos. No en semanas.

---

*Mantenido con ❤️ desde la calle. Si se rompe, `/update` y pa'lante.*
