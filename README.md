# RemarkableSync

Two-way sync between an [Obsidian](https://obsidian.md/) vault and a [reMarkable 2](https://remarkable.com/) tablet.

- **Obsidian → reMarkable**: Markdown notes are converted to e-ink-optimized PDFs and uploaded to a configurable notebook on your tablet.
- **reMarkable → Obsidian**: Annotated PDFs (with your handwriting/highlights) are downloaded back into the vault's `_annotations/` folder.

## Prerequisites

- Python 3.10+
- A reMarkable account with cloud sync enabled
- An Obsidian vault (can be a dedicated vault or a subfolder of an existing one)

## Setup

### 1. Clone and install dependencies

```bash
git clone <this-repo-url>
cd RemarkableSync
pip install -r requirements.txt
```

### 2. Download rmapi

Download the `rmapi` binary for your platform from [ddvk/rmapi releases](https://github.com/ddvk/rmapi/releases) and place `rmapi.exe` in this project folder.

On Windows with PowerShell:

```powershell
Invoke-WebRequest -Uri "https://github.com/ddvk/rmapi/releases/download/v0.0.32/rmapi-win64.zip" -OutFile rmapi.zip
Expand-Archive rmapi.zip -DestinationPath .
Remove-Item rmapi.zip
```

### 3. Configure

Edit the top of `sync_remarkable.py` to match your setup:

```python
VAULT_PATH = Path(r"D:\Vaults\Remarkable\remarkable")  # Your Obsidian vault path
REMARKABLE_FOLDER = "/Obsidian"                         # Folder on your reMarkable
```

### 4. Authenticate with reMarkable cloud

```bash
python sync_remarkable.py --setup
```

This will:
1. Launch `rmapi` which asks for a one-time code
2. Open https://my.remarkable.com/device/browser/connect in your browser
3. Enter the code shown by `rmapi`
4. Create the target folder on your reMarkable

You only need to do this once. The auth token is stored in `~/.rmapi`.

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

## Automated sync (Windows Task Scheduler)

To run the sync automatically every 15 minutes in the background:

```powershell
schtasks /Create /TN "RemarkableSync" `
  /TR "\"C:\path\to\python.exe\" \"D:\Work\Projects\RemarkableSync\sync_remarkable.py\"" `
  /SC MINUTE /MO 15 /F
```

Replace `C:\path\to\python.exe` with the output of `where python`.

To check the task status:

```powershell
schtasks /Query /TN "RemarkableSync"
```

To remove the task:

```powershell
schtasks /Delete /TN "RemarkableSync" /F
```

## How it works

```
Obsidian Vault (.md files)
        │
        ▼
   Markdown → PDF          (fpdf2 + markdown)
        │
        ▼
   Upload to reMarkable    (rmapi put)
        │
        ▼
   /Obsidian folder on tablet
        │
        ▼
   Read & annotate on tablet
        │
        ▼
   Download annotations    (rmapi geta)
        │
        ▼
   _annotations/ in vault  (viewable in Obsidian)
```

### Sync state

The script tracks file hashes in `<vault>/../.sync/state.json` to avoid re-uploading unchanged files. Cached PDFs are stored in `<vault>/../.sync/pdfs/`.

### Folder structure

```
D:\Vaults\Remarkable\
├── remarkable/              ← Obsidian vault
│   ├── Note A.md
│   ├── Note B.md
│   └── _annotations/       ← Annotated PDFs pulled from reMarkable
│       ├── Note A.pdf
│       └── Note B.pdf
└── .sync/                   ← Sync metadata (outside vault)
    ├── state.json
    └── pdfs/                ← Cached PDFs
```

## Limitations

- **Annotations are PDF-only**: reMarkable annotations (handwriting, highlights) come back as PDFs, not as editable markdown. They're embedded in Obsidian via `![[_annotations/filename.pdf]]`.
- **No conflict resolution**: If you modify a note in Obsidian *and* annotate it on reMarkable between syncs, the Obsidian version will overwrite the reMarkable version (the annotated PDF is preserved locally).
- **reMarkable cloud required**: This uses the reMarkable cloud API via `rmapi`. It does not work with USB/local transfer.

## License

MIT
