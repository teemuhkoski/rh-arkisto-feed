#!/usr/bin/env python3
"""Generoi Podcasting 2.0 -yhteensopivan feed.xml-syötteen sekä hakukelpoisen index.html-arkistosivun."""

from __future__ import annotations

import argparse
import html as html_lib
import json
import re
import subprocess
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from email.utils import format_datetime
from pathlib import Path
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
CHAPTERS_DIR = REPO_ROOT / "chapters"
TRANSCRIPTS_DIR = REPO_ROOT / "transcripts"
ANALYSES_DIR = REPO_ROOT / "analyysit"
AUDIO_DIR = REPO_ROOT / "Rane_Raitsikka_ja_Tokela"

MANIFEST_PATH = DATA_DIR / "manifest_rane-raitsikka-tokela.json"
METADATA_CACHE_PATH = DATA_DIR / "audio_metadata.json"
CUSTOM_DESCRIPTIONS_PATH = DATA_DIR / "episode_descriptions.json"
FEED_PATH = REPO_ROOT / "feed.xml"
INDEX_PATH = REPO_ROOT / "index.html"

BASE_URL = "https://teemuhkoski.github.io/rh-arkisto-feed"
SHOW_LINK = "https://www.radiohelsinki.fi/ohjelma/rane-raitsikka-tokela/"
HELSINKI_TZ = ZoneInfo("Europe/Helsinki")

# Virallinen kausijako Radio Helsingin lähetyspäivien ja juontojen perusteella:
# Kausi 1: 1.3.2017 – 24.5.2017 (jaksot 01–13)
# Kausi 2: 30.8.2017 – 3.1.2018 (jaksot 14–32)
# Kausi 3: 26.9.2018 – 19.12.2018 (jaksot 33–45)
SEASON_RANGES: list[tuple[int, int, int]] = [
    (1, 1, 13),
    (2, 14, 32),
    (3, 33, 45),
]

SEASON_LABELS: dict[int, str] = {
    1: "Kausi 1 (Kevät 2017)",
    2: "Kausi 2 (Syksy 2017 – Tammikuu 2018)",
    3: "Kausi 3 (Syksy 2018)",
}

# Säilytetään 4. jakson alkuperäinen GUID, jottei vanhoille tilaajille tule duplikaattia.
LEGACY_GUIDS: dict[int, str] = {
    4: "rane-raitsikka-tokela-2017-03-22-30554",
}

GENERIC_DESCRIPTIONS = {
    "",
    "musiikkia ja asennetta. tarunhohtoisia tarinoita. kyseessä voi olla ainoastaan rane ja tokela.",
    "studion valtaavat jälleen rane raitsikka & tokela!",
    "rokkikukot rane raitsikka & tokela valtaavat jälleen studion!",
    "kolmannen tuotantokauden viimeinen jakso!",
    "monissa liemissä dipatut rokkiäijät studiossa.",
}


def get_season_episode(global_ep: int) -> tuple[int, int]:
    for season, start_ep, end_ep in SEASON_RANGES:
        if start_ep <= global_ep <= end_ep:
            return season, global_ep - start_ep + 1
    raise ValueError(f"Jaksonumero {global_ep} ei kuulu määriteltyyn kausijakoon.")


def format_hms(seconds: int | float) -> str:
    total = int(round(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def parse_hms_to_seconds(hms: str) -> int:
    parts = [int(p) for p in hms.split(":")]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    return parts[0]


def parse_episode_selection(spec: str | None) -> set[int] | None:
    if not spec:
        return None
    selected: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            selected.update(range(int(a), int(b) + 1))
        else:
            selected.add(int(part))
    return selected


def probe_local_audio(global_ep: int, iso_date: str) -> dict[str, int | str] | None:
    matches = list(AUDIO_DIR.glob(f"{global_ep:02d}_{iso_date}_*.mp3"))
    if not matches:
        matches = list(AUDIO_DIR.glob(f"{global_ep:02d}_*.mp3"))
    if not matches:
        return None

    mp3_path = matches[0]
    size = mp3_path.stat().st_size
    duration_str = "03:00:00"
    try:
        out = subprocess.check_output(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(mp3_path),
            ],
            text=True,
        ).strip()
        if out:
            duration_str = format_hms(float(out))
    except Exception:
        pass
    return {"length": size, "duration": duration_str}


def fetch_s3_content_length(url: str) -> int | None:
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "rh-arkisto-feed/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            cl = resp.headers.get("Content-Length")
            return int(cl) if cl else None
    except Exception as exc:
        print(f"  VAROITUS: S3 HEAD epäonnistui ({url}): {exc}", file=sys.stderr)
        return None


def load_analysis_intro(global_ep: int) -> str | None:
    md_path = ANALYSES_DIR / f"jakso{global_ep:02d}_analyysi.md"
    if not md_path.exists():
        return None
    text = md_path.read_text(encoding="utf-8")
    m = re.search(r"^##\s*0\.\s*Jaksokuvaus\s*\n+(.*?)(?:\n+---|\n+##\s)", text, re.S | re.M)
    return m.group(1).strip() if m else None


def cdata_safe(text: str) -> str:
    return text.replace("]]>", "]]]]><![CDATA[>")


def load_episodes() -> list[dict]:
    episodes = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    episodes.sort(key=lambda x: datetime.strptime(x["date"], "%d.%m.%Y"))
    return episodes


def refresh_all_metadata(episodes: list[dict], metadata_cache: dict[str, dict], fetch_s3: bool) -> bool:
    dirty = False
    for global_ep, ep in enumerate(episodes, start=1):
        dt = datetime.strptime(ep["date"], "%d.%m.%Y")
        iso_date = dt.strftime("%Y-%m-%d")
        ep_key = f"{global_ep:02d}"
        meta = metadata_cache.get(ep_key, {})

        if "duration" not in meta or "length" not in meta:
            probed = probe_local_audio(global_ep, iso_date)
            if probed:
                if "duration" not in meta:
                    meta["duration"] = probed["duration"]
                if "length" not in meta:
                    meta["length"] = probed["length"]
                    meta["length_source"] = "local"
                metadata_cache[ep_key] = meta
                dirty = True

        if fetch_s3 and meta.get("length_source") != "s3":
            s3_len = fetch_s3_content_length(ep["url"])
            if s3_len:
                meta["length"] = s3_len
                meta["length_source"] = "s3"
                metadata_cache[ep_key] = meta
                dirty = True
                print(f"  [{ep_key}/45] S3 Content-Length: {s3_len:,} tavua ({meta.get('duration', '?')})")

    return dirty


def build_index_html(published: list[dict], total_episodes: int, has_cover: bool) -> str:
    feed_url = f"{BASE_URL}/feed.xml"
    apple_url = feed_url.replace("https://", "podcast://")
    pocketcasts_url = f"pktc://subscribe/{feed_url.replace('https://', '')}"

    total_sec = sum(parse_hms_to_seconds(ep["duration"]) for ep in published)
    total_hours = total_sec / 3600.0
    total_chapters = sum(len(ep["chapters"]) for ep in published)
    total_songs = sum(
        1
        for ep in published
        for ch in ep["chapters"]
        if ch["title"].lower().startswith(("musiikki:", "intro ja musiikki:", "alkumusiikki:", "loppukappale:"))
    )

    by_season: dict[int, list[dict]] = {}
    for ep in sorted(published, key=lambda x: x["global_ep"]):
        by_season.setdefault(ep["season"], []).append(ep)

    sections_html: list[str] = []
    for season_num in sorted(by_season.keys()):
        season_eps = by_season[season_num]
        cards: list[str] = []
        for ep in season_eps:
            ch_rows = []
            for ch in ep["chapters"]:
                ts = format_hms(ch["startTime"])
                title = html_lib.escape(ch["title"])
                low = ch["title"].lower()
                is_music = low.startswith(("musiikki:", "intro ja musiikki:", "alkumusiikki:", "loppukappale:"))
                is_ad = low.startswith("mainos")
                cls = "ch-music" if is_music else ("ch-ad" if is_ad else "ch-talk")
                icon = "♪" if is_music else ("·" if is_ad else "▸")
                ch_rows.append(
                    f'<li class="ch-row {cls}"><span class="ch-ts">{ts}</span>'
                    f'<span class="ch-icon">{icon}</span><span class="ch-title">{title}</span></li>'
                )

            links = [
                f'<a href="{html_lib.escape(ep["audio_url"])}" target="_blank" rel="noopener">MP3 (S3)</a>',
                f'<a href="{html_lib.escape(ep["chapters_rel"])}" target="_blank" rel="noopener">Chapters JSON</a>',
            ]
            if ep.get("vtt_rel"):
                links.append(
                    f'<a href="{html_lib.escape(ep["vtt_rel"])}" target="_blank" rel="noopener">Tekstitys (VTT)</a>'
                )

            search_blob = html_lib.escape(
                " ".join(
                    [
                        ep["title"],
                        ep["date_fi"],
                        ep["intro_desc"],
                        *(ch["title"] for ch in ep["chapters"]),
                    ]
                ).lower()
            )

            cards.append(
                f"""
      <article class="ep-card" data-search="{search_blob}">
        <div class="ep-head">
          <div>
            <span class="ep-badge">Kausi {ep['season']} · Jakso {ep['ep_in_season']:02d}</span>
            <h3>{html_lib.escape(ep['title'])}</h3>
          </div>
          <div class="ep-meta">
            <span>{html_lib.escape(ep['date_fi'])}</span>
            <span>{html_lib.escape(ep['duration'])}</span>
          </div>
        </div>
        <p class="ep-desc">{html_lib.escape(ep['intro_desc'])}</p>
        <div class="ep-links">{' '.join(links)}</div>
        <details class="ep-chapters">
          <summary>Näytä sisällysluettelo ja soitetut biisit ({len(ep['chapters'])} kpl)</summary>
          <ul class="ch-list">
            {''.join(ch_rows)}
          </ul>
        </details>
      </article>"""
            )

        sections_html.append(
            f"""
    <section class="season-section" data-season="{season_num}">
      <h2 class="season-title">{html_lib.escape(SEASON_LABELS.get(season_num, f'Kausi {season_num}'))} <span class="season-count">({len(season_eps)} jaksoa julkaistu)</span></h2>
      <div class="ep-grid">{''.join(cards)}</div>
    </section>"""
        )

    cover_html = '<img src="cover.jpg" alt="Rane Raitsikka ja Tokela" class="hero-cover" />' if has_cover else ""

    return f"""<!DOCTYPE html>
<html lang="fi">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <meta name="robots" content="noindex, nofollow" />
  <title>Rane Raitsikka ja Tokela – Podcast-arkisto</title>
  <style>
    :root {{
      --bg: #0f1014;
      --card: #181a21;
      --card-hover: #1e212b;
      --border: #2a2e3b;
      --text: #eceef4;
      --muted: #9aa1b2;
      --accent: #f59e0b;
      --accent-soft: rgba(245, 158, 11, 0.14);
      --music: #60a5fa;
      --ad: #6b7280;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Inter, sans-serif;
      background: var(--bg);
      color: var(--text);
      line-height: 1.55;
    }}
    .wrap {{
      max-width: 960px;
      margin: 0 auto;
      padding: 28px 18px 64px;
    }}
    .hero {{
      display: flex;
      gap: 24px;
      align-items: center;
      background: linear-gradient(135deg, #1b1d26 0%, #14151c 100%);
      border: 1px solid var(--border);
      border-radius: 16px;
      padding: 24px;
      margin-bottom: 24px;
    }}
    .hero-cover {{
      width: 148px;
      height: 148px;
      border-radius: 12px;
      object-fit: cover;
      flex-shrink: 0;
      box-shadow: 0 10px 28px rgba(0,0,0,0.45);
    }}
    .hero h1 {{
      margin: 0 0 6px;
      font-size: 1.75rem;
      letter-spacing: -0.02em;
    }}
    .hero p {{
      margin: 0 0 14px;
      color: var(--muted);
      font-size: 0.96rem;
    }}
    .hero p a {{
      color: var(--accent);
      text-decoration: none;
    }}
    .hero p a:hover {{ text-decoration: underline; }}
    .stats {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin-bottom: 16px;
    }}
    .stat {{
      background: rgba(255,255,255,0.04);
      border: 1px solid var(--border);
      border-radius: 999px;
      padding: 4px 12px;
      font-size: 0.82rem;
      color: var(--muted);
    }}
    .stat strong {{ color: var(--text); }}
    .actions {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      align-items: center;
    }}
    .btn {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 8px 14px;
      border-radius: 8px;
      font-size: 0.88rem;
      font-weight: 600;
      text-decoration: none;
      cursor: pointer;
      border: 1px solid var(--border);
      background: #232634;
      color: var(--text);
      transition: background 0.15s ease, border-color 0.15s ease;
    }}
    .btn:hover {{ background: #2d3142; border-color: #3f455c; }}
    .btn-primary {{
      background: var(--accent);
      border-color: var(--accent);
      color: #111;
    }}
    .btn-primary:hover {{ background: #fbbf24; border-color: #fbbf24; }}
    .rss-bar {{
      margin-top: 12px;
      display: flex;
      gap: 8px;
      align-items: center;
    }}
    .rss-url {{
      flex: 1;
      min-width: 0;
      background: #0d0e13;
      border: 1px solid var(--border);
      color: var(--muted);
      border-radius: 8px;
      padding: 7px 10px;
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      font-size: 0.8rem;
      overflow: hidden;
      text-overflow: ellipsis;
    }}
    .search-box {{
      margin-bottom: 24px;
      position: sticky;
      top: 10px;
      z-index: 10;
    }}
    .search-box input {{
      width: 100%;
      padding: 12px 16px;
      border-radius: 12px;
      border: 1px solid var(--border);
      background: rgba(24, 26, 33, 0.95);
      backdrop-filter: blur(8px);
      color: var(--text);
      font-size: 0.98rem;
      outline: none;
    }}
    .search-box input:focus {{ border-color: var(--accent); }}
    .season-title {{
      font-size: 1.2rem;
      margin: 28px 0 14px;
      padding-bottom: 8px;
      border-bottom: 1px solid var(--border);
    }}
    .season-count {{
      color: var(--muted);
      font-weight: 400;
      font-size: 0.92rem;
    }}
    .ep-grid {{
      display: flex;
      flex-direction: column;
      gap: 14px;
    }}
    .ep-card {{
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 18px 20px;
    }}
    .ep-head {{
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 12px;
      margin-bottom: 8px;
    }}
    .ep-badge {{
      display: inline-block;
      background: var(--accent-soft);
      color: var(--accent);
      font-size: 0.76rem;
      font-weight: 700;
      padding: 2px 8px;
      border-radius: 999px;
      margin-bottom: 4px;
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }}
    .ep-card h3 {{
      margin: 0;
      font-size: 1.12rem;
    }}
    .ep-meta {{
      display: flex;
      gap: 10px;
      color: var(--muted);
      font-size: 0.85rem;
      white-space: nowrap;
      padding-top: 2px;
    }}
    .ep-desc {{
      margin: 6px 0 12px;
      color: #cfd4e0;
      font-size: 0.93rem;
    }}
    .ep-links {{
      display: flex;
      flex-wrap: wrap;
      gap: 12px;
      margin-bottom: 10px;
      font-size: 0.84rem;
    }}
    .ep-links a {{
      color: var(--accent);
      text-decoration: none;
    }}
    .ep-links a:hover {{ text-decoration: underline; }}
    .ep-chapters summary {{
      cursor: pointer;
      color: var(--muted);
      font-size: 0.88rem;
      font-weight: 600;
      user-select: none;
      padding: 4px 0;
    }}
    .ep-chapters summary:hover {{ color: var(--text); }}
    .ch-list {{
      list-style: none;
      margin: 10px 0 0;
      padding: 10px 0 0;
      border-top: 1px solid var(--border);
      display: flex;
      flex-direction: column;
      gap: 4px;
    }}
    .ch-row {{
      display: flex;
      gap: 8px;
      font-size: 0.86rem;
      padding: 3px 6px;
      border-radius: 6px;
    }}
    .ch-row:hover {{ background: rgba(255,255,255,0.03); }}
    .ch-ts {{
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      color: var(--muted);
      flex-shrink: 0;
    }}
    .ch-icon {{
      width: 14px;
      text-align: center;
      flex-shrink: 0;
    }}
    .ch-music {{ color: var(--music); }}
    .ch-ad {{ color: var(--ad); font-size: 0.81rem; }}
    .ch-talk {{ color: var(--text); }}
    .hidden {{ display: none !important; }}
    footer {{
      margin-top: 40px;
      text-align: center;
      color: var(--muted);
      font-size: 0.82rem;
    }}
    footer a {{ color: var(--muted); }}
    @media (max-width: 640px) {{
      .hero {{ flex-direction: column; text-align: center; }}
      .stats, .actions {{ justify-content: center; }}
      .ep-head {{ flex-direction: column; gap: 4px; }}
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <header class="hero">
      {cover_html}
      <div style="flex:1;min-width:0;">
        <h1>Rane Raitsikka ja Tokela – Arkisto</h1>
        <p>Radio Helsingin ohjelmajaksot (2017–2018) sisällysluettelolla, aikaleimoilla ja siivotuilla tekstityksillä. Ääni toistetaan suoraan <a href="{SHOW_LINK}" target="_blank" rel="noopener">Radio Helsingin arkistosta</a>. Tämä on radiokanavasta riippumaton harrasteprojekti.</p>
        <div class="stats">
          <span class="stat"><strong>{len(published)} / {total_episodes}</strong> jaksoa julkaistu</span>
          <span class="stat"><strong>{total_hours:.1f} h</strong> kuunneltavaa</span>
          <span class="stat"><strong>{total_chapters}</strong> chapteria</span>
          <span class="stat"><strong>{total_songs}</strong> biisiä</span>
        </div>
        <div class="actions">
          <button class="btn btn-primary" id="copyBtn" type="button">Kopioi RSS-osoite</button>
          <a class="btn" href="{html_lib.escape(apple_url)}">Apple Podcasts</a>
          <a class="btn" href="{html_lib.escape(pocketcasts_url)}">Pocket Casts</a>
          <a class="btn" href="feed.xml">Avaa feed.xml</a>
        </div>
        <div class="rss-bar">
          <input class="rss-url" id="rssUrl" type="text" readonly value="{html_lib.escape(feed_url)}" />
        </div>
      </div>
    </header>

    <div class="search-box">
      <input id="searchInput" type="search" placeholder="Hae artistia, biisiä, henkilöä, paikkaa tai tarinaa (esim. Lepakko, Keith Richards, Motörhead)..." />
    </div>

    <main id="archiveMain">
      {''.join(sections_html)}
    </main>

    <footer>
      Alkuperäinen ohjelma: <a href="{SHOW_LINK}" target="_blank" rel="noopener">Radio Helsinki – Rane Raitsikka &amp; Tokela</a>
    </footer>
  </div>

  <script>
    const copyBtn = document.getElementById('copyBtn');
    const rssInput = document.getElementById('rssUrl');
    copyBtn.addEventListener('click', async () => {{
      try {{
        await navigator.clipboard.writeText(rssInput.value);
        copyBtn.textContent = 'Kopioitu!';
        setTimeout(() => copyBtn.textContent = 'Kopioi RSS-osoite', 1800);
      }} catch {{
        rssInput.select();
        document.execCommand('copy');
      }}
    }});

    const searchInput = document.getElementById('searchInput');
    const cards = Array.from(document.querySelectorAll('.ep-card'));
    const sections = Array.from(document.querySelectorAll('.season-section'));

    searchInput.addEventListener('input', () => {{
      const q = searchInput.value.trim().toLowerCase();
      for (const card of cards) {{
        const match = !q || card.dataset.search.includes(q);
        card.classList.toggle('hidden', !match);
        const det = card.querySelector('details');
        if (det) {{
          if (q && match) {{
            det.open = true;
            for (const row of card.querySelectorAll('.ch-row')) {{
              row.classList.toggle('hidden', !row.textContent.toLowerCase().includes(q));
            }}
          }} else {{
            det.open = false;
            for (const row of card.querySelectorAll('.ch-row')) {{
              row.classList.remove('hidden');
            }}
          }}
        }}
      }}
      for (const sec of sections) {{
        const anyVisible = sec.querySelector('.ep-card:not(.hidden)') !== null;
        sec.classList.toggle('hidden', !anyVisible);
      }}
    }});
  </script>
</body>
</html>
"""


def main() -> None:
    parser = argparse.ArgumentParser(description="Generoi feed.xml ja index.html manifestista sekä chapters-luvuista.")
    parser.add_argument("--episodes", default=None, help="Valinnaiset juoksevat jaksonumerot, esim. '1-13' tai '1,2,4'")
    parser.add_argument("--season", type=int, default=None, help="Sisällytä vain tietty kausi (1, 2 tai 3)")
    parser.add_argument(
        "--fetch-s3-lengths",
        action="store_true",
        help="Hakee täsmälliset Content-Length-koot HTTP HEAD -pyynnöllä S3:sta julkaistaville jaksoille",
    )
    parser.add_argument(
        "--fetch-all-s3-lengths",
        action="store_true",
        help="Hakee kaikkien 45 jakson S3 Content-Length -tavukoot ja paikalliset kestot audio_metadata.json-välimuistiin",
    )
    parser.add_argument("--output", default=str(FEED_PATH), help="Tulostettavan XML-tiedoston polku")
    parser.add_argument("--no-html", action="store_true", help="Älä generoi index.html-tiedostoa")
    args = parser.parse_args()

    episodes = load_episodes()
    selected_eps = parse_episode_selection(args.episodes)

    metadata_cache: dict[str, dict] = {}
    if METADATA_CACHE_PATH.exists():
        metadata_cache = json.loads(METADATA_CACHE_PATH.read_text(encoding="utf-8"))

    custom_descriptions: dict[str, str] = {}
    if CUSTOM_DESCRIPTIONS_PATH.exists():
        custom_descriptions = json.loads(CUSTOM_DESCRIPTIONS_PATH.read_text(encoding="utf-8"))

    cache_dirty = False
    if args.fetch_all_s3_lengths:
        print("Päivitetään kaikkien 45 jakson kestot ja S3 Content-Length -tavukoot...")
        cache_dirty = refresh_all_metadata(episodes, metadata_cache, fetch_s3=True)

    has_cover = (REPO_ROOT / "cover.jpg").exists()
    cover_url = f"{BASE_URL}/cover.jpg"

    items_data: list[tuple[datetime, str]] = []
    published_records: list[dict] = []

    for global_ep, ep in enumerate(episodes, start=1):
        season, ep_in_season = get_season_episode(global_ep)

        if args.season is not None and season != args.season:
            continue
        if selected_eps is not None and global_ep not in selected_eps:
            continue

        chapters_path = CHAPTERS_DIR / f"chapters_{global_ep:02d}.json"
        if not chapters_path.exists():
            continue

        chapters_data = json.loads(chapters_path.read_text(encoding="utf-8"))
        chapters = chapters_data.get("chapters", [])
        if not chapters:
            print(f"VAROITUS: {chapters_path.name} ei sisällä lukuja, ohitetaan.", file=sys.stderr)
            continue

        dt = datetime.strptime(ep["date"], "%d.%m.%Y").replace(hour=19, minute=0, second=0, tzinfo=HELSINKI_TZ)
        iso_date = dt.strftime("%Y-%m-%d")
        pub_date = format_datetime(dt)

        ep_key = f"{global_ep:02d}"
        meta = metadata_cache.get(ep_key, {})
        if not meta or "duration" not in meta or "length" not in meta:
            probed = probe_local_audio(global_ep, iso_date)
            if probed:
                meta.update(probed)
                metadata_cache[ep_key] = meta
                cache_dirty = True

        if args.fetch_s3_lengths and meta.get("length_source") != "s3":
            s3_len = fetch_s3_content_length(ep["url"])
            if s3_len:
                meta["length"] = s3_len
                meta["length_source"] = "s3"
                metadata_cache[ep_key] = meta
                cache_dirty = True

        audio_length = int(meta.get("length", 0))
        duration_str = str(meta.get("duration", "03:00:00"))

        manifest_desc = (ep.get("description") or "").strip()
        intro_desc = (
            custom_descriptions.get(ep_key)
            or load_analysis_intro(global_ep)
            or manifest_desc
        )
        if manifest_desc.lower() in GENERIC_DESCRIPTIONS and intro_desc == manifest_desc:
            print(
                f"HUOM: Jaksolla {ep_key} (Kausi {season}, Jakso {ep_in_season:02d}) on puuttuva tai geneerinen kuvaus. "
                f"Lisää esittely tiedostoon data/episode_descriptions.json tai analyysit/jakso{ep_key}_analyysi.md (## 0. Jaksokuvaus).",
                file=sys.stderr,
            )

        prev_t = -1
        ts_lines: list[str] = []
        for ch in chapters:
            t = int(round(ch["startTime"]))
            if t < prev_t:
                raise ValueError(f"Epäjärjestyksessä oleva aikaleima tiedostossa {chapters_path.name}: {t} < {prev_t}")
            prev_t = t
            ts_lines.append(f"{format_hms(t)} {ch['title']}")

        full_description = f"{intro_desc}\n\n" + "\n".join(ts_lines) if intro_desc else "\n".join(ts_lines)

        guid = LEGACY_GUIDS.get(global_ep, f"rane-raitsikka-tokela-{iso_date}-ep{global_ep:02d}")
        item_title = f"Kausi {season}, Jakso {ep_in_season:02d} ({iso_date})"

        chapters_rel = f"chapters/{chapters_path.name}"
        transcript_tags: list[str] = []
        vtt_rel: str | None = None
        vtt_path = TRANSCRIPTS_DIR / f"transcript_{global_ep:02d}.vtt"
        srt_path = TRANSCRIPTS_DIR / f"transcript_{global_ep:02d}.srt"
        if vtt_path.exists():
            vtt_rel = f"transcripts/{vtt_path.name}"
            transcript_tags.append(
                f'      <podcast:transcript url="{BASE_URL}/{vtt_rel}" type="text/vtt" language="fi" />'
            )
        if srt_path.exists():
            transcript_tags.append(
                f'      <podcast:transcript url="{BASE_URL}/transcripts/{srt_path.name}" type="application/x-subrip" language="fi" />'
            )

        image_tag = f'      <itunes:image href="{cover_url}" />\n' if has_cover else ""
        transcript_block = ("\n".join(transcript_tags) + "\n") if transcript_tags else ""

        item_xml = (
            f"    <item>\n"
            f"      <title>{escape(item_title)}</title>\n"
            f'      <guid isPermaLink="false">{escape(guid)}</guid>\n'
            f"      <pubDate>{pub_date}</pubDate>\n"
            f"      <itunes:season>{season}</itunes:season>\n"
            f"      <itunes:episode>{ep_in_season}</itunes:episode>\n"
            f"      <itunes:episodeType>full</itunes:episodeType>\n"
            f"      <itunes:explicit>true</itunes:explicit>\n"
            f"{image_tag}"
            f"      <description><![CDATA[{cdata_safe(full_description)}]]></description>\n"
            f'      <enclosure url="{escape(ep["url"])}"\n'
            f'                 length="{audio_length}"\n'
            f'                 type="audio/mpeg" />\n'
            f"      <itunes:duration>{duration_str}</itunes:duration>\n"
            f'      <podcast:chapters url="{BASE_URL}/{chapters_rel}"\n'
            f'                        type="application/json+chapters" />\n'
            f"{transcript_block}"
            f"    </item>"
        )
        items_data.append((dt, item_xml))
        published_records.append(
            {
                "global_ep": global_ep,
                "season": season,
                "ep_in_season": ep_in_season,
                "title": item_title,
                "date_fi": ep["date"],
                "duration": duration_str,
                "intro_desc": intro_desc,
                "audio_url": ep["url"],
                "chapters_rel": chapters_rel,
                "vtt_rel": vtt_rel,
                "chapters": chapters,
            }
        )

    if cache_dirty:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        METADATA_CACHE_PATH.write_text(
            json.dumps(metadata_cache, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    items_data.sort(key=lambda x: x[0], reverse=True)

    channel_image = ""
    if has_cover:
        channel_image = (
            f'    <itunes:image href="{cover_url}" />\n'
            f"    <image>\n"
            f"      <url>{cover_url}</url>\n"
            f"      <title>Rane Raitsikka ja Tokela – Arkisto</title>\n"
            f"      <link>{SHOW_LINK}</link>\n"
            f"    </image>\n"
        )

    feed_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0"\n'
        '     xmlns:atom="http://www.w3.org/2005/Atom"\n'
        '     xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"\n'
        '     xmlns:podcast="https://podcastindex.org/namespace/1.0">\n'
        "  <channel>\n"
        "    <title>Rane Raitsikka ja Tokela – Arkisto</title>\n"
        f"    <link>{SHOW_LINK}</link>\n"
        f'    <atom:link href="{BASE_URL}/feed.xml" rel="self" type="application/rss+xml" />\n'
        "    <language>fi</language>\n"
        "    <description>Radio Helsingin Rane Raitsikka &amp; Tokela -ohjelman jaksoarkisto (2017–2018) sisällysluettelolla ja aikaleimoilla.</description>\n"
        "    <itunes:author>Rane Raitsikka &amp; Tokela</itunes:author>\n"
        "    <itunes:type>episodic</itunes:type>\n"
        "    <itunes:explicit>true</itunes:explicit>\n"
        "    <itunes:block>yes</itunes:block>\n"
        "    <podcast:locked>yes</podcast:locked>\n"
        '    <itunes:category text="Music" />\n'
        f"{channel_image}\n"
        + "\n\n".join(item for _, item in items_data)
        + "\n  </channel>\n"
        "</rss>\n"
    )

    ET.fromstring(feed_xml)
    out_path = Path(args.output)
    out_path.write_text(feed_xml, encoding="utf-8")
    print(f"Generoitu {out_path} ({len(items_data)} jaksoa).")

    if not args.no_html:
        html_content = build_index_html(published_records, len(episodes), has_cover)
        INDEX_PATH.write_text(html_content, encoding="utf-8")
        print(f"Generoitu {INDEX_PATH} ({len(published_records)} jaksoa).")


if __name__ == "__main__":
    main()
