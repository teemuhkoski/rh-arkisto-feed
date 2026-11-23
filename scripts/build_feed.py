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
from urllib.parse import quote
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


def load_analysis_highlights(global_ep: int) -> list[dict[str, int | str]]:
    md_path = ANALYSES_DIR / f"jakso{global_ep:02d}_analyysi.md"
    if not md_path.exists():
        return []
    text = md_path.read_text(encoding="utf-8")
    highlights: list[dict[str, int | str]] = []
    for m in re.finditer(r"^\*\s+\*\*(\d{2}:\d{2}:\d{2})\s*[–-]\s*(.+?):\*\*\s*(.+)$", text, re.M):
        ts_str, hl_title, hl_body = m.group(1), m.group(2).strip(), m.group(3).strip()
        highlights.append(
            {
                "ts": ts_str,
                "t": parse_hms_to_seconds(ts_str),
                "title": hl_title,
                "body": hl_body,
            }
        )
    return highlights


def json_for_script(obj: object) -> str:
    return json.dumps(obj, ensure_ascii=False).replace("</", "<\\/")


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
    feed_no_scheme = feed_url.replace("https://", "")
    feed_encoded = quote(feed_url, safe="")
    subscribe_links = {
        "rss": feed_url,
        "apple": f"podcast://{feed_no_scheme}",
        "pocketcasts": f"pktc://subscribe/{feed_no_scheme}",
        "overcast": f"overcast://x-callback-url/add?url={feed_encoded}",
        "antennapod": f"https://antennapod.org/deeplink/subscribe?url={feed_encoded}",
        "podcastaddict": f"podcastaddict://{feed_no_scheme}",
    }

    total_sec = sum(parse_hms_to_seconds(ep["duration"]) for ep in published)
    total_hours = int(round(total_sec / 3600.0))
    total_songs = sum(
        1
        for ep in published
        for ch in ep["chapters"]
        if ch["title"].lower().startswith(("musiikki:", "intro ja musiikki:", "alkumusiikki:", "loppukappale:"))
    )

    season_labels_clean = {
        1: "Kausi 1 · Kevät 2017",
        2: "Kausi 2 · Syksy 2017 – Tammikuu 2018",
        3: "Kausi 3 · Syksy 2018",
    }

    episodes_js: list[dict] = []
    all_highlights_js: list[dict] = []

    for ep in sorted(published, key=lambda x: x["global_ep"]):
        chs_js = []
        for ch in ep["chapters"]:
            t = int(round(ch["startTime"]))
            title = ch["title"]
            low = title.lower()
            if low.startswith(("musiikki:", "intro ja musiikki:", "alkumusiikki:", "loppukappale:")):
                kind = "music"
            elif low.startswith("mainoskatko"):
                kind = "ad"
            else:
                kind = "talk"
            chs_js.append({"t": t, "ts": format_hms(t), "title": title, "kind": kind})

        hls = ep.get("highlights", [])
        for hl in hls:
            all_highlights_js.append(
                {
                    "epId": ep["global_ep"],
                    "ts": hl["ts"],
                    "t": hl["t"],
                    "title": hl["title"],
                }
            )

        episodes_js.append(
            {
                "id": ep["global_ep"],
                "key": f"{ep['global_ep']:02d}",
                "season": ep["season"],
                "epInSeason": ep["ep_in_season"],
                "label": f"Kausi {ep['season']}, Jakso {ep['ep_in_season']:02d}",
                "date": ep["date_fi"],
                "duration": ep["duration"],
                "desc": ep["intro_desc"],
                "audio": ep["audio_url"],
                "chaptersRel": ep["chapters_rel"],
                "vttRel": ep.get("vtt_rel"),
                "chapters": chs_js,
                "highlights": hls,
            }
        )

    cover_html = (
        '<img class="cover" src="cover.jpg" alt="Rane Raitsikka ja Tokela" />'
        if has_cover
        else ""
    )

    songs_fmt = f"{total_songs:,}".replace(",", "\u00a0")
    episodes_json = json_for_script(episodes_js)
    highlights_json = json_for_script(all_highlights_js)
    subscribe_json = json_for_script(subscribe_links)
    season_labels_json = json_for_script(season_labels_clean)

    return f"""<!DOCTYPE html>
<html lang="fi">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Rane Raitsikka ja Tokela – Podcast-arkisto</title>
  <meta name="description" content="Radio Helsingin Rane Raitsikka ja Tokela -ohjelman jaksoarkisto (2017–2018) sisällysluettelolla, aikaleimoilla ja äänisoittimella." />
  <style>
    :root {{
      --bg: #F6F3EC;
      --surface: #EFECE3;
      --ink: #141413;
      --muted: #57554E;
      --line: #D8D3C7;
      --accent: #0B5D63;
      --accent-hover: #08484D;
      --accent-soft: rgba(11, 93, 99, 0.08);
    }}
    * {{ box-sizing: border-box; }}
    html {{ background: var(--bg); color: var(--ink); }}
    body {{
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Helvetica Neue", "Segoe UI", Inter, sans-serif;
      line-height: 1.55;
      -webkit-font-smoothing: antialiased;
      padding-bottom: 96px;
    }}
    a {{ color: var(--accent); text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    .wrap {{ max-width: 920px; margin: 0 auto; padding: 28px 24px 72px; }}

    .masthead {{
      display: flex;
      gap: 24px;
      align-items: center;
      padding-bottom: 24px;
      border-bottom: 2px solid var(--ink);
    }}
    .cover {{
      width: 120px;
      height: 120px;
      object-fit: cover;
      flex-shrink: 0;
      display: block;
      border: 1px solid var(--line);
    }}
    .masthead-body {{ flex: 1; min-width: 0; }}
    .eyebrow {{
      font-size: 0.74rem;
      font-weight: 700;
      letter-spacing: 0.1em;
      text-transform: uppercase;
      color: var(--accent);
      margin-bottom: 4px;
    }}
    h1 {{
      margin: 0 0 6px;
      font-size: clamp(1.65rem, 3.2vw, 2.35rem);
      font-weight: 800;
      letter-spacing: -0.03em;
      line-height: 1.08;
    }}
    .lead {{
      margin: 0 0 14px;
      font-size: 0.94rem;
      color: var(--muted);
    }}
    .cta-row {{
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 16px;
    }}
    .sub-wrap {{ position: relative; }}
    .btn-primary {{
      background: var(--accent);
      color: #fff;
      border: 1px solid var(--accent);
      padding: 8px 16px;
      font-size: 0.88rem;
      font-weight: 600;
      cursor: pointer;
      letter-spacing: 0.01em;
    }}
    .btn-primary:hover {{ background: var(--accent-hover); }}
    .btn-text {{
      background: none;
      border: none;
      color: var(--ink);
      font-size: 0.88rem;
      font-weight: 600;
      cursor: pointer;
      padding: 6px 0;
      border-bottom: 1px solid var(--line);
    }}
    .btn-text:hover {{ color: var(--accent); border-bottom-color: var(--accent); }}

    .sub-pop {{
      display: none;
      position: absolute;
      top: calc(100% + 8px);
      left: 0;
      width: 288px;
      background: #FCFBF8;
      border: 1px solid var(--ink);
      padding: 14px;
      z-index: 50;
    }}
    .sub-pop.open {{ display: block; }}
    .sub-copy {{
      width: 100%;
      background: var(--ink);
      color: #fff;
      border: none;
      padding: 9px 12px;
      font-size: 0.85rem;
      font-weight: 600;
      cursor: pointer;
      margin-bottom: 10px;
    }}
    .sub-copy:hover {{ background: var(--accent); }}
    .sub-apps {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 6px;
      margin-bottom: 10px;
    }}
    .sub-apps a {{
      text-decoration: none;
      color: var(--ink);
      border: 1px solid var(--line);
      padding: 6px 8px;
      font-size: 0.81rem;
      font-weight: 500;
      text-align: center;
    }}
    .sub-apps a:hover {{ border-color: var(--accent); color: var(--accent); background: var(--accent-soft); }}
    .sub-note {{ font-size: 0.75rem; color: var(--muted); line-height: 1.4; }}

    .toolbar {{
      position: sticky;
      top: 0;
      z-index: 20;
      background: rgba(246, 243, 236, 0.96);
      backdrop-filter: blur(8px);
      padding: 14px 0 10px;
      border-bottom: 1px solid var(--line);
    }}
    .toolbar-row {{
      display: flex;
      gap: 16px;
      align-items: center;
      flex-wrap: wrap;
    }}
    .search-input {{
      flex: 1;
      min-width: 220px;
      background: transparent;
      border: none;
      border-bottom: 1px solid var(--ink);
      padding: 7px 2px;
      font-size: 0.96rem;
      color: var(--ink);
      outline: none;
    }}
    .search-input::placeholder {{ color: #7C786E; }}
    .search-input:focus {{ border-bottom: 2px solid var(--accent); padding-bottom: 6px; }}
    .season-tabs {{ display: flex; gap: 4px; }}
    .s-tab {{
      background: none;
      border: none;
      border-bottom: 2px solid transparent;
      padding: 5px 10px;
      font-size: 0.84rem;
      font-weight: 600;
      color: var(--muted);
      cursor: pointer;
    }}
    .s-tab:hover {{ color: var(--ink); }}
    .s-tab.active {{ color: var(--accent); border-bottom-color: var(--accent); }}
    .quick-picks {{
      margin-top: 8px;
      font-size: 0.79rem;
      color: var(--muted);
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      align-items: center;
    }}
    .quick-picks button {{
      background: none;
      border: none;
      padding: 0;
      font-size: 0.79rem;
      color: var(--muted);
      cursor: pointer;
      text-decoration: underline;
      text-decoration-color: var(--line);
      text-underline-offset: 3px;
    }}
    .quick-picks button:hover {{ color: var(--accent); text-decoration-color: var(--accent); }}

    .season-heading {{
      margin: 34px 0 0;
      padding-bottom: 8px;
      border-bottom: 1px solid var(--ink);
      font-size: 0.78rem;
      font-weight: 700;
      letter-spacing: 0.1em;
      text-transform: uppercase;
      color: var(--muted);
    }}
    .ep-row {{
      padding: 18px 0;
      border-bottom: 1px solid var(--line);
      display: grid;
      grid-template-columns: 36px 1fr auto;
      gap: 4px 16px;
      align-items: start;
    }}
    .ep-play {{
      width: 34px;
      height: 34px;
      padding: 0;
      border-radius: 50%;
      border: 1px solid var(--ink);
      background: transparent;
      color: var(--ink);
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      margin-top: 2px;
    }}
    .ep-play svg, .p-play svg {{ display: block; fill: currentColor; }}
    .icon-play {{ transform: translateX(1px); }}
    .ep-play:hover {{ background: var(--accent); border-color: var(--accent); color: #fff; }}
    .ep-meta {{
      font-size: 0.8rem;
      color: var(--muted);
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      margin-bottom: 3px;
    }}
    .ep-meta strong {{ color: var(--ink); font-family: inherit; font-weight: 700; margin-right: 8px; }}
    .ep-desc {{
      margin: 0 0 10px;
      font-size: 0.94rem;
      color: var(--ink);
      display: -webkit-box;
      -webkit-line-clamp: 3;
      -webkit-box-orient: vertical;
      overflow: hidden;
    }}
    .ep-highlights {{
      display: flex;
      flex-direction: column;
      gap: 4px;
      margin: 0;
      padding: 0;
      list-style: none;
    }}
    .hl-btn {{
      background: none;
      border: none;
      padding: 2px 0;
      text-align: left;
      cursor: pointer;
      font-size: 0.84rem;
      color: var(--muted);
      display: flex;
      gap: 8px;
      align-items: baseline;
    }}
    .hl-btn:hover {{ color: var(--accent); }}
    .hl-ts {{
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      font-size: 0.78rem;
      color: var(--accent);
      flex-shrink: 0;
    }}
    .ep-setlist-btn {{
      background: none;
      border: 1px solid var(--line);
      padding: 6px 12px;
      font-size: 0.8rem;
      font-weight: 600;
      color: var(--ink);
      cursor: pointer;
      white-space: nowrap;
      margin-top: 2px;
    }}
    .ep-setlist-btn:hover, .ep-setlist-btn.active {{ border-color: var(--accent); color: var(--accent); background: var(--accent-soft); }}

    .search-hits {{ margin-top: 10px; padding-left: 0; list-style: none; display: flex; flex-direction: column; gap: 4px; }}
    .hit-btn {{
      background: var(--accent-soft);
      border: none;
      border-left: 2px solid var(--accent);
      padding: 5px 10px;
      text-align: left;
      cursor: pointer;
      font-size: 0.84rem;
      color: var(--ink);
      display: flex;
      gap: 10px;
      width: 100%;
    }}
    .hit-btn:hover {{ background: rgba(11, 93, 99, 0.14); }}

    .drawer-backdrop {{
      display: none;
      position: fixed;
      inset: 0;
      background: rgba(20, 20, 19, 0.24);
      z-index: 60;
    }}
    .drawer-backdrop.open {{ display: block; }}
    .drawer {{
      position: fixed;
      top: 0;
      right: 0;
      bottom: 0;
      width: min(520px, 100vw);
      background: var(--bg);
      border-left: 1px solid var(--ink);
      z-index: 70;
      transform: translateX(100%);
      transition: transform 0.2s ease;
      display: flex;
      flex-direction: column;
    }}
    .drawer.open {{ transform: translateX(0); }}
    .drawer-head {{
      padding: 18px 22px 14px;
      border-bottom: 1px solid var(--line);
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 12px;
    }}
    .drawer-title {{ margin: 0 0 4px; font-size: 1.15rem; font-weight: 800; letter-spacing: -0.02em; }}
    .drawer-sub {{ font-size: 0.82rem; color: var(--muted); font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }}
    .drawer-close {{
      background: none;
      border: 1px solid var(--line);
      padding: 4px 10px;
      cursor: pointer;
      font-size: 0.85rem;
      color: var(--ink);
    }}
    .drawer-close:hover {{ border-color: var(--ink); }}
    .drawer-body {{
      overflow-y: auto;
      padding: 18px 22px 110px;
      flex: 1;
    }}
    .drawer-desc {{ margin: 0 0 16px; font-size: 0.92rem; color: var(--ink); }}
    .drawer-actions {{
      margin-bottom: 14px;
      display: flex;
      flex-wrap: wrap;
      gap: 12px;
      align-items: center;
    }}
    .drawer-meta-links {{
      display: flex;
      gap: 12px;
      font-size: 0.78rem;
      color: var(--muted);
    }}
    .drawer-meta-links a {{ color: var(--muted); text-decoration: underline; text-decoration-color: var(--line); text-underline-offset: 3px; }}
    .drawer-meta-links a:hover {{ color: var(--accent); }}
    .drawer-section-label {{
      font-size: 0.72rem;
      font-weight: 700;
      letter-spacing: 0.1em;
      text-transform: uppercase;
      color: var(--muted);
      margin: 20px 0 8px;
      display: flex;
      justify-content: space-between;
      align-items: center;
    }}
    .filter-inline {{ display: flex; gap: 12px; }}
    .filter-inline button {{
      background: none;
      border: none;
      padding: 0;
      font-size: 0.76rem;
      font-weight: 600;
      color: var(--muted);
      cursor: pointer;
      text-transform: none;
      letter-spacing: 0;
    }}
    .filter-inline button.active {{ color: var(--accent); text-decoration: underline; text-underline-offset: 3px; }}
    .ch-item {{
      width: 100%;
      background: none;
      border: none;
      border-bottom: 1px solid var(--line);
      padding: 8px 4px;
      text-align: left;
      cursor: pointer;
      display: flex;
      gap: 10px;
      align-items: baseline;
      font-size: 0.86rem;
      color: var(--ink);
    }}
    .ch-item:hover {{ background: var(--accent-soft); }}
    .ch-item.is-playing {{ background: var(--accent-soft); font-weight: 600; }}
    .ch-ts {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 0.78rem; color: var(--muted); flex-shrink: 0; }}
    .ch-music .ch-ts, .ch-music .ch-label {{ color: var(--accent); }}
    .hl-card {{
      padding: 10px 0;
      border-bottom: 1px solid var(--line);
    }}
    .hl-card button {{
      background: none; border: none; padding: 0; text-align: left; cursor: pointer; font-weight: 700; font-size: 0.88rem; color: var(--ink);
    }}
    .hl-card button:hover {{ color: var(--accent); }}
    .hl-card p {{ margin: 4px 0 0; font-size: 0.83rem; color: var(--muted); }}

    footer {{
      margin-top: 56px;
      padding-top: 20px;
      border-top: 1px solid var(--line);
      font-size: 0.8rem;
      color: var(--muted);
      display: flex;
      flex-wrap: wrap;
      justify-content: space-between;
      gap: 10px;
    }}

    .player-bar {{
      position: fixed;
      left: 0; right: 0; bottom: 0;
      background: #141413;
      color: #F6F3EC;
      z-index: 90;
      transform: translateY(110%);
      transition: transform 0.2s ease;
      border-top: 2px solid var(--accent);
    }}
    .player-bar.visible {{ transform: translateY(0); }}
    .player-inner {{
      max-width: 960px;
      margin: 0 auto;
      padding: 10px 18px 6px;
      display: flex;
      align-items: center;
      gap: 12px;
    }}
    .p-play {{
      width: 34px; height: 34px; padding: 0; border-radius: 50%;
      background: #F6F3EC; color: #141413; border: none; cursor: pointer; flex-shrink: 0;
      display: inline-flex; align-items: center; justify-content: center;
    }}
    .p-meta {{ flex: 1; min-width: 0; cursor: pointer; }}
    .p-ep {{ font-size: 0.72rem; color: #A6A298; }}
    .p-title {{ font-size: 0.86rem; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
    .p-controls {{ display: flex; gap: 4px; flex-wrap: nowrap; }}
    .p-btn, .p-close {{
      background: none; border: 1px solid #353430; color: #F6F3EC;
      padding: 4px 7px; font-size: 0.75rem; cursor: pointer;
    }}
    .p-btn:hover, .p-close:hover {{ border-color: #F6F3EC; }}
    .p-time {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 0.75rem; color: #A6A298; white-space: nowrap; }}
    .p-progress {{ width: 100%; display: block; accent-color: #0B5D63; margin: 0; height: 4px; cursor: pointer; }}

    @media (max-width: 640px) {{
      .wrap {{ padding: 18px 16px 88px; }}
      .masthead {{ gap: 16px; align-items: flex-start; }}
      .cover {{ width: 84px; height: 84px; }}
      .ep-row {{ grid-template-columns: 34px 1fr; }}
      .ep-setlist-btn {{ grid-column: 2; justify-self: start; margin-top: 8px; }}
      .p-time, #pSpeedBtn, #pReroll {{ display: none; }}
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <header class="masthead">
      {cover_html}
      <div class="masthead-body">
        <div class="eyebrow">Radio Helsinki · Keskiviikkoillat 2017–2018</div>
        <h1>Rane Raitsikka ja Tokela</h1>
        <p class="lead">{len(published)} jaksoa · {total_hours} h · {songs_fmt} kappaletta. Ääni toistetaan suoraan <a href="{SHOW_LINK}" target="_blank" rel="noopener">Radio Helsingin arkistosta</a>. Radiokanavasta riippumaton harrasteprojekti.</p>
        <div class="cta-row">
          <div class="sub-wrap">
            <button class="btn-primary" id="subBtn" type="button" aria-expanded="false">Tilaa podcast ▾</button>
            <div class="sub-pop" id="subPop" role="dialog" aria-label="Tilaa podcast">
              <button class="sub-copy" id="copyRssBtn" type="button">Kopioi RSS-osoite</button>
              <div class="sub-apps">
                <a href="{html_lib.escape(subscribe_links['apple'])}">Apple Podcasts</a>
                <a href="{html_lib.escape(subscribe_links['pocketcasts'])}">Pocket Casts</a>
                <a href="{html_lib.escape(subscribe_links['overcast'])}">Overcast</a>
                <a href="{html_lib.escape(subscribe_links['antennapod'])}" target="_blank" rel="noopener">AntennaPod</a>
                <a href="{html_lib.escape(subscribe_links['podcastaddict'])}">Podcast Addict</a>
                <a href="feed.xml">Avaa feed.xml</a>
              </div>
              <div class="sub-note">RSS-osoitteen voi liittää myös esim. YouTube Musiciin (Kirjasto → Podcastit → Lisää RSS-syötteen avulla) tai Castroon. Spotify ei tue suoria RSS-syötteitä.</div>
            </div>
          </div>
          <button class="btn-text" id="randomBtn" type="button">▶ Satunnainen tarina</button>
        </div>
      </div>
    </header>

    <div class="toolbar">
      <div class="toolbar-row">
        <input class="search-input" id="searchInput" type="search" placeholder="Hae biisiä, bändiä, paikkaa tai tarinaa..." />
        <div class="season-tabs" role="tablist">
          <button class="s-tab active" data-season="all" type="button">Kaikki</button>
          <button class="s-tab" data-season="1" type="button">K1</button>
          <button class="s-tab" data-season="2" type="button">K2</button>
          <button class="s-tab" data-season="3" type="button">K3</button>
        </div>
      </div>
      <div class="quick-picks" id="quickPicks">
        <span>Kokeile:</span>
        <button type="button" data-q="Lepakko">Lepakko</button>
        <button type="button" data-q="The Rolling Stones">The Rolling Stones</button>
        <button type="button" data-q="Hollywood">Hollywood</button>
        <button type="button" data-q="The Meters">The Meters</button>
        <button type="button" data-q="Lätkä">Lätkä</button>
      </div>
    </div>

    <main id="episodeList"></main>

    <footer>
      <span>Radiokanavasta riippumaton harrasteprojekti · Alkuperäinen ohjelma: <a href="{SHOW_LINK}" target="_blank" rel="noopener">Radio Helsinki – Rane Raitsikka &amp; Tokela</a></span>
      <span><a href="feed.xml">feed.xml</a></span>
    </footer>
  </div>

  <div class="drawer-backdrop" id="drawerBackdrop"></div>
  <aside class="drawer" id="drawer" aria-label="Jakson settilista">
    <div class="drawer-head">
      <div>
        <h2 class="drawer-title" id="drawerTitle"></h2>
        <div class="drawer-sub" id="drawerSub"></div>
      </div>
      <button class="drawer-close" id="drawerClose" type="button">Sulje ✕</button>
    </div>
    <div class="drawer-body" id="drawerBody"></div>
  </aside>

  <div class="player-bar" id="playerBar" role="region" aria-label="Äänisoitin">
    <audio id="globalAudio" preload="none"></audio>
    <div class="player-inner">
      <button class="p-play" id="pPlayBtn" type="button" aria-label="Toista tai tauota"><svg class="icon-play" width="11" height="12" viewBox="0 0 11 12" aria-hidden="true"><path d="M1 1l9 5-9 5z"/></svg></button>
      <div class="p-meta" id="pMeta" title="Avaa soivan jakson settilista">
        <div class="p-ep" id="pEp"></div>
        <div class="p-title" id="pTitle"></div>
      </div>
      <div class="p-controls">
        <button class="p-btn" id="pPrevBtn" type="button" title="Edellinen luku">⏮</button>
        <button class="p-btn" id="pBack15" type="button" title="-15s">-15s</button>
        <button class="p-btn" id="pFwd30" type="button" title="+30s">+30s</button>
        <button class="p-btn" id="pNextBtn" type="button" title="Seuraava luku">⏭</button>
        <button class="p-btn" id="pSpeedBtn" type="button" title="Toistonopeus">1×</button>
        <button class="p-btn" id="pReroll" type="button" title="Arvo toinen tarina">↻ Arvo</button>
      </div>
      <div class="p-time" id="pTime">00:00:00</div>
      <button class="p-close" id="pCloseBtn" type="button" aria-label="Sulje soitin">✕</button>
    </div>
    <input class="p-progress" id="pProgress" type="range" min="0" max="1000" value="0" aria-label="Toistokohta" />
  </div>

  <script>
    const EPISODES = {episodes_json};
    const ALL_HIGHLIGHTS = {highlights_json};
    const SUBSCRIBE = {subscribe_json};
    const SEASON_LABELS = {season_labels_json};

    let currentSeason = 'all';
    let currentQuery = '';
    let openDrawerEpId = null;
    let drawerFilter = 'all';

    const listEl = document.getElementById('episodeList');
    const searchInput = document.getElementById('searchInput');
    const quickPicks = document.getElementById('quickPicks');
    const drawer = document.getElementById('drawer');
    const drawerBackdrop = document.getElementById('drawerBackdrop');
    const drawerTitle = document.getElementById('drawerTitle');
    const drawerSub = document.getElementById('drawerSub');
    const drawerBody = document.getElementById('drawerBody');

    const audioEl = document.getElementById('globalAudio');
    const playerBar = document.getElementById('playerBar');
    const pEp = document.getElementById('pEp');
    const pTitle = document.getElementById('pTitle');
    const pTime = document.getElementById('pTime');
    const pPlayBtn = document.getElementById('pPlayBtn');
    const pProgress = document.getElementById('pProgress');
    const pSpeedBtn = document.getElementById('pSpeedBtn');
    const SPEEDS = [1, 1.25, 1.5];
    let speedIdx = 0;
    let activeEp = null;
    let activeChapterIdx = -1;
    let pendingSeekSec = null;

    function esc(s) {{
      return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
    }}

    function fmtTime(sec) {{
      sec = Math.max(0, Math.floor(sec || 0));
      const h = Math.floor(sec / 3600);
      const m = Math.floor((sec % 3600) / 60);
      const s = sec % 60;
      return `${{String(h).padStart(2,'0')}}:${{String(m).padStart(2,'0')}}:${{String(s).padStart(2,'0')}}`;
    }}

    function playAt(epId, startSec, customLabel) {{
      const ep = EPISODES.find(e => e.id === epId);
      if (!ep) return;
      const targetSec = Math.max(0, Number(startSec) || 0);
      const sameEp = activeEp && activeEp.id === ep.id && audioEl.src === ep.audio;
      activeEp = ep;
      playerBar.classList.add('visible');
      pTime.textContent = `${{fmtTime(targetSec)}} / ${{ep.duration}} · ladataan…`;
      updatePlayerChapter(targetSec, customLabel);

      if (sameEp && audioEl.readyState >= 1) {{
        try {{ audioEl.currentTime = targetSec; }} catch(e) {{}}
        audioEl.playbackRate = SPEEDS[speedIdx];
        audioEl.play().catch(() => {{}});
      }} else {{
        pendingSeekSec = targetSec;
        audioEl.src = ep.audio;
        audioEl.load();
        audioEl.play().catch(() => {{}});
      }}

      if ('mediaSession' in navigator) {{
        try {{
          navigator.mediaSession.metadata = new MediaMetadata({{
            title: customLabel || ep.label,
            artist: 'Rane Raitsikka ja Tokela',
            album: `${{ep.label}} (${{ep.date}})`,
            artwork: [{{ src: 'cover.jpg', sizes: '512x512', type: 'image/jpeg' }}]
          }});
          navigator.mediaSession.setActionHandler('previoustrack', () => jumpChapter(-1));
          navigator.mediaSession.setActionHandler('nexttrack', () => jumpChapter(1));
          navigator.mediaSession.setActionHandler('seekbackward', () => skipSeconds(-15));
          navigator.mediaSession.setActionHandler('seekforward', () => skipSeconds(30));
        }} catch(e) {{}}
      }}
    }}

    function updatePlayerChapter(curSec, overrideTitle) {{
      if (!activeEp) return;
      const nonAds = activeEp.chapters.filter(c => c.kind !== 'ad');
      let foundIdx = 0;
      for (let i = 0; i < nonAds.length; i++) {{
        if (nonAds[i].t <= curSec + 2) foundIdx = i;
      }}
      activeChapterIdx = foundIdx;
      const ch = nonAds[foundIdx];
      pEp.textContent = `${{activeEp.label}} (${{activeEp.date}})`;
      pTitle.textContent = overrideTitle || (ch ? ch.title : activeEp.label);
      highlightPlayingRow();
    }}

    function highlightPlayingRow() {{
      document.querySelectorAll('.is-playing').forEach(el => el.classList.remove('is-playing'));
      if (!activeEp) return;
      const nonAds = activeEp.chapters.filter(c => c.kind !== 'ad');
      const ch = nonAds[activeChapterIdx];
      if (ch) {{
        document.querySelectorAll(`[data-play-ep="${{activeEp.id}}"][data-play-sec="${{ch.t}}"]`).forEach(el => el.classList.add('is-playing'));
      }}
    }}

    function jumpChapter(delta) {{
      if (!activeEp) return;
      const nonAds = activeEp.chapters.filter(c => c.kind !== 'ad');
      const next = Math.max(0, Math.min(nonAds.length - 1, activeChapterIdx + delta));
      const ch = nonAds[next];
      if (ch) playAt(activeEp.id, ch.t, ch.title);
    }}

    function skipSeconds(delta) {{
      if (!activeEp) return;
      audioEl.currentTime = Math.max(0, (audioEl.currentTime || 0) + delta);
    }}

    function playRandomStory() {{
      if (!ALL_HIGHLIGHTS.length) return;
      const pick = ALL_HIGHLIGHTS[Math.floor(Math.random() * ALL_HIGHLIGHTS.length)];
      playAt(pick.epId, pick.t, pick.title);
    }}

    audioEl.addEventListener('loadedmetadata', () => {{
      if (pendingSeekSec !== null) {{
        const s = pendingSeekSec;
        pendingSeekSec = null;
        try {{ audioEl.currentTime = s; }} catch(e) {{}}
        audioEl.playbackRate = SPEEDS[speedIdx];
        audioEl.play().catch(() => {{}});
      }}
    }});
    audioEl.addEventListener('waiting', () => {{
      if (activeEp) pTime.textContent = `${{fmtTime(audioEl.currentTime)}} / ${{activeEp.duration}} · puskuroi…`;
    }});
    const ICON_PLAY = '<svg class="icon-play" width="11" height="12" viewBox="0 0 11 12" aria-hidden="true"><path d="M1 1l9 5-9 5z"/></svg>';
    const ICON_PAUSE = '<svg width="10" height="12" viewBox="0 0 10 12" aria-hidden="true"><rect x="1" y="1" width="3" height="10"/><rect x="6" y="1" width="3" height="10"/></svg>';
    audioEl.addEventListener('play', () => {{ pPlayBtn.innerHTML = ICON_PAUSE; }});
    audioEl.addEventListener('pause', () => {{ pPlayBtn.innerHTML = ICON_PLAY; }});
    audioEl.addEventListener('timeupdate', () => {{
      const cur = audioEl.currentTime || 0;
      const dur = audioEl.duration || 0;
      pTime.textContent = `${{fmtTime(cur)}} / ${{activeEp ? activeEp.duration : fmtTime(dur)}}`;
      if (dur > 0) pProgress.value = Math.round((cur / dur) * 1000);
      if (activeEp) {{
        const nonAds = activeEp.chapters.filter(c => c.kind !== 'ad');
        let idx = 0;
        for (let i = 0; i < nonAds.length; i++) {{
          if (nonAds[i].t <= cur + 1) idx = i;
        }}
        if (idx !== activeChapterIdx) {{
          activeChapterIdx = idx;
          if (nonAds[idx]) pTitle.textContent = nonAds[idx].title;
          highlightPlayingRow();
        }}
      }}
    }});

    pPlayBtn.addEventListener('click', () => {{
      if (audioEl.paused) audioEl.play().catch(() => {{}});
      else audioEl.pause();
    }});
    pProgress.addEventListener('input', () => {{
      if (audioEl.duration) audioEl.currentTime = (pProgress.value / 1000) * audioEl.duration;
    }});
    document.getElementById('pPrevBtn').addEventListener('click', () => jumpChapter(-1));
    document.getElementById('pNextBtn').addEventListener('click', () => jumpChapter(1));
    document.getElementById('pBack15').addEventListener('click', () => skipSeconds(-15));
    document.getElementById('pFwd30').addEventListener('click', () => skipSeconds(30));
    pSpeedBtn.addEventListener('click', () => {{
      speedIdx = (speedIdx + 1) % SPEEDS.length;
      audioEl.playbackRate = SPEEDS[speedIdx];
      pSpeedBtn.textContent = `${{SPEEDS[speedIdx]}}×`;
    }});
    document.getElementById('pReroll').addEventListener('click', playRandomStory);
    document.getElementById('pMeta').addEventListener('click', () => {{
      if (activeEp) openDrawer(activeEp.id);
    }});
    document.getElementById('pCloseBtn').addEventListener('click', () => {{
      audioEl.pause();
      playerBar.classList.remove('visible');
    }});

    const subBtn = document.getElementById('subBtn');
    const subPop = document.getElementById('subPop');
    const copyRssBtn = document.getElementById('copyRssBtn');
    subBtn.addEventListener('click', (e) => {{
      e.stopPropagation();
      const open = subPop.classList.toggle('open');
      subBtn.setAttribute('aria-expanded', open ? 'true' : 'false');
    }});
    document.addEventListener('click', (e) => {{
      if (!subPop.contains(e.target) && e.target !== subBtn) {{
        subPop.classList.remove('open');
        subBtn.setAttribute('aria-expanded', 'false');
      }}
    }});
    copyRssBtn.addEventListener('click', async () => {{
      try {{
        await navigator.clipboard.writeText(SUBSCRIBE.rss);
        copyRssBtn.textContent = 'RSS-osoite kopioitu!';
        setTimeout(() => copyRssBtn.textContent = 'Kopioi RSS-osoite', 1800);
      }} catch {{
        prompt('Kopioi RSS-osoite:', SUBSCRIBE.rss);
      }}
    }});

    function renderList() {{
      const q = currentQuery.trim().toLowerCase();
      quickPicks.style.display = q ? 'none' : 'flex';
      const filtered = EPISODES.filter(ep => currentSeason === 'all' || String(ep.season) === currentSeason);
      const bySeason = new Map();

      for (const ep of filtered) {{
        let hits = [];
        if (q) {{
          const descMatch = ep.desc.toLowerCase().includes(q) || ep.label.toLowerCase().includes(q) || ep.date.includes(q);
          for (const ch of ep.chapters) {{
            if (ch.kind !== 'ad' && ch.title.toLowerCase().includes(q)) {{
              hits.push({{ ts: ch.ts, t: ch.t, title: ch.title }});
            }}
          }}
          for (const hl of ep.highlights) {{
            if ((hl.title.toLowerCase().includes(q) || hl.body.toLowerCase().includes(q)) && !hits.some(h => h.t === hl.t)) {{
              hits.push({{ ts: hl.ts, t: hl.t, title: hl.title }});
            }}
          }}
          if (!descMatch && hits.length === 0) continue;
        }}
        if (!bySeason.has(ep.season)) bySeason.set(ep.season, []);
        bySeason.get(ep.season).push({{ ep, hits }});
      }}

      if (bySeason.size === 0) {{
        listEl.innerHTML = '<p style="margin-top:32px;color:var(--muted);">Ei osumia haulle.</p>';
        return;
      }}

      let out = '';
      for (const [s, items] of bySeason.entries()) {{
        out += `<h2 class="season-heading">${{esc(SEASON_LABELS[s])}}</h2>`;
        for (const {{ ep, hits }} of items) {{
          const top2 = ep.highlights.slice(0, 2);
          const hlHtml = q && hits.length
            ? `<ul class="search-hits">${{hits.slice(0, 5).map(h => `<li><button class="hit-btn" type="button" data-play-ep="${{ep.id}}" data-play-sec="${{h.t}}"><span class="hl-ts">${{h.ts}}</span><span>${{esc(h.title)}}</span></button></li>`).join('')}}</ul>`
            : `<ul class="ep-highlights">${{top2.map(h => `<li><button class="hl-btn" type="button" data-play-ep="${{ep.id}}" data-play-sec="${{h.t}}"><span class="hl-ts">${{h.ts}}</span><span>${{esc(h.title)}}</span></button></li>`).join('')}}</ul>`;
          out += `
            <article class="ep-row">
              <button class="ep-play" type="button" data-play-ep="${{ep.id}}" data-play-sec="0" title="Kuuntele jakso" aria-label="Kuuntele jakso">${{ICON_PLAY}}</button>
              <div>
                <div class="ep-meta"><strong>${{esc(ep.label)}}</strong>${{esc(ep.date)}} · ${{esc(ep.duration)}}</div>
                <p class="ep-desc">${{esc(ep.desc)}}</p>
                ${{hlHtml}}
              </div>
              <button class="ep-setlist-btn ${{openDrawerEpId === ep.id ? 'active' : ''}}" type="button" data-open-drawer="${{ep.id}}">Settilista</button>
            </article>
          `;
        }}
      }}
      listEl.innerHTML = out;
    }}

    function openDrawer(epId, pushState = true) {{
      const ep = EPISODES.find(e => e.id === epId);
      if (!ep) return;
      openDrawerEpId = ep.id;
      drawerTitle.textContent = ep.label;
      drawerSub.textContent = `${{ep.date}} · ${{ep.duration}}`;
      renderDrawerBody(ep);
      drawer.classList.add('open');
      drawerBackdrop.classList.add('open');
      document.querySelectorAll('.ep-setlist-btn').forEach(b => b.classList.toggle('active', Number(b.dataset.openDrawer) === epId));
      if (pushState && history.pushState) {{
        history.pushState({{ epId }}, '', '#ep-' + ep.key);
      }}
    }}

    function renderDrawerBody(ep) {{
      const nonAds = ep.chapters.filter(c => c.kind !== 'ad');
      const visibleChs = nonAds.filter(c => drawerFilter === 'all' || c.kind === drawerFilter);
      const hlsHtml = ep.highlights.map(h => `
        <div class="hl-card">
          <button type="button" data-play-ep="${{ep.id}}" data-play-sec="${{h.t}}"><span class="hl-ts">${{h.ts}}</span> ${{esc(h.title)}}</button>
          <p>${{esc(h.body)}}</p>
        </div>
      `).join('');
      const chsHtml = visibleChs.map(c => `
        <button class="ch-item ch-${{c.kind}}" type="button" data-play-ep="${{ep.id}}" data-play-sec="${{c.t}}">
          <span class="ch-ts">${{c.ts}}</span>
          <span class="ch-label">${{c.kind === 'music' ? '♪ ' : ''}}${{esc(c.title.replace(/^(Intro ja )?Musiikki:\\s*/i, ''))}}</span>
        </button>
      `).join('');
      const vttLink = ep.vttRel ? `<a href="${{ep.vttRel}}" target="_blank" rel="noopener">Tekstitys (.vtt)</a>` : '';
      drawerBody.innerHTML = `
        <p class="drawer-desc">${{esc(ep.desc)}}</p>
        <div class="drawer-actions">
          <button class="btn-primary" type="button" data-play-ep="${{ep.id}}" data-play-sec="0">▶ Kuuntele alusta</button>
          <div class="drawer-meta-links">
            <a href="${{ep.audio}}" target="_blank" rel="noopener">MP3</a>
            ${{vttLink}}
            <a href="${{ep.chaptersRel}}" target="_blank" rel="noopener">Chapters JSON</a>
          </div>
        </div>
        <div class="drawer-section-label">
          <span>Settilista (${{visibleChs.length}})</span>
          <div class="filter-inline">
            <button type="button" data-dfilter="all" class="${{drawerFilter==='all'?'active':''}}">Kaikki</button>
            <button type="button" data-dfilter="music" class="${{drawerFilter==='music'?'active':''}}">Biisit</button>
            <button type="button" data-dfilter="talk" class="${{drawerFilter==='talk'?'active':''}}">Puheet</button>
          </div>
        </div>
        <div>${{chsHtml}}</div>
        ${{ep.highlights.length ? `<div class="drawer-section-label"><span>Jakson parhaat nostot</span></div><div>${{hlsHtml}}</div>` : ''}}
      `;
      highlightPlayingRow();
    }}

    function closeDrawer() {{
      openDrawerEpId = null;
      drawer.classList.remove('open');
      drawerBackdrop.classList.remove('open');
      document.querySelectorAll('.ep-setlist-btn.active').forEach(b => b.classList.remove('active'));
    }}

    document.getElementById('drawerClose').addEventListener('click', closeDrawer);
    drawerBackdrop.addEventListener('click', closeDrawer);
    window.addEventListener('popstate', () => {{ if (openDrawerEpId) closeDrawer(); }});
    document.addEventListener('keydown', (e) => {{
      if (e.key === 'Escape') {{
        closeDrawer();
        subPop.classList.remove('open');
        subBtn.setAttribute('aria-expanded', 'false');
      }}
    }});

    document.addEventListener('click', (e) => {{
      const playEl = e.target.closest('[data-play-ep]');
      if (playEl) {{
        playAt(Number(playEl.dataset.playEp), Number(playEl.dataset.playSec || 0));
        return;
      }}
      const drawerBtn = e.target.closest('[data-open-drawer]');
      if (drawerBtn) {{
        openDrawer(Number(drawerBtn.dataset.openDrawer));
        return;
      }}
      const dFilter = e.target.closest('[data-dfilter]');
      if (dFilter && openDrawerEpId) {{
        drawerFilter = dFilter.dataset.dfilter;
        const ep = EPISODES.find(x => x.id === openDrawerEpId);
        if (ep) renderDrawerBody(ep);
        return;
      }}
      const qBtn = e.target.closest('[data-q]');
      if (qBtn) {{
        searchInput.value = qBtn.dataset.q;
        currentQuery = qBtn.dataset.q;
        renderList();
      }}
    }});

    document.querySelectorAll('.s-tab').forEach(btn => {{
      btn.addEventListener('click', () => {{
        document.querySelectorAll('.s-tab').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        currentSeason = btn.dataset.season;
        renderList();
      }});
    }});
    searchInput.addEventListener('input', () => {{
      currentQuery = searchInput.value;
      renderList();
    }});
    document.getElementById('randomBtn').addEventListener('click', playRandomStory);

    renderList();
    if (location.hash && /^#ep-\\d{{2}}$/.test(location.hash)) {{
      const targetId = parseInt(location.hash.slice(4), 10);
      if (EPISODES.some(e => e.id === targetId)) openDrawer(targetId, false);
    }}
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
                "highlights": load_analysis_highlights(global_ep),
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

    last_build_date = format_datetime(datetime.now(HELSINKI_TZ))
    channel_pub_date = format_datetime(items_data[0][0]) if items_data else last_build_date

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
        f"    <pubDate>{channel_pub_date}</pubDate>\n"
        f"    <lastBuildDate>{last_build_date}</lastBuildDate>\n"
        "    <ttl>60</ttl>\n"
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
