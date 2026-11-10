"""Declarative prompt templates for VLM interactions.

Каждая функция возвращает готовый system-prompt строку.
Публичный API оставлен прежним:
    markdown_system_prompt(test=False)
    layout_system_prompt(test=False)
    entity_system_prompt(document_type, visual, test)
    combined_system_prompt(document_type, test)
    annotation_system_prompt()
    visio_diagram_system_prompt(test=False)
"""

from __future__ import annotations

from idp.entity_schema import build_entity_type_descriptions

# ---------------------------------------------------------------------------
# Общие фрагменты правил (используются в нескольких промптах)
# ---------------------------------------------------------------------------

_NOISE_RULES = """STRICTLY EXCLUDE the following noise elements:
- Page numbers
- Running headers and footers
- Journal names, magazine titles, publication names at page bottom or top
- Watermarks
- Copyright notices
- URLs and email addresses not part of the main content area
- Any decorative or boilerplate text outside the main content area

"""

_MARKDOWN_STRUCTURE_RULES = """Preserve the actual document content in reading order.
Split text into paragraphs. Separate paragraphs with a blank line.
Mark the document title and every section/chapter heading with Markdown headings
(# for the title, ## for sections, ### for subsections) — never leave a heading as
a plain paragraph.
Keep numbered and bulleted lists as Markdown lists.
Preserve tables as Markdown tables.
When a table has many columns, read ALL columns — do not truncate to the first column.
If a horizontal table is too wide to read in full, note: 'Table too wide, truncated at <column name>'.

"""

_IMAGE_ANNOTATION_RULES = """Describe every image, chart, diagram, figure, photo, stamp or signature inline with
[Image: <kind>: <detailed description of what is shown> | bbox: <x1> <y1> <x2> <y2>], where
<kind> is one of: photo, chart, diagram, screenshot, stamp, signature, table, other.
bbox is the rectangle of that visual on the page image in a 0..1000 coordinate system with the
origin in the top-left corner of the page image.
If your request contains several page images, also append '| img: <N>', where N is the 1-based
number of the page image the visual belongs to.
If a page contains no images, do not add any image placeholder.

"""

_HANDWRITTEN_RULES = """HANDWRITING DETECTION:
If any text is handwritten (cursive, ink, marker, different from printed text), wrap it in
[HANDWRITTEN: ... | bbox: <x1> <y1> <x2> <y2>]. Give the bbox of the handwritten fragment on the
page image (0..1000, origin top-left) and, for several page images in one request, also
'| img: <N>' with the 1-based page image number.
Example: 'The price is [HANDWRITTEN: 5000 | bbox: 610 420 760 455] rubles.'

"""

_TAIL_RULES = "Output ONLY the Markdown text. No explanations. No thinking blocks. No reasoning."


def _markdown_body(test: bool) -> str:
    if test:
        return "Convert the document images to Markdown. Output only Markdown."
    return (
        "Reconstruct the document page as clean Markdown.\n\n"
        + _NOISE_RULES
        + _MARKDOWN_STRUCTURE_RULES
        + _IMAGE_ANNOTATION_RULES
        + _HANDWRITTEN_RULES
        + _TAIL_RULES
    )


# ---------------------------------------------------------------------------
# Markdown reconstruction
# ---------------------------------------------------------------------------


def markdown_system_prompt(test: bool = False) -> str:
    return _markdown_body(test)


# ---------------------------------------------------------------------------
# Layout (JSON)
# ---------------------------------------------------------------------------


def layout_system_prompt(test: bool = False) -> str:
    if test:
        return "Convert the document images to a JSON layout. Output only JSON."
    return """Analyze the document image and return a structured JSON layout of its content.

Identify every block of the document in reading order: headings, paragraphs,
lists, tables and images. For each block output an object with a 'type' field.

Block types:
- {"type": "heading", "level": 1..6, "text": "..."}
- {"type": "paragraph", "text": "..."}
- {"type": "list", "items": ["...", "..."]}
- {"type": "table", "rows": [["cell", "cell"], ["cell", "cell"]]}
- {"type": "image", "caption": "..."}

Rules:
- Preserve the reading order of the blocks.
- Mark titles and section headings as headings (level 1 for the title, 2 for sections, 3 for subsections).
- Keep numbered and bulleted lists as lists.
- Preserve tables as rows of cells — read ALL columns.
- Describe every image, chart, diagram or figure as an image block with a caption.
- Exclude page numbers, headers, footers, watermarks, copyright notices.

Return a single JSON object: {"blocks": [ ... ]}
No explanations, no code fences, no extra text, no thinking blocks, no reasoning."""


# ---------------------------------------------------------------------------
# Confidence guidance (shared by entity prompts)
# ---------------------------------------------------------------------------


def _confidence_guidance() -> str:
    return """CONFIDENCE ESTIMATION (CRITICAL): Estimate confidence (0.0-1.0) based on the actual visual quality
of the source: handwriting legibility, blur, contrast, character ambiguity, and stroke clarity.
Do NOT default to 1.0 for handwriting.
Use 0.9-1.0 only for perfectly crisp printed text that you can read with zero doubt.
Use 0.5-0.8 for clearly readable handwriting.
Use 0.3-0.5 for blurred, low-contrast, or ambiguous handwriting.
Use 0.0-0.3 for illegible handwriting or characters you had to guess.
Digits vs letters and Cyrillic vs Latin confusions in handwriting must lower confidence."""


# ---------------------------------------------------------------------------
# Entity extraction
# ---------------------------------------------------------------------------


def _entity_common_rules() -> str:
    return """ENTITY DEFINITION: An entity is a discrete, structured fact with a short specific value.
Valid examples: dates, amounts, phone numbers, INN, OGRN, names, addresses, document numbers, codes.

PERSON NAMES (CRITICAL): Extract ALL person names (ФИО) regardless of context.
This includes:
- Names with titles/positions: 'И. И. Иванов, ведущий научный сотрудник'
- Names without any title or context: just 'Сталин' or 'Иванов'
- Names in lists: 'И. И. Иванов, Д. Д. Дятлов, В.В. Должанский'
- Names in signatures: 'В. Лаптев'
- Names in documents: 'И.В. Сталин'

For person entities, extract the FULL NAME as it appears in the text.
If initials are used (e.g., 'И.В. Сталин'), keep them as-is.
If only last name appears (e.g., 'Сталин'), extract it as the value.

FORBIDDEN (do NOT extract these):
- Full sentences or clauses
- Paragraphs or headings
- Generic document type words without specific value (e.g., just 'приказ', 'договор', 'акт')
- Boilerplate phrases like 'см. приложение', 'без изменений', 'ответственный'
- Values longer than 150 characters
- Values that are identical to the surrounding sentence

For each entity provide:
- type: one of the schema types below, or 'other' if none matches
- value: exact short text from the document
- normalized_value: normalized form if applicable, otherwise omit
- page: 1-based page number
- paragraph: 1-based paragraph number within the page
- evidence: exact short snippet containing the entity
- confidence: float 0.0-1.0
- handwritten: true if handwritten, false otherwise"""


def _entity_output_instruction() -> str:
    return (
        f"Schema:\n{build_entity_type_descriptions()}\n\n"
        'Return ONLY valid JSON: {"entities": [...]}\n'
        "No explanations, no code fences, no thinking blocks, no reasoning."
    )


def _document_type_focus(document_type: str) -> str | None:
    focus_map = {
        "directive": """Extract atomic metadata entities from this directive document (order/instruction/recommendation).

Focus on:
- Directive type and number
- Date
- Addressee (whom it is addressed to: subdivision, position, name)
- Signer (who signed: position, name)
- Basis/reference document
- Item numbers and task descriptions
- Deadlines/dates
- Control officer""",
        "certificate": """Extract atomic metadata entities from this certificate/statement document.

Focus on:
- Certificate type, number, issue date
- To whom issued (ФИО, status)
- Issuer (organization, position, name)
- Basis document
- Status facts (has/does not have, working/studying, etc.)
- Periods and dates (pay attention to AS_OF vs FOR_PERIOD)
- Amounts, rates, codes""",
        "ttkh": """Extract atomic metadata entities from this tactical-technical characteristics (ТТХ) document.

Focus on:
- Object/equipment name and model
- Mass, dimensions
- Crew/size
- Armament/equipment
- Range, speed, endurance
- Engine/powerplant
- Armor/protection
- Electronics/systems
- Dates and versions

Preserve units and measurements exactly as written.
Do NOT normalize technical parameters to generic placeholders.

FORBIDDEN:
- Full sentences or clauses
- Generic words like 'характеристики' without value
- Values longer than 150 characters""",
        "ttz": """Extract atomic metadata entities from this technical specification (ТТЗ) document.

Focus on:
- Requirement numbers (e.g., 3.2.1)
- Requirement text (functional, non-functional, constraints)
- Quantitative norms: time, temperature, ranges, probabilities
- Standards (ГОСТ, ISO, IEEE, ТУ)
- Deadline/dates
- Executors/responsible persons""",
        "summary": """Extract atomic metadata entities from this operational/financial/epidemiological summary.

Focus on:
- Summary type and period
- Dates and times
- Locations/regions/objects
- Statuses and categories
- Quantities, counts, amounts
- Identifiers and codes
- Responsible persons/units

Preserve exact status wording (e.g., 'ликвидировано', 'в работе').
Do NOT lemmatize or normalize statuses.

FORBIDDEN:
- Full sentences or clauses
- Generic words like 'сводка' without specifics
- Values longer than 150 characters""",
    }
    return focus_map.get(document_type)


def entity_system_prompt(
    document_type: str = "other",
    visual: bool = False,
    test: bool = False,
) -> str:
    if test:
        return (
            "Extract entities from the text. Return only JSON with an 'entities' array. "
            "For each entity, also include a 'comment' field with a brief explanation in Russian when confidence is below 0.5.\n\n"
            + _confidence_guidance()
        )

    intro = (
        "Extract atomic metadata entities from this document page image.\n\n"
        if visual
        else "Extract atomic metadata entities from the provided text.\n\n"
    )
    rules = _entity_common_rules()
    output = _entity_output_instruction()
    focus = _document_type_focus(document_type)

    if focus:
        return intro + focus + "\n\n" + rules + "\n\n" + output
    return intro + rules + "\n\n" + output


# ---------------------------------------------------------------------------
# Combined (markdown + entities)
# ---------------------------------------------------------------------------


def combined_system_prompt(document_type: str = "other", test: bool = False) -> str:
    if test:
        return (
            "Process the document images. Reconstruct as Markdown and extract entities. "
            "Return a single JSON object with 'markdown' and 'entities' keys. "
            "For entities with confidence below 0.5, include a 'comment' field with a brief explanation in Russian. No extra text.\n\n"
            + _confidence_guidance()
        )

    return f"""You are a document analysis assistant. Process the document images and do BOTH tasks:

1. Reconstruct the document page as clean Markdown.
2. Extract all entities from the document.

For task 1, follow these rules:
- {_NOISE_RULES.strip()}
- Preserve tables as Markdown tables
- Read ALL columns of every table — never truncate to only the first column
- Mark titles and section headings with Markdown headings (# for the title, ## for sections, ### for subsections)
- Keep numbered and bulleted lists as Markdown lists
- Describe every image, chart, diagram, figure, photo, stamp or signature inline with
  [Image: <kind>: <detailed description> | bbox: <x1> <y1> <x2> <y2>] using a 0..1000 page
  coordinate system with the origin in the top-left corner, plus '| img: <N>' with the 1-based
  page image number when several page images are sent
- Wrap handwritten text in [HANDWRITTEN: ... | bbox: <x1> <y1> <x2> <y2>] (0..1000, origin top-left)
- Output ONLY the Markdown text

For task 2, extract ONLY atomic metadata entities.
ENTITY DEFINITION: An entity is a discrete, structured fact with a short specific value.
FORBIDDEN: full sentences, generic words like 'приказ' without number/date, boilerplate phrases, values > 150 chars.

For each entity provide: type, value, normalized_value (if applicable), page, paragraph, evidence, confidence (0.0-1.0), handwritten (true/false).

Return a single JSON object with TWO keys:
{{"markdown": "<reconstructed markdown>", "entities": [<entity objects>]}}
No explanations, no code fences, no extra text, no thinking blocks, no reasoning."""


# ---------------------------------------------------------------------------
# Annotation
# ---------------------------------------------------------------------------


def annotation_system_prompt() -> str:
    return (
        "Create a brief annotation (1-2 sentences) for this document. "
        "Identify the document type, main topic, and key entities. "
        "Output only the annotation text in Russian. No markdown, no quotes, no explanations."
    )


# ---------------------------------------------------------------------------
# Visio diagram
# ---------------------------------------------------------------------------


def visio_diagram_system_prompt(test: bool = False) -> str:
    if test:
        return "Analyze the Visio diagram image and return JSON with shapes. Output only JSON."
    return """Analyze this Visio diagram image and extract all shapes and connectors as structured JSON.

The diagram contains Visio shapes: rectangles, ellipses, diamonds, lines, connectors,
text boxes, and groups. Identify each shape and its properties.

Coordinate system: 0..1000, origin at TOP-LEFT of the image.

Return a single JSON object with this structure:
{
  "page": {"width": 1000, "height": 1000},
  "shapes": [
    {
      "id": "shape_1",
      "type": "rectangle|ellipse|diamond|line|connector|text|image|group",
      "x": 100,
      "y": 100,
      "width": 200,
      "height": 100,
      "text": "Shape text content",
      "font_size": 12,
      "bold": false,
      "italic": false,
      "fill": "#ffffff",
      "line_color": "#000000",
      "line_width": 1,
      "line_style": "solid|dashed|dotted",
      "rotation": 0
    },
    {
      "id": "shape_2",
      "type": "connector",
      "from": "shape_1",
      "to": "shape_3",
      "from_side": "right",
      "to_side": "left",
      "text": "Label",
      "line_style": "solid|dashed",
      "line_width": 1,
      "line_color": "#000000",
      "start_arrow": "none|arrow|open_arrow|dot",
      "end_arrow": "arrow|open_arrow|dot|none"
    }
  ]
}

Rules:
- Detect ALL shapes: rectangles, rounded rectangles, ellipses, diamonds (decision), triangles, hexagons, text boxes, images.
- Detect connectors/lines between shapes with arrow directions.
- Extract text inside each shape (including multi-line).
- For groups, either flatten or include as a group with child shape IDs.
- Preserve fill color, line color, line width, line style, font properties.
- Coordinates are in 0..1000 space (top-left origin).
- If text is rotated, include rotation in degrees.
- Output ONLY valid JSON. No explanations, no code fences, no extra text."""
