"""Create Notion page documenting the MiniMax-M3 truncation bug fix."""
import os
import sys
from notion_client import Client

NOTION_TOKEN = os.environ.get("NOTION_TOKEN")
if not NOTION_TOKEN:
    print("ERROR: NOTION_TOKEN not set", file=sys.stderr)
    sys.exit(1)

client = Client(auth=NOTION_TOKEN)
PARENT_ID = "37b7c219-93af-8057-8085-e27bc0dafb1a"
PAGE_TITLE = "🐛 Bug Fix — MiniMax-M3 respuestas truncadas en Telegram"


def p(text):
    return {
        "object": "block",
        "type": "paragraph",
        "paragraph": {
            "rich_text": [{"type": "text", "text": {"content": text}}]
        },
    }


def h1(text):
    return {
        "object": "block",
        "type": "heading_1",
        "heading_1": {
            "rich_text": [{"type": "text", "text": {"content": text}}]
        },
    }


def h2(text):
    return {
        "object": "block",
        "type": "heading_2",
        "heading_2": {
            "rich_text": [{"type": "text", "text": {"content": text}}]
        },
    }


def code(text, language="python"):
    return {
        "object": "block",
        "type": "code",
        "code": {
            "rich_text": [{"type": "text", "text": {"content": text}}],
            "language": language,
        },
    }


def bullet(text):
    return {
        "object": "block",
        "type": "bulleted_list_item",
        "bulleted_list_item": {
            "rich_text": [{"type": "text", "text": {"content": text}}]
        },
    }


def numbered(text):
    return {
        "object": "block",
        "type": "numbered_list_item",
        "numbered_list_item": {
            "rich_text": [{"type": "text", "text": {"content": text}}]
        },
    }


def callout(text, emoji="✅", color="green_background"):
    return {
        "object": "block",
        "type": "callout",
        "callout": {
            "rich_text": [{"type": "text", "text": {"content": text}}],
            "icon": {"type": "emoji", "emoji": emoji},
            "color": color,
        },
    }


def divider():
    return {"object": "block", "type": "divider", "divider": {}}


def p_rich(segments):
    """Paragraph with rich text segments. segments = [(text, annotations_dict)]"""
    rich = []
    for seg in segments:
        if isinstance(seg, str):
            rich.append({"type": "text", "text": {"content": seg}})
        else:
            text, ann = seg
            entry = {"type": "text", "text": {"content": text}}
            if ann:
                entry["annotations"] = ann
            rich.append(entry)
    return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": rich}}


def code_rich(segments, language="python"):
    rich = []
    for seg in segments:
        if isinstance(seg, str):
            rich.append({"type": "text", "text": {"content": seg}})
        else:
            text, ann = seg
            entry = {"type": "text", "text": {"content": text}}
            if ann:
                entry["annotations"] = ann
            rich.append(entry)
    return {"object": "block", "type": "code", "code": {"rich_text": rich, "language": language}}


def table_row(cells):
    """cells is a list of plain strings; each becomes a cell."""
    return {
        "object": "block",
        "type": "table_row",
        "table_row": {
            "cells": [[{"type": "text", "text": {"content": c}}] for c in cells]
        },
    }


# ---- Build blocks ----------------------------------------------------------
BOLD = {"bold": True}
CODE = {"code": True}

blocks = []

# Section 1: Resumen Ejecutivo
blocks.append(h1("1. Resumen Ejecutivo"))
blocks.append(p_rich([
    "El bot con modelo ",
    ("MiniMax-M3", CODE),
    " (",
    ("minimax/MiniMax-M3", CODE),
    ") empezó a mostrar respuestas truncadas en Telegram. Solo llegaba el footer ",
    (" · MiniMax-M3", CODE),
    " sin nada de contenido. El bug NO ocurría con DeepSeek.",
]))
blocks.append(p_rich([
    "Causa raíz: la regex en ",
    ("clean_opencode_output()", CODE),
    " solo matcheaba el agent ",
    ("build", CODE),
    " (default de OpenCode CLI). Cuando se activó ",
    ("--agent sdd-orchestrator", CODE),
    " (requerido para MiniMax), el header cambió a ",
    ("> sdd-orchestrator · MiniMax-M3", CODE),
    " que NO matcheaba la regex.",
]))
blocks.append(p_rich([
    "Fix: se generalizó la regex para matchear cualquier agent name usando ",
    ("\\S+", CODE),
    ". Ahora strippea ",
    ("> build · ...", CODE),
    ", ",
    ("> sdd-orchestrator · ...", CODE),
    " y cualquier otro.",
]))

# Section 2: Diagnóstico
blocks.append(h1("2. Diagnóstico"))

blocks.append(h2("2.1 Síntoma"))
blocks.append(code(
    "Usuario: \"Listo, el comando /pr funciona...\"\n"
    "Bot recibía en Telegram: \" · MiniMax-M3\"\n"
    "                      (sin contenido, solo el footer)",
    language="plain text",
))

blocks.append(h2("2.2 Investigación"))
blocks.append(bullet("1. Verificar que el bot estaba mandando mensajes a Telegram ✓"))
blocks.append(bullet("2. Comparar output raw de opencode CLI con DeepSeek vs MiniMax ✓"))
blocks.append(bullet("3. Diferencia encontrada: header de OpenCode es distinto:"))
blocks.append(p_rich([
    "  - DeepSeek (con agent default): ",
    ("> build · deepseek-v4-pro", CODE),
]))
blocks.append(p_rich([
    "  - MiniMax (con ",
    ("--agent sdd-orchestrator", CODE),
    "): ",
    ("> sdd-orchestrator · MiniMax-M3", CODE),
]))
blocks.append(bullet("4. Leer formatting/markdown.py:clean_opencode_output() ✓"))
blocks.append(bullet("5. Encontrar regex que solo matcheaba > build literalmente ✓"))
blocks.append(p_rich([
    "6. Confirmar: el header de MiniMax sobrevivía el primer clean → segundo clean lo ",
    ("interpretaba como delimitador", {"bold": True}),
    " → truncaba el contenido antes",
]))

blocks.append(h2("2.3 Causa raíz"))
blocks.append(code(
    "# formatting/markdown.py línea 66 (ANTES)\n"
    "text = re.sub(r'^> build .*$', '', text, flags=re.MULTILINE)",
    language="python",
))
blocks.append(p_rich([
    "Esta regex solo eliminaba headers que empezaban con ",
    ("> build ", CODE),
    ". Como OpenCode CLI usa el nombre del agent como primer token del header, al usar ",
    ("--agent sdd-orchestrator", CODE),
    ", el header empezaba con ",
    ("> sdd-orchestrator", CODE),
    " que NO matcheaba la regex.",
]))

# Section 3: El Fix
blocks.append(h1("3. El Fix"))

blocks.append(h2("3.1 Cambio de código"))
blocks.append(code(
    "# formatting/markdown.py línea 66 (DESPUÉS)\n"
    "text = re.sub(r'^> \\S+ .*$', '', text, flags=re.MULTILINE)",
    language="python",
))
blocks.append(p_rich([
    ("\\S+", CODE),
    " matchea cualquier secuencia de caracteres no-espacio. Funciona para ",
    ("build", CODE),
    ", ",
    ("sdd-orchestrator", CODE),
    ", ",
    ("v4-pro", CODE),
    ", etc.",
]))

blocks.append(h2("3.2 Por qué funcionaba con DeepSeek pero no con MiniMax"))
blocks.append(p_rich([
    "El agente default de OpenCode CLI es ",
    ("build", CODE),
    ". Cuando el bot corría con DeepSeek sin ",
    ("--agent", CODE),
    ", el header era ",
    ("> build · deepseek-v4-pro", CODE),
    " que matcheaba la regex. Pero para usar MiniMax-M3 con las herramientas y permisos correctos, el bot necesita ",
    ("--agent sdd-orchestrator", CODE),
    " (definido en ",
    ("C:\\Users\\marie\\.config\\opencode\\opencode.json", CODE),
    "). Con ese flag, el header cambia a ",
    ("> sdd-orchestrator · MiniMax-M3", CODE),
    " y la regex anterior fallaba.",
]))

# Section 4: Tests de Regresión
blocks.append(h1("4. Tests de Regresión"))

blocks.append(h2("4.1 Tests agregados"))
blocks.append(code(
    "# tests/test_utils.py - TestCleanOpencodeOutput\n"
    "\n"
    "def test_removes_agent_header_lines(self):\n"
    "    \"\"\"> sdd-orchestrator · MiniMax-M3 header is stripped.\"\"\"\n"
    "    text = \"> sdd-orchestrator · MiniMax-M3\\n\\nHola, ¿en qué te ayudo?\"\n"
    "    result = clean_opencode_output(text)\n"
    "    self.assertNotIn(\"sdd-orchestrator\", result)\n"
    "    self.assertIn(\"Hola\", result)\n"
    "\n"
    "def test_double_clean_does_not_truncate_body(self):\n"
    "    \"\"\"Double-clean (real production flow) doesn't lose body content.\"\"\"\n"
    "    stderr_header = \"> sdd-orchestrator · MiniMax-M3\\n\"\n"
    "    body = \"Esta es la respuesta del modelo.\"\n"
    "    text = stderr_header + body\n"
    "    # First clean (in _deliver_response)\n"
    "    once = clean_opencode_output(text)\n"
    "    # Second clean (inside _assemble_response)\n"
    "    twice = clean_opencode_output(once)\n"
    "    self.assertIn(\"Esta es la respuesta\", twice)",
    language="python",
))

blocks.append(h2("4.2 Resultados"))
blocks.append(callout(
    "33/33 tests pasando (19 en test_utils.py incluyendo 2 nuevos; 14 en otros suites sin cambios)",
    emoji="✅",
    color="green_background",
))

# Section 5: Verificación Manual
blocks.append(h1("5. Verificación Manual"))

blocks.append(h2("5.1 Pasos para reproducir el fix"))
blocks.append(numbered("Reiniciar el bot: taskkill /F /IM python.exe + .\\run_bot.bat"))
blocks.append(numbered("Configurar modelo: /model m3"))
blocks.append(numbered('Mandar un prompt largo (ej: "explica la diferencia entre lista y tupla en Python")'))
blocks.append(numbered("Verificar que llega la respuesta COMPLETA, no solo el footer"))

blocks.append(h2("5.2 Antes vs Después"))
blocks.append(code(
    "ANTES (con bug):\n"
    "  Bot recibía: \" · MiniMax-M3\"\n"
    "  Usuario veía: nada de contenido\n"
    "\n"
    "DESPUÉS (con fix):\n"
    "  Bot recibe: respuesta completa del modelo\n"
    "  Usuario ve: respuesta completa",
    language="plain text",
))

# Section 6: Archivos Modificados
blocks.append(h1("6. Archivos Modificados"))
# Notion table requires a parent table block + rows as children. Use table block.
blocks.append({
    "object": "block",
    "type": "table",
    "table": {
        "table_width": 3,
        "has_column_header": True,
        "has_row_header": False,
        "children": [
            table_row(["Archivo", "Cambio", "Líneas"]),
            table_row([
                "formatting/markdown.py",
                "Regex de header-strip generalizada (build → \\S+)",
                "66",
            ]),
            table_row([
                "tests/test_utils.py",
                "2 nuevos tests de regresión",
                "+40 (aprox)",
            ]),
        ],
    },
})

# Section 7: Notas Técnicas
blocks.append(h1("7. Notas Técnicas"))

blocks.append(h2("7.1 Por qué el doble-clean causa truncado"))
blocks.append(p_rich([
    ("clean_opencode_output()", CODE),
    " se llama DOS veces en el flujo de producción:",
]))
blocks.append(numbered("En services/prompt_service.py:_deliver_response() para limpiar stdout"))
blocks.append(numbered("Dentro de _assemble_response() para re-formatear la respuesta final"))
blocks.append(p_rich([
    "Si el header ",
    ("> sdd-orchestrator · ...", CODE),
    " sobrevive el primer clean (porque la regex no lo matchea), entonces en el segundo clean hay un paso (línea que recorta todo antes del primer ",
    (">", CODE),
    ") que interpreta ese header huérfano como el delimitador, truncando todo el contenido anterior.",
]))

blocks.append(h2("7.2 Patrón de agent en OpenCode CLI"))
blocks.append(p_rich([
    "El header que OpenCode CLI emite es siempre del formato ",
    ("> <agent-name> · <model-id>", CODE),
    ". El ",
    ("<agent-name>", CODE),
    " puede ser:",
]))
blocks.append(bullet("build (default)"))
blocks.append(bullet("sdd-orchestrator (configurado en opencode.json)"))
blocks.append(bullet("v4-flash (otro agent configurado)"))
blocks.append(bullet("Cualquier otro definido por el usuario"))
blocks.append(p_rich([
    "La regex ",
    ("\\S+", CODE),
    " cubre todos estos casos.",
]))

# Section 8: Referencias
blocks.append(h1("8. Referencias"))
blocks.append(bullet("formatting/markdown.py:59-66 — función clean_opencode_output"))
blocks.append(bullet("formatting/markdown.py:275 — segunda llamada dentro de _assemble_response"))
blocks.append(bullet("services/prompt_service.py:259 — primera llamada en _deliver_response"))
blocks.append(bullet("tests/test_utils.py — tests de clean_opencode_output"))
blocks.append(bullet("C:\\Users\\marie\\.config\\opencode\\opencode.json:4 — definición del agent sdd-orchestrator"))

# ---- Create page ------------------------------------------------------------
print(f"Creating page with {len(blocks)} blocks...")
new_page = client.pages.create(
    parent={"page_id": PARENT_ID},
    properties={
        "title": [{"type": "text", "text": {"content": PAGE_TITLE}}]
    },
    icon={"type": "emoji", "emoji": "🐛"},
)
page_id = new_page["id"]
page_url = new_page["url"]
print(f"Page created. ID: {page_id}")
print(f"URL: {page_url}")

# ---- Append blocks in chunks of 100 -----------------------------------------
CHUNK = 100
total = len(blocks)
appended = 0
for i in range(0, total, CHUNK):
    chunk = blocks[i : i + CHUNK]
    client.blocks.children.append(block_id=page_id, children=chunk)
    appended += len(chunk)
    print(f"Appended {appended}/{total} blocks")

print("DONE")
print(f"PAGE_ID={page_id}")
print(f"PAGE_URL={page_url}")
