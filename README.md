# RemarkableSync

Two-way sync between an [Obsidian](https://obsidian.md/) vault and a [reMarkable 2](https://remarkable.com/) tablet.

- **Obsidian → reMarkable**: Markdown notes are converted to e-ink-optimized PDFs and uploaded to a configurable notebook on your tablet.
- **reMarkable → Obsidian**: Annotated PDFs (with your handwriting/scribbles rendered) are downloaded back into the vault's `_annotations/` folder, viewable directly in Obsidian.

Annotation rendering supports the **reMarkable v6 `.rm` format** (software version 3+) using [rmscene](https://github.com/ricklupton/rmscene) and [rmc](https://github.com/ricklupton/rmc). Both annotated documents and pure handwritten notebooks are supported.

## Prerequisites

- **Python 3.10+**
- A **reMarkable account** with cloud sync enabled
- An **Obsidian vault** (can be a dedicated vault or a subfolder of an existing one)

## Quick Start

### 1. Clone and install dependencies

```bash
git clone https://github.com/yourusername/RemarkableSync.git
cd RemarkableSync
python -m venv .venv
.venv\Scripts\activate       # Windows
# source .venv/bin/activate  # macOS/Linux
pip install -r requirements.txt
```

### 2. Download rmapi

Download the `rmapi` binary for your platform from [ddvk/rmapi releases](https://github.com/ddvk/rmapi/releases) and place it in this project folder.

**Windows (PowerShell):**

```powershell
Invoke-WebRequest -Uri "https://github.com/ddvk/rmapi/releases/download/v0.0.32/rmapi-win64.zip" -OutFile rmapi.zip
Expand-Archive rmapi.zip -DestinationPath .
Remove-Item rmapi.zip
```

**macOS/Linux:**

```bash
# Download the appropriate binary from the releases page and make it executable
chmod +x rmapi
```

### 3. Configure

Edit the configuration section at the top of `sync_remarkable.py`:

```python
VAULT_PATH = Path(r"D:\Vaults\Remarkable\remarkable")  # Your Obsidian vault path
REMARKABLE_FOLDER = "/Obsidian"                         # Folder on your reMarkable
```

| Setting | Description |
|---------|-------------|
| `VAULT_PATH` | Absolute path to your Obsidian vault (the folder containing your `.md` files) |
| `REMARKABLE_FOLDER` | Path on the reMarkable where documents are synced. Creates the folder if it doesn't exist. Use `/` for root, or `/My Folder/Subfolder` for nested paths. |

### 4. Authenticate with reMarkable Cloud

```bash
python sync_remarkable.py --setup
```

This will:
1. Launch `rmapi` which displays a one-time code
2. Open https://my.remarkable.com/device/browser/connect in your browser
3. Enter the code shown by `rmapi`
4. Create the target folder on your reMarkable

You only need to do this **once**. The auth token is stored in `~/.rmapi`.

## Usage

### One-shot sync (both directions)

```bash
python sync_remarkable.py
```

### Push only (Obsidian → reMarkable)

```bash
python sync_remarkable.py --push
```

### Pull only (reMarkable → Obsidian)

```bash
python sync_remarkable.py --pull
```

### Watch mode (continuous)

```bash
python sync_remarkable.py --watch
```

Watches the vault for file changes and pushes immediately. Pulls from reMarkable every 5 minutes.

### Verbose logging

```bash
python sync_remarkable.py -v
```

## Automated Sync (Windows Task Scheduler)

A scheduled task runs the sync automatically every 15 minutes in the background:

```powershell
# Create the task (replace python path as needed)
schtasks /Create /TN "RemarkableSync" `
  /TR "\"C:\path\to\python.exe\" \"D:\Work\Projects\RemarkableSync\sync_remarkable.py\"" `
  /SC MINUTE /MO 15 /F
```

> **Tip:** Find your Python path with `where python` or `(Get-Command python).Source`

```powershell
# Check task status
schtasks /Query /TN "RemarkableSync"

# Remove the task
schtasks /Delete /TN "RemarkableSync" /F
```

## How It Works

```
Obsidian Vault (.md files)
        │
        ▼
   Markdown → PDF               (fpdf2 + markdown)
        │
        ▼
   Upload to reMarkable          (rmapi put)
        │
        ▼
   /Obsidian folder on tablet
        │
        ▼
   Read & annotate on tablet
        │
        ▼
   Download .rmdoc               (rmapi get)
        │
        ▼
   Render v6 annotations         (rmscene + rmc → SVG → PDF overlay)
        │
        ▼
   _annotations/ in vault        (viewable in Obsidian)
```

### Annotation Rendering Pipeline

reMarkable firmware v3+ uses the **v6 `.rm` format** for annotations. The built-in `rmapi geta` command cannot render these, so this tool has its own pipeline:

1. **Download** the raw `.rmdoc` archive (a zip containing the base PDF + `.rm` stroke files)
2. **Parse** the `.rm` files using [rmscene](https://github.com/ricklupton/rmscene)
3. **Render** strokes to SVG using [rmc](https://github.com/ricklupton/rmc)
4. **Convert** SVG to PDF and **overlay** on the original document using [PyMuPDF](https://pymupdf.readthedocs.io/) and [svglib](https://github.com/deeplook/svglib)

For **pure handwritten notebooks** (no base PDF), a new PDF is created at the reMarkable's native page dimensions (1404×1872 px).

### Sync State

The script tracks file hashes in `<vault>/../.sync/state.json` to avoid re-uploading unchanged files. Cached PDFs are stored in `<vault>/../.sync/pdfs/`.

### Folder Structure

```
D:\Vaults\Remarkable\
├── remarkable/              ← Obsidian vault
│   ├── Note A.md
│   ├── Note B.md
│   └── _annotations/       ← Annotated PDFs pulled from reMarkable
│       ├── Note A.pdf      ← PDF with your scribbles rendered
│       ├── Notebook.pdf    ← Pure handwritten notebook from reMarkable
│       └── Note B.pdf
└── .sync/                   ← Sync metadata (outside vault)
    ├── state.json
    └── pdfs/                ← Cached PDFs for upload
```

### What Syncs Back from reMarkable?

| Scenario | Result |
|----------|--------|
| Annotated PDF (scribbles on a pushed note) | PDF with annotations overlaid, saved to `_annotations/` |
| Pure handwritten notebook | New PDF created from strokes, stub `.md` note created in vault |
| Highlighted text | Highlights rendered as colored overlays on the PDF |
| Typed text (reMarkable keyboard) | Rendered in the PDF via rmscene's text support |

## Limitations

- **Annotations are PDF-only**: reMarkable annotations (handwriting, highlights) come back as PDFs, not as editable markdown. They're embedded in Obsidian via `![[_annotations/filename.pdf]]`.
- **No conflict resolution**: If you modify a note in Obsidian *and* annotate it on reMarkable between syncs, the Obsidian version will overwrite the reMarkable version (the annotated PDF is preserved locally).
- **reMarkable Cloud required**: This uses the reMarkable Cloud API via `rmapi`. It does not work with USB/local transfer.
- **rmscene warnings**: You may see "Some data has not been read" warnings — these are harmless and mean the reMarkable firmware wrote newer metadata fields that `rmscene` doesn't fully parse yet. Strokes still render correctly.

## Dependencies

| Package | Purpose |
|---------|---------|
| [fpdf2](https://pypi.org/project/fpdf2/) | Markdown → PDF conversion |
| [markdown](https://pypi.org/project/Markdown/) | Markdown parsing |
| [watchdog](https://pypi.org/project/watchdog/) | File system monitoring (watch mode) |
| [rmc](https://github.com/ricklupton/rmc) | reMarkable `.rm` v6 → SVG/PDF conversion |
| [rmscene](https://github.com/ricklupton/rmscene) | Parse reMarkable v6 `.rm` files |
| [PyMuPDF](https://pymupdf.readthedocs.io/) | PDF manipulation and overlay |
| [svglib](https://github.com/deeplook/svglib) | SVG → ReportLab drawing conversion |
| [svgwrite](https://pypi.org/project/svgwrite/) | SVG generation (rmc dependency) |
| [reportlab](https://pypi.org/project/reportlab/) | PDF rendering from SVG |
| [rmapi](https://github.com/ddvk/rmapi) | reMarkable Cloud API CLI (external binary) |

## License

MIT — see [LICENSE](LICENSE).
