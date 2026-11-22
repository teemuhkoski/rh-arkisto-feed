import json
import re
from datetime import datetime
from pathlib import Path

from mutagen.id3 import CHAP, COMM, CTOC, CTOCFlags, ID3, TIT2
from mutagen.mp3 import MP3

OUT_DIR = Path("Rane_Raitsikka_ja_Tokela")
MANIFEST_FILE = Path("manifest.json")


def clean_description(raw: str, date_str: str) -> str:
    text = raw.strip()
    # Poista alun päivämäärä ja kellonaika (esim. "19.12.2018 19.00")
    text = re.sub(r"^\d{1,2}\.\d{1,2}\.\d{4}(?:\s+\d{1,2}[.:]\d{2})?\s*", "", text)
    # Jos kuvauksessa on kaksoispiste ("...aiheita mm: X, Y, Z"), poimi tiivis aiheluettelo otsikkoon
    return text.strip()


def extract_short_topics(desc: str) -> str:
    if ":" in desc:
        head, tail = desc.split(":", 1)
        if len(head) < 110 and len(tail.strip()) > 15:
            return tail.strip()
    return desc


def main():
    episodes = json.loads(MANIFEST_FILE.read_text(encoding="utf-8"))
    episodes.sort(key=lambda x: datetime.strptime(x["date"], "%d.%m.%Y"))

    updated = 0
    for idx, ep in enumerate(episodes, start=1):
        dt = datetime.strptime(ep["date"], "%d.%m.%Y")
        iso_date = dt.strftime("%Y-%m-%d")
        mp3_path = OUT_DIR / f"{idx:02d}_{iso_date}_Rane_Raitsikka_ja_Tokela.mp3"
        if not mp3_path.exists():
            continue

        full_desc = clean_description(ep["description"], ep["date"])
        topics = extract_short_topics(full_desc)
        display_title = f"{idx:02d} ({ep['date']}) – {topics}" if topics else f"Jakso {idx:02d} ({ep['date']})"

        audio = MP3(mp3_path)
        duration_ms = int(audio.info.length * 1000)

        tags = ID3(mp3_path)
        # 1. Otsikko soittonäkymään ja lukitusnäytölle
        tags.delall("TIT2")
        tags.add(TIT2(encoding=3, text=display_title))

        # 2. Täysi kuvaus BookPlayerin Details-näkymään (vaatii lang="eng" iOS:lle)
        tags.delall("COMM")
        tags.add(COMM(encoding=3, lang="eng", desc="", text=full_desc))

        # 3. Sisällysluettelo (Chapters-nappi soittimen alareunaan)
        tags.delall("CHAP")
        tags.delall("CTOC")
        tags.add(
            CTOC(
                element_id="toc",
                flags=CTOCFlags.TOP_LEVEL | CTOCFlags.ORDERED,
                child_element_ids=["chp1"],
                sub_frames=[TIT2(encoding=3, text="Jakson sisältö")],
            )
        )
        tags.add(
            CHAP(
                element_id="chp1",
                start_time=0,
                end_time=duration_ms,
                sub_frames=[TIT2(encoding=3, text=full_desc)],
            )
        )

        tags.save(mp3_path)
        updated += 1

    print(f"Valmis! Päivitettiin {updated} MP3-tiedostoa.")


if __name__ == "__main__":
    main()