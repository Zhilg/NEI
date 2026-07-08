# Industrial IDP System — Architecture Review

**Author**: Principal AI Architect  
**Date**: 2026-07-08  
**Status**: FINAL  
**Goal**: Maximum accuracy entity/metadata extraction from heterogeneous documents

---

## 1. Critical Analysis of the Proposed Architecture

Your proposed pipeline:

```
Document → Classification → Text Quality → OCR → Layout → Tables → Semantic Reconstruction → Entity Extraction → Validation → Confidence Engine → Output
```

### 1.1 What Is Wrong Fundamentally

**The architecture is a linear pipeline. This is the single biggest flaw.**

Linear pipelines have a well-documented failure mode: **error cascading**. Every stage introduces errors that propagate irrecoverably downstream. In 2025-2026 research, this is the #1 cited limitation of multi-stage OCR systems:

> *"Pipeline systems decompose document parsing into layout detection, element-wise recognition, and rule-based assembly. They suffer from inter-stage error propagation and irreversible loss of visual context during text extraction."*  
> — Qianfan-OCR (Baidu, CVPR 2026)

> *"The cascaded structure of multiple models introduces inherent drawbacks, including error propagation and elevated development and maintenance overhead."*  
> — HunyuanOCR (Tencent, 2025)

**Concrete failure scenario**: Layout detection misclassifies a table as a text block → Table extraction is skipped entirely → Semantic reconstruction receives flat text instead of tabular data → Entity extraction hallucinates values from unstructured text. The error is invisible at output but catastrophic for accuracy.

### 1.2 Specific Stage Problems

| Stage | Problem |
|-------|---------|
| **Classification** | Premature classification commits the system to a single document type before understanding its content. Hybrid documents (invoice + contract terms + attachments) break this. |
| **Text Quality** | Binary "good/bad" quality assessment is insufficient. Quality varies by region within a single page. A page can have digital text in headers, scanned stamps, and handwritten signatures simultaneously. |
| **OCR** | Positioned as a single step — but which OCR? Different engines excel at different content: TrOCR for handwriting, PaddleOCR for printed text, Tesseract for clean digital. A single OCR pass is a guaranteed accuracy ceiling. |
| **Layout → Tables** | Sequential dependency is wrong. Layout and table extraction should happen jointly or with cross-feedback. Modern approaches (POTATR, PaddleOCR-VL) do both simultaneously. |
| **Semantic Reconstruction** | Vague, undefined stage. What does it actually do? Reading order? Paragraph assembly? Cross-page linking? This is where your pipeline needs the most specificity. |
| **Entity Extraction** | Single-model extraction is the weakest link for accuracy-critical systems. The 2026 state-of-the-art is **dual-model reconciliation** (specialist + VLM, see ExtractConf). |
| **Validation** | Rule-based validation cannot catch semantic errors. "The date is in the future" catches format errors but not "this invoice number doesn't exist." |
| **Confidence Engine** | Post-hoc confidence on a single extraction is unreliable. LLM confidence scores are systematically miscalibrated. Research shows raw confidence of 0.95 can correspond to actual accuracy of 0.70. |

---

## 2. What Is Outdated in the Proposed Architecture

### 2.1 Separate OCR as a Pipeline Stage (OUTDATED)

**2024 approach**: Dedicated OCR engine → text → downstream processing.

**2025-2026 approach**: VLMs that perform OCR-free document understanding, OR coarse-to-fine architectures that couple layout detection with element-level recognition in a single model.

Key evidence:
- **SmolDocling** (IBM, ICCV 2025): 256M parameter end-to-end VLM producing DocTags — no separate OCR stage at all.
- **PaddleOCR-VL** (Baidu, CVPR 2026): Coarse-to-fine architecture — RT-DETR for layout + 0.9B VLM for recognition. Layout and OCR are decoupled but NOT sequential pipeline stages.
- **HunyuanOCR** (Tencent, 2025): 1B end-to-end VLM, explicitly eliminating separate OCR.

The correct 2026 architecture treats OCR as **a capability embedded within the parsing model**, not a standalone pipeline stage.

### 2.2 Rule-Based Validation (OUTDATED)

Deterministic rules ("field X must be a date", "field Y must be numeric") miss:
- Semantically incorrect but syntactically valid extractions
- Cross-field inconsistencies (invoice total ≠ sum of line items)
- Hallucinated values that pass all format checks

**2026 approach**: LLM-driven validation with structured comparison (IDP Accelerator, 2026), or Reconstruction-as-Validation (RaV-IDP, 2026) where extracted data is rendered back to visual form and compared against the original document.

### 2.3 Single Confidence Score (OUTDATED)

A single confidence number per extraction is insufficient for production.

**2026 approach**: Multi-signal confidence:
- OCR region-level confidence
- Model logprob entropy
- Cross-model agreement/disagreement (Hunter-Mapper pattern from ExtractConf, 2026)
- Image quality scores
- Spatial layout coherence
- Post-hoc calibration (isotonic regression, ECE < 0.03)

---

## 3. Missing Stages

### 3.1 MISSING: Format Normalization & Multi-Resolution Rendering

Your architecture jumps from "Document" directly to "Classification." Missing: the critical conversion of heterogeneous inputs (DOCX, XLSX, PPTX, HTML, Email, TIFF) to a canonical page-image format at multiple resolutions. This stage determines the quality ceiling for everything downstream.

**Why it matters**: A DOCX rendered at 72 DPI produces garbage OCR. A TIFF at 600 DPI wastes GPU memory. Dynamic resolution — rendering at the native resolution the parsing model needs — is a key innovation in PaddleOCR-VL.

### 3.2 MISSING: Document Packet Segmentation

Real-world documents arrive as **packets** — a single PDF containing an invoice, a contract addendum, a W-9 form, and a handwritten note. Without segmentation, the system processes the entire packet as one document type.

**2026 approach**: DocSplit (IDP Accelerator, 2026) uses BIO tagging on page sequences to segment multi-document packets into constituent documents.

### 3.3 MISSING: Multi-Model Extraction with Reconciliation

Your architecture has a single "Entity Extraction" stage. For maximum accuracy, you need **two or more extraction approaches** that fail in different ways:

- **Specialist model** (LayoutLMv3, fine-tuned on your document type): Fast, stable, fails on OOD layouts
- **Frontier VLM** (Claude 3.5, GPT-4o, Qwen2.5-VL): Robust to layout variation, unstable across runs
- **Reconciliation engine**: When they agree → auto-accept. When they disagree → tiebreaker (third model, rules, or human)

Evidence: A top-3 EU bank achieved 99.2% accuracy at 4.1% manual review rate using dual-model reconciliation, vs. 96.8% with specialist alone or 94.5% with VLM alone.

### 3.4 MISSING: Self-Correcting / Iterative Extraction Loop

Your pipeline is single-pass. If the first extraction attempt fails, there's no recovery.

**2026 approach**: Agentic extraction with self-correction:
1. Extract → Validate → If confidence low → Reformulate prompt → Re-extract with different strategy
2. Reconstruction-as-Validation: Render extracted data back to image → Compare with original → If mismatch → Trigger fallback extractor

### 3.5 MISSING: Reading Order as a First-Class Stage

Reading order is mentioned nowhere in your pipeline, but it's one of the hardest problems in document understanding — especially for multi-column layouts, sidebars, footnotes, and cross-page tables.

PaddleOCR-VL uses a dedicated pointer network (6 transformer layers) specifically for reading order prediction. This is NOT a trivial sub-task of layout analysis.

### 3.6 MISSING: Human-in-the-Loop (HITL) Integration Point

For a system targeting maximum accuracy, there must be a structured HITL stage — not as an afterthought but as a first-class architectural component with:
- Configurable confidence thresholds per field type
- Role-based review (Admin vs Reviewer)
- Feedback loops that improve models over time

### 3.7 MISSING: Cross-Page Element Handling

Tables, paragraphs, and form sections that span multiple pages are a major blind spot. PubTables-v2 (2025) introduced the first large-scale benchmark for multi-page table extraction, showing that most models fail completely on tables spanning >2 pages.

---

## 4. Stages That Are Redundant

### 4.1 "Classification" as a Separate Upstream Stage

**Verdict: Merge into intelligent routing.**

A rigid classify-then-process pipeline fails on:
- Hybrid documents (invoice + contract + receipt in one file)
- Documents that don't fit predefined categories
- Multi-document packets

Instead: **Adaptive routing** — a lightweight classifier determines initial processing strategy, but the system can re-route mid-pipeline based on content discovered during parsing. This is the Adaptive RAG pattern applied to document processing.

### 4.2 "Text Quality" as a Standalone Stage

**Verdict: Embed as a signal, not a stage.**

Text quality is not binary and varies by region. Instead of a separate stage:
- OCR confidence scores per region serve as quality signals
- Image quality metrics (blur, skew, resolution) feed directly into the confidence engine
- The system should dynamically select OCR engines based on detected quality, not gate the entire pipeline

### 4.3 Separate "Tables" Stage After "Layout"

**Verdict: Merge with layout detection.**

Modern layout models (PP-DocLayoutV2, RT-DETR based) already detect and classify tables as part of layout analysis. A separate "Tables" stage implies re-detection. Table structure recognition (rows, columns, cells) should be triggered by layout detection output, not as an independent sequential stage.

---

## 5. Modern Approaches (2025-2026)

### 5.1 End-to-End VLMs Replacing Pipelines

| Model | Params | Approach | OmniDocBench Score |
|-------|--------|----------|-------------------|
| PaddleOCR-VL-1.6 | 0.9B | Coarse-to-fine (RT-DETR + VLM) | 96.33% |
| Qianfan-OCR | 4B | End-to-end + Layout-as-Thought | 93.12% |
| HunyuanOCR | 1B | End-to-end VLM | SOTA on multiple benchmarks |
| SmolDocling | 256M | End-to-end DocTags | Competitive with 27x larger models |
| GLM-OCR | 0.9B | Two-stage + Multi-Token Prediction | Competitive SOTA |

**Key insight**: The trend is NOT to replace pipelines with monolithic VLMs. It's to use **compact specialist VLMs** (0.25B–4B) in a coarse-to-fine architecture where layout detection and element recognition are decoupled but NOT sequentially pipelined.

### 5.2 Layout-as-Thought (Qianfan-OCR, 2026)

Instead of a separate layout analysis stage, the VLM generates layout reasoning as an optional "thinking" phase triggered by `<think>` tokens. The model produces bounding boxes, element types, and reading order BEFORE producing final content. This:
- Recovers layout analysis within the end-to-end paradigm
- Provides targeted accuracy improvements on complex layouts
- Eliminates inter-stage error propagation for layout

### 5.3 Structured Layout Priors (IBM, 2026)

Pre-resolve layout detection outside the VLM by running a lightweight RT-DETR detector, serializing its outputs in the parser's native DocTags vocabulary, and injecting them into the prompt alongside the full page image. Results:
- Markdown F1: 0.37 → 0.92 on OOD documents
- Table TEDS: 0.01 → 0.36 on Chinese documents
- Infinite-loop decoding failures: eliminated

### 5.4 Multi-Signal Confidence (ExtractConf, 2026)

The state-of-the-art confidence engine uses:
- **Hunter-Mapper dual-call design**: Same LLM, two structurally asymmetric prompts (field-guided vs. document-guided). Different failure modes make disagreement informative.
- **40 features**: OCR confidence, logprob entropy, spatial centroid divergence, image quality, cross-call agreement
- **CatBoost classifier** with post-hoc isotonic regression calibration
- **Result**: 0.928 ROC AUC, 99.1% accuracy at 80% coverage, zero-shot transfer across domains

### 5.5 Reconstruction-as-Validation (RaV-IDP, 2026)

After extraction, render the extracted representation back into a form comparable to the original document region. A comparator scores fidelity between reconstruction and source. Low fidelity → trigger fallback extractor.

### 5.6 Agentic Document Processing (IDP Accelerator, 2026)

Production IDP systems are moving toward **agentic architectures** where:
- The system autonomously decides which extraction strategy to use
- Self-corrects when validation fails
- Routes to specialized sub-processors based on discovered content
- Integrates HITL as a first-class component with MCP (Model Context Protocol)

---

## 6. What Enterprise IDP Systems Actually Use (2026)

### 6.1 Google Document AI
- Gemini Layout Parser for table recognition, reading order, context-aware chunking (GA May 2026)
- Few-shot and zero-shot extraction with Gemini 2.5/3 Pro
- CEL-based validation rules
- Specialized processors per document type

### 6.2 Azure Document Intelligence
- Pre-built models for 20+ document types
- Custom neural models trained on 5-20 samples
- Container deployment for on-premises
- Batch API for high-volume processing

### 6.3 LandingAI ADE
- Visual-first parsing with coordinate-level visual grounding
- 99.16% DocVQA accuracy
- Semantic chunking preserving document hierarchy
- Schema-based extraction with visual citations

### 6.4 Common Enterprise Patterns (2026)

1. **Dual-model reconciliation** — specialist + VLM, auto-accept on agreement
2. **Confidence-based routing** — high confidence → auto-process, low → human review
3. **Per-document-type calibration curves** — separate confidence models for invoices vs. contracts vs. forms
4. **Continuous feedback loops** — human corrections retrain extraction models
5. **Modular, swappable stages** — each stage independently replaceable without system redesign

---

## 7. Proposed New Architecture (From Scratch)

### 7.1 Design Principles

1. **No linear pipeline** — use a directed acyclic graph (DAG) with conditional routing
2. **Dual-path extraction** — always run two independent extractors and reconcile
3. **Multi-signal confidence** — never trust a single confidence score
4. **Self-correcting loops** — retry with different strategies on failure
5. **HITL as first-class** — not an afterthought
6. **Per-region processing** — different regions of the same page may need different strategies
7. **Swappable components** — every stage can be replaced without system redesign

### 7.2 Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                        DOCUMENT INTAKE                              │
│  ┌──────────┐  ┌──────────────┐  ┌─────────────┐                  │
│  │ Ingestion │→│ Format Norm  │→│ Multi-Res    │                  │
│  │ (queue)   │  │ (to images)  │  │ Rendering    │                  │
│  └──────────┘  └──────────────┘  └──────┬──────┘                  │
│                                         │                          │
└─────────────────────────────────────────┼──────────────────────────┘
                                          ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    DOCUMENT UNDERSTANDING                            │
│                                                                     │
│  ┌────────────────────────────────────────────────────────────┐    │
│  │            Coarse-to-Fine Parsing (VLM-based)              │    │
│  │                                                            │    │
│  │  ┌─────────────┐    ┌──────────────┐    ┌──────────────┐  │    │
│  │  │ Packet Segm │───▶│ Layout Detect│───▶│ Reading Order│  │    │
│  │  │ (DocSplit)  │    │ (RT-DETR)    │    │ (Pointer Net)│  │    │
│  │  └─────────────┘    └──────┬───────┘    └──────┬───────┘  │    │
│  │                            │                    │          │    │
│  │                     ┌──────┴───────┐            │          │    │
│  │                     ▼              ▼            ▼          │    │
│  │              ┌──────────┐  ┌──────────┐  ┌──────────┐     │    │
│  │              │ Text     │  │ Table    │  │ Figure/  │     │    │
│  │              │ Region   │  │ Structure│  │ Chart/   │     │    │
│  │              │ OCR(VLM) │  │ (TATR/   │  │ Formula  │     │    │
│  │              │          │  │  VLM)    │  │ (VLM)    │     │    │
│  │              └──────────┘  └──────────┘  └──────────┘     │    │
│  │                     │              │            │          │    │
│  │                     └──────────────┴────────────┘          │    │
│  │                                  │                         │    │
│  │                          ┌───────▼───────┐                │    │
│  │                          │  Structured   │                │    │
│  │                          │  Document     │                │    │
│  │                          │  Representation│                │    │
│  │                          └───────────────┘                │    │
│  └────────────────────────────────────────────────────────────┘    │
│                                                                     │
└─────────────────────────────────────┬───────────────────────────────┘
                                      ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    DUAL-PATH EXTRACTION                              │
│                                                                     │
│  ┌─────────────────────┐         ┌─────────────────────┐           │
│  │ Path A: Specialist  │         │ Path B: VLM         │           │
│  │ (LayoutLMv3 /       │         │ (Claude 3.5 /       │           │
│  │  fine-tuned model)  │         │  GPT-4o /           │           │
│  │                     │         │  Qwen2.5-VL)        │           │
│  │ - Schema-guided     │         │ - Prompt-driven     │           │
│  │ - Fast, stable      │         │ - Robust to layout  │           │
│  │ - Fails on OOD      │         │ - Fails inconsistently           │
│  └──────────┬──────────┘         └──────────┬──────────┘           │
│             │                               │                      │
│             └───────────┬───────────────────┘                      │
│                         ▼                                          │
│              ┌─────────────────────┐                               │
│              │  Reconciliation     │                               │
│              │  Engine             │                               │
│              │                     │                               │
│              │  Agreement → Accept │                               │
│              │  Disagree → Tiebreak│                               │
│              └──────────┬──────────┘                               │
│                         │                                          │
└─────────────────────────┼──────────────────────────────────────────┘
                          ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    VALIDATION & CONFIDENCE                           │
│                                                                     │
│  ┌──────────────────────────────────────────────────────────┐      │
│  │              Multi-Signal Confidence Engine               │      │
│  │                                                          │      │
│  │  Signals:                                                │      │
│  │  ├── OCR region confidence                               │      │
│  │  ├── Model logprob entropy (both paths)                  │      │
│  │  ├── Cross-path agreement score                          │      │
│  │  ├── Image quality metrics                               │      │
│  │  ├── Spatial layout coherence                            │      │
│  │  ├── Reconstruction fidelity (RaV-IDP)                   │      │
│  │  └── Cross-field semantic consistency                    │      │
│  │                                                          │      │
│  │  Calibration: Isotonic regression per field type          │      │
│  │  Target: ECE < 0.03                                      │      │
│  └──────────────────────┬───────────────────────────────────┘      │
│                         │                                          │
│  ┌──────────────────────▼───────────────────────────────────┐      │
│  │              Validation Layer                             │      │
│  │                                                          │      │
│  │  ├── Format validation (deterministic)                   │      │
│  │  ├── Cross-field consistency (LLM-driven)                │      │
│  │  ├── External system validation (DB lookup, API)         │      │
│  │  └── Reconstruction-vs-source fidelity check             │      │
│  └──────────────────────┬───────────────────────────────────┘      │
│                         │                                          │
└─────────────────────────┼──────────────────────────────────────────┘
                          ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    ROUTING & OUTPUT                                  │
│                                                                     │
│  ┌──────────────────────────────────────────────────────────┐      │
│  │              Confidence-Based Router                      │      │
│  │                                                          │      │
│  │  confidence ≥ 0.95  → AUTO-ACCEPT                        │      │
│  │  confidence ≥ 0.85  → AUTO-ACCEPT + AUDIT SAMPLE (5%)    │      │
│  │  confidence ≥ 0.70  → HUMAN REVIEW (normal priority)     │      │
│  │  confidence ≥ 0.50  → HUMAN REVIEW (high priority)       │      │
│  │  confidence < 0.50  → REJECT / REPROCESS                 │      │
│  └──────────────────────┬───────────────────────────────────┘      │
│                         │                                          │
│             ┌───────────┼───────────┐                              │
│             ▼           ▼           ▼                              │
│      ┌──────────┐ ┌──────────┐ ┌──────────┐                       │
│      │ Auto     │ │ Human    │ │ Reject / │                       │
│      │ Output   │ │ Review   │ │ Retry    │                       │
│      │          │ │ Queue    │ │ (self-   │                       │
│      │          │ │          │ │ correct) │                       │
│      └──────────┘ └────┬─────┘ └──────────┘                       │
│                        │                                          │
│                        ▼                                          │
│                 ┌──────────────┐                                   │
│                 │ Feedback     │                                   │
│                 │ Loop         │                                   │
│                 │ (retrain     │                                   │
│                 │  models)     │                                   │
│                 └──────────────┘                                   │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

### 7.3 Decision Flow

```
Document Arrives
│
├── 1. INGEST: Accept from email/FTP/API/upload → persist to durable storage
│
├── 2. NORMALIZE: Convert to canonical format
│   ├── PDF (digital) → Extract native images + text layer
│   ├── PDF (scan) → Render pages at 300 DPI
│   ├── PDF (hybrid) → Extract digital text regions + render scan regions
│   ├── TIFF/PNG/JPG → Direct image
│   ├── DOCX → Render via LibreOffice to PDF → images
│   ├── XLSX → Parse native structure + render sheets as images
│   ├── PPTX → Render slides as images
│   ├── HTML → Render via headless browser to images
│   └── Email → Parse MIME → separate attachments → recurse
│
├── 3. SEGMENT (if multi-document): DocSplit BIO tagging
│   └── Single document → skip
│
├── 4. PARSE (Coarse-to-Fine):
│   ├── 4a. Layout Detection (RT-DETR / PP-DocLayoutV2)
│   │   → Bounding boxes + element types (text, table, figure, formula, signature, stamp, ...)
│   │
│   ├── 4b. Reading Order Prediction (Pointer Network)
│   │   → Ordered sequence of detected regions
│   │
│   ├── 4c. Per-Region Recognition:
│   │   ├── Text regions → Compact VLM OCR (PaddleOCR-VL-0.9B / SmolDocling)
│   │   ├── Table regions → Table structure recognition (TATR/POTATR) + cell OCR
│   │   ├── Figure/chart regions → VLM captioning + data extraction
│   │   ├── Formula regions → LaTeX/MathML recognition
│   │   ├── Signature regions → Signature detection + identity matching
│   │   └── Stamp/seal regions → Seal recognition
│   │
│   └── 4d. Assemble Structured Document Representation
│       → Markdown/JSON with bounding boxes, reading order, hierarchy
│
├── 5. EXTRACT (Dual-Path):
│   ├── Path A: Specialist model (LayoutLMv3, fine-tuned per document type)
│   │   → Schema-guided extraction, fast, stable
│   │
│   ├── Path B: Frontier VLM (Claude 3.5 / GPT-4o / Qwen2.5-VL)
│   │   → Prompt-driven extraction with structured output schema
│   │   → Includes Layout-as-Thought for complex layouts
│   │
│   └── Reconciliation:
│       ├── Exact match → Accept (high confidence)
│       ├── Fuzzy match (edit distance ≤ 2) → Accept with flag
│       ├── Numeric match (within tolerance) → Accept with flag
│       └── Disagreement → Tiebreaker (third model / rules / human)
│
├── 6. VALIDATE:
│   ├── Format validation (deterministic rules)
│   ├── Cross-field consistency (LLM-driven: total = sum of line items?)
│   ├── External validation (DB lookup, API check)
│   └── Reconstruction fidelity (RaV-IDP: render extracted → compare with source)
│
├── 7. CONFIDENCE:
│   ├── Compute multi-signal confidence per field
│   ├── Apply per-field-type calibration curves
│   └── Produce calibrated confidence score [0, 1]
│
├── 8. ROUTE:
│   ├── ≥ 0.95 → Auto-accept → Output
│   ├── 0.85–0.95 → Auto-accept + audit sample → Output
│   ├── 0.70–0.85 → Human review (normal) → Output
│   ├── 0.50–0.70 → Human review (high priority) → Output
│   └── < 0.50 → Self-correct loop:
│       ├── Reformulate extraction prompt
│       ├── Try alternative OCR engine
│       ├── Try different VLM
│       └── If still failing after 2 retries → Human review
│
└── 9. FEEDBACK LOOP:
    ├── Human corrections → Retrain specialist model
    ├── Confidence drift monitoring → Recalibrate
    └── New document types → Extend classifier + add calibration curves
```

---

## 8. Why This Architecture Is Better

### 8.1 Error Propagation Mitigation

| Your Architecture | New Architecture |
|---|---|
| Linear pipeline — errors cascade | DAG with dual-path extraction — errors caught at reconciliation |
| Single OCR pass — permanent error | Per-region OCR selection — best engine per content type |
| Single extraction — silent failures | Dual-path extraction — disagreement flags errors |
| Post-hoc validation — too late | Reconstruction-as-Validation — catches errors before output |

### 8.2 Accuracy Gains (Based on Published Benchmarks)

| Technique | Accuracy Improvement | Source |
|---|---|---|
| Dual-model reconciliation | +2.4% over best single model | EU bank invoice benchmark (2026) |
| Layout priors injection | F1: 0.37 → 0.92 on OOD docs | IBM Structured Layout Priors (2026) |
| Reconstruction-as-Validation | +38.1% failed table recovery | RaV-IDP (2026) |
| Multi-signal confidence | 99.1% accuracy at 80% coverage | ExtractConf (2026) |
| Coarse-to-fine parsing | 96.33% OmniDocBench (SOTA) | PaddleOCR-VL-1.6 (2026) |

### 8.3 Architectural Properties

1. **Resilience**: No single point of failure. If one extractor fails, the other catches it.
2. **Adaptability**: New document types require adding a calibration curve, not redesigning the pipeline.
3. **Observability**: Per-field confidence with component breakdown (OCR quality vs. extraction quality vs. layout quality).
4. **Scalability**: Each stage independently scalable. Layout detection on GPU, extraction via API, validation on CPU.
5. **Cost control**: Confidence-based routing means expensive VLM extraction only runs as Path B (or tiebreaker), not on every document.

---

## 9. Compromises and Trade-offs

### 9.1 Latency vs. Accuracy

Dual-path extraction roughly doubles extraction time. Mitigation:
- Run paths in parallel (not sequentially)
- Use specialist as primary, VLM only on low-confidence fields
- Cache extraction results for repeated document templates

### 9.2 Cost vs. Coverage

Frontier VLM extraction (GPT-4o) costs ~$0.008/page. Running it on every document is expensive. Mitigation:
- Use confidence from Path A to decide whether to run Path B
- For high-volume standard documents (invoices), specialist alone with high confidence threshold may suffice
- Reserve VLM for complex/variable documents

### 9.3 Complexity vs. Maintainability

The dual-path + reconciliation architecture is more complex to build and maintain than a linear pipeline. Mitigation:
- Modular design with clear interfaces between stages
- Each component independently testable and replaceable
- Comprehensive observability from day one

### 9.4 Model Dependency

Relying on frontier VLMs (Claude, GPT-4o) creates vendor dependency. Mitigation:
- Abstract extraction behind a provider interface
- Maintain open-source alternatives (Qwen2.5-VL, InternVL)
- Specialist model (LayoutLMv3) is self-hosted and vendor-independent

---

## 10. Remaining Potential Problems

### 10.1 Multi-Page Tables

Tables spanning 3+ pages remain extremely challenging. PubTables-v2 (2025) introduced the first benchmark showing most models fail on tables >2 pages. Current mitigation: page-boundary detection + cross-page merging, but this is an active research area.

### 10.2 Handwriting + Printed Text Mix

Documents mixing handwritten annotations with printed text require different OCR engines per region. Region-level routing adds complexity and potential misclassification.

### 10.3 Non-Latin Scripts and Complex Layouts

Right-to-left text (Arabic, Hebrew), vertical text (CJK), and mixed-script documents still challenge even state-of-the-art models. PaddleOCR-VL supports 109 languages but performance varies significantly.

### 10.4 Confidence Calibration Drift

Calibration curves degrade over time as document populations shift. Requires continuous monitoring and periodic recalibration (weekly ECE tracking, alerting at ECE > 0.03).

### 10.5 Hallucination in VLM Extraction

VLMs can hallucinate values that don't exist in the document. Reconstruction-as-Validation (RaV-IDP) mitigates but doesn't eliminate this. For high-stakes fields (invoice amounts, contract dates), external validation against business systems is essential.

### 10.6 Long Documents

Documents exceeding the VLM's context window (even with 131K extended context) require chunking strategies that may split semantically related sections. Gemini 1.5 Pro's million-token context or retrieval-augmented approaches are current mitigations.

---

## 11. Supporting Research and References

| Paper/Source | Year | Key Contribution |
|---|---|---|
| Qianfan-OCR (Baidu) | 2026 | End-to-end 4B VLM, Layout-as-Thought, OmniDocBench SOTA |
| PaddleOCR-VL-1.6 (Baidu) | 2026 | Coarse-to-fine 0.9B VLM, 96.33% OmniDocBench |
| SmolDocling (IBM) | 2025 | 256M end-to-end VLM, DocTags format, ICCV 2025 |
| HunyuanOCR (Tencent) | 2025 | 1B end-to-end VLM, XD-RoPE for spatial reasoning |
| GLM-OCR (Zhipu) | 2026 | 0.9B VLM with Multi-Token Prediction |
| Structured Layout Priors (IBM) | 2026 | RT-DETR priors injected into VLM prompt, F1: 0.37→0.92 |
| DocCogito | 2026 | Layout tower + Visual-Semantic Chain for grounded reasoning |
| POTATR / PubTables-v2 (Microsoft) | 2025-2026 | 29M page-level table extraction, GriTS 0.964 |
| TDATR (CVPR 2026) | 2026 | End-to-end table recognition with cell-level alignment |
| ExtractConf | 2026 | Hunter-Mapper dual-call confidence, 0.928 AUC, ECE 0.034 |
| RaV-IDP | 2026 | Reconstruction-as-Validation, +38.1% failed table recovery |
| IDP Accelerator (AWS) | 2026 | Agentic IDP with DocSplit, MCP, HITL, 98% accuracy |
| DISCO | 2026 | OCR pipelines vs VLMs: dual strategy for different doc types |
| GutenOCR | 2026 | Grounded OCR front-end with token-box alignment |
| Enterprise 7-Stage Architecture | 2026 | Canonical IDP pipeline (ingestion→normalization→layout→OCR→structure→extract→validate) |

---

## 12. Summary of Architectural Recommendations

1. **Replace linear pipeline with DAG** — conditional routing, parallel paths, self-correcting loops
2. **Eliminate standalone OCR stage** — use coarse-to-fine VLM architecture (layout detection + element-level recognition)
3. **Add dual-path extraction** — specialist + VLM with reconciliation
4. **Add multi-signal confidence** — Hunter-Mapper, OCR confidence, spatial coherence, reconstruction fidelity
5. **Add Reconstruction-as-Validation** — render extracted data back, compare with source
6. **Add document packet segmentation** — DocSplit for multi-document inputs
7. **Add cross-page element handling** — explicit support for multi-page tables and sections
8. **Make HITL first-class** — configurable thresholds, role-based review, feedback loops
9. **Per-field-type calibration** — separate confidence models for different extraction types
10. **Self-correcting retry loop** — reformulate + re-extract on low confidence, max 2 retries
