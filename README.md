# SDD Orchestrator Telegram Bot

Bot de Telegram que expone OpenCode CLI con el orquestador SDD Gentleman AI y MCPs (Context7, Engram, Notion).

## Instalación

```bash
pip install -r requirements.txt
```

## Configuración

Crear `.env`:
```
TELEGRAM_BOT_TOKEN=<token_de_botfather>
ALLOWED_CHAT_IDS=<tu_chat_id>
OPENCODE_WORKDIR=<ruta_del_proyecto>
OPENCODE_TIMEOUT=600
```

## Uso

```bash
python bot.py
# o doble-click en run_bot.bat
```

## Comandos desde Telegram

| Comando | Función |
|---------|---------|
| Cualquier texto | Prompt SDD directo |
| `/model pro\|flash` | Cambiar modelo |
| `/cancel` | Cancelar prompt |
| `/status` | Estado de sesión |
| `/new` | Nueva sesión |
| `/session new\|list\|switch\|delete\|info\|discover\|adopt` | Gestión multi-sesión |

## Documentación completa

Ver [DOCUMENTACION.md](DOCUMENTACION.md) (8236 palabras).
