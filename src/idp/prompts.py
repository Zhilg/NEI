"""System prompts for VLM interactions."""

from __future__ import annotations


def annotation_system_prompt() -> str:
    return (
        "Create a brief annotation (1-2 sentences) for this document. "
        "Identify the document type, main topic, and key entities. "
        "Output only the annotation text in Russian. No markdown, no quotes, no explanations."
    )


def markdown_system_prompt(test: bool = False) -> str:
    if test:
        return "Convert the document images to Markdown. Output only Markdown."
    return (
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
        "Preserve tables as Markdown tables.\n"
        "When a table has many columns, read ALL columns — do not truncate to the first column.\n"
        "If a horizontal table is too wide to read in full, note: 'Table too wide, truncated at <column name>'.\n"
        "Describe every image, chart, diagram, or figure inline with [Image: detailed description of what is shown]. "
        "If a page contains no images, do not add any image placeholder.\n"
        "For each image, include what type of visual it is (photo, chart, diagram, screenshot, table, etc.) "
        "and describe its content in detail.\n\n"
        "HANDWRITING DETECTION:\n"
        "If any text is handwritten (cursive, ink, marker, different from printed text), wrap it in [HANDWRITTEN: ...]. "
        "Example: 'The price is [HANDWRITTEN: 5000] rubles.'\n\n"
        "Output ONLY the Markdown text. No explanations. No thinking blocks. No reasoning."
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
    from idp.vlm_client import _build_entity_type_descriptions

    return (
        f"Schema:\n{_build_entity_type_descriptions()}\n\n"
        'Return ONLY valid JSON: {"entities": [...]}\n'
        "No explanations, no code fences, no thinking blocks, no reasoning."
    )


def _base_entity_intro() -> str:
    return "Extract atomic metadata entities from the provided text.\n\n"


def _visual_entity_intro() -> str:
    return "Extract atomic metadata entities from this document page image.\n\n"


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


def entity_system_prompt(
    document_type: str = "other",
    visual: bool = False,
    test: bool = False,
) -> str:
    if test:
        return (
            "Extract entities from the text. Return only JSON with an 'entities' array. "
            "For each entity, also include a 'comment' field with a brief explanation in Russian when confidence is below 0.5."
            "\n\n"
            + _confidence_guidance()
        )
    intro = _visual_entity_intro() if visual else _base_entity_intro()
    base = intro + _entity_common_rules() + _entity_output_instruction()
    if document_type == "directive":
        return (
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
            + _entity_common_rules()
            + _entity_output_instruction()
        )
    if document_type == "certificate":
        return (
            "Extract atomic metadata entities from this certificate/statement document.\n\n"
            "Focus on:\n"
            "- Certificate type, number, issue date\n"
            "- To whom issued (ФИО, status)\n"
            "- Issuer (organization, position, name)\n"
            "- Basis document\n"
            "- Status facts (has/does not have, working/studying, etc.)\n"
            "- Periods and dates (pay attention to AS_OF vs FOR_PERIOD)\n"
            "- Amounts, rates, codes\n\n"
            + _entity_common_rules()
            + _entity_output_instruction()
        )
    if document_type == "ttkh":
        return (
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
            + _entity_common_rules()
            + _entity_output_instruction()
        )
    if document_type == "ttz":
        return (
            "Extract atomic metadata entities from this technical specification (ТТЗ) document.\n\n"
            "Focus on:\n"
            "- Requirement numbers (e.g., 3.2.1)\n"
            "- Requirement text (functional, non-functional, constraints)\n"
            "- Quantitative norms: time, temperature, ranges, probabilities\n"
            "- Standards (ГОСТ, ISO, IEEE, ТУ)\n"
            "- Deadline/dates\n"
            "- Executors/responsible persons\n\n"
            + _entity_common_rules()
            + _entity_output_instruction()
        )
    if document_type == "summary":
        return (
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
            + _entity_common_rules()
            + _entity_output_instruction()
        )
    return base


def combined_system_prompt(document_type: str = "other", test: bool = False) -> str:
    if test:
        return (
            "Process the document images. Reconstruct as Markdown and extract entities. "
            "Return a single JSON object with 'markdown' and 'entities' keys. "
            "For entities with confidence below 0.5, include a 'comment' field with a brief explanation in Russian. No extra text."
            "\n\n"
            + _confidence_guidance()
        )
    md = markdown_system_prompt(test=False)
    ent = entity_system_prompt(document_type=document_type, visual=True, test=False)
    return (
        "You are a document analysis assistant. Process the document images and do BOTH tasks:\n\n"
        "1. Reconstruct the document page as clean Markdown.\n"
        "2. Extract all entities from the document.\n\n"
        "For task 1, follow these rules:\n"
        "- Exclude page numbers, headers, footers, watermarks, copyright notices, decorative text\n"
        "- Preserve tables as Markdown tables\n"
        "- Read ALL columns of every table — never truncate to only the first column\n"
        "- Describe every image, chart, diagram, or figure inline with [Image: detailed description]\n"
        "- Wrap handwritten text in [HANDWRITTEN: ...]\n"
        "- Output ONLY the Markdown text\n\n"
        "For task 2, extract ONLY atomic metadata entities.\n"
        "ENTITY DEFINITION: An entity is a discrete, structured fact with a short specific value. "
        "FORBIDDEN: full sentences, generic words like 'приказ' without number/date, boilerplate phrases, values > 150 chars.\n\n"
        "For each entity provide: type, value, normalized_value (if applicable), page, paragraph, evidence, confidence (0.0-1.0), handwritten (true/false).\n\n"
        "Return a single JSON object with TWO keys:\n"
        '{"markdown": "<reconstructed markdown>", "entities": [<entity objects>]}\n'
        "No explanations, no code fences, no extra text, no thinking blocks, no reasoning."
    )
