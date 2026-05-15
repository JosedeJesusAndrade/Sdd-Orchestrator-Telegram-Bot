# Telegram-OpenCode SDD Bridge — Documentacion Completa

---

## Indice / Tabla de Contenidos

1. [Vision General](#-vision-general)
2. [Arquitectura](#-arquitectura)
3. [Estructura del Proyecto](#-estructura-del-proyecto)
4. [Comandos del Bot](#-comandos-del-bot)
5. [Flujo de Trabajo SDD](#-flujo-de-trabajo-sdd)
6. [Configuracion](#-configuracion)
7. [Seguridad](#-seguridad)
8. [Problemas Conocidos y Soluciones](#-problemas-conocidos-y-soluciones)
9. [Guia de Instalacion](#-guia-de-instalacion)
10. [Evolucion del Proyecto](#-evolucion-del-proyecto)
11. [Roadmap / Mejoras Futuras](#-roadmap--mejoras-futuras)

---

## Vision General

### Que es el proyecto

El **Telegram-OpenCode SDD Bridge** es un bot de Telegram que actua como puente entre un usuario y **OpenCode CLI**, el orquestador SDD (Spec-Driven Development). Convierte Telegram en una interfaz movil completa para ejecutar prompts de desarrollo de software con acceso total a MCPs (Model Context Protocols) como **Context7**, **Engram** y **Notion**.

A partir de la v1.7, el bridge incorpora **gestion multi-sesion** con `sessions.json` como fuente de verdad, **adopcion de sesiones OpenCode existentes** via `/session discover | adopt`, y **consulta enriquecida de metadatos** desde la base SQLite de OpenCode. La v1.8 (Phase 0 UX) agrega **contador asincrono**, **MarkdownV2 con fallback a texto plano**, **conversion de tablas**, y **filtrado de tool traces multilinea**.

### Caso de uso principal

El usuario es un **repartidor de Uber** que necesita gestionar proyectos de software mientras esta en la calle. Desde su telefono:
- Inicia y navega entre multiples sesiones SDD con `/session new|list|switch|delete|info`
- Adopta sesiones OpenCode existentes con `/session discover | adopt`
- Consulta documentacion de librerias via Context7
- Persiste decisiones y descubrimientos en Engram
- Lee y escribe paginas de Notion
- Ve respuestas con formato (negrita, codigo, tablas) via MarkdownV2
- Monitorea el progreso de prompts largos con un contador asincrono cada 5s
- Todo sin tocar una laptop ni una terminal

### Problema que resuelve

OpenCode CLI solo se ejecuta en una terminal de escritorio. No tiene interfaz web ni movil. Este bridge expone **el 100% de la funcionalidad del orquestador SDD** a traves de un chat de Telegram, permitiendo:

- **Desarrollo remoto**: Ejecutar prompts de codigo desde cualquier lugar con senal movil
- **Multi-sesion**: Gestionar varias sesiones SDD simultaneamente (ej: `bot_telegram`, `docs`, `balanceate_api`) y alternar entre ellas con un comando
- **Continuidad de sesion**: Retomar trabajo exactamente donde se dejo, incluso horas despues
- **Acceso a MCPs**: Todos los MCPs configurados en OpenCode (Context7, Engram, Notion) estan disponibles
- **Control granular**: Cambiar modelo (pro vs flash), cancelar prompts, reiniciar sesiones
- **Persistencia entre reinicios**: `sessions.json` sobrevive a reinicios del bot; las sesiones mapeadas no se pierden

---

## Arquitectura

### Diagrama de flujo

```
  Telegram App                    Windows PC (daemon)
  ─────────────                   ────────────────────
  ┌──────────┐     HTTP/API       ┌─────────────────────┐
  │ Usuario  │ ──────────────────>│ python-telegram-bot │
  │ (movil)  │ <──────────────────│ (v22+, asyncio)     │
  └──────────┘     Mensajes       └──────┬──────────────┘
                                         │ subprocess.Popen
                                         │      +
                                         │ loop.run_in_executor
                                         ▼
                               ┌─────────────────────┐
                               │   OpenCode CLI       │
                               │   (opencode.cmd)     │
                               │   npm global pkg     │
                               └──────┬──────────────┘
                                      │
                       ┌──────────────┼──────────────┐
                       ▼              ▼              ▼
                  ┌─────────┐  ┌──────────┐  ┌──────────┐
                  │Context7 │  │  Engram  │  │  Notion  │
                  │  MCP    │  │   MCP    │  │   MCP    │
                  └─────────┘  └──────────┘  └──────────┘

  Capa de Sesiones (filesystem)
  ──────────────────────────────
  sessions.json  ◄───►  active_sessions (in-memory cache)
       │                       │
       │  {chat_id:            │  session_name, session_id,
       │   active: "name",     │  first_message, last_used,
       │   sessions: {         │  prompt_count
       │     name: {           │
       │       id: "ses_xxx",  │
       │       title: "...",   │
       │       ...             │
       │     }}}               │
       │                       │
       └─────── SQLite ────────┘
               opencode db
               (metadatos enriquecidos)
```

### Componentes principales

| Componente | Rol | Tecnologia |
|---|---|---|
| **Telegram Bot** | Recibir comandos, enviar respuestas | `python-telegram-bot` v22+ |
| **Bridge Daemon** | Gestionar sesiones, ejecutar OpenCode, limpiar output | `bot.py` (~1500 lineas) |
| **Sessions.json** | Persistencia de mapeo sesion-nombre → ID real | JSON en disco |
| **OpenCode CLI** | Orquestador SDD con acceso a MCPs | npm global package |
| **OpenCode SQLite DB** | Metadatos enriquecidos (mensajes, modelo, timestamps) | Consultado via `opencode db` |
| **MCPs** | Context7 (docs), Engram (memoria), Notion (paginas) | Protocolos MCP nativos |
| **Event Loop** | Mantener el bot responsivo durante prompts largos | `asyncio` + `nest_asyncio` |

### Capa de gestion de sesiones (sessions.json ↔ SQLite)

El bridge mantiene dos niveles de datos sobre sesiones:

1. **sessions.json** — Fuente de verdad (persiste entre reinicios):
   ```
   {chat_id: {
     active: "nombre_sesion",
     sessions: {
       "nombre": {id, title, created, last_used, prompt_count}
     }
   }}
   ```
   - `active` determina que sesion se usa en el proximo prompt
   - Cada entrada tiene el `id` real de OpenCode (`ses_xxx`) o `null` si es lazy (no ejecutada aun)
   - `_process_prompt()` lee SIEMPRE de sessions.json primero; `active_sessions` es solo cache volatil

2. **SQLite de OpenCode** — Metadatos enriquecidos (solo lectura):
   - Tablas: `session`, `message`, `prompt`, `file`, `event`
   - Schema descubierto en startup via `PRAGMA table_info`
   - `/session info` consulta: mensajes en BD, modelo usado, fecha de creacion real
   - Acceso via `opencode db "SQL" --format json` (NUNCA sqlite3 directo)

3. **active_sessions (dict en memoria)** — Cache volatil para uso durante la sesion activa:
   - Se sincroniza con sessions.json en cada `_process_prompt()`
   - Se invalida al hacer `/session switch` o `/new`
   - Pierde datos si el bot se reinicia (pero sessions.json sobrevive)

### Stack tecnologico

```
Python 3.13
├── python-telegram-bot >= 20.0     # Framework de bots Telegram
├── nest_asyncio >= 1.6.0           # Permite anidar event loops (necesario en Windows)
├── python-dotenv == 1.1.1          # Carga variables de entorno desde .env
├── subprocess (stdlib)              # Ejecuta OpenCode CLI como proceso hijo
├── asyncio (stdlib)                 # Event loop asincrono para el bot
├── json (stdlib)                    # Persistencia de sessions.json
├── logging (stdlib)                 # Logs rotativos a archivo y consola
├── re (stdlib)                      # Limpieza de ANSI escape codes y parseo de session list
└── OpenCode CLI (npm)               # Orquestador SDD externo
```

### Mecanismo de ejecucion de OpenCode

El bridge **no interpreta ni modifica los prompts**. Cada mensaje del usuario se convierte en:

```bash
# Sesion existente con ID real:
opencode run --model deepseek/deepseek-v4-pro --continue --session ses_xxx "<prompt>"

# Sesion nueva (sin ID aun):
opencode run --model deepseek/deepseek-v4-pro "<prompt>"
```

El flag `--continue` se usa SOLO cuando hay un `session_id` real (`ses_xxx`), no un ID inventado. La captura del ID real ocurre en la primera ejecucion exitosa de una sesion nueva y se persiste en `sessions.json`.

La ejecucion ocurre en un **subprocess.Popen** con `encoding="utf-8"` dentro de un `run_in_executor` (thread pool), lo que mantiene el event loop de Telegram libre para procesar `/cancel` y otros comandos.

---

## Estructura del Proyecto

```
Balanceate/                          # Raiz del proyecto (app Reflex)
├── .env                             # Variables de entorno (tokens, config)
├── .gitignore                       # Exclusiones de git
├── requirements.txt                 # Dependencias Python del proyecto
├── telegram_bridge/                 # ── EL BRIDGE ──
│   ├── __init__.py                  # Marca el directorio como paquete
│   ├── bot.py                       # DAEMON PRINCIPAL (~1500 lineas)
│   ├── sessions.json                # Mapeo persistente de sesiones (fuente de verdad)
│   ├── get_chat_id.py               # Utilidad para obtener el chat ID
│   ├── run_bot.bat                  # Launcher para Windows
│   ├── bot.log                      # Logs rotativos (5 MB max, 3 backups)
│   └── DOCUMENTACION.md             # Este archivo
├── Balanceate/                      # App web Balanceate (Reflex)
├── assets/                          # Recursos estaticos
├── venv/                            # Entorno virtual Python
└── .atl/                            # Artefactos SDD y skill registry
```

### Descripcion de cada archivo del bridge

#### `bot.py` — El corazon del sistema (~1500 lineas)

| Linea(s) | Funcion / Bloque | Descripcion |
|---|---|---|
| 1-7 | Docstring | Descripcion general del modulo |
| 9-36 | Imports | stdlib, nest_asyncio, dotenv, telegram.ext (incluye ParseMode) |
| 38-41 | Config paths | Resuelve BASE_DIR, SESSION_DB, carga .env |
| 44-55 | `resolve_opencode_cmd()` | Busca el ejecutable de opencode en 4 ubicaciones |
| 57-77 | Constantes | BOT_TOKEN, ALLOWED_CHAT_IDS, OPENCODE_WORKDIR, OPENCODE_TIMEOUT, DEFAULT_MODEL |
| 79-97 | Logger | Configuracion de logging a consola + archivo rotativo |
| 99-106 | Validacion inicial | Verifica que BOT_TOKEN este configurado |
| 108-121 | Estado global | active_sessions, current_model, current_process, cancel_requests |
| 123-127 | `mask_chat_id()` | Ofusca chat IDs en logs |
| 130-147 | `_filter_stderr()` | Filtra lineas de metadata de stderr |
| 150-176 | `_remove_tool_traces()` | Tracking de llaves `{ }` para filtrar tool calls multilinea |
| 179-221 | `clean_opencode_output()` | Elimina ANSI, lineas de build, tool traces, auto-reject |
| 224-272 | `telegramify_markdown()` | Convierte tablas `\|...\|` a bloques monospace (MarkdownV2 no soporta tablas) |
| 275-305 | `split_message()` | Parte respuestas > 4000 caracteres para Telegram |
| 308-309 | `authorize()` | Verifica si el chat_id esta en la whitelist |
| 312-325 | `load_session_map()` / `save_session_map()` | Persistencia de sessions.json |
| 328-341 | `parse_opencode_session_list()` | Regex con `\s{2,}` para parsear columnas de `opencode session list` |
| 344-370 | `fetch_opencode_sessions()` | Ejecuta `opencode session list` en `run_in_executor` |
| 373-393 | `query_opencode_db()` | Ejecuta `opencode db "SQL" --format json` (NO sqlite3 directo) |
| 396-435 | `run_opencode()` | Ejecuta OpenCode con Popen + encoding utf-8 + timeout + taskkill |
| 438-454 | `_relative_time()` | Convierte datetime a "hace X min" en espanol |
| 457-469 | `_assemble_response()` | Combina stdout y stderr filtrado |
| 472-486 | `progress_updater()` | Contador asincrono: actualiza mensaje "Procesando..." cada 5s con asyncio.Event |
| 489-775 | `_process_prompt()` | **Funcion central**: lee sessions.json, sincroniza cache, ejecuta, captura ID real, MarkdownV2 + fallback |
| 779-792 | `start_command()` | Handler de /start |
| 795-817 | `help_command()` | Handler de /help con todos los comandos (incluyendo /session) |
| 820-868 | `status_command()` | Handler de /status con nombre de sesion, ID real, prompt count |
| 871-891 | `new_command()` | Handler de /new (limpia ID en sessions.json) |
| 894-922 | `model_command()` | Handler de /model (cambia pro/flash) |
| 925-950 | `cancel_command()` | Handler de /cancel (taskkill + flag) |
| 953-986 | `session_preview_command()` | Handler de /session_preview (atajo visual de sesiones) |
| 989-1024 | `session_command()` | Router de subcomandos /session |
| 1027-1057 | `_session_new()` | Crea sesion lazy (solo sessions.json, sin llamar a OpenCode) |
| 1060-1099 | `_session_list()` | Lista sesiones con marcador de activa |
| 1102-1129 | `_session_switch()` | Cambia sesion activa e invalida cache |
| 1132-1186 | `_session_delete()` | Elimina sesion (local + `opencode session delete`) |
| 1189-1255 | `_session_info()` | Detalles enriquecidos con consultas a SQLite (mensajes, modelo, fecha) |
| 1258-1309 | `_session_discover()` | Descubre sesiones OpenCode existentes para adopcion |
| 1312-1380 | `_session_adopt()` | Adopta una sesion OpenCode por ID real |
| 1383-1400 | `handle_message()` | Handler para mensajes de texto sin comando |
| 1403-1421 | `open_command()` | Handler de /open (prompt explicito) |
| 1424-1446 | `build_application()` | Construye la Application de Telegram con todos los handlers |
| 1449-1498 | `run_bot()` | Corre el bot: schema discovery en startup, manejo de senales |
| 1501-1508 | `__main__` | Punto de entrada, crea y ejecuta el event loop |

#### `sessions.json` — Persistencia de sesiones

Archivo JSON que actua como **fuente de verdad** para el mapeo de sesiones. Cada entrada por chat_id:

```json
{
  "8664220427": {
    "sessions": {
      "default": {
        "id": "ses_1dad0b10effeP067uYWHlVM0U2",
        "title": "...",
        "created": "2026-05-14T06:32:31+00:00",
        "last_used": "2026-05-14T06:32:31+00:00",
        "prompt_count": 1
      },
      "bot_telegram": {
        "id": "ses_20477ac36ffec2aYyBiD359Zfp",
        "title": "OpenCode remoto con bot de Telegram",
        "created": "2026-05-14T06:39:51+00:00",
        "last_used": "2026-05-14T16:16:07+00:00",
        "prompt_count": 11
      }
    },
    "active": "bot_telegram"
  }
}
```

Caracteristicas clave:
- `id: null` en sesiones lazy (creadas con `/session new` pero sin ejecutar aun)
- `active` determina que sesion se usa en el proximo prompt
- Sobrevive a reinicios del bot (a diferencia de `active_sessions`)
- `_process_prompt()` lo lee al inicio de cada ejecucion para sincronizar estado

#### `get_chat_id.py`

Script independiente para obtener el chat ID de un usuario de Telegram. Util cuando se configura el bot por primera vez:

1. Inicia un bot minimalista solo con `/start`
2. El usuario envia `/start` desde Telegram
3. El script imprime el `chat_id` en consola
4. Ese ID se agrega a `ALLOWED_CHAT_IDS` en `.env`

#### `run_bot.bat`

Launcher para Windows que:
1. Cambia al directorio del proyecto
2. Ejecuta `python -m telegram_bridge.bot`
3. Mantiene la ventana abierta con `pause`

#### `__init__.py`

Vacio. Marca `telegram_bridge/` como un paquete Python para permitir `python -m telegram_bridge.bot`.

---

## Comandos del Bot

### Tabla completa

| Comando | Funcion | Ejemplo |
|---|---|---|
| `/start` | Inicia el bot, muestra mensaje de bienvenida | `/start` |
| `/help` | Lista todos los comandos disponibles | `/help` |
| `/status` | Muestra estado de sesion, modelo, uptime | `/status` |
| `/new` | Reinicia la sesion activa (limpia ID, preserva modelo) | `/new` |
| `/model pro` | Cambia a modelo deepseek-v4-pro (pensamiento profundo) | `/model pro` |
| `/model flash` | Cambia a modelo deepseek-v4-flash (rapido) | `/model flash` |
| `/model` | Muestra el modelo actual sin cambiarlo | `/model` |
| `/cancel` | Cancela el prompt en ejecucion (mata el proceso) | `/cancel` |
| `/open <prompt>` | Envia un prompt explicitamente al orquestador | `/open explica este codigo` |
| `/session new <nombre>` | Crea una sesion nombrada (lazy, sin ejecutar OpenCode) | `/session new bot_telegram` |
| `/session list` | Lista todas las sesiones del chat con marcador de activa | `/session list` |
| `/session switch <nombre>` | Cambia a otra sesion nombrada | `/session switch docs` |
| `/session delete <nombre>` | Elimina una sesion (local + OpenCode si tiene ID real) | `/session delete vieja` |
| `/session info [nombre]` | Detalles enriquecidos de una sesion (usa activa si se omite nombre) | `/session info bot_telegram` |
| `/session discover` | Descubre sesiones OpenCode existentes (no adoptadas aun) | `/session discover` |
| `/session adopt <id> <nombre>` | Adopta una sesion OpenCode existente por su ID real | `/session adopt ses_xxx mi_sesion` |
| `/session_preview` | Atajo: vista combinada de sesiones del bot + raw de OpenCode | `/session_preview` |
| _(cualquier texto)_ | Atajo: texto sin comando = prompt directo | `Que es SDD?` |

### Descripcion detallada

#### `/start`
Muestra un mensaje de bienvenida con los comandos basicos. No inicia sesion SDD (eso ocurre con el primer prompt).

#### `/help`
Lista todos los comandos disponibles con su sintaxis, incluyendo los 7 subcomandos de `/session`. Incluye el recordatorio de que cualquier mensaje funciona como prompt.

#### `/status`
Despliega un panel con:
- **Nombre de sesion**: nombre asignado (ej: `bot_telegram`)
- **ID OpenCode**: ID real `ses_xxx` o "pendiente" si aun no se ejecuto
- **Modelo activo**: deepseek-v4-pro o deepseek-v4-flash
- **Primera interaccion**: "hace X min" desde que inicio la sesion
- **Ultima interaccion**: "hace X min" desde el ultimo mensaje
- **Total de prompts**: cuantos mensajes se han procesado en esta sesion
- **Uptime del bot**: cuanto tiempo lleva corriendo el daemon

#### `/new`
Reinicia la sesion activa: limpia el `id` real en `sessions.json` (lo pone a `null`) y borra el cache en memoria. El proximo mensaje comenzara desde cero, capturando un nuevo ID de OpenCode. **El modelo elegido se preserva** (no vuelve al default).

#### `/model pro` / `/model flash`
Cambia el modelo de OpenCode para el chat actual:
- **pro** (`deepseek/deepseek-v4-pro`): Modelo completo con razonamiento profundo. Ideal para tareas complejas, debugging, arquitectura.
- **flash** (`deepseek/deepseek-v4-flash`): Modelo rapido y economico. Ideal para consultas simples, recordatorios, preguntas rapidas.

El cambio persiste incluso despues de `/new`.

#### `/cancel`
Mata inmediatamente el proceso de OpenCode en ejecucion. Util cuando:
- Un prompt esta tardando demasiado
- El usuario cambio de opinion
- El modelo entro en un loop

En Windows usa `taskkill /F /T /PID` para matar el arbol de procesos completo (incluye procesos hijo de Node.js).

#### `/open <prompt>`
Envia un prompt explicitamente. Equivalente a enviar texto sin comando, pero util cuando el mensaje podria confundirse con otro comando.

#### `/session new <nombre>`
Crea una sesion nombrada de forma **lazy**: solo escribe la entrada en `sessions.json` con `id: null`. No ejecuta OpenCode. El ID real se captura automaticamente en el primer prompt exitoso dentro de esa sesion. El nombre debe tener entre 1 y 30 caracteres alfanumericos con guiones.

#### `/session list`
Lista todas las sesiones del chat desde `sessions.json`. Muestra:
- Marcador verde (🟢) para la sesion activa, gris (⚪) para las inactivas
- ID de OpenCode (truncado) o "(nueva, sin usar aun)"
- Cantidad de prompts y tiempo desde el ultimo uso

#### `/session switch <nombre>`
Cambia la sesion activa. Actualiza el campo `active` en `sessions.json` e **invalida el cache en memoria** (`active_sessions`). El proximo prompt usara la nueva sesion.

#### `/session delete <nombre>`
Elimina una sesion del mapeo. Si tenia un ID real de OpenCode, tambien ejecuta `opencode session delete <id>` para limpiar en OpenCode. Si la sesion eliminada era la activa, vuelve a `default`.

#### `/session info [nombre]`
Muestra detalles enriquecidos de una sesion. Si se omite el nombre, usa la sesion activa. Ademas de los datos de `sessions.json`, consulta la base SQLite de OpenCode via `opencode db` para mostrar:
- **Mensajes en BD**: cuantos mensajes hay registrados en `message` table
- **Creada (BD)**: timestamp de creacion desde `session` table
- **Modelo (BD)**: modelo usado en la sesion desde `session` table

La consulta SQLite es no-fatal: si falla, solo muestra los datos locales.

#### `/session discover`
Ejecuta `opencode session list` y muestra todas las sesiones OpenCode existentes. Para cada una indica si ya fue adoptada (con su nombre local) o si esta disponible para adopcion (con el comando exacto para adoptarla).

#### `/session adopt <id> <nombre>`
Adopta una sesion OpenCode existente por su ID real (`ses_xxx`). La agrega al mapeo en `sessions.json` con el nombre dado. Util para recuperar sesiones creadas fuera del bot (ej: desde la terminal) y seguir usandolas desde Telegram.

#### `/session_preview`
Atajo que combina la vista de sesiones del bot (desde `sessions.json`) con la lista raw de sesiones OpenCode. Util para diagnostico rapido.

#### Mensaje directo (sin comando)
Cualquier texto que no empiece con `/` se trata como un prompt SDD. Es la forma mas natural de interactuar:

```
Usuario: Que framework usamos en Balanceate?
Bot: [Procesando...] → Respuesta de OpenCode
```

### Ejemplos de uso real

```
# Iniciar el dia de trabajo
/start
/model pro

# Crear sesiones para distintos temas
/session new balanceate_api
/session new docs
/session new bot_telegram

# Trabajar en una feature
/open necesito agregar autenticacion JWT a la API

# Cambiar de contexto
/session switch docs
/open documenta el endpoint de login

# Adoptar una sesion vieja de la terminal
/session discover
/session adopt ses_abc123def mi_sesion_vieja

# Ver info enriquecida de una sesion
/session info bot_telegram

# Consulta rapida mientras manejo
solo dime que puerto usa MongoDB por defecto

# Verificar estado entre entregas
/status

# Cambiar a modelo rapido para consultas simples
/model flash

# Cancelar un prompt que se colgo
/cancel
```

---

## Flujo de Trabajo SDD

### Como se mantienen las sesiones

OpenCode CLI usa sesiones nativas basadas en un `session_id`. El bridge mantiene un sistema de **doble capa**:

1. **sessions.json** (fuente de verdad, persiste en disco):
   ```json
   {
     "chat_id": {
       "active": "nombre_sesion",
       "sessions": {
         "nombre": {"id": "ses_xxx", "title": "...", ...}
       }
     }
   }
   ```

2. **active_sessions** (cache volatil en memoria):
   ```python
   active_sessions: dict[int, dict] = {}
   # {chat_id: {session_name, session_id, first_message, last_used, prompt_count}}
   ```

### Continuidad entre mensajes

El bridge decide si continuar o iniciar nueva sesion en cada mensaje:

```
Mensaje recibido
  │
  ├── Leer sessions.json → obtener session_name activa + ID real
  │
  ├── Sincronizar active_sessions (invalidar si el nombre cambio)
  │
  ├── No hay ID real? → NUEVA sesion (opencode run ... "<prompt>")
  │
  ├── Sesion expirada? (>30 min sin uso) → NUEVA sesion
  │
  └── Sesion activa con ID real → CONTINUAR (opencode run --continue --session <id> ...)
```

Los flags `--continue` y `--session <id_real>` le dicen a OpenCode que mantenga el contexto de la conversacion anterior. Esto permite dialogos multi-turno como:

```
User: Crea un archivo de configuracion para el proyecto
Bot:  He creado config.py con las variables...

User: Ahora agregale soporte para multiples entornos
Bot:  He actualizado config.py para soportar dev/staging/prod...
```

### Captura de IDs reales

Cuando una sesion es nueva (no tiene `id` en `sessions.json`), el bridge **no inventa IDs**. En lugar de eso:

1. Ejecuta `opencode run "<prompt>"` sin flags `--continue --session`
2. OpenCode crea internamente una sesion con ID real (ej: `ses_1dad0b10effeP067uYWHlVM0U2`)
3. Al terminar exitosamente, el bridge ejecuta `opencode session list` y captura el ID
4. Guarda el ID en `sessions.json` para futuros prompts con `--continue`

Si `fetch_opencode_sessions()` no encuentra resultados, espera 1 segundo y reintenta (OpenCode puede tardar en escribir la sesion a disco).

### Navegacion multi-sesion

El usuario puede alternar entre sesiones manteniendo contexto independiente:

```
/session new api        → Crea sesion "api" (lazy, id=null)
/open crea endpoint     → Captura ID real, prompt #1 en "api"
/session switch docs    → Cambia a "docs" (debe existir)
/open documenta JWT     → Prompt #1 en "docs" (o continua si ya tenia ID)
/session switch api     → Vuelve a "api"
/status                 → Muestra prompt_count=1 en "api"
```

Cada switch invalida `active_sessions[chat_id]` para forzar re-sincronizacion desde `sessions.json` en el proximo prompt.

### Lazy creation

`/session new <nombre>` **no ejecuta OpenCode**. Solo escribe la entrada en `sessions.json`. El ID real se captura en el primer prompt exitoso. Esto permite crear sesiones desde el telefono sin consumir recursos hasta que realmente se necesiten.

### Adopcion de sesiones viejas

El flujo `/session discover` + `/session adopt` permite recuperar sesiones OpenCode creadas fuera del bot:

1. `/session discover` → lista todas las sesiones OpenCode con su ID `ses_xxx`
2. El usuario elige una y ejecuta `/session adopt ses_xxx mi_nombre`
3. El bridge verifica que el ID existe en OpenCode y lo agrega a `sessions.json`
4. Desde ese momento, la sesion se puede usar normalmente

### Timeout de sesion (30 min)

Parametro: `SESSION_TIMEOUT_MINUTES = 30`

Si el usuario no envia mensajes durante 30 minutos:
1. La sesion se marca como expirada
2. Se elimina de `active_sessions` (cache volatil)
3. El proximo mensaje inicia una sesion fresca
4. Se pierde el contexto de la conversacion anterior (OpenCode ya no recibe `--continue`)

Nota: `sessions.json` NO se borra al expirar. La entrada persiste con su `prompt_count` anterior. Una sesion nueva sobreescribira el `id` con el nuevo ID real.

### Cambio de modelo (pro vs flash)

El modelo se configura **por chat**, no por sesion:

```python
current_model: dict[int, str] = {}  # {chat_id: model_name}
DEFAULT_MODEL = "deepseek/deepseek-v4-pro"
```

- `/model pro` → `deepseek/deepseek-v4-pro` (razonamiento profundo, SDD completo)
- `/model flash` → `deepseek/deepseek-v4-flash` (respuestas rapidas, consultas simples)
- El modelo **sobrevive a `/new`** (resets de sesion)
- El modelo **sobrevive a expiraciones de sesion**
- El modelo **sobrevive a `/session switch`** (el modelo es por chat, no por sesion)
- Se pasa como `--model` en cada ejecucion de OpenCode

### Una sesion tipica en el tiempo

```
09:00  /start                       → Bot responde con bienvenida
09:00  /session new feature-x       → Crea sesion lazy
09:01  "Hola"                       → NUEVA sesion SDD (captura ID real)
09:02  "Explica el proyecto"        → CONTINUA sesion (prompt #2)
09:15  /status                      → Muestra 2 prompts, session=feature-x
09:16  /session new docs            → Crea otra sesion
09:17  /session switch docs         → Cambia a docs
09:18  "Documenta la API"           → NUEVA sesion "docs" (captura ID)
09:30  /session switch feature-x    → Vuelve a feature-x
09:31  "Agrega un endpoint"         → CONTINUA sesion (prompt #3 en feature-x)
09:35  /model flash                 → Cambia a modelo rapido
09:36  "Que es JWT?"                → CONTINUA sesion (prompt #4, modelo flash)
10:00  [pausa para delivery]
10:35  "Donde estabamos?"           → Sesion EXPIRADA (>30 min), NUEVA sesion
```

---

## Configuracion

### Variables de entorno (.env)

El archivo `.env` vive en la raiz del proyecto (`Balanceate/.env`) y es cargado por `python-dotenv`:

```bash
# ──── Telegram ────
TELEGRAM_BOT_TOKEN=<token_del_bot_de_BotFather>
ALLOWED_CHAT_IDS=<chat_id_1>,<chat_id_2>,<chat_id_3>

# ──── OpenCode CLI ────
OPENCODE_WORKDIR=C:\Users\marie\Desktop\mono\python\Balanceate
OPENCODE_TIMEOUT=600
OPENCODE_CMD=                                   # (opcional, auto-detectado)
```

| Variable | Requerida | Default | Descripcion |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | **Si** | — | Token del bot obtenido de @BotFather |
| `ALLOWED_CHAT_IDS` | **Si** | `""` | Lista de chat IDs separados por coma (whitelist) |
| `OPENCODE_WORKDIR` | No | Raiz del proyecto | Directorio donde se ejecuta OpenCode |
| `OPENCODE_TIMEOUT` | No | `300` | Timeout en segundos para cada prompt (recomendado: 600 para prompts largos) |
| `OPENCODE_CMD` | No | Auto-detect | Ruta al ejecutable de OpenCode |

### Resolucion automatica de OPENCODE_CMD

El bridge busca el ejecutable en este orden (`bot.py:44-55`):

1. Variable de entorno `OPENCODE_CMD`
2. `shutil.which("opencode")` — busca en PATH
3. `shutil.which("opencode.cmd")` — busca wrapper de npm en Windows
4. Ruta conocida: `C:\Users\<user>\AppData\Roaming\npm\opencode.cmd`

Si ninguna funciona, usa `"opencode"` como fallback (confiando en el PATH del sistema).

### Formato de sessions.json

El archivo `sessions.json` se crea automaticamente en `telegram_bridge/`. Estructura:

```json
{
  "<chat_id>": {
    "active": "<nombre_sesion>",
    "sessions": {
      "<nombre>": {
        "id": "ses_xxx" | null,
        "title": "<titulo>",
        "created": "<ISO8601>",
        "last_used": "<ISO8601>" | null,
        "prompt_count": <int>
      }
    }
  }
}
```

- `active`: nombre de la sesion que se usara en el proximo prompt
- `id`: ID real de OpenCode (`ses_xxx`) o `null` si la sesion es lazy (creada pero nunca ejecutada)
- `created`: timestamp ISO8601 de cuando se creo el mapeo (no necesariamente cuando OpenCode creo la sesion)
- `prompt_count`: contador incremental actualizado tras cada ejecucion exitosa

### MCPs configurados

Los MCPs NO se configuran en el bridge — se configuran en OpenCode CLI (via `opencode` o archivos de configuracion de OpenCode). El bridge simplemente hereda todos los MCPs disponibles:

| MCP | Funcion | Usado para |
|---|---|---|
| **Context7** | Documentacion de librerias actualizada | Consultar APIs, frameworks, ejemplos de codigo |
| **Engram** | Memoria persistente entre sesiones | Guardar decisiones, bugs, patrones, descubrimientos |
| **Notion** | Lectura/escritura de paginas y bases de datos | Gestion de documentos, notas, tracking |

### Timeout y settings

```python
# bot.py constantes
OPENCODE_TIMEOUT = 600          # 10 minutos (configurable en .env)
SESSION_TIMEOUT_MINUTES = 30    # 30 minutos (hardcodeado)
LOG_FILE maxBytes = 5 * 1024 * 1024   # 5 MB rotacion
LOG_FILE backupCount = 3        # 3 archivos de backup
```

### Logging

El sistema usa dos handlers:

1. **Consola** (`sys.stdout`): Formato simplificado para monitoreo en tiempo real
2. **Archivo rotativo** (`bot.log`): 5 MB maximo, 3 backups, encoding UTF-8

Ejemplo de salida del log:

```
2026-05-14 06:32:31 [INFO] opencode_bot: Session for 86******27: first message (model=deepseek/deepseek-v4-pro)
2026-05-14 06:32:31 [INFO] opencode_bot: Captured session: default -> ses_1dad0b10effeP067uYWHlVM0U2
2026-05-14 06:32:31 [INFO] opencode_bot: Completed for 86******27 | duration=7.8s | response_len=884 | exit=0
2026-05-14 06:39:51 [INFO] opencode_bot: Session 'bot_telegram' created for 86******27
2026-05-14 16:16:07 [INFO] opencode_bot: Session bot_telegram: continuing (model=deepseek/deepseek-v4-pro)
```

Los chat IDs se ofuscan parcialmente (`86******27`) por privacidad.

---

## Seguridad

### Whitelist por chat_id

El mecanismo principal de seguridad es la whitelist de `ALLOWED_CHAT_IDS`. **Cada mensaje entrante** pasa por la funcion `authorize()`:

```python
def authorize(chat_id: int) -> bool:
    return chat_id in ALLOWED_CHAT_IDS
```

Si un chat_id no esta en la lista:
- El mensaje se ignora silenciosamente
- Se registra en logs como `"Unauthorized /XXX from <masked_id>"`
- El remitente NO recibe respuesta (ni siquiera un "no autorizado")

### Token en .env

- El token de Telegram **nunca se hardcodea** en el codigo
- Se almacena en `.env` (excluido de git via `.gitignore`)
- Si no se encuentra, el bot hace `sys.exit(1)` con un mensaje critico
- El `.gitignore` incluye `.env` explicitamente

### Auto-rechazo de permisos externos

La funcion `clean_opencode_output()` (`bot.py:214-219`) filtra activamente mensajes relacionados con permisos:

```python
if 'auto-rejecting' in lower or 'permission requested' in lower:
    continue
if 'user rejected permission' in lower:
    continue
```

Esto evita que el bot reenvie dialogos de confirmacion de OpenCode que podrian confundir al usuario.

### Proteccion contra ejecucion paralela

El bridge **rechaza prompts adicionales** mientras hay uno en ejecucion (`bot.py:1394-1398`):

```python
if chat_id in current_process:
    await update.message.reply_text(
        "\u23f3 Ya hay un prompt en proceso. Us\u00e1 /cancel para cancelarlo."
    )
    return
```

Esto previene condiciones de carrera y consumo excesivo de recursos.

### SQL Injection prevention en consultas DB

Las consultas a `query_opencode_db()` interpolan el `session_id` directamente en el SQL. Para mitigar riesgos, `_session_info()` valida que el ID cumpla el formato `ses_xxx` via regex antes de consultar:

```python
if oc_id and re.match(r'^[a-zA-Z0-9_]+$', oc_id):
    msg_rows = await query_opencode_db(...)
```

Esto previene que un `session_id` malicioso en `sessions.json` inyecte SQL.

### Masking de chat IDs en logs

La funcion `mask_chat_id()` ofusca IDs antes de escribirlos en logs:

```python
def mask_chat_id(chat_id: int) -> str:
    s = str(chat_id)
    if len(s) <= 4:
        return "*" * len(s)
    return s[:2] + "*" * (len(s) - 4) + s[-2:]
```

Ejemplo: `8664220427` → `86******27`

---

## Problemas Conocidos y Soluciones

### 1. ANSI escape codes en respuestas

**Problema**: OpenCode CLI emite codigos de escape ANSI para colores y formato en terminal. En Telegram, estos aparecen como caracteres basura o cuadrados.

**Solucion**: `clean_opencode_output()` (`bot.py:179-221`):

```python
ansi_pattern = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
text = ansi_pattern.sub('', text)
```

Tambien elimina:
- Lineas de build (`> build · deepseek-v4-pro`)
- Trazas de tool calls (`⚙ <tool> {...}`) — incluyendo multilinea via tracking de `{ }`
- Simbolos Unicode problematicos
- Lineas de auto-rechazo de permisos
- Lineas vacias multiples

### 2. Tool traces multilinea (Phase 0)

**Problema**: Las tool calls de OpenCode pueden ser JSON multilinea. Filtrar solo lineas que empiezan con `⚙` dejaba lineas JSON sueltas de tool traces.

**Solucion**: `_remove_tool_traces()` (`bot.py:150-176`) implementa tracking de llaves:

```python
def _remove_tool_traces(text: str) -> str:
    in_tool_trace = False
    brace_depth = 0
    for line in text.split('\n'):
        if stripped.startswith('⚙') or stripped.startswith('âš™'):
            in_tool_trace = True
            brace_depth = 0
        if in_tool_trace:
            brace_depth += stripped.count('{') - stripped.count('}')
            if brace_depth <= 0 and ('{' in stripped or '}' in stripped):
                in_tool_trace = False
            continue
        result.append(line)
```

El tracking de `{` y `}` permite detectar cuando el bloque JSON multilinea termina (brace_depth vuelve a 0). Tambien maneja el caracter `⚙` corrupto por cp1252 (`âš™`).

### 3. Encoding UTF-8 en Windows (Phase 0)

**Problema**: `subprocess.Popen` con `text=True` usaba cp1252 en Windows, corrompiendo caracteres Unicode (emojis, tildes) en el output de OpenCode.

**Solucion**: `run_opencode()` (`bot.py:402-409`) usa `encoding="utf-8"` explicitamente:

```python
process = subprocess.Popen(
    cmd,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    encoding="utf-8",        # <-- explicito, no confiar en text=True
    cwd=workdir,
    errors="replace",
    ...
)
```

### 4. Tablas en MarkdownV2 (Phase 0)

**Problema**: Telegram MarkdownV2 **no soporta tablas** (`| col1 | col2 |`). OpenCode frecuentemente responde con tablas markdown que se renderizan como texto plano ilegible.

**Solucion**: `telegramify_markdown()` (`bot.py:224-272`) convierte tablas a bloques de codigo monospace:

```python
def telegramify_markdown(text: str) -> str:
    # Detecta: linea |...| seguida de |---| separator
    # Convierte el bloque de tabla a ``` ... ```
    # Usa caracteres de caja (│) para simular bordes
```

El resultado es una tabla monospace dentro de un bloque de codigo, que Telegram renderiza correctamente.

### 5. MarkdownV2 parse failure (Phase 0)

**Problema**: `ParseMode.MARKDOWN_V2` es estricto con caracteres especiales. Si el texto contiene `_`, `*`, `[`, `]`, `(`, `)`, `~`, `` ` ``, `>`, `#`, `+`, `-`, `=`, `|`, `{`, `}`, `.`, `!` sin escapar, Telegram rechaza el mensaje con error.

**Solucion**: Doble estrategia (`bot.py:694-708`):

```python
try:
    await context.bot.send_message(
        chat_id=chat_id,
        text=fragment,
        parse_mode=ParseMode.MARKDOWN_V2
    )
except Exception as e:
    logger.debug(f"MarkdownV2 parse failed: {e}")
    await context.bot.send_message(
        chat_id=chat_id,
        text=fragment    # fallback a texto plano
    )
```

Intenta MarkdownV2 primero (para negrita, codigo, formato). Si falla, reenvia el mismo texto sin `parse_mode` (texto plano). El log del error es `logger.debug` para no generar ruido en produccion.

### 6. Contador asincrono de progreso (Phase 0)

**Problema**: Prompts largos (>30s) dejaban al usuario sin feedback. El mensaje "Procesando..." era estatico y generaba incertidumbre.

**Solucion**: `progress_updater()` (`bot.py:472-486`) con `asyncio.Event` y `asyncio.create_task`:

```python
async def progress_updater(context, chat_id, message_id, stop_event):
    seconds = 0
    while not stop_event.is_set():
        await asyncio.sleep(5)
        seconds += 5
        if not stop_event.is_set():
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text="⏳ OpenCode procesando... ({}s)".format(seconds),
            )
```

- Se lanza como task antes de ejecutar OpenCode
- Cada 5s edita el mensaje mostrando "⏳ OpenCode procesando... (Xs)"
- Al terminar (finally), `stop_event.set()` detiene el updater
- El mensaje se reemplaza por "✅ [nombre] Completado (Xs)"

### 7. Event loop anidado (nest_asyncio)

**Problema**: `python-telegram-bot` v22+ usa `asyncio` internamente. En Windows, ciertos patrones de ejecucion causan errores de "event loop is already running". Ademas, `subprocess.run()` es bloqueante y congelaria el event loop.

**Solucion**: Doble estrategia (`bot.py:20-22, 599-606`):

```python
import nest_asyncio
nest_asyncio.apply()

# Ejecutar opencode en un thread separado
loop = asyncio.get_running_loop()
stdout, stderr, exitcode, timed_out = await loop.run_in_executor(
    None, run_opencode, cmd, OPENCODE_WORKDIR, OPENCODE_TIMEOUT, chat_id
)
```

`nest_asyncio` permite anidar event loops, y `run_in_executor` mueve la ejecucion bloqueante de OpenCode a un thread del pool, manteniendo el event loop de Telegram libre.

### 8. Asyncio subprocess en Windows

**Problema**: `asyncio.create_subprocess_exec` no funciona correctamente en Windows con `nest_asyncio`. Causa deadlocks y `NotImplementedError` en ciertos patrones.

**Solucion**: Usar `loop.run_in_executor` + `subprocess.Popen` sincrono en lugar de `asyncio.create_subprocess_exec` para todas las ejecuciones de OpenCode. La unica excepcion es `_session_delete()` que usa `asyncio.create_subprocess_exec` para `opencode session delete` — esto es aceptable porque es un comando rapido (~1s) y no ocurre durante la ejecucion de prompts.

### 9. Timeout colgado (proceso zombie)

**Problema**: Cuando un prompt excede el timeout, `subprocess.run(timeout=N)` lanza `TimeoutExpired`, pero el proceso hijo (y sus nietos de Node.js) pueden quedar colgados.

**Solucion**: `run_opencode()` (`bot.py:396-435`) usa `subprocess.Popen` en lugar de `subprocess.run`, y en Windows fuerza la terminacion con:

```python
subprocess.run(
    ["taskkill", "/F", "/T", "/PID", str(process.pid)],
    capture_output=True,
)
```

El flag `/T` (tree kill) asegura que todos los procesos hijo de Node.js tambien mueran.

**Ademas**: `CREATE_NEW_PROCESS_GROUP` en Windows previene que Ctrl+C en el bot mate accidentalmente los procesos de OpenCode.

### 10. IDs de sesion inventados

**Problema**: En versiones anteriores, el bridge inventaba IDs como `telegram-<chat_id>`. OpenCode no reconocia estos IDs, resultando en sesiones que no se continuaban correctamente.

**Solucion**: El bridge ahora captura IDs reales (`ses_xxx`) de OpenCode. La primera ejecucion de una sesion nueva usa `opencode run "<prompt>"` sin flags, y al terminar captura el ID via `opencode session list`. Ese ID real se persiste en `sessions.json` y se usa con `--session` en ejecuciones futuras.

### 11. Staleness de active_sessions tras switch

**Problema**: `active_sessions` (cache en memoria) retenia el nombre de sesion anterior despues de un `/session switch`. El proximo prompt usaba la sesion vieja porque el cache no se habia invalidado.

**Solucion**: `_process_prompt()` (`bot.py:522-530`) detecta inconsistencias entre el cache y `sessions.json`:

```python
mem_session = active_sessions.get(chat_id)
if mem_session and mem_session.get("session_name") != session_name:
    logger.info("In-memory session '%s' stale after switch to '%s', clearing", ...)
    active_sessions.pop(chat_id, None)
    mem_session = None
```

Si el nombre en cache difiere del `active` en `sessions.json`, el cache se descarta y se reconstruye.

### 12. stdout vs stderr (priorizacion inteligente)

**Problema**: OpenCode CLI puede enviar respuestas por stdout, stderr, o ambos. Inicialmente el bridge solo capturaba stdout, perdiendo respuestas que iban por stderr.

**Solucion**: `_assemble_response()` (`bot.py:457-469`) implementa una estrategia de priorizacion:

```python
def _assemble_response(raw_stdout, raw_stderr):
    stdout_text = raw_stdout.strip() if raw_stdout else ""
    meaningful_stderr = _filter_stderr(raw_stderr)

    if stdout_text:
        response = stdout_text
        if meaningful_stderr:
            response += "\n\n---\n" + meaningful_stderr  # stderr como anexo
    else:
        response = meaningful_stderr if meaningful_stderr else raw_stderr.strip()

    return clean_opencode_output(response)
```

La logica:
1. Si hay stdout → es la respuesta principal; stderr filtrado se anexa al final
2. Si no hay stdout → stderr filtrado es la respuesta
3. Si no hay nada → el texto crudo de stderr (para debugging)
4. `_filter_stderr()` elimina lineas de metadata como `[INFO]`, `[DEBUG]`, lineas de build

### 13. Mensajes largos (limite de 4096 de Telegram)

**Problema**: Telegram limita los mensajes a 4096 caracteres. Las respuestas de OpenCode frecuentemente exceden este limite.

**Solucion**: `split_message()` (`bot.py:275-305`) parte respuestas largas en chunks de 4000 caracteres (margen de seguridad). Intenta cortar en:
1. Doble salto de linea (`\n\n`)
2. Punto y espacio (`. `)
3. Salto de linea simple (`\n`)
4. Espacio
5. Corte forzado (si nada de lo anterior funciona)

Cada parte se numera: `(parte 1/3)`, `(parte 2/3)`, etc.

### 14. Cancelacion durante ejecucion

**Problema**: Si el usuario envia `/cancel` mientras OpenCode esta ejecutando, el proceso se mata pero el codigo que espera el resultado podria igual enviar la respuesta parcial.

**Solucion**: Sistema de flags (`bot.py:119-120, 610-612`):

```python
cancel_requests: set[int] = set()

# En _process_prompt, despues de que run_opencode retorna:
if chat_id in cancel_requests:
    cancel_requests.discard(chat_id)
    return  # No enviar respuesta
```

El `/cancel`:
1. Saca el proceso de `current_process`
2. Agrega `chat_id` a `cancel_requests`
3. Mata el proceso con `taskkill`
4. Cuando `_process_prompt` despierta, ve el flag y suprime la respuesta

---

## Guia de Instalacion

### Requisitos

- **Windows 10/11** (o Linux/Mac con ajustes minimos)
- **Python 3.13+** (o 3.10+)
- **Node.js** (para OpenCode CLI)
- **OpenCode CLI** instalado globalmente via npm
- **Bot de Telegram** creado via @BotFather
- **Git** (para clonar el repositorio)

### Paso a paso

#### 1. Clonar el repositorio

```bash
git clone <repo-url>
cd Balanceate
```

#### 2. Crear entorno virtual e instalar dependencias

```bash
python -m venv venv
venv\Scripts\activate      # Windows
# source venv/bin/activate # Linux/Mac
pip install -r requirements.txt
```

Las dependencias clave del bridge:
- `python-telegram-bot>=20.0`
- `nest_asyncio>=1.6.0`
- `python-dotenv==1.1.1`

#### 3. Instalar OpenCode CLI

```bash
npm install -g @anthropic/opencode   # o el paquete npm que corresponda
```

Verificar la instalacion:

```bash
opencode --version
```

#### 4. Crear el bot de Telegram

1. Abre Telegram y busca **@BotFather**
2. Envia `/newbot`
3. Sigue las instrucciones (nombre, username)
4. **Guarda el token** que te da BotFather (ej: `123456:ABC-DEF1234gh...`)

#### 5. Obtener tu chat ID

Ejecuta el script auxiliar:

```bash
python telegram_bridge/get_chat_id.py
```

El script inicia un bot temporal. Envia `/start` a tu bot desde Telegram y el script imprimira tu chat ID:

```
Your chat ID is: 8664220427
Add this to your .env file: ALLOWED_CHAT_IDS=8664220427
```

Presiona `Ctrl+C` para detener el script.

#### 6. Configurar .env

Crea o edita `Balanceate/.env`:

```bash
TELEGRAM_BOT_TOKEN=123456:ABC-DEF1234gh...
ALLOWED_CHAT_IDS=8664220427
OPENCODE_WORKDIR=C:\Users\marie\Desktop\mono\python\Balanceate
OPENCODE_TIMEOUT=600
```

Para multiples usuarios (ej: tu y un colega):

```bash
ALLOWED_CHAT_IDS=8664220427,1234567890
```

#### 7. Iniciar el bot

**Opcion A: Manual**

```bash
python -m telegram_bridge.bot
```

**Opcion B: Con el .bat**

Doble click en `telegram_bridge/run_bot.bat`

**Opcion C: Como servicio de Windows (recomendado para produccion)**

Usar NSSM (Non-Sucking Service Manager) o similar para ejecutar el bot como servicio.

#### 8. Verificar que funciona

1. El bot debe mostrar en consola: `Bot is running. Press Ctrl+C to stop.`
2. El startup incluye schema discovery: `OpenCode DB tables: ['event', 'file', 'message', 'prompt', 'session']`
3. Envia `/start` desde Telegram a tu bot
4. Deberias recibir: `OpenCode Bot listo.` con referencia a `/session new|list|switch|delete|info`
5. Envia un mensaje de prueba: `Hola`
6. El bot respondera con `⏳ OpenCode procesando...` (que se actualiza cada 5s) y luego la respuesta

### Solucion de problemas comunes

| Error | Causa probable | Solucion |
|---|---|---|
| `TELEGRAM_BOT_TOKEN not set` | Falta .env o variable mal escrita | Verificar que .env existe en la raiz del proyecto |
| `ALLOWED_CHAT_IDS is empty` | No se configuro la whitelist | Ejecutar get_chat_id.py y agregar el ID |
| `FileNotFoundError: opencode` | OpenCode CLI no esta en PATH | Instalar con npm o configurar OPENCODE_CMD en .env |
| `Timed out after 600s` | El prompt tardo mas del timeout | Aumentar OPENCODE_TIMEOUT o usar /model flash |
| El bot no responde | Chat ID no esta en whitelist | Verificar ALLOWED_CHAT_IDS en .env |
| ANSI codes visibles en Telegram | `clean_opencode_output` no cubrio un caso | Reportar el raw output para agregar el patron |
| MarkdownV2 error en logs | Caracteres sin escapar en respuesta | Es manejado automaticamente (fallback a texto plano) |
| "No se encontro ID" en primera ejecucion | OpenCode tardo en escribir sesion a disco | El bridge reintenta tras 1s; si persiste, verificar `opencode session list` |

---

## Evolucion del Proyecto

Basado en el historial de git, los logs (`bot.log`) y el codigo actual, esta es la cronologia de desarrollo:

### v1.0 — Prototipo inicial (version simple)

- Bot basico con `subprocess.run(["opencode", "run", prompt])`
- Sin manejo de sesiones (cada mensaje era independiente)
- Sin limpieza de output
- Sin timeout configurable

**Bug**: `FileNotFoundError: opencode` — el ejecutable no se encontraba en PATH

### v1.1 — Resolucion de ruta y timeout

- `resolve_opencode_cmd()`: busqueda en 4 ubicaciones
- Timeout configurable via `OPENCODE_TIMEOUT`
- `Popen` en lugar de `subprocess.run` para mejor control

### v1.2 — Manejo de sesiones SDD

- `active_sessions`: tracker de sesiones por chat_id
- `--continue --session <id>`: continuidad entre mensajes
- `SESSION_TIMEOUT_MINUTES = 30`: expiracion automatica
- Comando `/new`: reinicio manual de sesion

### v1.3 — Limpieza de output

- `clean_opencode_output()`: eliminacion de ANSI escape codes
- `_filter_stderr()`: filtrado de metadata de stderr
- `_assemble_response()`: priorizacion inteligente stdout/stderr
- `split_message()`: particion de mensajes > 4000 chars

### v1.4 — Cambio de modelo y cancelacion

- `/model pro|flash`: cambio de modelo por chat
- `current_model`: preferencia que persiste entre sesiones
- `/cancel`: cancelacion con taskkill + flag de supresion
- `current_process`: tracker de procesos activos
- Proteccion contra prompts paralelos

### v1.5 — Comando /status mejorado

- Panel con modelo, sesion, tiempos relativos, uptime
- `_relative_time()`: formato "hace X min" en espanol
- `START_TIME`: uptime del daemon

### v1.6 — Robustez y produccion

- `nest_asyncio`: compatibilidad con event loops anidados
- `CREATE_NEW_PROCESS_GROUP`: proteccion contra Ctrl+C
- `run_in_executor`: ejecucion no bloqueante de OpenCode
- `RotatingFileHandler`: logs rotativos (5 MB, 3 backups)
- `mask_chat_id()`: privacidad en logs
- Manejo de senales (SIGINT/SIGTERM) para apagado limpio
- `build_application()`: separacion de construccion y ejecucion

### v1.7 — Gestion multi-sesion (Phases 1-3)

- `sessions.json`: persistencia en disco como fuente de verdad del mapeo sesiones
- `load_session_map()` / `save_session_map()`: lectura/escritura atomica
- `parse_opencode_session_list()`: regex con `\s{2,}` para parsear columnas
- `fetch_opencode_sessions()`: ejecuta `opencode session list` en `run_in_executor`
- `/session new|list|switch|delete|info|discover|adopt`: 7 subcomandos completos
- `/session_preview`: atajo de diagnostico
- Captura de IDs reales (`ses_xxx`) en primera ejecucion — **no mas IDs inventados**
- Lazy creation: `/session new` solo escribe sessions.json, sin llamar a OpenCode
- Adopcion de sesiones viejas via `/session discover` + `/session adopt`
- `sessions.json` como fuente de verdad: `_process_prompt()` lee siempre del disco primero
- Invalidacion de `active_sessions` en `/session switch` y `/new`
- Limpieza en OpenCode al hacer `/session delete` (llama a `opencode session delete`)
- Timeout aumentado a 600s (10 minutos) para prompts largos

### v1.8 — UX Layer (Phase 0)

- **Contador asincrono**: `progress_updater()` con `asyncio.Event` y `asyncio.create_task`. Actualiza mensaje cada 5s mostrando "⏳ OpenCode procesando... (Xs)". Integrado con try/finally. Al finalizar edita a "✅ [nombre] Completado (Xs)".
- **MarkdownV2 fallback**: `ParseMode.MARKDOWN_V2` en try/except. Si falla (caracteres sin escapar), envia texto plano sin prefijo visible. Log interno con `logger.debug`.
- **telegramify_markdown()**: convierte tablas `|...|` a bloques ``` monospace porque MarkdownV2 de Telegram no soporta tablas. Usa caracteres de caja (│) para simular bordes.
- **UTF-8 fix**: `encoding="utf-8"` en Popen (antes `text=True` usaba cp1252 en Windows).
- **_remove_tool_traces()**: tracking de llaves `{ }` para filtrar tool calls multilinea completos.

### v1.8.1 — SQLite Metadata (Phase 4 Light)

- `query_opencode_db()`: ejecuta `opencode db "SQL" --format json` (NO sqlite3 directo)
- **Schema discovery en startup**: `PRAGMA table_info` para cada tabla (session, message, prompt, file, event)
- **Session info enriquecido**: `/session info` consulta mensajes en BD, modelo, fecha de creacion desde SQLite
- SQL injection prevention: validacion regex del session_id antes de interpolar en queries

### Bugs corregidos en cada iteracion

| Version | Bug | Causa | Solucion |
|---|---|---|---|
| v1.0 | `FileNotFoundError` | `opencode` no en PATH | `resolve_opencode_cmd()` con 4 fallbacks |
| v1.2 | Perdida de contexto | Sin `--continue --session` | Sesiones trackedas con flags |
| v1.3 | ANSI codes en Telegram | OpenCode emite secuencias de escape | Regex de limpieza en `clean_opencode_output()` |
| v1.3 | Respuestas truncadas | Limite de 4096 chars de Telegram | `split_message()` con cortes inteligentes |
| v1.4 | Procesos zombie | `TimeoutExpired` no mataba hijos | `taskkill /T` para arbol completo |
| v1.5 | Respuesta post-cancel | El codigo no sabia que se cancelo | `cancel_requests` set como flag |
| v1.6 | Event loop bloqueado | `subprocess.run` sincrono | `run_in_executor` en thread pool |
| v1.7 | IDs inventados | Bridge creaba `telegram-<id>` | Captura de `ses_xxx` reales via `opencode session list` |
| v1.7 | Cache stale tras switch | `active_sessions` no se invalidaba | Deteccion de mismatch nombre y re-sincronizacion |
| v1.7 | Sesiones perdidas en reinicio | Solo cache en memoria | `sessions.json` como fuente de verdad persistente |
| v1.8 | Caracteres corruptos | `text=True` usaba cp1252 en Windows | `encoding="utf-8"` explicito en Popen |
| v1.8 | Tablas ilegibles | MarkdownV2 no soporta tablas | `telegramify_markdown()` convierte a monospace |
| v1.8 | MarkdownV2 crashes | Caracteres sin escapar en output | try/except con fallback a texto plano |
| v1.8 | Tool traces parciales | JSON multilinea no detectado | Tracking de `{ }` en `_remove_tool_traces()` |
| v1.8 | Feedback en prompts largos | Mensaje "Procesando..." estatico | `progress_updater()` cada 5s con asyncio.Event |

---

## Roadmap / Mejoras Futuras

### Fase 5 — Calidad de vida y robustez

- [ ] **Soporte multi-usuario concurrente**: Agregar un semaforo global o cola de trabajos para evitar sobrecarga con multiples chats simultaneos.
- [ ] **Adjuntar archivos**: Permitir que el usuario envie capturas de pantalla, PDFs o archivos de codigo que OpenCode pueda procesar.
- [ ] **Comando /retry**: Reenviar el ultimo prompt (util cuando falla por timeout).
- [ ] **Persistencia de prompt history**: Guardar los ultimos N prompts y respuestas para referencia en `sessions.json` o Engram.

### Fase 6 — Integraciones y notificaciones

- [ ] **Notificaciones proactivas**: El bot podria enviar mensajes sin que el usuario pregunte (ej: "termino el build", "se encontro un error en logs").
- [ ] **Integracion con GitHub**: El bot podria crear issues/PRs directamente desde Telegram.
- [ ] **Dashboard web simple**: Una pagina web local que muestre logs, sesiones activas, y estadisticas.

### Fase 7 — Multi-plataforma y UX avanzada

- [ ] **Soporte para Linux/Mac como servicio**: Scripts systemd/launchd para ejecutar el bot como daemon nativo.
- [ ] **Comando /config**: Ver y modificar configuracion del bot desde Telegram (timeout, workdir) sin tocar .env.
- [ ] **Voice messages**: Procesar notas de voz con speech-to-text y enviarlas como prompts.
- [ ] **Rate limiting**: Limitar la cantidad de prompts por minuto para evitar consumo excesivo.
- [ ] **Tests automatizados**: Suite de tests para `clean_opencode_output`, `split_message`, `telegramify_markdown`, `_remove_tool_traces` y el flujo completo con un OpenCode mock.

### Ideas experimentales

- [ ] **Modo "drive-through"**: Un flujo optimizado para usar mientras se maneja — comandos por voz, respuestas cortas, confirmaciones rapidas.
- [ ] **Auto-resumen de sesion**: Al expirar una sesion, guardar automaticamente un resumen en Engram de lo que se hizo.
- [ ] **Integracion con Google Maps API**: "OpenCode, busca el restaurante mas cercano y crea una nota en Notion" — mezclando SDD con herramientas cotidianas.

### Completado

- [x] **Phase 0 — UX Layer**: Contador asincrono, MarkdownV2 fallback, telegramify_markdown, UTF-8 fix, _remove_tool_traces
- [x] **Phase 1 — Session Management**: sessions.json, load/save, parse_opencode_session_list, fetch_opencode_sessions
- [x] **Phase 2 — Session Commands**: /session new, list, switch, delete
- [x] **Phase 3 — Session Adoption**: /session discover, adopt, info (con `session_preview` bonus)
- [x] **Phase 4 — SQLite Metadata**: query_opencode_db, schema discovery en startup, session info enriquecido

---

## Apendice: Flujo de ejecucion detallado

### Camino completo de un mensaje

```
1. Usuario envia "Hola" por Telegram
       │
2. Telegram API envia Update al webhook/polling del bot
       │
3. python-telegram-bot recibe el Update
       │
4. MessageHandler filtra: es TEXT y no es COMMAND
       │
5. handle_message() en bot.py:1383
       │
6. authorize(chat_id)? ─── NO ──> Ignorar (log warning)
       │
7. Hay current_process? ─── SI ──> "Ya hay un prompt en proceso"
       │
8. _process_prompt(update, chat_id, "Hola", context)
       │
9. Leer sessions.json → obtener session_name activa + ID real
       │
10. Sincronizar active_sessions (invalidar si nombre difiere)
       │
11. Construir comando:
    opencode run --model deepseek/deepseek-v4-pro [--continue --session ses_xxx] "Hola"
       │
12. Enviar "⏳ OpenCode procesando..." + lanzar progress_updater task
       │
13. loop.run_in_executor(None, run_opencode, cmd, ...)
       │
14. subprocess.Popen ejecuta el comando (encoding="utf-8")
       │ (el event loop sigue libre para /cancel, /status, etc.)
       │ (progress_updater edita el mensaje cada 5s)
       │
15. process.communicate(timeout=600)
       │
16. Timeout? ──SI──> taskkill /F /T, retornar mensaje de timeout
       │
17. Cancelado? ──SI──> Discard cancel_requests, suprimir respuesta
       │
18. _assemble_response(stdout, stderr) → priorizar y combinar
       │
19. clean_opencode_output() → ANSI, build lines, tool traces multilinea
       │
20. Capturar ID real si es sesion nueva (opencode session list)
       │
21. Actualizar prompt_count en sessions.json
       │
22. split_message() → partir en chunks de 4000 chars
       │
23. telegramify_markdown(fragment) → convertir tablas a monospace
       │
24. Enviar con ParseMode.MARKDOWN_V2 (si falla → texto plano)
       │
25. stop_event.set() → detener progress_updater
       │
26. Editar mensaje a "✅ [nombre] Completado (Xs)"
       │
27. Usuario recibe la respuesta en Telegram
```

### Flujo de cancelacion

```
1. Usuario envia /cancel
       │
2. cancel_command() en bot.py:925
       │
3. current_process.pop(chat_id) → obtener Popen
       │
4. proc is None o ya termino? ──SI──> "No hay prompt en ejecucion"
       │
5. cancel_requests.add(chat_id)
       │
6. taskkill /F /T /PID <pid> → matar arbol de procesos
       │
7. await "❌ Prompt cancelado"
       │
8. _process_prompt() despierta del run_in_executor
       │
9. Ve cancel_requests → NO envia respuesta, pasa al finally
       │
10. stop_event.set() → detener progress_updater
       │
11. Editar mensaje a "✅ [...] Completado (...s)"
```

### Flujo de adopcion de sesion

```
1. Usuario envia /session discover
       │
2. _session_discover() en bot.py:1258
       │
3. fetch_opencode_sessions() → lista de {id, title, updated}
       │
4. Comparar con sessions.json → marcar adoptadas vs disponibles
       │
5. Mostrar lista con comandos /session adopt listos para copiar
       │
6. Usuario envia /session adopt ses_xxx mi_sesion
       │
7. _session_adopt() en bot.py:1312
       │
8. Validar formato del nombre (1-30 chars alfanumericos)
       │
9. Verificar que el ID existe en OpenCode (fetch_opencode_sessions)
       │
10. Verificar que el nombre no existe ya en sessions.json
       │
11. Guardar entrada en sessions.json con el ID real
       │
12. Si no hay sesion activa, establecer esta como activa
       │
13. Listo: el proximo prompt usara --continue --session ses_xxx
```

---

*Documentacion generada el 2026-05-14 a partir del codigo fuente de `telegram_bridge/bot.py` (v1.8.1, ~1500 lineas).*
