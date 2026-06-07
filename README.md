# Telegram Bridge - Entorno Aislado

## ✅ Configuración Completada

El proyecto `telegram_bridge` ahora tiene su propio entorno virtual aislado de `Balanceate`.

## 📁 Estructura

```
telegram_bridge/
├── .venv/              # Entorno virtual aislado
├── requirements.txt    # Dependencias específicas del bridge
├── run_bot.bat        # Script de ejecución actualizado
├── bot.py             # Punto de entrada
└── ...
```

## 🚀 Uso

### Opción 1: Ejecutar con el script .bat
```batch
cd telegram_bridge
run_bot.bat
```

### Opción 2: Activar manualmente el entorno virtual
```powershell
cd telegram_bridge
.venv\Scripts\Activate.ps1
python bot.py
```

### Opción 3: Desde VS Code
1. Abre el archivo `telegram_bridge/bot.py`
2. VS Code debería detectar automáticamente el entorno `.venv` del telegram_bridge
3. Presiona F5 para ejecutar con debug

## 📦 Dependencias

Las dependencias se gestionan independientemente en `telegram_bridge/requirements.txt`:

- `python-telegram-bot>=21.0` - Librería para crear bots de Telegram
- `nest-asyncio>=1.6.0` - Soporte para event loops anidados
- `python-dotenv>=1.0.0` - Gestión de variables de entorno
- `openai>=1.0.0` - Transcripción de mensajes de voz

## 🔧 Mantenimiento

### Instalar nuevas dependencias
```powershell
cd telegram_bridge
.venv\Scripts\Activate.ps1
pip install <paquete>
pip freeze > requirements.txt
```

### Actualizar dependencias
```powershell
cd telegram_bridge
.venv\Scripts\Activate.ps1
pip install --upgrade -r requirements.txt
```

## ⚙️ Configuración de VS Code

Para trabajar específicamente en el telegram_bridge con su entorno virtual:

1. Abre la carpeta `telegram_bridge` como workspace en VS Code
2. VS Code detectará automáticamente `.venv`
3. O manualmente selecciona el intérprete: `Ctrl+Shift+P` → "Python: Select Interpreter" → `.venv`

## 🔄 Migración desde el entorno compartido

El proyecto ahora está completamente aislado de `Balanceate`. Ya no depende del `.venv` o `venv` del directorio padre.

## 📝 Notas

- El entorno virtual está excluido de git (`.gitignore`)
- Cada desarrollador debe crear su propio `.venv` usando `python -m venv .venv`
- El archivo `.env` se carga desde el directorio padre (`Balanceate/.env`)
