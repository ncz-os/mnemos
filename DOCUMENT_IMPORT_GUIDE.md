# MNEMOS Document Import with Docling

v3.0.0 includes intelligent document import using IBM's [Docling](https://www.ibm.com/products/docling) library. This allows automatic conversion of documents (PDFs, Word, Excel, PowerPoint, etc.) into MNEMOS memory records with automatic chunking and metadata extraction.

## Installation

Document import is optional and requires the Docling extra:

```bash
pip install mnemos-os[docling]
```

This installs:
- `docling>=2.5.0` — Main document parsing library
- `docling-core>=2.0.0` — Core parsing utilities
- `pillow>=10.0.0` — Image handling for PDF/multi-format support

If Docling is not installed, the document import endpoints return `501 Not Implemented`.

## Supported Formats

| Format | Extension | Notes |
|--------|-----------|-------|
| PDF | `.pdf` | Full support, including images and tables |
| Word Document | `.docx`, `.doc` | Preserves formatting and structure |
| PowerPoint | `.pptx`, `.ppt` | Converts slides to structured content |
| Excel Spreadsheet | `.xlsx`, `.xls` | Converts sheets to tables |
| Text | `.txt` | Plain text files |
| Markdown | `.md` | Preserves structure and formatting |
| HTML | `.html` | Web content extraction |

## API Endpoints

### Single Document Import

```bash
POST /v1/documents/import
Content-Type: multipart/form-data

file: <document file>
category: "documents"  # Optional, defaults to "documents"
subcategory: "pdfs"     # Optional
```

**Response (200 OK):**
```json
{
  "source_file": "report.pdf",
  "memories_created": 5,
  "memory_ids": ["uuid-1", "uuid-2", ...],
  "chunks_processed": 5,
  "errors": [],
  "metadata": {
    "source_file": "report.pdf",
    "source_type": "PDF",
    "parsed_at": "2026-04-20T10:30:00.000Z",
    "page_count": 12
  },
  "total_text_length": 15240
}
```

### Batch Import

```bash
POST /v1/documents/batch-import
Content-Type: multipart/form-data

files: <multiple document files>
category: "documents"
```

**Response:** Array of import results (one per document)

## Usage Examples

### Python with httpx

```python
import httpx

client = httpx.Client(base_url="http://localhost:5002")

# Single document
with open("research_paper.pdf", "rb") as f:
    response = client.post(
        "/v1/documents/import",
        files={"file": f},
        data={"category": "research", "subcategory": "papers"}
    )
    result = response.json()
    print(f"Created {result['memories_created']} memory records")
    for mem_id in result['memory_ids']:
        print(f"  - {mem_id}")
```

### cURL

```bash
# Single document
curl -X POST http://localhost:5002/v1/documents/import \
  -F "file=@document.pdf" \
  -F "category=documents" \
  -F "subcategory=reports" \
  -H "Authorization: Bearer $TOKEN"

# Batch import
curl -X POST http://localhost:5002/v1/documents/batch-import \
  -F "file=@doc1.pdf" \
  -F "file=@doc2.docx" \
  -F "file=@doc3.xlsx" \
  -F "category=bulk_import" \
  -H "Authorization: Bearer $TOKEN"
```

## How It Works

1. **Document Parsing** — Docling extracts structured content using AI-powered layout analysis
2. **Metadata Extraction** — Automatically captures:
   - Source filename and type
   - Parse timestamp
   - Page count (for PDFs)
   - Section headings and hierarchy
3. **Intelligent Chunking** — Content is split into memory-sized segments (~1500 chars / ~500 tokens) aligned with semantic boundaries (sections, paragraphs)
4. **Memory Creation** — Each chunk becomes a separate memory record with:
   - Content
   - Metadata (source, chunk number, section title, page count)
   - User context (owner_id, namespace)
   - Category/subcategory tags

## Metadata in Created Memories

Each imported document chunk includes:

```json
{
  "id": "memory-uuid",
  "content": "Chunk text content...",
  "category": "documents",
  "subcategory": "pdf",
  "metadata": {
    "source_file": "report.pdf",
    "source_type": "PDF",
    "parsed_at": "2026-04-20T10:30:00Z",
    "page_count": 12,
    "chunk_num": 2,
    "chunk_title": "Methodology"
  },
  "owner_id": "user-uuid",
  "namespace": "default",
  "created": "2026-04-20T10:30:00Z"
}
```

## Error Handling

| Status | Meaning | Example |
|--------|---------|---------|
| **200** | Success | Document parsed, memories created |
| **400** | Bad Request | Empty file, invalid format |
| **401** | Unauthorized | Missing/invalid auth token |
| **413** | Payload Too Large | File exceeds MAX_BODY_BYTES (default 5 MB) |
| **501** | Not Implemented | Docling not installed (`pip install mnemos-os[docling]`) |
| **503** | Service Unavailable | Database not available |

**Example error response:**
```json
{
  "detail": "Document parsing failed: Unsupported file format .xyz"
}
```

## Performance Characteristics

- **PDF (5 pages):** ~2–3 seconds
- **DOCX (50 pages):** ~1–2 seconds
- **XLSX (multiple sheets):** ~1–2 seconds
- **Batch of 10 documents:** ~15–30 seconds (sequential per file, parallel chunks)

Times depend on:
- File size (MB)
- Content complexity (images, tables, formatting)
- System load
- Docling model cache state (first run slower)

## Configuration

No configuration needed — Docling uses sensible defaults. Override via environment variables if needed:

```bash
# Max file size (default 5 MB)
export MAX_BODY_BYTES=10485760  # 10 MB

# Server port (default 5002)
export MNEMOS_PORT=5002

# Docling parser (if multiple available)
# Docling auto-selects best parser for format
```

## Limitations

1. **File size:** Default limit is 5 MB (configurable via MAX_BODY_BYTES)
2. **Languages:** Docling best supports English; other languages may lose some structure
3. **OCR:** Not included by default (requires additional setup)
4. **Tables:** Complex multi-level tables may lose some formatting
5. **Images:** Images are extracted as text descriptions, not stored as binary

## Integration with Other MNEMOS Features

Imported memories automatically integrate with:

- **Semantic Search** — Query imported content with `/v1/memories/search`
- **Full-Text Search** — Find documents by keyword
- **Compression** — On-demand compression of large document chunks
- **DAG Versioning** — Track changes to imported memories
- **RLS (Row-Level Security)** — Imported memories respect user namespace restrictions
- **Audit Logging** — All imports tracked in MNEMOS audit ledger

## Next Steps

After importing documents:

```bash
# Search imported content
curl -X POST http://localhost:5002/v1/memories/search \
  -d '{"query": "key concept", "category": "documents"}' \
  -H "Authorization: Bearer $TOKEN"

# Create memory branch (e.g., for annotations)
curl -X POST http://localhost:5002/v1/memories/{memory_id}/branch \
  -d '{"branch_name": "annotated"}' \
  -H "Authorization: Bearer $TOKEN"

# Update memory with additional context
curl -X PATCH http://localhost:5002/v1/memories/{memory_id} \
  -d '{"metadata": {"custom_tag": "important"}}' \
  -H "Authorization: Bearer $TOKEN"
```

## Troubleshooting

**Q: "Docling not installed" error**
```bash
A: Install with: pip install mnemos-os[docling]
```

**Q: Document parsing fails with "Unsupported format"**
```bash
A: Check file extension is correct. Docling supports: PDF, DOCX, PPTX, XLSX, TXT, MD, HTML
```

**Q: Imports are slow**
```bash
A: Docling models are cached after first run. First run slower (model download). 
   Subsequent imports faster. Check disk space for model cache (~1 GB).
```

**Q: Memory chunking seems off**
```bash
A: Chunking is content-aware (semantic boundaries). Use metadata.chunk_title 
   to understand chunk source. Adjust target_chunk_size in document_import.py (default 1500 chars).
```

## See Also

- [Docling Documentation](https://github.com/DS4SD/docling)
- [Memory API Reference](./API.md#memories)
- [MNEMOS Semantic Search Guide](./SEMANTIC_SEARCH.md)
