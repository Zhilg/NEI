"""Declarative prompt templates for VLM interactions."""

from __future__ import annotations

from dataclasses import dataclass

from idp.entity_schema import build_entity_type_descriptions


@dataclass(frozen=True)
class PromptTemplate:
    name: str
    content: str


@dataclass
class PromptBuilder:
    test: bool = False
    document_type: str = "other"
    visual: bool = False

    def markdown(self) -> PromptTemplate:
        if self.test:
            return PromptTemplate(
                name="markdown_test",
                content="Convert the document images to Markdown. Output only Markdown.",
            )
        return PromptTemplate(
            name="markdown",
            content=(
                "Reconstruct the document page as clean Markdown.\n\n"
                "STRICTLY EXCLUDE the following noise elements:\n"
                "- Page numbers\n"
                "- Running headers and footers\n"
                "- Journal names, magazine titles, publication names at page bottom or top\n"
                "- Watermarks\n"
                "- Copyright notices\n"
                "- URLs and email addresses not part of the main content\n"
                "- Any decorative or boilerplate text outside the main content area\n\n"
                "Preserve the actual document content in reading order.\n"
                "Split text into paragraphs. Separate paragraphs with a blank line.\n"
                "Mark the document title and every section/chapter heading with Markdown headings "
                "(# for the title, ## for sections, ### for subsections) — never leave a heading as "
                "a plain paragraph.\n"
                "Keep numbered and bulleted lists as Markdown lists.\n"
                "Preserve tables as Markdown tables.\n"
                "When a table has many columns, read ALL columns — do not truncate to the first column.\n"
                "If a horizontal table is too wide to read in full, note: 'Table too wide, truncated at <column name>'.\n"
                "Describe every image, chart, diagram, figure, photo, stamp or signature inline with "
                "[Image: <kind>: <detailed description of what is shown> | bbox: <x1> <y1> <x2> <y2>], where "
                "<kind> is one of: photo, chart, diagram, screenshot, stamp, signature, table, other. "
                "bbox is the rectangle of that visual on the page image in a 0..1000 coordinate system with the "
                "origin in the top-left corner of the page image. "
                "If your request contains several page images, also append '| img: <N>', where N is the 1-based "
                "number of the page image the visual belongs to. "
                "If a page contains no images, do not add any image placeholder.\n\n"
                "HANDWRITING DETECTION:\n"
                "If any text is handwritten (cursive, ink, marker, different from printed text), wrap it in "
                "[HANDWRITTEN: ... | bbox: <x1> <y1> <x2> <y2>]. Give the bbox of the handwritten fragment on the "
                "page image (0..1000, origin top-left) and, for several page images in one request, also "
                "'| img: <N>' with the 1-based page image number.\n"
                "Example: 'The price is [HANDWRITTEN: 5000 | bbox: 610 420 760 455] rubles.'\n\n"
                "Output ONLY the Markdown text. No explanations. No thinking blocks. No reasoning."
            ),
        )

    def layout(self) -> PromptTemplate:
        if self.test:
            return PromptTemplate(
                name="layout_test",
                content="Convert the document images to a JSON layout. Output only JSON.",
            )
        return PromptTemplate(
            name="layout",
            content=(
                "Analyze the document image and return a structured JSON layout of its content.\n\n"
                "Identify every block of the document in reading order: headings, paragraphs,\n"
                "lists, tables and images. For each block output an object with a 'type' field.\n\n"
                "Block types:\n"
                '- {"type": "heading", "level": 1..6, "text": "..."}\n'
                '- {"type": "paragraph", "text": "..."}\n'
                '- {"type": "list", "items": ["...", "..."]}\n'
                '- {"type": "table", "rows": [["cell", "cell"], ["cell", "cell"]]}\n'
                '- {"type": "image", "caption": "..."}\n\n'
                "Rules:\n"
                "- Preserve the reading order of the blocks.\n"
                "- Mark titles and section headings as headings (level 1 for the title, 2 for sections, 3 for subsections).\n"
                "- Keep numbered and bulleted lists as lists.\n"
                "- Preserve tables as rows of cells — read ALL columns.\n"
                "- Describe every image, chart, diagram or figure as an image block with a caption.\n"
                "- Exclude page numbers, headers, footers, watermarks, copyright notices.\n\n"
                'Return a single JSON object: {"blocks": [ ... ]}\n'
                "No explanations, no code fences, no extra text, no thinking blocks, no reasoning."
            ),
        )

    def entity(self) -> PromptTemplate:
        if self.test:
            return PromptTemplate(
                name="entity_test",
                content=(
                    "Extract entities from the text. Return only JSON with an 'entities' array. "
                    "For each entity, also include a 'comment' field with a brief explanation in Russian when confidence is below 0.5."
                    "\n\n"
                    + _confidence_guidance()
                ),
            )

        intro = (
            "Extract atomic metadata entities from this document page image.\n\n"
            if self.visual
            else "Extract atomic metadata entities from the provided text.\n\n"
        )
        rules = _entity_common_rules()
        output = _entity_output_instruction()
        focus = _document_type_focus(self.document_type)

        if focus:
            return PromptTemplate(
                name=f"entity_{self.document_type}",
                content=intro + focus + rules + output,
            )
        return PromptTemplate(
            name="entity_generic",
            content=intro + rules + output,
        )

    def combined(self) -> PromptTemplate:
        if self.test:
            return PromptTemplate(
                name="combined_test",
                content=(
                    "Process the document images. Reconstruct as Markdown and extract entities. "
                    "Return a single JSON object with 'markdown' and 'entities' keys. "
                    "For entities with confidence below 0.5, include a 'comment' field with a brief explanation in Russian. No extra text."
                    "\n\n"
                    + _confidence_guidance()
                ),
            )
        return PromptTemplate(
            name="combined",
            content=(
                "You are a document analysis assistant. Process the document images and do BOTH tasks:\n\n"
                "1. Reconstruct the document page as clean Markdown.\n"
                "2. Extract all entities from the document.\n\n"
                "For task 1, follow these rules:\n"
                "- Exclude page numbers, headers, footers, watermarks, copyright notices, decorative text\n"
                "- Preserve tables as Markdown tables\n"
                "- Read ALL columns of every table — never truncate to only the first column\n"
                "- Mark titles and section headings with Markdown headings (# for the title, ## for "
                "sections, ### for subsections)\n"
                "- Keep numbered and bulleted lists as Markdown lists\n"
                "- Describe every image, chart, diagram, figure, photo, stamp or signature inline with "
                "[Image: <kind>: <detailed description> | bbox: <x1> <y1> <x2> <y2>] using a 0..1000 page "
                "coordinate system with the origin in the top-left corner, plus '| img: <N>' with the 1-based "
                "page image number when several page images are sent\n"
                "- Wrap handwritten text in [HANDWRITTEN: ... | bbox: <x1> <y1> <x2> <y2>] (0..1000, origin top-left)\n"
                "- Output ONLY the Markdown text\n\n"
                "For task 2, extract ONLY atomic metadata entities.\n"
                "ENTITY DEFINITION: An entity is a discrete, structured fact with a short specific value. "
                "FORBIDDEN: full sentences, generic words like 'приказ' without number/date, boilerplate phrases, values > 150 chars.\n\n"
                "For each entity provide: type, value, normalized_value (if applicable), page, paragraph, evidence, confidence (0.0-1.0), handwritten (true/false).\n\n"
                "Return a single JSON object with TWO keys:\n"
                '{"markdown": "<reconstructed markdown>", "entities": [<entity objects>]}\n'
                "No explanations, no code fences, no extra text, no thinking blocks, no reasoning."
            ),
        )


def annotation_system_prompt() -> str:
    return (
        "Create a brief annotation (1-2 sentences) for this document. "
        "Identify the document type, main topic, and key entities. "
        "Output only the annotation text in Russian. No markdown, no quotes, no explanations."
    )


def markdown_system_prompt(test: bool = False) -> str:
    return PromptBuilder(test=test).markdown().content


def layout_system_prompt(test: bool = False) -> str:
    return PromptBuilder(test=test).layout().content


def entity_system_prompt(
    document_type: str = "other",
    visual: bool = False,
    test: bool = False,
) -> str:
    return PromptBuilder(test=test, document_type=document_type, visual=visual).entity().content


def combined_system_prompt(document_type: str = "other", test: bool = False) -> str:
    return PromptBuilder(test=test, document_type=document_type, visual=True).combined().content


def _confidence_guidance() -> str:
    return (
        "CONFIDENCE ESTIMATION (CRITICAL): Estimate confidence (0.0-1.0) based on the actual visual quality "
        "of the source: handwriting legibility, blur, contrast, character ambiguity, and stroke clarity. "
        "Do NOT default to 1.0 for handwriting. "
        "Use 0.9-1.0 only for perfectly crisp printed text that you can read with zero doubt. "
        "Use 0.5-0.8 for clearly readable handwriting. "
        "Use 0.3-0.5 for blurred, low-contrast, or ambiguous handwriting. "
        "Use 0.0-0.3 for illegible handwriting or characters you had to guess. "
        "Digits vs letters and Cyrillic vs Latin confusions in handwriting must lower confidence.\n"
    )


def _entity_common_rules() -> str:
    return (
        "ENTITY DEFINITION: An entity is a discrete, structured fact with a short specific value. "
        "Valid examples: dates, amounts, phone numbers, INN, OGRN, names, addresses, document numbers, codes.\n\n"
        "PERSON NAMES (CRITICAL): Extract ALL person names (ФИО) regardless of context. "
        "This includes:\n"
        "- Names with titles/positions: 'И. И. Иванов, ведущий научный сотрудник'\n"
        "- Names without any title or context: just 'Сталин' or 'Иванов'\n"
        "- Names in lists: 'И. И. Иванов, Д. Д. Дятлов, В.В. Должанский'\n"
        "- Names in signatures: 'В. Лаптев'\n"
        "- Names in documents: 'И.В. Сталин'\n\n"
        "For person entities, extract the FULL NAME as it appears in the text. "
        "If initials are used (e.g., 'И.В. Сталин'), keep them as-is. "
        "If only last name appears (e.g., 'Сталин'), extract it as the value.\n\n"
        "FORBIDDEN (do NOT extract these):\n"
        "- Full sentences or clauses\n"
        "- Paragraphs or headings\n"
        "- Generic document type words without specific value (e.g., just 'приказ', 'договор', 'акт')\n"
        "- Boilerplate phrases like 'см. приложение', 'без изменений', 'ответственный'\n"
        "- Values longer than 150 characters\n"
        "- Values that are identical to the surrounding sentence\n\n"
        "For each entity provide:\n"
        "- type: one of the schema types below, or 'other' if none matches\n"
        "- value: exact short text from the document\n"
        "- normalized_value: normalized form if applicable, otherwise omit\n"
        "- page: 1-based page number\n"
        "- paragraph: 1-based paragraph number within the page\n"
        "- evidence: exact short snippet containing the entity\n"
        "- confidence: float 0.0-1.0\n"
        "- handwritten: true if handwritten, false otherwise\n\n"
    )


def _entity_output_instruction() -> str:
    return (
        f"Schema:\n{build_entity_type_descriptions()}\n\n"
        'Return ONLY valid JSON: {"entities": [...]}\n'
        "No explanations, no code fences, no thinking blocks, no reasoning."
    )


def _document_type_focus(document_type: str) -> str | None:
    focus_map = {
        "directive": (
            "Extract atomic metadata entities from this directive document (order/instruction/recommendation).\n\n"
            "Focus on:\n"
            "- Directive type and number\n"
            "- Date\n"
            "- Addressee (whom it is addressed to: subdivision, position, name)\n"
            "- Signer (who signed: position, name)\n"
            "- Basis/reference document\n"
            "- Item numbers and task descriptions\n"
            "- Deadlines/dates\n"
            "- Control officer\n\n"
        ),
        "certificate": (
            "Extract atomic metadata entities from this certificate/statement document.\n\n"
            "Focus on:\n"
            "- Certificate type, number, issue date\n"
            "- To whom issued (ФИО, status)\n"
            "- Issuer (organization, position, name)\n"
            "- Basis document\n"
            "- Status facts (has/does not have, working/studying, etc.)\n"
            "- Periods and dates (pay attention to AS_OF vs FOR_PERIOD)\n"
            "- Amounts, rates, codes\n\n"
        ),
        "ttkh": (
            "Extract atomic metadata entities from this tactical-technical characteristics (ТТХ) document.\n\n"
            "Focus on:\n"
            "- Object/equipment name and model\n"
            "- Mass, dimensions\n"
            "- Crew/size\n"
            "- Armament/equipment\n"
            "- Range, speed, endurance\n"
            "- Engine/powerplant\n"
            "- Armor/protection\n"
            "- Electronics/systems\n"
            "- Dates and versions\n\n"
            "Preserve units and measurements exactly as written. "
            "Do NOT normalize technical parameters to generic placeholders.\n\n"
            "FORBIDDEN:\n"
            "- Full sentences or clauses\n"
            "- Generic words like 'характеристики' without value\n"
            "- Values longer than 150 characters\n\n"
        ),
        "ttz": (
            "Extract atomic metadata entities from this technical specification (ТТЗ) document.\n\n"
            "Focus on:\n"
            "- Requirement numbers (e.g., 3.2.1)\n"
            "- Requirement text (functional, non-functional, constraints)\n"
            "- Quantitative norms: time, temperature, ranges, probabilities\n"
            "- Standards (ГОСТ, ISO, IEEE, ТУ)\n"
            "- Deadline/dates\n"
            "- Executors/responsible persons\n\n"
        ),
        "summary": (
            "Extract atomic metadata entities from this operational/financial/epidemiological summary.\n\n"
            "Focus on:\n"
            "- Summary type and period\n"
            "- Dates and times\n"
            "- Locations/regions/objects\n"
            "- Statuses and categories\n"
            "- Quantities, counts, amounts\n"
            "- Identifiers and codes\n"
            "- Responsible persons/units\n\n"
            "Preserve exact status wording (e.g., 'ликвидировано', 'в работе'). "
            "Do NOT lemmatize or normalize statuses.\n\n"
            "FORBIDDEN:\n"
            "- Full sentences or clauses\n"
            "- Generic words like 'сводка' without specifics\n"
            "- Values longer than 150 characters\n\n"
        ),
    }
    return focus_map.get(document_type)


def visio_diagram_system_prompt(test: bool = False) -> str:
    return visio_diagram_prompt(test=test).content


def visio_diagram_prompt(test: bool = False) -> PromptTemplate:
    if test:
        return PromptTemplate(
            name="visio_diagram_test",
            content="Analyze the Visio diagram image and return JSON with shapes. Output only JSON.",
        )
    return PromptTemplate(
        name="visio_diagram",
        content=(
            "Analyze this Visio diagram image and extract all shapes and connectors as structured JSON.\n\n"
            "The diagram contains Visio shapes: rectangles, ellipses, diamonds, lines, connectors, "
            "text boxes, and groups. Identify each shape and its properties.\n\n"
            "Coordinate system: 0..1000, origin at TOP-LEFT of the image.\n\n"
            "Return a single JSON object with this structure:\n"
            "{\n"
            '  "page": {"width": 1000, "height": 1000},\n'
            '  "shapes": [\n'
            "    {\n"
            '      "id": "shape_1",\n'
            '      "type": "rectangle|ellipse|diamond|line|connector|text|image|group",\n'
            '      "x": 100,\n'
            '      "y": 100,\n'
            '      "width": 200,\n'
            '      "height": 100,\n'
            '      "text": "Shape text content",\n'
            '      "font_size": 12,\n'
            '      "bold": false,\n'
            '      "italic": false,\n'
            '      "fill": "#ffffff",\n'
            '      "line_color": "#000000",\n'
            '      "line_width": 1,\n'
            '      "line_style": "solid|dashed|dotted",\n'
            '      "rotation": 0\n'
            "    },\n"
            "    {\n"
            '      "id": "shape_2",\n'
            '      "type": "connector",\n'
            '      "from": "shape_1",\n'
            '      "to": "shape_3",\n'
            '      "from_side": "right",\n'
            '      "to_side": "left",\n'
            '      "text": "Label",\n'
            '      "line_style": "solid|dashed",\n'
            '      "line_width": 1,\n'
            '      "line_color": "#000000",\n'
            '      "start_arrow": "none|arrow|open_arrow|dot",\n'
            '      "end_arrow": "arrow|open_arrow|dot|none"\n'
            "    }\n"
            "  ]\n"
            "}\n\n"
            "Rules:\n"
            "- Detect ALL shapes: rectangles, rounded rectangles, ellipses, diamonds (decision), triangles, hexagons, text boxes, images.\n"
            "- Detect connectors/lines between shapes with arrow directions.\n"
            "- Extract text inside each shape (including multi-line).\n"
            "- For groups, either flatten or include as a group with child shape IDs.\n"
            "- Preserve fill color, line color, line width, line style, font properties.\n"
            "- Coordinates are in 0..1000 space (top-left origin).\n"
            "- If text is rotated, include rotation in degrees.\n"
            "- Output ONLY valid JSON. No explanations, no code fences, no extra text."
        ),
    )
