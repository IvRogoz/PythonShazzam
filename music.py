from __future__ import annotations
import argparse, os, re, sys, time, shutil, asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple, List, Dict, Any

try:
    from termcolor import colored
except Exception:
    print("ERROR: 'termcolor' is required. Install with: pip install termcolor", file=sys.stderr)
    sys.exit(1)

try:
    import colorama
    colorama.just_fix_windows_console()
except Exception:
    if os.name == "nt":
        print("ERROR: 'colorama' is required on Windows. Install with: pip install colorama", file=sys.stderr)
        sys.exit(1)

import requests
from mutagen.id3 import ID3, APIC, TIT2, TPE1, TALB
from mutagen.mp3 import MP3
from shazamio import Shazam

@dataclass
class TrackInfo:
    artist: str
    title: str
    album: Optional[str] = None
    cover_url: Optional[str] = None

@dataclass
class FileResult:
    src_name: str
    dest_name: str
    identified: bool = False
    renamed: bool = False
    tags_ok: bool = False
    art_ok: bool = False
    skipped: bool = False
    error: Optional[str] = None

INVALID_WIN_RE = re.compile(r'[<>:"/\\|?*]')
MULTISPACE_RE = re.compile(r"\s+")

SYMBOL_OK = "✓"
SYMBOL_ERR = "✗"
SYMBOL_WARN = "⚠"

def sanitize_filename(name: str) -> str:
    name = INVALID_WIN_RE.sub(" ", name)
    name = MULTISPACE_RE.sub(" ", name).strip().rstrip(" .")
    name = re.sub(r"\s*-\s*", " - ", name)
    name = MULTISPACE_RE.sub(" ", name).strip().rstrip(" .")
    return name or "untitled"

def make_target_filename(artist: str, title: str, album: Optional[str] = None) -> str:
    if album:
        base = f"{artist} - {title} - {album}.mp3"
    else:
        base = f"{artist} - {title}.mp3"
    return sanitize_filename(base)

def safe_rename(src: Path, target_name: str) -> Path:
    dest = src.with_name(target_name)
    if dest == src:
        return src
    if not dest.exists():
        src.rename(dest); return dest
    stem, ext = os.path.splitext(target_name)
    i = 1
    while True:
        candidate = src.with_name(f"{stem} ({i}){ext}")
        if not candidate.exists():
            src.rename(candidate); return candidate
        i += 1

def sniff_image_mime(data: bytes) -> str:
    if data.startswith(b"\xff\xd8\xff"): return "image/jpeg"
    if data.startswith(b"\x89PNG"): return "image/png"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"): return "image/gif"
    return "application/octet-stream"

def ensure_id3(audio: MP3) -> ID3:
    if audio.tags is None:
        audio.add_tags()
    return audio.tags

def update_tags(file_path: Path, track: TrackInfo) -> Tuple[bool, bool]:
    audio = MP3(file_path)
    tags = ensure_id3(audio)
    tags.delall("TIT2"); tags.delall("TPE1"); tags.delall("TALB"); tags.delall("APIC")
    tags.add(TIT2(encoding=3, text=track.title))
    tags.add(TPE1(encoding=3, text=track.artist))
    if track.album:
        tags.add(TALB(encoding=3, text=track.album))

    art_ok = False
    if track.cover_url:
        try:
            r = requests.get(track.cover_url, timeout=15)
            r.raise_for_status()
            data = r.content
            tags.add(APIC(encoding=3, mime=sniff_image_mime(data), type=3, desc="Cover", data=data))
            art_ok = True
        except Exception:
            art_ok = False

    audio.save(v2_version=3)
    return True, art_ok

def extract_trackinfo_from_shazam(payload: dict) -> Optional[TrackInfo]:
    try:
        track = payload.get("track") or {}
        title = track.get("title")
        artist = track.get("subtitle")
        if not title or not artist:
            return None

        album = None
        for sec in track.get("sections", []) or []:
            if not isinstance(sec, dict):
                continue
            meta = sec.get("metadata")
            if not meta:
                continue
            for md in meta:
                if not isinstance(md, dict):
                    continue
                if (md.get("title") or "").strip().lower() == "album":
                    album = md.get("text") or album

        images = track.get("images", {}) or {}
        cover = images.get("coverarthq") or images.get("coverart") or images.get("background") or None

        artist = MULTISPACE_RE.sub(" ", artist).strip()
        title = MULTISPACE_RE.sub(" ", title).strip()
        if album:
            album = MULTISPACE_RE.sub(" ", album).strip()

        return TrackInfo(artist=artist, title=title, album=album, cover_url=cover)
    except Exception:
        return None

async def shazam_identify_file(shazam: Shazam, file_path: Path, timeout: float = 40.0) -> Optional[TrackInfo]:
    try:
        payload = await asyncio.wait_for(shazam.recognize(str(file_path)), timeout=timeout)
        if not payload:
            return None
        return extract_trackinfo_from_shazam(payload) or None
    except asyncio.TimeoutError:
        return None
    except Exception:
        return None

def paint_status(s: str) -> str:
    u = s.upper()
    if "ERROR" in u:
        return colored(s, "red", attrs=["bold"])
    if "SKIP" in u:
        return colored(s, "yellow", attrs=["bold"])
    if any(x in u for x in ("ID", "RENAME", "LOOKUP", "TAGS", "ART")):
        return colored(s, "cyan")
    if "DONE" in u:
        return colored(s, "green", attrs=["bold"])
    return colored(s, "white")

def paint_tag(tag: str) -> str:
    m = tag.upper()
    if m == "REN":
        return colored("REN", "cyan")
    if m == "TAG":
        return colored("TAG", "green")
    if m == "ART":
        return colored("ART", "magenta")
    if m == "SKIP":
        return colored("SKIP", "yellow")
    if m == "ERR":
        return colored("ERR", "red", attrs=["bold"])
    return tag

def term_width(default: int = 100) -> int:
    try:
        return shutil.get_terminal_size().columns
    except Exception:
        return default

def trunc(s: str, maxlen: int) -> str:
    return s if len(s) <= maxlen else s[:maxlen-1] + "…"

def render_bar(frac: float, width: int) -> str:
    filled = int(frac * width)
    return "█" * filled + "░" * (width - filled)

def print_progress(current: int, total: int, start_time: float, label: str, status: str, width_min: int = 70):
    cols = max(term_width(), width_min)
    frac = current / total if total else 1.0
    elapsed = time.time() - start_time
    eta = (elapsed / current) * (total - current) if current else 0

    bar_width = max(20, min(40, cols // 3))
    bar = render_bar(frac, bar_width)

    left = f"[{bar}] {current:>4}/{total:<4} ({frac*100:5.1f}%) ETA {int(eta):4d}s"
    remain = cols - len(left) - 5
    status_space = max(12, min(24, remain // 3))
    label_space = max(10, remain - status_space)
    label = trunc(label, label_space)
    status_col = trunc(paint_status(status), status_space)

    line = f"\r{left} | {status_col:<{status_space}} | {label:<{label_space}}"
    sys.stdout.write(line)
    sys.stdout.flush()

def gather_files(root: Path, recurse: bool) -> List[Path]:
    return list(root.rglob("*.mp3") if recurse else root.glob("*.mp3"))

async def process_files_async(files: List[Path], args) -> Dict[str, Any]:
    start = time.time()
    results: List[FileResult] = []
    processed = errors = unidentified = 0

    shazam = Shazam()

    for i, orig_path in enumerate(files, 1):
        result = FileResult(src_name=orig_path.name, dest_name=orig_path.name)
        path = orig_path
        status = "ID…"
        if not args.verbose:
            print_progress(i, len(files), start, label=orig_path.name, status=status)

        try:
            ti = await shazam_identify_file(shazam, path, timeout=args.timeout)
            if not ti:
                result.skipped = True
                unidentified += 1
                status = "SKIP (no ID)"
                if not args.verbose:
                    print_progress(i, len(files), start, label=orig_path.name, status=status)
                else:
                    print(colored(f"[SKIP] {path.name}: Not identified by Shazam.", "yellow", attrs=["bold"]))
                results.append(result)
                continue
            result.identified = True

            if args.verbose:
                album_msg = ti.album if ti.album else colored("unknown", "yellow")
                cov = colored("yes", "magenta") if ti.cover_url else colored("no", "yellow")
                print(colored(f"[ID]   {path.name} → {ti.artist} — {ti.title}", "cyan"))
                print(f"[META] album={album_msg!s} cover={cov}")

            status = "RENAME…"
            if not args.verbose:
                print_progress(i, len(files), start, label=orig_path.name, status=status)
            new_name = make_target_filename(ti.artist, ti.title, ti.album)
            if new_name != path.name and not args.dry_run:
                path = safe_rename(path, new_name)
                result.dest_name = path.name
                result.renamed = True
                if args.verbose:
                    print(colored(f"[NAME] {orig_path.name} → {path.name}", "cyan"))

            status = "TAGS/ART…"
            if not args.verbose:
                print_progress(i, len(files), start, label=path.name, status=status)
            if not args.dry_run:
                ok, art_ok = update_tags(path, ti)
                result.tags_ok = ok
                result.art_ok = art_ok
            else:
                result.tags_ok = True
                result.art_ok = bool(ti.cover_url)

            processed += 1
            status = "DONE"
            if not args.verbose:
                print_progress(i, len(files), start, label=path.name, status=status)

        except Exception as e:
            errors += 1
            result.error = str(e)
            status = "ERROR"
            if not args.verbose:
                print_progress(i, len(files), start, label=path.name, status=status)
            else:
                print(colored(f"[ERR]  {path.name}: {e}", "red", attrs=["bold"]))

        results.append(result)
        if args.verbose:
            flags = []
            if result.renamed: flags.append(paint_tag("REN"))
            if result.tags_ok: flags.append(paint_tag("TAG"))
            if result.art_ok:  flags.append(paint_tag("ART"))
            if result.skipped: flags.append(paint_tag("SKIP"))
            if result.error:   flags.append(paint_tag("ERR"))
            flags_str = ", ".join(flags) if flags else "no-op"
            print(f"[OK]   {result.src_name} → {result.dest_name}  ({flags_str})")

    elapsed = time.time() - start
    if not args.verbose:
        print()  # newline after progress bar
    return {
        "results": results,
        "processed": processed,
        "unidentified": unidentified,
        "errors": errors,
        "elapsed": elapsed,
        "total": len(files),
    }

def print_summary(summary: Dict[str, Any]):
    ok = colored(SYMBOL_OK + " OK", "green", attrs=["bold"])
    warn = colored(SYMBOL_WARN + " Unidentified", "yellow", attrs=["bold"])
    err = colored(SYMBOL_ERR + " Errors", "red", attrs=["bold"])

    print("\n" + colored("Summary", "white", attrs=["bold"]) + ":")
    print(f"  {ok}: {summary['processed']}   {warn}: {summary['unidentified']}   {err}: {summary['errors']}   Time: {summary['elapsed']:.1f}s")

    print("\n" + colored("Results", "white", attrs=["bold"]) + ":")
    name_w = max(24, min(60, max(len(r.src_name) for r in summary["results"])))
    dest_w = max(24, min(60, max(len(r.dest_name) for r in summary["results"])))
    header = f"{'Source':<{name_w}}  →  {'Dest':<{dest_w}}  |  Status"
    print(colored(header, "cyan", attrs=["bold"]))
    print(colored("-" * len(header), "cyan"))

    for r in summary["results"]:
        bits = []
        if r.skipped: bits.append(colored("SKIP", "yellow"))
        if r.renamed: bits.append(colored("REN", "cyan"))
        if r.tags_ok: bits.append(colored("TAG", "green"))
        if r.art_ok:  bits.append(colored("ART", "magenta"))
        if r.error:   bits.append(colored("ERR", "red", attrs=["bold"]))
        status = ", ".join(bits) if bits else "-"
        if r.error:
            status += " " + colored(f"({r.error})", "red")
        print(f"{r.src_name:<{name_w}}  →  {r.dest_name:<{dest_w}}  |  {status}")

def main():
    ap = argparse.ArgumentParser(description="Identify MP3s with Shazam, rename to 'Artist - Title - Album.mp3', embed cover art, and colorize status.")
    ap.add_argument("root", type=str, help="Root folder to scan.")
    ap.add_argument("--recurse", action="store_true", help="Recurse into subfolders.")
    ap.add_argument("--dry-run", action="store_true", help="Preview actions without modifying files.")
    ap.add_argument("--verbose", action="store_true", help="Verbose per-file logging.")
    ap.add_argument("--timeout", type=float, default=40.0, help="Per-file Shazam timeout in seconds (default 40).")
    args = ap.parse_args()

    root = Path(args.root).resolve()
    if not root.exists() or not root.is_dir():
        print(colored(f"Root not found or not a directory: {root}", "red", attrs=["bold"]))
        sys.exit(1)

    files = gather_files(root, args.recurse)
    if not files:
        print(colored("No MP3 files found.", "yellow", attrs=["bold"])); return

    print(colored(f"Processing {len(files)} MP3 files…", "white", attrs=["bold"]) + "\n")

    try:
        summary = asyncio.run(process_files_async(files, args))
    except KeyboardInterrupt:
        print(colored("\nInterrupted by user.", "red", attrs=["bold"]))
        sys.exit(130)

    print_summary(summary)

if __name__ == "__main__":
    main()
