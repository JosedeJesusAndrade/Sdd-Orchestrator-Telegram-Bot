"""Build the roadmap script with proper UTF-8 encoding, then exec it.

This avoids the write tool's issue with backslash-u escapes.
"""
import subprocess
import sys
from pathlib import Path

# The actual script content, written with real UTF-8 characters.
# We'll build it as a list of lines to avoid any escape confusion.
SCRIPT = r'''# -*- coding: utf-8 -*-
"""Create the 'Roadmap para leer el codigo base' Notion page."""
from __future__ import annotations
import os, re, sys
from typing import Any
from notion_client import Client

NOTION_TOKEN = os.environ.get("NOTION_TOKEN")
if not NOTION_TOKEN:
    env_path = r"C:\Users\marie\.config\opencode\opencode.json"
    if os.path.exists(env_path):
        with open(env_path, encoding="utf-8") as f:
            content = f.read()
            m = re.search(r'NOTION_TOKEN["\']?\s*[:=]\s*["\']([^"\']+)', content)
            if m:
                NOTION_TOKEN = m.group(1)
if not NOTION_TOKEN:
    sys.exit("NOTION_TOKEN not found")
client = Client(auth=NOTION_TOKEN)
PARENT_ID = "37b7c219-93af-8057-8085-e27bc0dafb1a"


def rt(text, bold=False, code=False):
    ann = {}
    if bold: ann["bold"] = True
    if code: ann["code"] = True
    obj = {"type": "text", "text": {"content": text}}
    if ann: obj["annotations"] = ann
    return obj


def para(*parts):
    rts = [p if isinstance(p, dict) else rt(p) for p in parts]
    return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": rts}}


def h1(text):
    return {"object": "block", "type": "heading_1", "heading_1": {"rich_text": [rt(text)]}}


def h2(text):
    return {"object": "block", "type": "heading_2", "heading_2": {"rich_text": [rt(text)]}}


def h3(text):
    return {"object": "block", "type": "heading_3", "heading_3": {"rich_text": [rt(text)]}}


def bullet(*parts):
    rts = [p if isinstance(p, dict) else rt(p) for p in parts]
    return {"object": "block", "type": "bulleted_list_item", "bulleted_list_item": {"rich_text": rts}}


def numbered(*parts):
    rts = [p if isinstance(p, dict) else rt(p) for p in parts]
    return {"object": "block", "type": "numbered_list_item", "numbered_list_item": {"rich_text": rts}}


def code_blk(text, language="python"):
    return {"object": "block", "type": "code", "code": {"rich_text": [rt(text)], "language": language}}


def divider():
    return {"object": "block", "type": "divider", "divider": {}}


def callout(emoji, *parts, color="gray_background"):
    rts = [p if isinstance(p, dict) else rt(p) for p in parts]
    return {"object": "block", "type": "callout",
            "callout": {"rich_text": rts, "icon": {"type": "emoji", "emoji": emoji}, "color": color}}


def table_row(cells):
    return {"object": "block", "type": "table_row", "table_row": {"cells": [[rt(c)] for c in cells]}}


def table(rows, has_column_header=True):
    width = max(len(r) for r in rows)
    return [{"object": "block", "type": "table",
             "table": {"table_width": width, "has_column_header": has_column_header,
                       "has_row_header": False, "children": [table_row(r) for r in rows]}}]


def build():
    B = []

    # ============================================================
    # SECTION 1
    # ============================================================
    B.append(h1("¿Por qué existe este roadmap?"))

    B.append(para(
        "Leer código sin mapa es como caminar en la niebla: avanzas, pero no sabes dónde estás ni hacia dónde vas. ",
        "Este roadmap ", rt("es tu mapa", bold=True), "."
    ))

    B.append(para(
        "Está diseñado para que entiendas el ", rt("por qué", bold=True),
        " antes que el cómo. La filosofía detrás es \"Easy to change\" (de The Pragmatic Programmer): ",
        "el código que es fácil de cambiar es el que se mantiene vivo. ",
        "Antes de memorizar una firma de función, pregúntate: ¿por qué el autor decidió hacer esto así?"
    ))

    B.append(callout("🎯",
        "Tiempo total estimado: ~7 horas distribuidas en 1-2 semanas. ",
        "Sigue las fases EN ORDEN — cada una construye sobre la anterior. ",
        "Si saltas, vas a leer código sin contexto y no vas a entender nada.",
        color="blue_background"))

    B.append(divider())

    # ============================================================
    # SECTION 2: Glosario
    # ============================================================
    B.append(h1("Glosario de términos clave"))

    B.append(para(
        "Esta sección es ", rt("CRÍTICA", bold=True), ". Muchos términos aparecerán en fases posteriores. ",
        "Si no los entiendes aquí, te vas a perder más adelante. ",
        "Cada concepto viene con: qué es, por qué se usa aquí, cómo reconocerlo, y una analogía del mundo real."
    ))

    # Protocol
    B.append(h3("Protocol (structural subtyping)"))
    B.append(bullet(rt("Qué es", bold=True), ": una clase que define métodos sin implementarlos. ",
        "Otras clases la \"implementan\" implícitamente (duck typing)."))
    B.append(bullet(rt("Por qué aquí", bold=True), ": nos permite cambiar de librería de Telegram o de proveedor de AI ",
        "sin reescribir todo el código."))
    B.append(bullet(rt("Cómo reconocerlo", bold=True), ": ", rt("from typing import Protocol", code=True), " ",
        "más métodos con cuerpo ", rt("...", code=True), "."))
    B.append(bullet(rt("Analogía", bold=True), ": como una descripción de puesto de trabajo. ",
        "Cualquiera que sepa hacer las tareas califica, sin importar de dónde viene."))
    B.append(bullet(rt("Ejemplo en este proyecto", bold=True), ": ", rt("AIBackend", code=True), " Protocol en ",
        rt("services/ai_backend.py", code=True), "."))

    # DI
    B.append(h3("Dependency Injection (DI)"))
    B.append(bullet(rt("Qué es", bold=True), ": en vez de que una clase cree sus dependencias, ",
        "éstas se le pasan desde fuera."))
    B.append(bullet(rt("Por qué aquí", bold=True), ": testabilidad + flexibilidad. ",
        "Fácil de mockear servicios en tests."))
    B.append(bullet(rt("Cómo reconocerlo", bold=True), ": el constructor recibe otros objetos como parámetros."))
    B.append(bullet(rt("Analogía", bold=True), ": un mesero de restaurante. Tú le dices \"quiero café\", ",
        "él te lo trae. No sabes quién lo preparó ni cómo."))
    B.append(bullet(rt("Ejemplo", bold=True), ": ", rt("AppContainer", code=True), " en ",
        rt("services/container.py", code=True), " se construye en ",
        rt("bot.py:run_bot", code=True), " y se pasa a los handlers vía ", rt("bot_data", code=True), "."))

    # SSOT
    B.append(h3("Single Source of Truth (SSOT)"))
    B.append(bullet(rt("Qué es", bold=True), ": un solo lugar donde vive la data. ",
        "Todo lo demás solo lee de ahí."))
    B.append(bullet(rt("Por qué aquí", bold=True), ": evita bugs de \"fuera de sincronía\" ",
        "donde dos lugares no se ponen de acuerdo."))
    B.append(bullet(rt("Cómo reconocerlo", bold=True), ": una clase con métodos CRUD; los demás la importan."))
    B.append(bullet(rt("Analogía", bold=True), ": el catálogo de una biblioteca. ",
        "Solo el bibliotecario lo actualiza; los usuarios solo leen."))
    B.append(bullet(rt("Ejemplo", bold=True), ": ", rt("SessionStore", code=True), " en ",
        rt("services/session_store.py", code=True), "."))

    # Adapter
    B.append(h3("Adapter Pattern"))
    B.append(bullet(rt("Qué es", bold=True), ": envuelve una librería externa exponiendo una interfaz interna limpia."))
    B.append(bullet(rt("Por qué aquí", bold=True), ": oculta detalles del vendor. ",
        "Cambias de librería sin romper la lógica de negocio."))
    B.append(bullet(rt("Cómo reconocerlo", bold=True), ": una clase envuelve a otro objeto y expone métodos mínimos."))
    B.append(bullet(rt("Analogía", bold=True), ": un adaptador de corriente. ",
        "El enchufe de tu laptop no entra en el europeo; el adaptador traduce."))
    B.append(bullet(rt("Ejemplo", bold=True), ": ", rt("TelegramAdapter", code=True), " en ",
        rt("services/telegram_adapter.py", code=True), "."))

    # Decorator
    B.append(h3("Decorator Pattern"))
    B.append(bullet(rt("Qué es", bold=True), ": función que envuelve a otra para añadir comportamiento sin modificarla."))
    B.append(bullet(rt("Por qué aquí", bold=True), ": cross-cutting concerns (auth, logging, cache) aplicados de forma uniforme."))
    B.append(bullet(rt("Cómo reconocerlo", bold=True), ": ", rt("@nombre_decorador", code=True), " arriba de una función."))
    B.append(bullet(rt("Analogía", bold=True), ": un envoltorio de regalo. Añade presentación sin cambiar el regalo."))
    B.append(bullet(rt("Ejemplo", bold=True), ": ", rt("@authorized", code=True), " en ",
        rt("handlers/__init__.py", code=True), "."))

    # Factory
    B.append(h3("Factory Pattern"))
    B.append(bullet(rt("Qué es", bold=True), ": función/clase que crea otros objetos basándose en una clave."))
    B.append(bullet(rt("Por qué aquí", bold=True), ": fácil agregar nuevos proveedores sin modificar código existente (OCP)."))
    B.append(bullet(rt("Cómo reconocerlo", bold=True), ": método tipo ", rt("get(nombre)", code=True), " o ", rt("create(tipo)", code=True), "."))
    B.append(bullet(rt("Analogía", bold=True), ": una máquina expendedora. Presionas B4, sale un Snickers. ",
        "Distintos botones, distintos snacks, misma máquina."))
    B.append(bullet(rt("Ejemplo", bold=True), ": ", rt("AIProviderFactory", code=True), " en ",
        rt("services/ai_provider_factory.py", code=True), "."))

    # OCP
    B.append(h3("Open/Closed Principle (OCP)"))
    B.append(bullet(rt("Qué es", bold=True), ": abierto para extensión, cerrado para modificación."))
    B.append(bullet(rt("Por qué aquí", bold=True), ": agregar features sin romper lo que ya funciona."))
    B.append(bullet(rt("Cómo reconocerlo", bold=True), ": nuevo comportamiento se añade con clases nuevas, ",
        "no editando las viejas."))

    # SRP
    B.append(h3("SRP (Single Responsibility)"))
    B.append(bullet(rt("Qué es", bold=True), ": una clase = un trabajo."))
    B.append(bullet(rt("Por qué aquí", bold=True), ": más fácil de testear, cambiar y reusar."))
    B.append(bullet(rt("Cómo reconocerlo", bold=True), ": el nombre de la clase describe UNA cosa, ",
        "y todos sus métodos se relacionan con eso."))

    B.append(divider())

    # ============================================================
    # SECTION 3: El viaje del usuario
    # ============================================================
    B.append(h1("El viaje del usuario"))

    B.append(para(
        "Antes de meterte al código, entiende qué PASA cuando un usuario manda un mensaje. ",
        "Este modelo mental end-to-end es la guía para cada archivo que leas. ",
        "Si en algún momento te pierdes, vuelve aquí y ubícate."
    ))

    B.append(para(rt("Los 15 pasos del viaje", bold=True), ":"))

    B.append(numbered(rt("Telegram recibe el mensaje", bold=True), " → lo reenvía al webhook del bot."))
    B.append(numbered(rt("bot.py:run_bot está haciendo polling", bold=True), " → recibe el update."))
    B.append(numbered(rt("python-telegram-bot rutea", bold=True), " → encuentra el handler que matchea."))
    B.append(numbered(rt("Se invoca el handler", bold=True), " → en ", rt("handlers/messages.py:handle_message", code=True), "."))
    B.append(numbered(rt("Corre el decorador @authorized", bold=True), " → valida el chat_id."))
    B.append(numbered(rt("El handler obtiene el container", bold=True), " → vía ", rt("_get_container(context)", code=True), "."))
    B.append(numbered(rt("El handler llama al service", bold=True), " → ", rt("container.prompt_service.execute(...)", code=True), "."))
    B.append(numbered(rt("El service orquesta", bold=True), " → obtiene sesión, modelo, llama al backend."))
    B.append(numbered(rt("El backend corre un subprocess", bold=True), " → ", rt("opencode run --agent X --model Y \"prompt\"", code=True), "."))
    B.append(numbered(rt("OpenCode CLI habla con la API de AI", bold=True), " → DeepSeek o MiniMax."))
    B.append(numbered(rt("La AI devuelve respuesta", bold=True), " → OpenCode la formatea."))
    B.append(numbered(rt("El backend captura stdout", bold=True), " → se lo regresa a PromptService."))
    B.append(numbered(rt("PromptService entrega", bold=True), " → vía MessageSender."))
    B.append(numbered(rt("MessageSender formatea", bold=True), " → MDV2 para Telegram."))
    B.append(numbered(rt("Telegram manda al usuario", bold=True), " → el mensaje aparece."))

    B.append(para("El flujo completo en pseudocódigo:"))

    B.append(code_blk(
        "# Pseudocodigo del viaje completo del usuario\n"
        "update = await bot.polling()              # Telegram -> python-telegram-bot\n"
        "authorized(update)                         # @authorized decorator\n"
        "container = _get_container(update.context) # DI lookup\n"
        "container.prompt_service.execute(          # Service layer\n"
        "    chat_id=update.effective_chat.id,\n"
        "    prompt_text=update.message.text,\n"
        ")\n"
        "# Inside PromptService:\n"
        "session = container.session_store.get_active_session(chat_id)\n"
        "model = container.session_store.get_model(chat_id)\n"
        "backend = container.provider_factory.get(\"opencode\")\n"
        "result = await backend.execute(prompt, model, session.real_id)\n"
        "container.message_sender.send(chat_id, result.stdout)",
        language="python"
    ))

    B.append(divider())

    # ============================================================
    # SECTION 4: Mapa de dependencias
    # ============================================================
    B.append(h1("Mapa de dependencias"))

    B.append(para(
        "El diagrama que amaste, ahora como arte ASCII oficial. ",
        "Cada cuadro es un archivo (o un grupo conceptual). ",
        "Las flechas dicen quién depende de quién."
    ))

    B.append(code_blk(
        "                    +-----------------+\n"
        "                    |     bot.py      |  Entry point, asyncio.run()\n"
        "                    |   (entrypoint)  |  Builds everything\n"
        "                    +--------+--------+\n"
        "                             | creates\n"
        "                             v\n"
        "                    +-----------------+\n"
        "                    | AppContainer    |  services/container.py\n"
        "                    |   (DI root)     |  Dataclass holding all services\n"
        "                    +--------+--------+\n"
        "                             | contains\n"
        "        +--------------------+--------------------+\n"
        "        |                    |                    |\n"
        "        v                    v                    v\n"
        "+--------------+   +------------------+   +-------------+\n"
        "| BotPort      |   | PromptService    |   | SessionStore|\n"
        "| Protocol     |   | (orquestador)    |   | (SSOT)      |\n"
        "+--------------+   +--------+---------+   +-------------+\n"
        "        ^                    |\n"
        "        | implements         | uses\n"
        "        |                    v\n"
        "+--------------+   +--------------------+\n"
        "| Telegram     |   | AIProviderFactory  |\n"
        "| Adapter      |   | (factory)          |\n"
        "+--------------+   +---------+----------+\n"
        "                             | get(\"opencode\")\n"
        "                             v\n"
        "                   +--------------------+\n"
        "                   | OpenCodeCLIBackend |\n"
        "                   | (AIBackend impl)   |\n"
        "                   +--------------------+\n"
        "                             |\n"
        "                             v\n"
        "                   +--------------------+\n"
        "                   | OpenCode CLI       |\n"
        "                   | (subprocess)       |\n"
        "                   +--------------------+",
        language="plain text"
    ))

    B.append(para(
        rt("Por qué este layering", bold=True), ": cada capa solo conoce a la capa DEBAJO, nunca a la de arriba. ",
        "PromptService no sabe nada de Telegram. SessionStore no sabe nada de OpenCode. ",
        "Si te toca cambiar un proveedor, solo tocas UNA capa."
    ))

    B.append(para(
        rt("Por qué es testeable", bold=True), ": puedes mockear la capa de abajo. ",
        "Por ejemplo, un ", rt("FakeOpenCodeCLIBackend", code=True), " que regresa respuestas canned, ",
        "y testeas PromptService sin gastar API credits."
    ))

    B.append(para(
        rt("Por qué es swappable", bold=True), ": reemplazas ", rt("TelegramAdapter", code=True), " por ",
        rt("DiscordAdapter", code=True), " y todo lo demás ni se entera. ",
        "Esa es la magia de los Protocols."
    ))

    B.append(divider())

    # ============================================================
    # SECTION 5: Roadmap de lectura en 5 fases
    # ============================================================
    B.append(h1("Roadmap de lectura en 5 fases"))

    B.append(para(
        "Esta es la parte central del roadmap. ",
        "Para cada fase te doy: qué vas a aprender, qué archivos leer, ",
        "qué patrones se aplican, las trampas comunes, preguntas de auto-chequeo, y por qué este orden."
    ))

    # Phase 1
    B.append(h2("Fase 1: Los contratos"))
    B.append(para(rt("Qué vas a aprender", bold=True), ": cómo Python implementa interfaces sin la palabra reservada ", rt("interface", code=True), "."))
    B.append(para(rt("Archivos a leer (en orden)", bold=True), ":"))
    B.append(bullet(rt("services/bot_port.py", code=True), " (31 líneas)"))
    B.append(bullet(rt("services/container.py", code=True), " (17 líneas)"))
    B.append(bullet(rt("services/ai_provider_factory.py", code=True), " (25 líneas)"))
    B.append(para(rt("Patrones aplicados", bold=True), ": Protocol, DI, Factory."))
    B.append(para(rt("Trampas comunes", bold=True), ":"))
    B.append(bullet("Confundir \"Protocol\" con una abstract base class. ",
        "Son diferentes: Protocol usa STRUCTURAL typing (cualquier clase con métodos que matchean sirve)."))
    B.append(para(rt("Preguntas de auto-chequeo", bold=True), ":"))
    B.append(bullet("¿Puedes dibujar el AppContainer de memoria con todos sus campos y tipos?"))
    B.append(para(rt("Por qué este orden", bold=True), ": estos son los \"reglas del juego\". ",
        "Entiéndelas primero, después conoce a los jugadores."))

    # Phase 2
    B.append(h2("Fase 2: Persistencia + Single Source of Truth"))
    B.append(para(rt("Qué vas a aprender", bold=True), ": el patrón SSOT + cache + lock (fundamental en backend)."))
    B.append(para(rt("Archivos a leer (en orden)", bold=True), ":"))
    B.append(bullet(rt("persistence/sessions.py", code=True), " (174 líneas)"))
    B.append(bullet(rt("services/session_store.py", code=True), " (285 líneas)"))
    B.append(para(rt("Patrones aplicados", bold=True), ": SSOT, locking, cache invalidation."))
    B.append(para(rt("Trampas comunes", bold=True), ":"))
    B.append(bullet("Leer ", rt("persistence/sessions.py", code=True), " pensando que es lo principal. ",
        "NO lo es — es un wrapper de I/O de bajo nivel. ",
        "El importante es ", rt("SessionStore", code=True), " (en services/)."))
    B.append(para(rt("Preguntas de auto-chequeo", bold=True), ":"))
    B.append(bullet("¿Qué diferencia hay entre ", rt("persistence/sessions.py", code=True), " y ",
        rt("services/session_store.py", code=True), "? ¿Por qué dos capas?"))
    B.append(para(rt("Por qué este orden", bold=True), ": después de entender los contratos, ",
        "ve CÓMO fluye la data a través de ellos."))

    # Phase 3
    B.append(h2("Fase 3: Adaptadores externos"))
    B.append(para(rt("Qué vas a aprender", bold=True), ": cómo envolver librerías externas detrás de tu propia interfaz."))
    B.append(para(rt("Archivos a leer (en orden)", bold=True), ":"))
    B.append(bullet(rt("services/telegram_adapter.py", code=True), " (56 líneas)"))
    B.append(bullet(rt("services/opencode_cli_backend.py", code=True), " (74 líneas)"))
    B.append(bullet(rt("opencode/client.py", code=True), " (84 líneas)"))
    B.append(para(rt("Patrones aplicados", bold=True), ": Adapter, async subprocess, process management."))
    B.append(para(rt("Trampas comunes", bold=True), ":"))
    B.append(bullet("Pensar que ", rt("OpenCodeCLIBackend", code=True), " es \"el bot\". ",
        "NO lo es — es solo UNA manera de llamar a OpenCode. ",
        "Mañana podría haber una versión HTTP."))
    B.append(para(rt("Preguntas de auto-chequeo", bold=True), ":"))
    B.append(bullet("Si reemplazaras python-telegram-bot por otra librería, ¿cuántos archivos cambiarían?"))
    B.append(para(rt("Por qué este orden", bold=True), ": después del flujo interno, ",
        "ve cómo se aíslan las dependencias externas."))

    # Phase 4
    B.append(h2("Fase 4: Servicios de orquestación"))
    B.append(para(rt("Qué vas a aprender", bold=True), ": cómo un service coordina múltiples componentes."))
    B.append(para(rt("Archivos a leer (en orden)", bold=True), ":"))
    B.append(bullet(rt("services/message_sender.py", code=True), " (104 líneas)"))
    B.append(bullet(rt("services/prompt_service.py", code=True), " (297 líneas)"))
    B.append(para(rt("Patrones aplicados", bold=True), ": SRP, orquestación, lifecycle management."))
    B.append(para(rt("Trampas comunes", bold=True), ":"))
    B.append(bullet(rt("PromptService", code=True), " tiene 297 líneas — NO intentes leerlo todo de una. ",
        "Lee por chunks: (1) constructor, (2) flujo de ", rt("execute()", code=True), ", ",
        "(3) cancelación, (4) progress counter."))
    B.append(para(rt("Preguntas de auto-chequeo", bold=True), ":"))
    B.append(bullet("¿PromptService sabe de Telegram? ¿De OpenCode? ¿Por qué sí o por qué no?"))
    B.append(para(rt("Por qué este orden", bold=True), ": ahora ya puedes leer el \"cerebro\" — ",
        "lo que ata todo junto."))

    # Phase 5
    B.append(h2("Fase 5: Presentación (handlers) + soporte"))
    B.append(para(rt("Qué vas a aprender", bold=True), ": cómo los comandos de Telegram se mapean a funciones de Python."))
    B.append(para(rt("Archivos a leer (en orden)", bold=True), ":"))
    B.append(bullet(rt("locales/es.py", code=True), " (53 líneas)"))
    B.append(bullet(rt("formatting/markdown.py", code=True), " (235 líneas)"))
    B.append(bullet(rt("utils/time_formatting.py", code=True), " (42 líneas)"))
    B.append(bullet(rt("handlers/commands.py", code=True), " (317 líneas)"))
    B.append(bullet(rt("handlers/messages.py", code=True), " (116 líneas)"))
    B.append(bullet(rt("handlers/sessions.py", code=True), " (353 líneas)"))
    B.append(bullet(rt("handlers/ci.py", code=True), " (241 líneas)"))
    B.append(bullet(rt("handlers/admin.py", code=True), " (77 líneas)"))
    B.append(para(rt("Patrones aplicados", bold=True), ": i18n, separación de responsabilidades, composición de decoradores."))
    B.append(para(rt("Trampas comunes", bold=True), ":"))
    B.append(bullet("handlers/ es la sección MÁS LARGA — fácil perderse. ",
        "Lee en este orden: ", rt("commands.py", code=True), " (entry points), ",
        rt("messages.py", code=True), " (texto/voz), luego las especializadas."))
    B.append(para(rt("Preguntas de auto-chequeo", bold=True), ":"))
    B.append(bullet("¿Por qué handlers/ está separado de services/? ¿Qué se rompería si los uniéramos?"))
    B.append(para(rt("Por qué este orden", bold=True), ": deja el \"edge\" para el final — ",
        "estos archivos orquestan todo lo que aprendiste."))

    # Phase Bonus
    B.append(h2("Bonus: Tests + utilitarios"))
    B.append(para(rt("Qué vas a aprender", bold=True), ": cómo leer tests como documentación."))
    B.append(para(rt("Archivos", bold=True), ":"))
    B.append(bullet(rt("tests/*", code=True), " (5 archivos, ~272 líneas en total)"))
    B.append(bullet(rt("utils/*", code=True)))
    B.append(bullet(rt("get_chat_id.py", code=True)))
    B.append(callout("💡",
        "Por qué importan los tests: te muestran CÓMO se ESPERA que se use el código. ",
        "Leer tests suele ser más rápido que leer el código de producción, ",
        "y además te da ejemplos ejecutables.",
        color="yellow_background"))

    B.append(divider())

    # ============================================================
    # SECTION 6: Timeline
    # ============================================================
    B.append(h1("Timeline histórico del refactor"))

    B.append(para(
        "Tienes contexto de 5 semanas de refactor. Esto te ayuda a entender ",
        rt("POR QUÉ", bold=True), " el código está estructurado así. ",
        "Cada semana tiene un commit mensaje que explica la decisión detrás del cambio."
    ))

    for blk in table([
        ["Semana", "Tema", "Commits clave"],
        ["W1", "Fundamento (pyproject.toml, type hints, dead code, @authorized)", "Primera profesionalización"],
        ["W2", "Service Layer (SessionStore, MessageSender, PromptService)", "Extraídos del monolito de handlers"],
        ["W3", "Protocols & DI (AIBackend, BotPort, AppContainer)", "Decoupling de librerías"],
        ["W4", "Multi-User & Config (per-chat settings, i18n)", "Personalización por usuario"],
        ["W5", "Polish + CI/CD (/pr, /update, /wdir, /health)", "CI/CD desde Telegram"],
    ]):
        B.append(blk)

    B.append(callout("📖",
        "Este contexto histórico es tu mejor aliado. ",
        "Si una estructura te parece sobrediseñada, probablemente es porque ",
        "hubo una semana donde dolió no tenerla.",
        color="purple_background"))

    B.append(divider())

    # ============================================================
    # SECTION 7: Ejercicios
    # ============================================================
    B.append(h1("Ejercicios de comprensión"))

    B.append(para(
        "Para cada fase, te propongo UN ejercicio práctico. ",
        "No lo leas — hazlo. La diferencia entre leer sobre código y entender código está en hacer."
    ))

    B.append(numbered(
        rt("Ejercicio Fase 1", bold=True), ": dibuja el ", rt("AppContainer", code=True),
        " en papel con todos sus campos y tipos. Compara con el código. ",
        "¿Olvidaste alguno? Vuelve a leer ", rt("container.py", code=True), "."
    ))
    B.append(numbered(
        rt("Ejercicio Fase 2", bold=True), ": traza qué pasa cuando dos usuarios de Telegram ",
        "cambian su modelo al mismo tiempo. ¿Hay race condition? ¿Cómo lo mitiga el código?"
    ))
    B.append(numbered(
        rt("Ejercicio Fase 3", bold=True), ": escribe una clase ", rt("MockOpenCodeBackend", code=True),
        " que regrese \"FAKE RESPONSE\" en vez de correr un subprocess. ",
        "¿Cuántas líneas necesitas? (Tip: implementa ", rt("AIBackend", code=True), " Protocol.)"
    ))
    B.append(numbered(
        rt("Ejercicio Fase 4", bold=True), ": agrega un ", rt("logger.info()", code=True),
        " en cada paso de ", rt("PromptService.execute()", code=True), " para ver el flujo. ",
        "Corre el bot, manda un mensaje, observa los logs."
    ))
    B.append(numbered(
        rt("Ejercicio Fase 5", bold=True), ": agrega un comando nuevo ", rt("/echo", code=True),
        " que regrese el mensaje del usuario. ¿Cuántas líneas? ¿Qué archivos tocaste?"
    ))

    B.append(divider())

    # ============================================================
    # SECTION 8: Archivos que NO leer
    # ============================================================
    B.append(h1("Archivos que NO deberías leer todavía"))

    B.append(callout(
        "⚠️", "Algunos archivos en el repo ", rt("NO", bold=True),
        " son parte del proyecto — son scripts auxiliares de sesiones de debugging pasadas. ",
        "Si los lees, te van a confundir porque no siguen los patrones del código de producción.",
        color="red_background"
    ))

    B.append(bullet(rt("tmp/create_bugfix_page.py", code=True), " (359 líneas) — script para crear página de Notion."))
    B.append(bullet(rt("fix_roadmap_section3.py", code=True), " (202 líneas) — otro script auxiliar."))
    B.append(bullet(rt("cleanup_section3.py", code=True), " (93 líneas) — otro script auxiliar."))

    B.append(para(rt("Recomendación", bold=True), ": bórralos. Comando:"))

    B.append(code_blk(
        "rm tmp/create_bugfix_page.py, fix_roadmap_section3.py, cleanup_section3.py",
        language="bash"
    ))

    B.append(para("(Después: ", rt("git add -A && git commit -m \"chore: remove one-off Notion fix scripts\"", code=True), ".)"))

    B.append(divider())

    # ============================================================
    # SECTION 9: Tips
    # ============================================================
    B.append(h1("Tips de aprendizaje"))

    B.append(numbered(
        rt("Lee firmas antes que cuerpos", bold=True), ": primero los imports, luego las definiciones de clase, ",
        "luego las firmas de métodos. Solo cuando entiendes QUÉ hace cada pieza, lees CÓMO lo hace."
    ))
    B.append(numbered(
        rt("Anota en papel", bold=True), ": clases, constructores, métodos públicos, con quién habla. ",
        "Escribir a mano te obliga a sintetizar."
    ))
    B.append(numbered(
        rt("Pregúntate ¿qué pasaría si...?", bold=True), ": si cambio esto, ¿qué se rompe? ",
        "Esa pregunta te lleva a las dependencias."
    ))
    B.append(numbered(
        rt("Vuelve al AppContainer", bold=True), ": si te pierdes, ",
        rt("services/container.py", code=True), " es tu mapa. Mira qué tiene y a quién habla."
    ))
    B.append(numbered(
        rt("Lee tests al final", bold=True), ": son la mejor documentación. ",
        "Te muestran uso real sin la carga de la lógica de negocio."
    ))
    B.append(numbered(
        rt("No leas en línea recta", bold=True), ": salta entre archivos cuando hay relación. ",
        "Es normal abrir 4 archivos al mismo tiempo."
    ))
    B.append(numbered(
        rt("Cantidades > perfección", bold=True), ": leer 70% bien es mejor que 100% mal. ",
        "No te frustres si no entiendes todo a la primera."
    ))

    B.append(divider())

    # ============================================================
    # SECTION 10: Recursos
    # ============================================================
    B.append(h1("Recursos adicionales"))

    B.append(para(rt("Libros", bold=True), ":"))
    B.append(bullet(rt("\"The Pragmatic Programmer\"", bold=True), " — Tips 8, 11, 14 (DRY, Ortogonalidad)."))
    B.append(bullet(rt("\"Clean Architecture\"", bold=True), " — Robert C. Martin (capítulos sobre DI y boundaries)."))
    B.append(bullet(rt("\"Fluent Python\"", bold=True), " — Luciano Ramalho (capítulos sobre Protocols)."))

    B.append(para(rt("En este repo", bold=True), ":"))
    B.append(bullet(rt("DOCUMENTACION.md", code=True), " — arquitectura completa."))
    B.append(bullet(rt("README.md", code=True), " — quick start."))

    B.append(para(rt("En Notion", bold=True), ":"))
    B.append(bullet("Página \"🐛 Bug Fix — MiniMax-M3\" — ejemplo de debugging real."))
    B.append(bullet("Página \"🚀 Comando /pr\" — ejemplo de feature completa."))
    B.append(bullet("Página \"🗺️ Roadmap — /update improvements\" — ejemplo de planning."))

    B.append(callout("🎯",
        "Ánimo, carnal. Si te atoras, vuelve al glosario o a la sección 3. ",
        "El código es más simple de lo que parece la primera vez.",
        color="green_background"))

    return B


def main():
    print("Creating page...")
    new_page = client.pages.create(
        parent={"page_id": PARENT_ID},
        properties={"title": [{"type": "text", "text": {"content": "Roadmap para leer el código base"}}]},
        icon={"type": "emoji", "emoji": "📚"},
    )
    page_id = new_page["id"]
    print(f"Page created: {page_id}")

    blocks = build()
    print(f"Total blocks: {len(blocks)}")

    BATCH = 100
    for i in range(0, len(blocks), BATCH):
        chunk = blocks[i:i + BATCH]
        print(f"Appending blocks {i+1}..{i+len(chunk)}...")
        client.blocks.children.append(block_id=page_id, children=chunk)

    print("DONE")
    print(f"URL: https://app.notion.com/p/{page_id.replace('-', '')}")
    return page_id


if __name__ == "__main__":
    main()
'''

# Write the script with UTF-8 encoding
out_path = Path(r"C:\Users\marie\Desktop\mono\python\Sdd-Orchestrator-Telegram-Bot\tmp\create_roadmap_page.py")
out_path.write_text(SCRIPT, encoding="utf-8")
print(f"Wrote {out_path}")
print(f"Size: {out_path.stat().st_size} bytes")

# Run it
result = subprocess.run(
    [sys.executable, str(out_path)],
    capture_output=True, text=True
)
print("--- STDOUT ---")
print(result.stdout)
print("--- STDERR ---")
print(result.stderr)
print(f"Exit code: {result.returncode}")
