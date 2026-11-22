#!/usr/bin/env python3
"""Siivoaa Whisper-.srt-litteroinnit ja muuntaa ne Podcasting 2.0 -yhteensopivaan WebVTT-muotoon (.vtt)."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
AUDIO_DIR = REPO_ROOT / "Rane_Raitsikka_ja_Tokela"
CHAPTERS_DIR = REPO_ROOT / "chapters"
TRANSCRIPTS_DIR = REPO_ROOT / "transcripts"

# Tyypilliset Whisperin suomenkieliset musiikkihallusinaatiot (täsmälliset tai koko rivin kattavat)
HALLUCINATION_EXACT = {
    "kiitos.",
    "kiitos!",
    "kiitos",
    "kiitos kun katsoit videon!",
    "kiitos kun katsoit videon.",
    "kiitos kun katsoit!",
    "kiitos kun katsoit.",
    "kiitos katsomisesta!",
    "kiitos katsomisesta.",
    "yle areena",
    "yle areena.",
    "yhteistyössä",
    "tervetuloa!",
    "tervetuloa.",
    "tämä on seuraava.",
    "katsotaanpa.",
    "tekstitys: yle",
    "jussi latvala",
}

# Korjaussanakirja tyypillisimmille Whisperin litterointivirheille (case-insensitive)
REPLACEMENTS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bRane Raitsika[a-zäö]*\b", re.I), "Rane Raitsikka"),
    (re.compile(r"\bRaane Raitsika[a-zäö]*\b", re.I), "Rane Raitsikka"),
    (re.compile(r"\bRaine Raitsika[a-zäö]*\b", re.I), "Rane Raitsikka"),
    (re.compile(r"\bRani Raitsika[a-zäö]*\b", re.I), "Rane Raitsikka"),
    (re.compile(r"\bRann Raitsika[a-zäö]*\b", re.I), "Rane Raitsikka"),
    (re.compile(r"\bRaana Raitsik[a-zäö]*\b", re.I), "Rane Raitsikka"),
    (re.compile(r"\bRanna Raitsikk?a\b", re.I), "Rane Raitsikka"),
    (re.compile(r"\bRaan Raachika\b", re.I), "Rane Raitsikka"),
    (re.compile(r"\bRaana Raichika\b", re.I), "Rane Raitsikka"),
    (re.compile(r"\bRai Rai Chikai\b", re.I), "Rane Raitsikka ja"),
    (re.compile(r"\bRaale Räitsika[a-zäö]*\b", re.I), "Rane Raitsikka"),
    (re.compile(r"\bRanea Tokela\b", re.I), "Rane ja Tokela"),
    (re.compile(r"\bTokala\b", re.I), "Tokela"),
    (re.compile(r"\bTokila\b", re.I), "Tokela"),
    (re.compile(r"\bTokella\b", re.I), "Tokela"),
    (re.compile(r"\bTokio Radio\b", re.I), "Tokela Radio"),
    (re.compile(r"\bTokio Stadissa\b", re.I), "Tokela Stadissa"),
    (re.compile(r"\bcustom sounds ja custom boards\b", re.I), "Custom Sounds ja Custom Boards"),
    (re.compile(r"\bTiketti, ponkes, Custom Sounds", re.I), "Tiketti, Ponke's, Custom Sounds"),
    (re.compile(r"\bQueen's (?:on )?Stone Age\b", re.I), "Queens of the Stone Age"),
    (re.compile(r"\bOld Dirty Bastard\b", re.I), "Ol' Dirty Bastard"),
    (re.compile(r"\bRacoon Tours\b", re.I), "Raconteurs"),
    (re.compile(r"\bSliced Stone\b", re.I), "Sly Stone"),
    (re.compile(r"\bRansidin Radio\b", re.I), "Rancidin Radio"),
    (re.compile(r"\bGeneral Jassa\b", re.I), "General Njassa"),
    (re.compile(r"\bPahvi Maijainen\b", re.I), "Pave Maijanen"),
    (re.compile(r"\bElias Lönruuti\b", re.I), "Elias Lönnrot"),
    (re.compile(r"\bTom Jorke\b", re.I), "Thom Yorke"),
    (re.compile(r"\bBrandt Björk\b", re.I), "Brant Bjork"),
    (re.compile(r"\bBrent Björk\b", re.I), "Brant Bjork"),
    (re.compile(r"\bRonny Wood", re.I), "Ronnie Wood"),
    (re.compile(r"\bKit Richards", re.I), "Keith Richards"),
    (re.compile(r"\bG-Live-lä[bpv][a-zäö]*\b", re.I), "G Livelab"),
    (re.compile(r"\bHaake-Helia", re.I), "Haaga-Helia"),
    (re.compile(r"\bArtec\b", re.I), "Artek"),
    (re.compile(r"\bVupu\.fi\b", re.I), "Pupu.fi"),
    (re.compile(r"\bPille Blåfjell\b", re.I), "Ville Blåfield"),
    (re.compile(r"\bVille Blåfjell\b", re.I), "Ville Blåfield"),
    (re.compile(r"\bLeba(?=[ ,.\-]|$)", re.I), "Lepa"),
]


def parse_srt_time(ts: str) -> float:
    ts = ts.strip().replace(",", ".")
    h, m, s = ts.split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def format_vtt_time(seconds: float) -> str:
    ms_total = max(0, int(round(seconds * 1000)))
    h, rem = divmod(ms_total, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def is_hallucination(text: str) -> bool:
    norm = re.sub(r"\s+", " ", text).strip().lower()
    if not norm:
        return True
    if norm in HALLUCINATION_EXACT:
        return True
    # Toistuvat roskasanat (esim. "joulu, joulu, joulu...", "stoooooo...", "hurtatatata...")
    if re.search(r"(.)\1{10,}", norm):
        return True
    if re.search(r"(\b\w{2,10}\b[,\s]*)\1{4,}", norm):
        return True
    if re.search(r"([a-zäö]{2,6})\1{5,}", norm):
        return True
    return False


def clean_text(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    for pattern, repl in REPLACEMENTS:
        text = pattern.sub(repl, text)
    return text


def is_music_chapter(title: str) -> bool:
    low = title.lower()
    return (
        low.startswith("musiikki:")
        or low.startswith("intro ja musiikki:")
        or low.startswith("alkumusiikki:")
        or low.startswith("loppukappale:")
    )


def build_music_intervals(chapters: list[dict]) -> list[tuple[float, float, str]]:
    intervals: list[tuple[float, float, str]] = []
    for idx, ch in enumerate(chapters):
        title = ch["title"]
        if not is_music_chapter(title):
            continue
        start = float(ch["startTime"])
        # Musiikki alkaa joskus muutama sekunti introjuonnon/sponsorin jälkeen;
        # jätetään 2 sekunnin marginaali luvun loppuun, jotta seuraava juonto ei katkea.
        end = float(chapters[idx + 1]["startTime"]) if idx + 1 < len(chapters) else start + 600.0
        intervals.append((start, end, title))
    return intervals


def find_music_interval(t: float, intervals: list[tuple[float, float, str]]) -> tuple[float, float, str] | None:
    for start, end, title in intervals:
        # Ensimmäisen luvun ("Intro ja Musiikki:") alussa saattaa olla sponsori (0–10s), pidetään se!
        eff_start = start + 12.0 if start == 0 and title.lower().startswith("intro ja ") else start
        if eff_start <= t < end - 1.5:
            return (start, end, title)
    return None


def parse_srt(srt_path: Path) -> list[tuple[float, float, str]]:
    raw = srt_path.read_text(encoding="utf-8", errors="replace")
    blocks = re.split(r"\r?\n\s*\r?\n", raw.strip())
    cues: list[tuple[float, float, str]] = []
    for block in blocks:
        lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        if len(lines) < 3 or "-->" not in lines[1]:
            continue
        s_str, e_str = lines[1].split("-->", 1)
        start = parse_srt_time(s_str)
        end = parse_srt_time(e_str)
        text = " ".join(lines[2:])
        cues.append((start, end, text))
    return cues


def clean_episode(ep_num: int) -> Path | None:
    srt_matches = list(AUDIO_DIR.glob(f"{ep_num:02d}_*.srt"))
    chapters_path = CHAPTERS_DIR / f"chapters_{ep_num:02d}.json"
    if not srt_matches:
        print(f"Jakso {ep_num:02d}: .srt-tiedostoa ei löytynyt, ohitetaan.", file=sys.stderr)
        return None
    if not chapters_path.exists():
        print(f"Jakso {ep_num:02d}: {chapters_path.name} puuttuu, ohitetaan.", file=sys.stderr)
        return None

    srt_path = srt_matches[0]
    chapters_data = json.loads(chapters_path.read_text(encoding="utf-8"))
    chapters = chapters_data.get("chapters", [])
    music_intervals = build_music_intervals(chapters)

    raw_cues = parse_srt(srt_path)
    out_cues: list[tuple[float, float, str]] = []
    inserted_music: set[float] = set()

    for start, end, text in raw_cues:
        # Tarkista osuuko rivi musiikkiluvun sisälle
        music_hit = find_music_interval(start, music_intervals)
        if music_hit is not None:
            m_start, m_end, m_title = music_hit
            if m_start not in inserted_music:
                inserted_music.add(m_start)
                marker_start = max(m_start, start) if m_start == 0 else m_start
                marker_end = min(m_end, marker_start + 8.0)
                label = re.sub(r"^Intro ja\s+", "", m_title, flags=re.I)
                suffix = "" if label.endswith((".", "!", "?")) else "."
                out_cues.append((marker_start, marker_end, f"♪ {label}{suffix}"))
            continue

        if is_hallucination(text):
            continue

        cleaned = clean_text(text)
        if not cleaned or is_hallucination(cleaned):
            continue

        # Korjaa mahdolliset ylipitkät Whisper-hallusinaatioaikavälit
        if end <= start:
            end = start + 2.0
        out_cues.append((start, end, cleaned))

    # Varmista että myös ne musiikkiluvut, joista Whisper ei tuottanut yhtään riviä, näkyvät
    for m_start, m_end, m_title in music_intervals:
        if m_start not in inserted_music:
            marker_start = m_start + 6.0 if m_start == 0 else m_start
            marker_end = min(m_end, marker_start + 8.0)
            label = re.sub(r"^Intro ja\s+", "", m_title, flags=re.I)
            suffix = "" if label.endswith((".", "!", "?")) else "."
            out_cues.append((marker_start, marker_end, f"♪ {label}{suffix}"))

    out_cues.sort(key=lambda x: x[0])

    # Poista peräkkäiset päällekkäisyydet aikaleimoista
    normalized: list[tuple[float, float, str]] = []
    for start, end, text in out_cues:
        if normalized and start < normalized[-1][1]:
            prev_s, prev_e, prev_t = normalized[-1]
            normalized[-1] = (prev_s, max(prev_s + 0.5, start), prev_t)
        if end <= start:
            end = start + 1.0
        normalized.append((start, end, text))

    TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = TRANSCRIPTS_DIR / f"transcript_{ep_num:02d}.vtt"

    lines = ["WEBVTT", ""]
    for start, end, text in normalized:
        lines.append(f"{format_vtt_time(start)} --> {format_vtt_time(end)}")
        lines.append(text)
        lines.append("")

    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(
        f"Jakso {ep_num:02d}: {len(raw_cues)} -> {len(normalized)} riviä "
        f"({out_path.stat().st_size // 1024} KB) -> {out_path.relative_to(REPO_ROOT)}"
    )
    return out_path


def parse_selection(spec: str | None) -> list[int]:
    if not spec:
        nums = []
        for p in sorted(CHAPTERS_DIR.glob("chapters_*.json")):
            m = re.match(r"chapters_(\d+)\.json$", p.name)
            if m:
                nums.append(int(m.group(1)))
        return nums
    out: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            out.update(range(int(a), int(b) + 1))
        else:
            out.add(int(part))
    return sorted(out)


def main() -> None:
    parser = argparse.ArgumentParser(description="Siivoaa Whisper-.srt-tiedostot WebVTT-tekstityksiksi.")
    parser.add_argument("--episodes", default=None, help="Jaksonumerot, esim. '1,2,4' tai '1-13' (oletus: kaikki chapters_*.json)")
    args = parser.parse_args()

    eps = parse_selection(args.episodes)
    if not eps:
        print("Ei käsiteltäviä jaksoja.", file=sys.stderr)
        sys.exit(1)

    for ep in eps:
        clean_episode(ep)


if __name__ == "__main__":
    main()
