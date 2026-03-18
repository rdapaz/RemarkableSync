#!/usr/bin/env python3
"""
Obsidian <-> reMarkable Two-Way Sync

Syncs markdown notes from an Obsidian vault to a reMarkable tablet as PDFs,
and pulls annotated PDFs (with handwriting/scribbles rendered) back into the vault.

Supports reMarkable v6 .rm annotation format (firmware v3+) via rmscene/rmc.

Dependencies (pip):  fpdf2, markdown, watchdog, rmc, rmscene, PyMuPDF, svglib,
                     svgwrite, reportlab
External:           rmapi (https://github.com/ddvk/rmapi/releases)

Usage:
    python sync_remarkable.py              # One-shot sync (both directions)
    python sync_remarkable.py --watch      # Watch for changes and sync continuously
    python sync_remarkable.py --push       # Only push Obsidian -> reMarkable
    python sync_remarkable.py --pull       # Only pull reMarkable -> Obsidian
    python sync_remarkable.py --setup      # First-time setup (register rmapi)

    # Use with a different vault/folder:
    python sync_remarkable.py --vault "D:\Vaults\Work" --folder "/Work"
"""

import subprocess
import json
import hashlib
import os
import shutil
import sys
import re
import argparse
import logging
import tempfile
import zipfile
from pathlib import Path
from datetime import datetime

import markdown
from fpdf import FPDF
from fpdf.enums import XPos, YPos
from rmc import rm_to_svg
from svglib.svglib import svg2rlg
from reportlab.graphics import renderPDF
import fitz  # PyMuPDF

# ─── Configuration ───────────────────────────────────────────────────────────

SCRIPT_DIR = Path(__file__).resolve().parent
RMAPI = str(SCRIPT_DIR / "rmapi.exe")
LOG_FILE = SCRIPT_DIR / "sync.log"

# Defaults — can be overridden via --vault and --folder CLI args
DEFAULT_VAULT_PATH = Path(r"D:\Vaults\Remarkable\remarkable")
DEFAULT_REMARKABLE_FOLDER = "/Obsidian"

# These globals are set in main() based on CLI args
VAULT_PATH: Path = DEFAULT_VAULT_PATH
REMARKABLE_FOLDER: str = DEFAULT_REMARKABLE_FOLDER
SYNC_DIR: Path = DEFAULT_VAULT_PATH.parent / ".sync"
STATE_FILE: Path = SYNC_DIR / "state.json"
PDF_CACHE: Path = SYNC_DIR / "pdfs"
ANNOTATIONS_DIR: Path = DEFAULT_VAULT_PATH / "_annotations"


def configure(vault_path: Path, remarkable_folder: str):
    """Set all derived paths from the vault path and reMarkable folder."""
    global VAULT_PATH, REMARKABLE_FOLDER, SYNC_DIR, STATE_FILE, PDF_CACHE, ANNOTATIONS_DIR
    VAULT_PATH = vault_path
    REMARKABLE_FOLDER = remarkable_folder
    SYNC_DIR = VAULT_PATH.parent / ".sync"
    STATE_FILE = SYNC_DIR / "state.json"
    PDF_CACHE = SYNC_DIR / "pdfs"
    ANNOTATIONS_DIR = VAULT_PATH / "_annotations"

# ─── Logging ─────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("remarkable-sync")

# ─── State Management ────────────────────────────────────────────────────────


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"pushed": {}, "pulled": {}, "last_sync": None}


def save_state(state: dict):
    SYNC_DIR.mkdir(parents=True, exist_ok=True)
    state["last_sync"] = datetime.now().isoformat()
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def file_hash(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


# ─── Dependency Check ────────────────────────────────────────────────────────


def check_rmapi():
    rmapi_path = Path(RMAPI)
    if not rmapi_path.exists():
        log.error(f"rmapi.exe not found at {rmapi_path}")
        log.error("Download from: https://github.com/ddvk/rmapi/releases")
        sys.exit(1)
    log.info("rmapi.exe found.")


# ─── rmapi Helpers ───────────────────────────────────────────────────────────


def _rmapi_env() -> dict:
    """Environment for rmapi subprocesses — prevents MSYS/Git Bash path mangling."""
    env = os.environ.copy()
    env["MSYS_NO_PATHCONV"] = "1"
    env["MSYS2_ARG_CONV_EXCL"] = "*"
    return env


def rmapi_run(args: list[str], check=True) -> subprocess.CompletedProcess:
    cmd = [RMAPI] + args
    log.debug(f"Running: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60,
                            env=_rmapi_env())
    if check and result.returncode != 0:
        log.error(f"rmapi error: {result.stderr.strip()}")
    return result


def rmapi_ls(folder: str) -> list[dict]:
    result = rmapi_run(["ls", folder], check=False)
    if result.returncode != 0:
        return []
    items = []
    for line in result.stdout.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        match = re.match(r"\[([df])\]\s+(.+)", line)
        if match:
            item_type = "folder" if match.group(1) == "d" else "file"
            items.append({"name": match.group(2).strip(), "type": item_type})
    return items


def rmapi_mkdir(folder: str):
    rmapi_run(["mkdir", folder], check=False)


def rmapi_upload(local_path: Path, remote_folder: str) -> bool:
    """Upload a file to reMarkable. Runs rmapi from a temp dir with just the
    filename so the document name on the tablet is clean."""
    with tempfile.TemporaryDirectory() as tmp:
        clean_path = Path(tmp) / local_path.name
        shutil.copy2(local_path, clean_path)
        # Use just the filename as the argument, with cwd set to the temp dir
        cmd = [RMAPI, "put", local_path.name, remote_folder]
        log.debug(f"Running: {' '.join(cmd)}")
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=60,
            env=_rmapi_env(), cwd=tmp,
        )
        if result.returncode != 0:
            log.error(f"rmapi error: {result.stderr.strip()}")
    return result.returncode == 0


def rmapi_download(remote_path: str, local_dir: Path) -> bool:
    """Download raw .rmdoc using 'get' (not 'geta') so we can render
    annotations ourselves — rmapi's built-in renderer fails on v6 .rm files."""
    local_dir.mkdir(parents=True, exist_ok=True)
    env = _rmapi_env()
    result = subprocess.run(
        [RMAPI, "get", remote_path],
        capture_output=True, text=True, timeout=120,
        cwd=str(local_dir), env=env,
    )
    return result.returncode == 0


def render_rmdoc_to_pdf(rmdoc_path: Path, output_pdf: Path) -> bool:
    """Render an .rmdoc archive to PDF with annotations overlaid."""
    try:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            # Extract the rmdoc (zip) contents
            with zipfile.ZipFile(rmdoc_path, "r") as z:
                z.extractall(tmp_path)

            rm_files = sorted(tmp_path.rglob("*.rm"))
            pdf_files = list(tmp_path.rglob("*.pdf"))

            has_base_pdf = bool(pdf_files)

            if not rm_files and not has_base_pdf:
                log.warning(f"No content in {rmdoc_path.name}")
                return False

            if has_base_pdf and not rm_files:
                # No annotations — just copy the original PDF
                shutil.copy2(pdf_files[0], output_pdf)
                return True

            # Render each .rm page to SVG -> PDF
            page_pdfs = []
            for i, rm_file in enumerate(rm_files):
                svg_path = tmp_path / f"ann_{i}.svg"
                ann_pdf_path = tmp_path / f"ann_{i}.pdf"

                rm_to_svg(str(rm_file), str(svg_path))

                drawing = svg2rlg(str(svg_path))
                if drawing is None:
                    continue
                renderPDF.drawToFile(drawing, str(ann_pdf_path))
                page_pdfs.append(ann_pdf_path)

            if has_base_pdf:
                # Overlay annotations on existing PDF
                orig = fitz.open(str(pdf_files[0]))
                for i, ann_pdf_path in enumerate(page_pdfs):
                    page_idx = min(i, len(orig) - 1)
                    page = orig[page_idx]
                    ann_doc = fitz.open(str(ann_pdf_path))
                    page.show_pdf_page(page.rect, ann_doc, 0, overlay=True)
                    ann_doc.close()
                orig.save(str(output_pdf))
                orig.close()
            else:
                # Handwritten-only notebook — merge annotation pages into
                # a new PDF (reMarkable page size: 1404x1872 px @ 226 DPI)
                RM_WIDTH_PT = 1404 * 72 / 226   # ~447.6 pt
                RM_HEIGHT_PT = 1872 * 72 / 226   # ~596.7 pt
                doc = fitz.open()
                for ann_pdf_path in page_pdfs:
                    page = doc.new_page(
                        width=RM_WIDTH_PT, height=RM_HEIGHT_PT
                    )
                    ann_doc = fitz.open(str(ann_pdf_path))
                    page.show_pdf_page(page.rect, ann_doc, 0, overlay=True)
                    ann_doc.close()
                if len(doc) == 0:
                    log.warning(f"No renderable pages in {rmdoc_path.name}")
                    doc.close()
                    return False
                doc.save(str(output_pdf))
                doc.close()

            return True

    except Exception as e:
        log.error(f"Failed to render {rmdoc_path.name}: {e}")
        return False


# ─── Markdown to PDF (pure Python) ──────────────────────────────────────────


class RemarkablePDF(FPDF):
    """PDF optimized for reMarkable 2 e-ink display (1872x1404 px)."""

    def __init__(self, title: str = ""):
        super().__init__()
        self._doc_title = title
        self.set_auto_page_break(auto=True, margin=20)

    def header(self):
        if self._doc_title and self.page_no() == 1:
            self.set_font("Helvetica", "B", 20)
            self.cell(0, 15, self._doc_title, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            self.ln(5)

    def footer(self):
        self.set_y(-15)
        self.set_font("Helvetica", "I", 8)
        self.set_text_color(128, 128, 128)
        self.cell(0, 10, f"Page {self.page_no()}", align="C")
        self.set_text_color(0, 0, 0)


def md_to_pdf(md_path: Path, pdf_path: Path) -> bool:
    """Convert a markdown file to a PDF optimized for reMarkable e-ink."""
    pdf_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        md_text = md_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        md_text = md_path.read_text(encoding="latin-1")

    log.info(f"Converting: {md_path.name} -> PDF")

    try:
        pdf = RemarkablePDF(title=md_path.stem)
        pdf.add_page()
        pdf.set_margins(15, 15, 15)

        # Convert markdown to HTML, then use fpdf2's write_html
        html = markdown.markdown(
            md_text,
            extensions=["tables", "fenced_code", "codehilite", "nl2br"],
        )

        # Set base font for body text — large for e-ink readability
        pdf.set_font("Helvetica", size=13)
        pdf.write_html(html)

        pdf.output(str(pdf_path))
        return True
    except Exception as e:
        log.error(f"PDF conversion failed for {md_path.name}: {e}")
        return False


# ─── Sync: Push (Obsidian -> reMarkable) ─────────────────────────────────────


def push_sync(state: dict) -> int:
    pushed_count = 0
    rmapi_mkdir(REMARKABLE_FOLDER)

    md_files = list(VAULT_PATH.rglob("*.md"))

    for md_path in md_files:
        # Skip the _annotations folder
        if ANNOTATIONS_DIR.name in md_path.parts:
            continue

        rel_path = md_path.relative_to(VAULT_PATH)
        current_hash = file_hash(md_path)
        state_key = str(rel_path)

        prev_hash = state.get("pushed", {}).get(state_key, {}).get("hash")
        if prev_hash == current_hash:
            log.debug(f"Unchanged: {rel_path}")
            continue

        pdf_name = md_path.stem + ".pdf"
        pdf_path = PDF_CACHE / pdf_name

        if not md_to_pdf(md_path, pdf_path):
            continue

        log.info(f"Uploading: {pdf_name} -> {REMARKABLE_FOLDER}")
        if rmapi_upload(pdf_path, REMARKABLE_FOLDER):
            state.setdefault("pushed", {})[state_key] = {
                "hash": current_hash,
                "pdf_name": pdf_name,
                "uploaded_at": datetime.now().isoformat(),
            }
            pushed_count += 1
            log.info(f"  Uploaded successfully.")
        else:
            log.error(f"  Upload failed for {pdf_name}")

    return pushed_count


# ─── Sync: Pull (reMarkable -> Obsidian) ─────────────────────────────────────


def pull_sync(state: dict) -> int:
    pulled_count = 0
    ANNOTATIONS_DIR.mkdir(parents=True, exist_ok=True)

    remote_items = rmapi_ls(REMARKABLE_FOLDER)

    pushed_pdfs = {
        v["pdf_name"] for v in state.get("pushed", {}).values()
        if "pdf_name" in v
    }

    for item in remote_items:
        if item["type"] != "file":
            continue

        name = item["name"]
        remote_path = f"{REMARKABLE_FOLDER}/{name}"
        final_pdf = ANNOTATIONS_DIR / f"{name}.pdf"

        log.info(f"Downloading: {remote_path}")
        if not rmapi_download(remote_path, ANNOTATIONS_DIR):
            log.warning(f"  Download failed for {name}")
            continue

        # rmapi downloads as .rmdoc or .zip — find whatever it created
        downloaded = list(ANNOTATIONS_DIR.glob(f"{name}.*"))
        if not downloaded:
            log.warning(f"  No file found after download for {name}")
            continue

        dl_file = downloaded[0]
        log.info(f"  Downloaded: {dl_file.name}")

        # Extract the PDF from .rmdoc/.zip archives
        if dl_file.suffix in (".rmdoc", ".zip"):
            if render_rmdoc_to_pdf(dl_file, final_pdf):
                log.info(f"  Extracted: {final_pdf.name}")
                # Clean up the archive
                dl_file.unlink(missing_ok=True)
            else:
                log.warning(f"  Could not render PDF from {dl_file.name}")
                continue
        elif dl_file.suffix == ".pdf":
            # Already a PDF, just rename if needed
            if dl_file != final_pdf:
                shutil.move(str(dl_file), str(final_pdf))

        if final_pdf.exists():
            state.setdefault("pulled", {})[name] = {
                "local_path": str(final_pdf),
                "pulled_at": datetime.now().isoformat(),
            }
            pulled_count += 1

            # Create a stub .md note for NEW files from reMarkable
            if f"{name}.pdf" not in pushed_pdfs:
                stub_path = VAULT_PATH / f"{name}.md"
                if not stub_path.exists():
                    stub_path.write_text(
                        f"# {name}\n\n"
                        f"*Imported from reMarkable on "
                        f"{datetime.now().strftime('%Y-%m-%d %H:%M')}*\n\n"
                        f"![[_annotations/{name}.pdf]]\n",
                        encoding="utf-8",
                    )
                    log.info(f"  Created stub note: {stub_path.name}")

    return pulled_count


# ─── Setup ────────────────────────────────────────────────────────────────────


def setup():
    check_rmapi()

    SYNC_DIR.mkdir(parents=True, exist_ok=True)
    PDF_CACHE.mkdir(parents=True, exist_ok=True)
    ANNOTATIONS_DIR.mkdir(parents=True, exist_ok=True)

    print("\n" + "=" * 60)
    print("  reMarkable Sync Setup")
    print("=" * 60)
    print()
    print("Step 1: Register with your reMarkable account")
    print("   Go to: https://my.remarkable.com/device/browser/connect")
    print("   Enter the code that rmapi gives you.")
    print()
    print("Running rmapi for first-time registration...")
    print()

    subprocess.run([RMAPI, "ls", "/"], timeout=120)

    print()
    print("Step 2: Creating reMarkable folder...")
    rmapi_mkdir(REMARKABLE_FOLDER)
    print(f"   Created '{REMARKABLE_FOLDER}' on reMarkable")
    print()
    print("Setup complete!")
    print("=" * 60)


# ─── Watch Mode ───────────────────────────────────────────────────────────────


def watch_and_sync():
    try:
        from watchdog.observers import Observer
        from watchdog.events import FileSystemEventHandler
    except ImportError:
        log.error("watchdog not installed. Run: pip install watchdog")
        log.info("Falling back to polling every 30 seconds...")
        poll_and_sync()
        return

    class VaultHandler(FileSystemEventHandler):
        def __init__(self):
            self.pending = False

        def on_modified(self, event):
            if event.src_path.endswith(".md"):
                self.pending = True

        def on_created(self, event):
            if event.src_path.endswith(".md"):
                self.pending = True

    handler = VaultHandler()
    observer = Observer()
    observer.schedule(handler, str(VAULT_PATH), recursive=True)
    observer.start()

    log.info(f"Watching {VAULT_PATH} for changes... (Ctrl+C to stop)")
    log.info("Pulling from reMarkable every 5 minutes.")

    import time
    last_pull = 0
    try:
        while True:
            time.sleep(5)
            if handler.pending:
                handler.pending = False
                log.info("Changes detected, syncing...")
                state = load_state()
                pushed = push_sync(state)
                save_state(state)
                if pushed:
                    log.info(f"Pushed {pushed} file(s)")
            now = time.time()
            if now - last_pull > 300:
                state = load_state()
                pulled = pull_sync(state)
                save_state(state)
                if pulled:
                    log.info(f"Pulled {pulled} file(s)")
                last_pull = now
    except KeyboardInterrupt:
        observer.stop()
        log.info("Stopped watching.")
    observer.join()


def poll_and_sync():
    import time
    log.info(f"Polling {VAULT_PATH} every 30 seconds... (Ctrl+C to stop)")
    last_pull = 0
    try:
        while True:
            state = load_state()
            pushed = push_sync(state)
            now = time.time()
            if now - last_pull > 300:
                pulled = pull_sync(state)
                last_pull = now
            else:
                pulled = 0
            save_state(state)
            if pushed or pulled:
                log.info(f"Synced: {pushed} pushed, {pulled} pulled")
            time.sleep(30)
    except KeyboardInterrupt:
        log.info("Stopped polling.")


# ─── Main ─────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="Obsidian <-> reMarkable Sync")
    parser.add_argument("--vault", type=str, default=None,
                        help=f"Path to Obsidian vault (default: {DEFAULT_VAULT_PATH})")
    parser.add_argument("--folder", type=str, default=None,
                        help=f"reMarkable folder to sync with (default: {DEFAULT_REMARKABLE_FOLDER})")
    parser.add_argument("--setup", action="store_true", help="First-time setup")
    parser.add_argument("--watch", action="store_true", help="Watch and sync continuously")
    parser.add_argument("--push", action="store_true", help="Only push to reMarkable")
    parser.add_argument("--pull", action="store_true", help="Only pull from reMarkable")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    args = parser.parse_args()

    # Apply vault/folder overrides
    vault = Path(args.vault) if args.vault else DEFAULT_VAULT_PATH
    folder = args.folder if args.folder else DEFAULT_REMARKABLE_FOLDER
    configure(vault, folder)

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    log.info(f"Vault: {VAULT_PATH}")
    log.info(f"reMarkable folder: {REMARKABLE_FOLDER}")

    if args.setup:
        setup()
        return

    check_rmapi()

    SYNC_DIR.mkdir(parents=True, exist_ok=True)
    PDF_CACHE.mkdir(parents=True, exist_ok=True)

    if args.watch:
        watch_and_sync()
        return

    state = load_state()
    do_push = not args.pull
    do_pull = not args.push

    if do_push:
        log.info("── Push: Obsidian -> reMarkable ──")
        pushed = push_sync(state)
        log.info(f"Pushed {pushed} file(s)")

    if do_pull:
        log.info("── Pull: reMarkable -> Obsidian ──")
        pulled = pull_sync(state)
        log.info(f"Pulled {pulled} file(s)")

    save_state(state)
    log.info("Sync complete.")


if __name__ == "__main__":
    main()
