#!/usr/bin/env python3
"""Litteroi Radio Helsinki -arkistojaksot MLX Whisperillä ja tallentaa .srt-tiedostot jaksojen viereen."""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

DEFAULT_MODEL = "mlx-community/whisper-large-v3-mlx-4bit"
AUDIO_EXTS = {".mp3", ".m4a", ".wav", ".flac"}


def find_mlx_whisper() -> str:
    """Etsii mlx_whisper-komennon aktiivisesta tai tunnetuista virtuaaliympäristöistä."""
    found = shutil.which("mlx_whisper")
    if found:
        return found

    candidates = [
        Path(sys.executable).parent / "mlx_whisper",
        Path.home() / ".venvs" / "mlx-whisper" / "bin" / "mlx_whisper",
        Path.home() / ".venvs" / "rane-tokela" / "bin" / "mlx_whisper",
    ]
    for candidate in candidates:
        if candidate.exists() and os.access(candidate, os.X_OK):
            return str(candidate)

    raise FileNotFoundError(
        "mlx_whisper-komentoa ei löytynyt. Aktivoi ympäristö tai asenna se:\n"
        "  python3 -m venv ~/.venvs/mlx-whisper && ~/.venvs/mlx-whisper/bin/pip install mlx-whisper"
    )


def parse_episode_selection(spec: str | None) -> set[int] | None:
    """Jäsentää valinnan muodossa '1-5,8,12-15' jaksonumerojoukoksi."""
    if not spec:
        return None
    selected: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start_s, end_s = part.split("-", 1)
            selected.update(range(int(start_s), int(end_s) + 1))
        else:
            selected.add(int(part))
    return selected


def extract_episode_number(path: Path) -> int | None:
    m = re.match(r"^(\d+)_", path.name)
    return int(m.group(1)) if m else None


def ensure_local_file(audio_path: Path, timeout_sec: int = 300) -> None:
    """Varmistaa, ettei tiedosto ole macOS iCloud Driven pilveen siirtämä stub-tiedosto."""
    st = audio_path.stat()
    if st.st_size > 0 and getattr(st, "st_blocks", 1) == 0:
        print(f"  Tiedosto on iCloudissa, ladataan paikalliselle levylle: {audio_path.name}")
        subprocess.run(["brctl", "download", str(audio_path)], check=False)
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            time.sleep(2)
            if getattr(audio_path.stat(), "st_blocks", 0) > 0:
                print("  Lataus valmis.")
                return
        raise TimeoutError(f"iCloud-lataus aikakatkaistiin tiedostolle: {audio_path.name}")


def format_duration(seconds: float) -> str:
    total = int(round(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h > 0:
        return f"{h} t {m:02d} min {s:02d} s"
    return f"{m} min {s:02d} s"


def transcribe_episode(
    mlx_bin: str,
    audio_path: Path,
    srt_path: Path,
    model: str,
    language: str,
) -> float:
    ensure_local_file(audio_path)

    with tempfile.TemporaryDirectory(prefix="whisper_ep_", dir="/tmp") as tmp_dir:
        tmp_out = Path(tmp_dir)
        cmd = [
            mlx_bin,
            str(audio_path),
            "--model",
            model,
            "--language",
            language,
            "--condition-on-previous-text",
            "False",
            "--output-format",
            "srt",
            "--output-dir",
            str(tmp_out),
        ]

        t0 = time.time()
        subprocess.run(cmd, check=True)
        elapsed = time.time() - t0

        generated = list(tmp_out.glob("*.srt"))
        if not generated:
            raise RuntimeError(f"Whisper ei tuottanut .srt-tiedostoa jaksolle {audio_path.name}")

        srt_path.write_bytes(generated[0].read_bytes())
        return elapsed


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Litteroi kaikki kansion äänitiedostot MLX Whisperillä (.srt tiedostojen viereen)."
    )
    parser.add_argument(
        "kansio",
        nargs="?",
        default="Rane_Raitsikka_ja_Tokela",
        help="Kansio, jossa jaksojen äänitiedostot sijaitsevat (oletus: Rane_Raitsikka_ja_Tokela)",
    )
    parser.add_argument(
        "--jaksot",
        default=None,
        help="Valinnaiset jaksonumerot, esim. '1-5' tai '1,3,10-12' (oletus: kaikki)",
    )
    parser.add_argument(
        "--malli",
        default=DEFAULT_MODEL,
        help=f"MLX Whisper -malli (oletus: {DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--kieli",
        default="fi",
        help="Puheen kieli (oletus: fi)",
    )
    parser.add_argument(
        "--uudelleen",
        action="store_true",
        help="Litteroi myös jaksot, joilla on jo valmis .srt-tiedosto",
    )
    args = parser.parse_args()

    target_dir = Path(args.kansio).expanduser().resolve()
    if not target_dir.is_dir():
        sys.exit(f"Kansiota ei löydy: {target_dir}")

    mlx_bin = find_mlx_whisper()
    selected_nums = parse_episode_selection(args.jaksot)

    audio_files = sorted(
        p for p in target_dir.iterdir()
        if p.is_file() and p.suffix.lower() in AUDIO_EXTS and not p.name.startswith(".")
    )

    if selected_nums is not None:
        audio_files = [
            p for p in audio_files
            if (num := extract_episode_number(p)) is not None and num in selected_nums
        ]

    if not audio_files:
        sys.exit("Käsiteltäviä äänitiedostoja ei löytynyt.")

    to_process: list[tuple[Path, Path]] = []
    skipped = 0
    for audio_path in audio_files:
        srt_path = audio_path.with_suffix(".srt")
        if not args.uudelleen and srt_path.exists() and srt_path.stat().st_size > 100:
            skipped += 1
        else:
            to_process.append((audio_path, srt_path))

    print(f"Kansio:   {target_dir}")
    print(f"Malli:    {args.malli}")
    print(f"Valmiina: {skipped} jaksoa (ohitetaan)")
    print(f"Jonossa:  {len(to_process)} jaksoa\n")

    if not to_process:
        print("Kaikki valitut jaksot on jo litteroitu.")
        return

    durations: list[float] = []
    for idx, (audio_path, srt_path) in enumerate(to_process, start=1):
        print(f"[{idx}/{len(to_process)}] Litteroidaan: {audio_path.name}")
        try:
            elapsed = transcribe_episode(
                mlx_bin=mlx_bin,
                audio_path=audio_path,
                srt_path=srt_path,
                model=args.malli,
                language=args.kieli,
            )
        except KeyboardInterrupt:
            print("\nKeskeytetty käyttäjän toimesta. Valmiit .srt-tiedostot ovat tallessa.")
            sys.exit(130)

        durations.append(elapsed)
        avg_sec = sum(durations) / len(durations)
        remaining = len(to_process) - idx
        eta_str = format_duration(avg_sec * remaining) if remaining > 0 else "0 s"
        print(
            f"  Valmis ({format_duration(elapsed)}) -> {srt_path.name} "
            f"| Jäljellä arviolta: {eta_str}\n"
        )

    print("Kaikki jonossa olleet jaksot litteroitu!")


if __name__ == "__main__":
    main()
