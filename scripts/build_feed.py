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
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />
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
    html {{
      background: var(--bg);
      color: var(--ink);
      overflow-x: clip;
      overscroll-behavior-x: none;
      max-width: 100%;
    }}
    body {{
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Helvetica Neue", "Segoe UI", Inter, sans-serif;
      line-height: 1.55;
      -webkit-font-smoothing: antialiased;
      overflow-x: clip;
      overscroll-behavior-x: none;
      max-width: 100%;
      padding-bottom: calc(124px + env(safe-area-inset-bottom, 0px));
    }}
    html.drawer-open, body.drawer-open {{ overflow: hidden; }}
    a {{ color: var(--accent); text-decoration: none; }}
    .wrap {{ max-width: 920px; margin: 0 auto; padding: 28px 24px 72px; }}

    .masthead {{
      position: relative;
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
    .search-field {{
      position: relative;
      flex: 1;
      min-width: 220px;
      display: flex;
      align-items: center;
    }}
    .search-input {{
      width: 100%;
      min-width: 0;
      background: transparent;
      border: none;
      border-radius: 0;
      border-bottom: 1px solid var(--ink);
      padding: 8px 44px 8px 2px;
      font-size: 1rem;
      color: var(--ink);
      outline: none;
      -webkit-appearance: none;
      appearance: none;
    }}
    .search-input::-webkit-search-cancel-button,
    .search-input::-webkit-search-decoration {{
      -webkit-appearance: none;
      appearance: none;
    }}
    .search-input::placeholder {{ color: #7C786E; }}
    .search-input:focus {{ border-bottom: 2px solid var(--accent); padding-bottom: 7px; }}
    .search-clear {{
      position: absolute;
      right: 0;
      top: 50%;
      transform: translateY(-50%);
      width: 44px;
      height: 44px;
      padding: 0;
      background: none;
      border: none;
      color: var(--muted);
      font-size: 0.95rem;
      line-height: 1;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      justify-content: center;
    }}
    .search-clear[hidden] {{ display: none; }}
    .season-tabs {{ display: flex; gap: 4px; flex-wrap: wrap; }}
    .s-tab {{
      background: none;
      border: none;
      border-bottom: 2px solid transparent;
      padding: 5px 10px;
      font-size: 0.84rem;
      font-weight: 600;
      color: var(--muted);
      cursor: pointer;
      white-space: nowrap;
    }}
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

    .resume-bar {{
      margin-top: 14px;
      padding: 4px 4px 4px 12px;
      background: var(--surface);
      border-left: 3px solid var(--accent);
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
    }}
    .resume-bar[hidden] {{ display: none; }}
    .resume-btn {{
      flex: 1;
      min-width: 0;
      background: none;
      border: none;
      padding: 8px 0;
      text-align: left;
      cursor: pointer;
      font: inherit;
      font-size: 0.86rem;
      color: var(--ink);
      display: flex;
      align-items: baseline;
      gap: 8px;
      flex-wrap: wrap;
    }}
    .resume-btn strong {{ color: var(--accent); font-weight: 700; }}
    .resume-ts {{
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      font-size: 0.78rem;
      color: var(--muted);
    }}
    .resume-dismiss {{
      width: 44px;
      height: 44px;
      flex-shrink: 0;
      background: none;
      border: none;
      color: var(--muted);
      font-size: 0.9rem;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      justify-content: center;
    }}

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
    .ep-play svg, .p-play svg, .p-btn svg {{ display: block; fill: currentColor; }}
    .icon-play {{ transform: translateX(1px); }}
    .ep-content {{ min-width: 0; overflow-wrap: break-word; }}
    .ep-summary-btn {{
      display: block;
      width: 100%;
      background: none;
      border: none;
      padding: 0;
      margin: 0 0 10px;
      text-align: left;
      font: inherit;
      color: inherit;
      cursor: pointer;
      -webkit-tap-highlight-color: rgba(11, 93, 99, 0.12);
    }}
    .ep-summary-btn:active {{ opacity: 0.72; }}
    .ep-summary-btn:focus-visible {{ outline: 2px solid var(--accent); outline-offset: 4px; }}
    .ep-meta {{
      display: block;
      font-size: 0.8rem;
      color: var(--muted);
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      margin-bottom: 3px;
    }}
    .ep-meta strong {{ color: var(--ink); font-family: inherit; font-weight: 700; margin-right: 8px; transition: color 0.12s ease; }}
    .ep-desc {{
      margin: 0;
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
    .ep-setlist-btn.active {{ border-color: var(--accent); color: var(--accent); background: var(--accent-soft); }}

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
    .ep-more-hits {{
      background: none;
      border: none;
      padding: 6px 2px;
      font-size: 0.8rem;
      font-weight: 600;
      color: var(--accent);
      cursor: pointer;
      text-align: left;
    }}

    .drawer-backdrop {{
      display: none;
      position: fixed;
      inset: 0;
      background: rgba(20, 20, 19, 0.24);
      z-index: 60;
      touch-action: none;
      overscroll-behavior: contain;
    }}
    .drawer-backdrop.open {{ display: block; }}
    .drawer {{
      position: fixed;
      top: 0;
      right: 0;
      bottom: 0;
      width: min(520px, 100%);
      background: var(--bg);
      border-left: 1px solid var(--ink);
      z-index: 70;
      transform: translateX(100%);
      visibility: hidden;
      pointer-events: none;
      transition: transform 0.2s ease, visibility 0s linear 0.2s;
      display: flex;
      flex-direction: column;
      overscroll-behavior: contain;
    }}
    .drawer.open {{
      transform: translateX(0);
      visibility: visible;
      pointer-events: auto;
      transition: transform 0.2s ease, visibility 0s;
    }}
    .drawer-head {{
      padding: 18px 22px 14px;
      border-bottom: 1px solid var(--line);
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 12px;
      touch-action: none;
    }}
    .drawer-title {{ margin: 0 0 4px; font-size: 1.15rem; font-weight: 800; letter-spacing: -0.02em; }}
    .drawer-sub {{ font-size: 0.82rem; color: var(--muted); font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }}
    .drawer-close {{
      background: none;
      border: 1px solid var(--line);
      min-width: 44px;
      min-height: 40px;
      padding: 8px 12px;
      cursor: pointer;
      font-size: 0.85rem;
      color: var(--ink);
      flex-shrink: 0;
    }}
    .drawer-body {{
      overflow-y: auto;
      overscroll-behavior: contain;
      padding: 18px 22px calc(124px + env(safe-area-inset-bottom, 0px));
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
    .drawer-files {{
      margin-top: 24px;
      padding-top: 12px;
      border-top: 1px solid var(--line);
      font-size: 0.78rem;
      color: var(--muted);
    }}
    .drawer-files summary {{
      cursor: pointer;
      color: var(--muted);
      user-select: none;
    }}
    .drawer-meta-links {{
      margin-top: 10px;
      display: flex;
      flex-wrap: wrap;
      gap: 8px 16px;
      font-size: 0.78rem;
      color: var(--muted);
    }}
    .drawer-meta-links a {{ color: var(--muted); text-decoration: underline; text-decoration-color: var(--line); text-underline-offset: 3px; }}
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
      visibility: hidden;
      pointer-events: none;
      transition: transform 0.2s ease, visibility 0s linear 0.2s;
      border-top: 2px solid var(--accent);
      padding-bottom: env(safe-area-inset-bottom, 0px);
    }}
    .player-bar.visible {{
      transform: translateY(0);
      visibility: visible;
      pointer-events: auto;
      transition: transform 0.2s ease, visibility 0s;
    }}
    .p-progress-wrap {{
      max-width: 960px;
      margin: 0 auto;
      padding: 4px 18px 0;
    }}
    .p-progress {{
      -webkit-appearance: none;
      appearance: none;
      width: 100%;
      height: 34px;
      margin: 0;
      padding: 0;
      background: transparent;
      cursor: pointer;
      display: block;
      touch-action: none;
      --pct: 0%;
    }}
    .p-progress::-webkit-slider-runnable-track {{
      height: 4px;
      border-radius: 2px;
      background: linear-gradient(to right, var(--accent) var(--pct), #3A3934 var(--pct));
    }}
    .p-progress::-webkit-slider-thumb {{
      -webkit-appearance: none;
      appearance: none;
      width: 14px;
      height: 14px;
      border-radius: 50%;
      background: #F6F3EC;
      border: 2px solid var(--accent);
      margin-top: -5px;
      box-shadow: 0 1px 3px rgba(0, 0, 0, 0.4);
    }}
    .p-progress::-moz-range-track {{
      height: 4px;
      border-radius: 2px;
      background: #3A3934;
    }}
    .p-progress::-moz-range-progress {{
      height: 4px;
      border-radius: 2px;
      background: var(--accent);
    }}
    .p-progress::-moz-range-thumb {{
      width: 14px;
      height: 14px;
      border-radius: 50%;
      background: #F6F3EC;
      border: 2px solid var(--accent);
      box-sizing: border-box;
      box-shadow: 0 1px 3px rgba(0, 0, 0, 0.4);
    }}
    .player-inner {{
      max-width: 960px;
      margin: 0 auto;
      padding: 2px 18px 10px;
      display: flex;
      align-items: center;
      gap: 12px;
    }}
    .p-main {{ display: contents; }}
    .p-play {{
      width: 36px; height: 36px; padding: 0; border-radius: 50%;
      background: #F6F3EC; color: #141413; border: none; cursor: pointer; flex-shrink: 0;
      display: inline-flex; align-items: center; justify-content: center;
    }}
    .p-meta {{
      flex: 1;
      min-width: 0;
      background: none;
      border: none;
      padding: 2px 0;
      margin: 0;
      color: inherit;
      font: inherit;
      text-align: left;
      cursor: pointer;
    }}
    .p-meta:focus-visible, .p-play:focus-visible, .p-btn:focus-visible, .p-close:focus-visible {{
      outline: 2px solid #F6F3EC;
      outline-offset: 2px;
    }}
    .p-ep-row {{
      display: flex;
      align-items: baseline;
      justify-content: space-between;
      gap: 8px;
    }}
    .p-ep {{ font-size: 0.72rem; color: #A6A298; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
    .p-time-inline {{
      display: none;
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      font-size: 0.72rem;
      color: #D8D3C7;
      white-space: nowrap;
      flex-shrink: 0;
    }}
    .p-title-row {{
      display: flex;
      align-items: center;
      gap: 4px;
      min-width: 0;
    }}
    .p-title {{ font-size: 0.86rem; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
    .p-chev {{ color: #A6A298; font-size: 0.9rem; flex-shrink: 0; }}
    .p-controls {{ display: flex; gap: 4px; flex-wrap: nowrap; }}
    .p-btn, .p-close {{
      background: none; border: 1px solid #353430; color: #F6F3EC;
      padding: 5px 8px; font-size: 0.75rem; cursor: pointer;
      display: inline-flex; align-items: center; justify-content: center;
    }}
    .p-close {{ order: 5; flex-shrink: 0; }}
    .p-time {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 0.75rem; color: #A6A298; white-space: nowrap; }}

    @media (hover: hover) {{
      a:hover {{ text-decoration: underline; }}
      .btn-primary:hover {{ background: var(--accent-hover); }}
      .btn-text:hover {{ color: var(--accent); border-bottom-color: var(--accent); }}
      .sub-copy:hover {{ background: var(--accent); }}
      .sub-apps a:hover {{ border-color: var(--accent); color: var(--accent); background: var(--accent-soft); }}
      .s-tab:hover {{ color: var(--ink); }}
      .quick-picks button:hover {{ color: var(--accent); text-decoration-color: var(--accent); }}
      .resume-btn:hover strong {{ text-decoration: underline; }}
      .resume-dismiss:hover {{ color: var(--ink); }}
      .ep-play:hover {{ background: var(--accent); border-color: var(--accent); color: #fff; }}
      .ep-summary-btn:hover .ep-meta strong {{ color: var(--accent); }}
      .hl-btn:hover {{ color: var(--accent); }}
      .ep-setlist-btn:hover {{ border-color: var(--accent); color: var(--accent); background: var(--accent-soft); }}
      .search-clear:hover {{ color: var(--ink); }}
      .hit-btn:hover {{ background: rgba(11, 93, 99, 0.14); }}
      .ep-more-hits:hover {{ text-decoration: underline; }}
      .drawer-close:hover {{ border-color: var(--ink); }}
      .drawer-files summary:hover {{ color: var(--ink); }}
      .drawer-meta-links a:hover {{ color: var(--accent); }}
      .ch-item:hover {{ background: var(--accent-soft); }}
      .hl-card button:hover {{ color: var(--accent); }}
      .p-btn:hover, .p-close:hover {{ border-color: #F6F3EC; }}
      .p-meta:hover .p-title {{ text-decoration: underline; }}
    }}

    @media (max-width: 640px) {{
      body {{ padding-bottom: calc(174px + env(safe-area-inset-bottom, 0px)); }}
      .wrap {{ padding: 18px 16px 88px; }}
      .masthead {{ gap: 16px; align-items: flex-start; }}
      .cover {{ width: 84px; height: 84px; }}
      .sub-wrap {{ position: static; }}
      .sub-pop {{ left: 0; right: 0; width: auto; top: calc(100% - 16px); }}
      .search-field {{ flex: 1 1 100%; min-width: 0; }}
      .s-tab {{ min-height: 40px; padding: 8px 10px; }}
      .ep-row {{ grid-template-columns: 42px 1fr; gap: 4px 12px; }}
      .ep-play {{ width: 42px; height: 42px; }}
      .hl-btn {{ padding: 6px 0; }}
      .ep-setlist-btn {{ grid-column: 2; justify-self: start; margin-top: 8px; min-height: 40px; padding: 8px 14px; }}
      .drawer {{ border-left: none; }}
      .drawer-body {{ padding: 18px 18px calc(174px + env(safe-area-inset-bottom, 0px)); }}
      .filter-inline {{ gap: 4px; }}
      .filter-inline button {{ min-height: 40px; padding: 6px 10px; }}
      .p-progress-wrap {{ padding: 2px 12px 0; }}
      .p-progress {{ height: 36px; }}
      .player-inner {{
        flex-direction: column;
        align-items: stretch;
        gap: 6px;
        padding: 2px 12px 10px;
      }}
      .p-main {{
        display: flex;
        align-items: center;
        gap: 10px;
        min-width: 0;
      }}
      .p-play {{ width: 42px; height: 42px; }}
      .p-close {{ order: 0; min-width: 44px; min-height: 44px; padding: 0; font-size: 0.9rem; }}
      .p-time-inline {{ display: inline; }}
      .p-time, #pReroll {{ display: none; }}
      .p-controls {{
        display: grid;
        grid-template-columns: repeat(6, minmax(0, 1fr));
        gap: 6px;
      }}
      .p-btn {{
        min-height: 42px;
        min-width: 0;
        padding: 0 2px;
        font-size: 0.78rem;
        white-space: nowrap;
        overflow: hidden;
      }}
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
        <div class="search-field">
          <input class="search-input" id="searchInput" type="search" aria-label="Hae biisiä, bändiä, paikkaa tai tarinaa" placeholder="Hae biisiä, bändiä, paikkaa tai tarinaa..." autocomplete="off" autocorrect="off" spellcheck="false" />
          <button class="search-clear" id="searchClear" type="button" aria-label="Tyhjennä haku" hidden>✕</button>
        </div>
        <div class="season-tabs" role="tablist" aria-label="Suodata kauden mukaan">
          <button class="s-tab active" data-season="all" type="button" role="tab" aria-selected="true">Kaikki</button>
          <button class="s-tab" data-season="1" type="button" role="tab" aria-selected="false">Kausi 1</button>
          <button class="s-tab" data-season="2" type="button" role="tab" aria-selected="false">Kausi 2</button>
          <button class="s-tab" data-season="3" type="button" role="tab" aria-selected="false">Kausi 3</button>
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

    <div class="resume-bar" id="resumeBar" hidden>
      <button class="resume-btn" id="resumeBtn" type="button"></button>
      <button class="resume-dismiss" id="resumeDismiss" type="button" aria-label="Piilota jatkamispalkki">✕</button>
    </div>

    <main id="episodeList"></main>

    <footer>
      <span>Radiokanavasta riippumaton harrasteprojekti · Alkuperäinen ohjelma: <a href="{SHOW_LINK}" target="_blank" rel="noopener">Radio Helsinki – Rane Raitsikka &amp; Tokela</a></span>
      <span><a href="feed.xml">feed.xml</a></span>
    </footer>
  </div>

  <div class="drawer-backdrop" id="drawerBackdrop"></div>
  <aside class="drawer" id="drawer" role="dialog" aria-modal="true" aria-labelledby="drawerTitle">
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
    <div class="p-progress-wrap">
      <input class="p-progress" id="pProgress" type="range" min="0" max="1000" value="0" aria-label="Toistokohta" />
    </div>
    <div class="player-inner">
      <div class="p-main">
        <button class="p-play" id="pPlayBtn" type="button" aria-label="Toista tai tauota"><svg class="icon-play" width="11" height="12" viewBox="0 0 11 12" aria-hidden="true"><path d="M1 1l9 5-9 5z"/></svg></button>
        <button class="p-meta" id="pMeta" type="button" title="Avaa soivan jakson biisit ja aiheet">
          <span class="p-ep-row">
            <span class="p-ep" id="pEp"></span>
            <span class="p-time-inline" id="pTimeInline" aria-live="off"></span>
          </span>
          <span class="p-title-row">
            <span class="p-title" id="pTitle"></span>
            <span class="p-chev" aria-hidden="true">›</span>
          </span>
        </button>
        <button class="p-close" id="pCloseBtn" type="button" aria-label="Sulje soitin">✕</button>
      </div>
      <div class="p-controls">
        <button class="p-btn" id="pPrevBtn" type="button" title="Edellinen luku" aria-label="Edellinen luku"><svg width="13" height="12" viewBox="0 0 13 12" aria-hidden="true"><rect x="1" y="1" width="2" height="10"/><path d="M12 1L4 6l8 5z"/></svg></button>
        <button class="p-btn" id="pBack15" type="button" title="-15s" aria-label="15 sekuntia taaksepäin">-15s</button>
        <button class="p-btn" id="pFwd30" type="button" title="+30s" aria-label="30 sekuntia eteenpäin">+30s</button>
        <button class="p-btn" id="pNextBtn" type="button" title="Seuraava luku" aria-label="Seuraava luku"><svg width="13" height="12" viewBox="0 0 13 12" aria-hidden="true"><path d="M1 1l8 5-8 5z"/><rect x="10" y="1" width="2" height="10"/></svg></button>
        <button class="p-btn" id="pSpeedBtn" type="button" title="Toistonopeus" aria-label="Toistonopeus">1×</button>
        <button class="p-btn" id="pShareBtn" type="button" title="Kopioi linkki tähän kohtaan" aria-label="Kopioi linkki tähän kohtaan">Jaa</button>
        <button class="p-btn" id="pReroll" type="button" title="Arvo toinen tarina">↻ Arvo</button>
      </div>
      <div class="p-time" id="pTime">00:00:00</div>
    </div>
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
    const searchClear = document.getElementById('searchClear');
    const quickPicks = document.getElementById('quickPicks');
    const resumeBar = document.getElementById('resumeBar');
    const resumeBtn = document.getElementById('resumeBtn');
    const resumeDismiss = document.getElementById('resumeDismiss');
    const drawer = document.getElementById('drawer');
    const drawerBackdrop = document.getElementById('drawerBackdrop');
    const drawerCloseBtn = document.getElementById('drawerClose');
    const drawerTitle = document.getElementById('drawerTitle');
    const drawerSub = document.getElementById('drawerSub');
    const drawerBody = document.getElementById('drawerBody');

    const audioEl = document.getElementById('globalAudio');
    const playerBar = document.getElementById('playerBar');
    const pEp = document.getElementById('pEp');
    const pTitle = document.getElementById('pTitle');
    const pTime = document.getElementById('pTime');
    const pTimeInline = document.getElementById('pTimeInline');
    const pPlayBtn = document.getElementById('pPlayBtn');
    const pProgress = document.getElementById('pProgress');
    const pSpeedBtn = document.getElementById('pSpeedBtn');
    const pShareBtn = document.getElementById('pShareBtn');
    const SPEEDS = [1, 1.25, 1.5];
    const STORAGE_KEY = 'rt_archive_player_v1';
    let speedIdx = 0;
    let activeEp = null;
    let activeChapterIdx = -1;
    let pendingSeekSec = null;
    let pendingAutoplay = true;
    let suppressNextSeekSave = false;
    let isScrubbing = false;
    let scrubValue = null;
    let scrubEndTimer = null;
    let lastSaveMs = 0;
    let lastDrawerTrigger = null;
    let lastAppliedHash = '';

    function parseDurationSec(str) {{
      const parts = String(str || '').split(':').map(Number);
      if (parts.length === 3 && parts.every(Number.isFinite)) return parts[0] * 3600 + parts[1] * 60 + parts[2];
      if (parts.length === 2 && parts.every(Number.isFinite)) return parts[0] * 60 + parts[1];
      return 0;
    }}

    function getActiveDuration() {{
      if (audioEl.duration && isFinite(audioEl.duration) && audioEl.duration > 0) return audioEl.duration;
      return activeEp ? parseDurationSec(activeEp.duration) : 0;
    }}

    function getActiveTimeSec() {{
      if (pendingSeekSec !== null) return pendingSeekSec;
      return Math.max(0, audioEl.currentTime || 0);
    }}

    function setTimeDisplay(text) {{
      pTime.textContent = text;
      pTimeInline.textContent = text;
      pProgress.setAttribute('aria-valuetext', text);
    }}

    function setProgressVisual(val) {{
      const clamped = Math.max(0, Math.min(1000, Math.round(Number(val) || 0)));
      pProgress.value = clamped;
      pProgress.style.setProperty('--pct', `${{(clamped / 10).toFixed(1)}}%`);
    }}

    function readSavedState() {{
      try {{
        const raw = localStorage.getItem(STORAGE_KEY);
        if (!raw) return null;
        const data = JSON.parse(raw);
        if (!data || typeof data !== 'object') return null;
        const epId = Number(data.epId);
        const ep = EPISODES.find(e => e.id === epId);
        if (!ep) return null;
        const maxDur = parseDurationSec(ep.duration) || 86400;
        const t = Number(data.t);
        const clampedT = Number.isFinite(t) ? Math.max(0, Math.min(maxDur, Math.floor(t))) : 0;
        const sIdx = Number.isInteger(data.speedIdx) && data.speedIdx >= 0 && data.speedIdx < SPEEDS.length ? data.speedIdx : 0;
        return {{ epId: ep.id, t: clampedT, speedIdx: sIdx }};
      }} catch (e) {{
        return null;
      }}
    }}

    function savePlayerState(force = false) {{
      if (!activeEp) return;
      const cur = Math.floor(getActiveTimeSec());
      const dur = getActiveDuration();
      if (audioEl.ended || (dur > 0 && dur - cur < 15)) {{
        clearSavedState();
        return;
      }}
      const now = Date.now();
      if (!force && now - lastSaveMs < 3000) return;
      lastSaveMs = now;
      try {{
        localStorage.setItem(STORAGE_KEY, JSON.stringify({{
          epId: activeEp.id,
          t: cur,
          speedIdx,
          updatedAt: now
        }}));
      }} catch (e) {{}}
    }}

    function clearSavedState() {{
      try {{ localStorage.removeItem(STORAGE_KEY); }} catch (e) {{}}
    }}

    function renderResumeBar(saved) {{
      if (!saved || saved.t < 5) {{
        resumeBar.hidden = true;
        return;
      }}
      const ep = EPISODES.find(e => e.id === saved.epId);
      if (!ep) {{
        resumeBar.hidden = true;
        return;
      }}
      const epDur = parseDurationSec(ep.duration);
      if (epDur > 0 && epDur - saved.t < 15) {{
        resumeBar.hidden = true;
        return;
      }}
      resumeBtn.innerHTML = `<span>▶ Jatka: <strong>${{esc(ep.label)}}</strong></span><span class="resume-ts">${{fmtTime(saved.t)}} / ${{esc(ep.duration)}}</span>`;
      resumeBtn.dataset.resumeEp = String(ep.id);
      resumeBtn.dataset.resumeSec = String(saved.t);
      resumeBar.hidden = false;
    }}

    function buildShareUrl(ep, sec) {{
      const base = location.href.split('#')[0];
      const t = Math.max(0, Math.floor(Number(sec) || 0));
      return t > 0 ? `${{base}}#ep-${{ep.key}}&t=${{t}}` : `${{base}}#ep-${{ep.key}}`;
    }}

    async function copyTextWithFeedback(text, btn, okLabel) {{
      if (!btn.dataset.origLabel) btn.dataset.origLabel = btn.textContent;
      let copied = false;
      try {{
        if (navigator.clipboard && navigator.clipboard.writeText) {{
          await navigator.clipboard.writeText(text);
          copied = true;
        }}
      }} catch (e) {{}}
      if (!copied) {{
        try {{
          const ta = document.createElement('textarea');
          ta.value = text;
          ta.setAttribute('readonly', '');
          ta.style.position = 'fixed';
          ta.style.opacity = '0';
          document.body.appendChild(ta);
          ta.select();
          copied = document.execCommand('copy');
          document.body.removeChild(ta);
        }} catch (e) {{}}
      }}
      if (copied) {{
        btn.textContent = okLabel;
        if (btn._fbTimer) clearTimeout(btn._fbTimer);
        btn._fbTimer = setTimeout(() => {{
          btn.textContent = btn.dataset.origLabel;
          btn._fbTimer = null;
        }}, 1800);
      }} else {{
        prompt('Kopioi osoite:', text);
      }}
    }}

    function syncSearchClear() {{
      searchClear.hidden = !searchInput.value;
    }}

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

    function playAt(epId, startSec, customLabel, opts = {{}}) {{
      const ep = EPISODES.find(e => e.id === epId);
      if (!ep) return;
      const shouldAutoplay = opts.autoplay !== false;
      const epDur = parseDurationSec(ep.duration);
      const rawTarget = Math.max(0, Number(startSec) || 0);
      const targetSec = epDur > 0 ? Math.min(rawTarget, epDur) : rawTarget;
      const sameEp = activeEp && activeEp.id === ep.id && audioEl.src === ep.audio;
      activeEp = ep;
      resumeBar.hidden = true;
      playerBar.classList.add('visible');
      setTimeDisplay(shouldAutoplay ? `${{fmtTime(targetSec)}} / ${{ep.duration}} · ladataan…` : `${{fmtTime(targetSec)}} / ${{ep.duration}}`);
      updatePlayerChapter(targetSec, customLabel);
      if (epDur > 0) setProgressVisual((targetSec / epDur) * 1000);
      else setProgressVisual(0);

      if (sameEp && audioEl.readyState >= 1) {{
        pendingSeekSec = null;
        pendingAutoplay = shouldAutoplay;
        suppressNextSeekSave = !shouldAutoplay;
        try {{ audioEl.currentTime = targetSec; }} catch(e) {{}}
        if (audioEl.duration > 0) setProgressVisual((targetSec / audioEl.duration) * 1000);
        audioEl.playbackRate = SPEEDS[speedIdx];
        if (shouldAutoplay) audioEl.play().catch(() => {{}});
        else {{
          audioEl.pause();
          pPlayBtn.innerHTML = ICON_PLAY;
        }}
      }} else {{
        pendingSeekSec = targetSec;
        pendingAutoplay = shouldAutoplay;
        audioEl.src = ep.audio;
        if (shouldAutoplay) {{
          audioEl.load();
          audioEl.play().catch(() => {{}});
        }} else {{
          pPlayBtn.innerHTML = ICON_PLAY;
        }}
      }}
      if (shouldAutoplay) savePlayerState(true);

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
      const dur = getActiveDuration();
      const base = getActiveTimeSec();
      const target = Math.max(0, dur > 0 ? Math.min(dur, base + delta) : base + delta);
      if (audioEl.readyState >= 1) {{
        audioEl.currentTime = target;
      }} else {{
        pendingSeekSec = target;
      }}
      if (dur > 0) setProgressVisual((target / dur) * 1000);
      setTimeDisplay(`${{fmtTime(target)}} / ${{activeEp.duration}}`);
      updatePlayerChapter(target);
      savePlayerState(true);
    }}

    function playRandomStory() {{
      if (!ALL_HIGHLIGHTS.length) return;
      const pick = ALL_HIGHLIGHTS[Math.floor(Math.random() * ALL_HIGHLIGHTS.length)];
      playAt(pick.epId, pick.t, pick.title);
    }}

    audioEl.addEventListener('loadedmetadata', () => {{
      if (pendingSeekSec !== null) {{
        const s = pendingSeekSec;
        const doPlay = pendingAutoplay && playerBar.classList.contains('visible');
        pendingSeekSec = null;
        pendingAutoplay = false;
        suppressNextSeekSave = !doPlay;
        try {{ audioEl.currentTime = s; }} catch(e) {{}}
        if (audioEl.duration > 0) setProgressVisual((s / audioEl.duration) * 1000);
        audioEl.playbackRate = SPEEDS[speedIdx];
        if (doPlay) audioEl.play().catch(() => {{}});
        else if (activeEp) setTimeDisplay(`${{fmtTime(s)}} / ${{activeEp.duration}}`);
      }}
    }});
    audioEl.addEventListener('error', () => {{
      pendingAutoplay = false;
      pPlayBtn.innerHTML = ICON_PLAY;
      if (activeEp) setTimeDisplay('Äänen lataus epäonnistui');
    }});
    audioEl.addEventListener('waiting', () => {{
      if (activeEp && !isScrubbing) setTimeDisplay(`${{fmtTime(getActiveTimeSec())}} / ${{activeEp.duration}} · puskuroi…`);
    }});
    const ICON_PLAY = '<svg class="icon-play" width="11" height="12" viewBox="0 0 11 12" aria-hidden="true"><path d="M1 1l9 5-9 5z"/></svg>';
    const ICON_PAUSE = '<svg width="10" height="12" viewBox="0 0 10 12" aria-hidden="true"><rect x="1" y="1" width="3" height="10"/><rect x="6" y="1" width="3" height="10"/></svg>';
    audioEl.addEventListener('play', () => {{
      pPlayBtn.innerHTML = ICON_PAUSE;
      savePlayerState(true);
    }});
    audioEl.addEventListener('pause', () => {{
      pPlayBtn.innerHTML = ICON_PLAY;
      if (audioEl.currentTime > 0 && !suppressNextSeekSave) savePlayerState(true);
    }});
    audioEl.addEventListener('ended', () => {{
      pPlayBtn.innerHTML = ICON_PLAY;
      clearSavedState();
    }});
    audioEl.addEventListener('seeked', () => {{
      if (suppressNextSeekSave) {{
        suppressNextSeekSave = false;
        return;
      }}
      if (audioEl.currentTime > 0 || !audioEl.paused) savePlayerState(true);
    }});
    audioEl.addEventListener('timeupdate', () => {{
      if (pendingSeekSec !== null) return;
      const cur = audioEl.currentTime || 0;
      const dur = getActiveDuration();
      if (!isScrubbing) {{
        setTimeDisplay(`${{fmtTime(cur)}} / ${{activeEp ? activeEp.duration : fmtTime(dur)}}`);
        if (dur > 0) setProgressVisual((cur / dur) * 1000);
      }}
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
        if (dur > 0 && dur - cur < 15) clearSavedState();
        else savePlayerState(false);
      }}
    }});

    pPlayBtn.addEventListener('click', () => {{
      if (!activeEp) return;
      if (audioEl.paused) {{
        pendingAutoplay = true;
        if (audioEl.readyState === 0) {{
          setTimeDisplay(`${{fmtTime(getActiveTimeSec())}} / ${{activeEp.duration}} · ladataan…`);
          audioEl.load();
        }}
        audioEl.playbackRate = SPEEDS[speedIdx];
        audioEl.play().catch(() => {{}});
      }} else {{
        pendingAutoplay = false;
        audioEl.pause();
      }}
    }});
    const startScrub = () => {{
      if (scrubEndTimer) {{ clearTimeout(scrubEndTimer); scrubEndTimer = null; }}
      isScrubbing = true;
    }};
    const endScrub = () => {{
      if (!isScrubbing) return;
      if (scrubEndTimer) clearTimeout(scrubEndTimer);
      scrubEndTimer = setTimeout(() => {{
        isScrubbing = false;
        scrubValue = null;
        scrubEndTimer = null;
      }}, 150);
    }};
    pProgress.addEventListener('pointerdown', startScrub);
    pProgress.addEventListener('touchstart', startScrub, {{ passive: true }});
    pProgress.addEventListener('mousedown', startScrub);
    pProgress.addEventListener('blur', endScrub);
    window.addEventListener('pointerup', endScrub);
    window.addEventListener('pointercancel', endScrub);
    window.addEventListener('touchend', endScrub);
    window.addEventListener('mouseup', endScrub);
    window.addEventListener('blur', endScrub);
    pProgress.addEventListener('input', () => {{
      if (scrubEndTimer) {{ clearTimeout(scrubEndTimer); scrubEndTimer = null; }}
      isScrubbing = true;
      const val = Number(pProgress.value) || 0;
      scrubValue = val;
      pProgress.style.setProperty('--pct', `${{(val / 10).toFixed(1)}}%`);
      const dur = getActiveDuration();
      if (dur > 0) {{
        const previewSec = (val / 1000) * dur;
        setTimeDisplay(`${{fmtTime(previewSec)}} / ${{activeEp ? activeEp.duration : fmtTime(dur)}}`);
      }}
    }});
    pProgress.addEventListener('change', () => {{
      if (scrubEndTimer) {{ clearTimeout(scrubEndTimer); scrubEndTimer = null; }}
      const val = scrubValue !== null ? scrubValue : (Number(pProgress.value) || 0);
      scrubValue = null;
      isScrubbing = false;
      pProgress.value = val;
      pProgress.style.setProperty('--pct', `${{(val / 10).toFixed(1)}}%`);
      const dur = getActiveDuration();
      if (dur > 0) {{
        const targetSec = (val / 1000) * dur;
        if (audioEl.readyState >= 1 && audioEl.duration > 0) {{
          try {{ audioEl.currentTime = targetSec; }} catch (e) {{}}
        }} else {{
          pendingSeekSec = targetSec;
        }}
        setTimeDisplay(`${{fmtTime(targetSec)}} / ${{activeEp ? activeEp.duration : fmtTime(dur)}}`);
        updatePlayerChapter(targetSec);
        savePlayerState(true);
      }}
    }});
    document.getElementById('pPrevBtn').addEventListener('click', () => jumpChapter(-1));
    document.getElementById('pNextBtn').addEventListener('click', () => jumpChapter(1));
    document.getElementById('pBack15').addEventListener('click', () => skipSeconds(-15));
    document.getElementById('pFwd30').addEventListener('click', () => skipSeconds(30));
    pSpeedBtn.addEventListener('click', () => {{
      speedIdx = (speedIdx + 1) % SPEEDS.length;
      audioEl.playbackRate = SPEEDS[speedIdx];
      pSpeedBtn.textContent = `${{SPEEDS[speedIdx]}}×`;
      savePlayerState(true);
    }});
    pShareBtn.addEventListener('click', () => {{
      if (!activeEp) return;
      copyTextWithFeedback(buildShareUrl(activeEp, getActiveTimeSec()), pShareBtn, 'OK ✓');
    }});
    document.getElementById('pReroll').addEventListener('click', playRandomStory);
    document.getElementById('pMeta').addEventListener('click', () => {{
      if (activeEp) openDrawer(activeEp.id);
    }});
    document.getElementById('pCloseBtn').addEventListener('click', () => {{
      pendingAutoplay = false;
      if (!audioEl.paused || audioEl.currentTime > 0) savePlayerState(true);
      audioEl.pause();
      playerBar.classList.remove('visible');
    }});
    window.addEventListener('pagehide', () => {{
      if (!audioEl.paused || audioEl.currentTime > 0) savePlayerState(true);
    }});
    document.addEventListener('visibilitychange', () => {{
      if (document.visibilityState === 'hidden' && (!audioEl.paused || audioEl.currentTime > 0)) savePlayerState(true);
    }});
    resumeBtn.addEventListener('click', () => {{
      const epId = Number(resumeBtn.dataset.resumeEp);
      const sec = Number(resumeBtn.dataset.resumeSec || 0);
      if (epId) playAt(epId, sec);
    }});
    resumeDismiss.addEventListener('click', () => {{
      resumeBar.hidden = true;
      clearSavedState();
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
    copyRssBtn.addEventListener('click', () => {{
      copyTextWithFeedback(SUBSCRIBE.rss, copyRssBtn, 'RSS-osoite kopioitu!');
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
          const moreHitsHtml = hits.length > 5
            ? `<li><button class="ep-more-hits" type="button" data-open-drawer="${{ep.id}}">+ ${{hits.length - 5}} muuta osumaa ›</button></li>`
            : '';
          const hlHtml = q && hits.length
            ? `<ul class="search-hits">${{hits.slice(0, 5).map(h => `<li><button class="hit-btn" type="button" data-play-ep="${{ep.id}}" data-play-sec="${{h.t}}"><span class="hl-ts">${{h.ts}}</span><span>${{esc(h.title)}}</span></button></li>`).join('')}}${{moreHitsHtml}}</ul>`
            : `<ul class="ep-highlights">${{top2.map(h => `<li><button class="hl-btn" type="button" data-play-ep="${{ep.id}}" data-play-sec="${{h.t}}"><span class="hl-ts">${{h.ts}}</span><span>${{esc(h.title)}}</span></button></li>`).join('')}}</ul>`;
          out += `
            <article class="ep-row">
              <button class="ep-play" type="button" data-play-ep="${{ep.id}}" data-play-sec="0" title="Kuuntele ${{esc(ep.label)}}" aria-label="Kuuntele ${{esc(ep.label)}}">${{ICON_PLAY}}</button>
              <div class="ep-content">
                <button class="ep-summary-btn" type="button" data-open-drawer="${{ep.id}}">
                  <span class="ep-meta"><strong>${{esc(ep.label)}}</strong>${{esc(ep.date)}} · ${{esc(ep.duration)}}</span>
                  <span class="ep-desc">${{esc(ep.desc)}}</span>
                </button>
                ${{hlHtml}}
              </div>
              <button class="ep-setlist-btn ${{openDrawerEpId === ep.id ? 'active' : ''}}" type="button" data-open-drawer="${{ep.id}}">Biisit &amp; aiheet ›</button>
            </article>
          `;
        }}
      }}
      listEl.innerHTML = out;
      highlightPlayingRow();
    }}

    function openDrawer(epId, pushState = true) {{
      const ep = EPISODES.find(e => e.id === epId);
      if (!ep) return;
      if (!drawer.classList.contains('open')) {{
        lastDrawerTrigger = document.activeElement;
      }}
      if (openDrawerEpId !== ep.id) {{
        drawerFilter = 'all';
      }}
      openDrawerEpId = ep.id;
      drawerTitle.textContent = ep.label;
      drawerSub.textContent = `${{ep.date}} · ${{ep.duration}}`;
      renderDrawerBody(ep);
      drawerBody.scrollTop = 0;
      drawer.classList.add('open');
      drawerBackdrop.classList.add('open');
      document.documentElement.classList.add('drawer-open');
      document.body.classList.add('drawer-open');
      document.querySelectorAll('.ep-setlist-btn').forEach(b => b.classList.toggle('active', Number(b.dataset.openDrawer) === epId));
      const targetHash = '#ep-' + ep.key;
      if (pushState && history.pushState && location.hash !== targetHash) {{
        history.pushState({{ epId }}, '', targetHash);
      }}
      lastAppliedHash = location.hash;
      try {{ drawerCloseBtn.focus({{ preventScroll: true }}); }} catch (e) {{}}
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
      const vttLink = ep.vttRel ? `<a href="${{esc(ep.vttRel)}}" target="_blank" rel="noopener">Tekstitys (.vtt)</a>` : '';
      drawerBody.innerHTML = `
        <p class="drawer-desc">${{esc(ep.desc)}}</p>
        <div class="drawer-actions">
          <button class="btn-primary" type="button" data-play-ep="${{ep.id}}" data-play-sec="0">▶ Kuuntele alusta</button>
          <button class="btn-text" type="button" data-share-ep="${{ep.id}}">Jaa linkki</button>
        </div>
        <div class="drawer-section-label">
          <span>Biisit &amp; aiheet (${{visibleChs.length}})</span>
          <div class="filter-inline">
            <button type="button" data-dfilter="all" class="${{drawerFilter==='all'?'active':''}}">Kaikki</button>
            <button type="button" data-dfilter="music" class="${{drawerFilter==='music'?'active':''}}">Biisit</button>
            <button type="button" data-dfilter="talk" class="${{drawerFilter==='talk'?'active':''}}">Puheet</button>
          </div>
        </div>
        <div>${{chsHtml}}</div>
        ${{ep.highlights.length ? `<div class="drawer-section-label"><span>Jakson parhaat nostot</span></div><div>${{hlsHtml}}</div>` : ''}}
        <details class="drawer-files">
          <summary>Tiedostot ja lähdedata</summary>
          <div class="drawer-meta-links">
            <a href="${{esc(ep.audio)}}" target="_blank" rel="noopener">Äänitiedosto (MP3)</a>
            ${{vttLink}}
            <a href="${{esc(ep.chaptersRel)}}" target="_blank" rel="noopener">Luvut (raaka JSON-data)</a>
          </div>
        </details>
      `;
      highlightPlayingRow();
    }}

    function closeDrawer(fromPopState = false) {{
      if (!openDrawerEpId && !drawer.classList.contains('open')) return;
      openDrawerEpId = null;
      lastAppliedHash = '';
      drawer.classList.remove('open');
      drawerBackdrop.classList.remove('open');
      document.documentElement.classList.remove('drawer-open');
      document.body.classList.remove('drawer-open');
      document.querySelectorAll('.ep-setlist-btn.active').forEach(b => b.classList.remove('active'));
      if (!fromPopState && location.hash && location.hash.startsWith('#ep-') && history.replaceState) {{
        history.replaceState(null, '', location.pathname + location.search);
      }}
      if (lastDrawerTrigger && document.contains(lastDrawerTrigger)) {{
        try {{ lastDrawerTrigger.focus({{ preventScroll: true }}); }} catch (e) {{}}
      }}
      lastDrawerTrigger = null;
    }}

    drawerCloseBtn.addEventListener('click', () => closeDrawer(false));
    drawerBackdrop.addEventListener('click', () => closeDrawer(false));
    window.addEventListener('popstate', () => {{
      if (location.hash && location.hash.startsWith('#ep-')) {{
        applyLocationHash();
      }} else if (openDrawerEpId) {{
        closeDrawer(true);
      }}
    }});
    window.addEventListener('hashchange', () => applyLocationHash());
    document.addEventListener('keydown', (e) => {{
      if (e.key === 'Escape') {{
        closeDrawer(false);
        if (subPop.classList.contains('open')) {{
          subPop.classList.remove('open');
          subBtn.setAttribute('aria-expanded', 'false');
          try {{ subBtn.focus({{ preventScroll: true }}); }} catch (err) {{}}
        }}
        return;
      }}
      if (e.key === 'Tab' && drawer.classList.contains('open')) {{
        const focusables = Array.from(drawer.querySelectorAll('button, a[href], summary, input, [tabindex]:not([tabindex="-1"])')).filter(el => !el.disabled && el.offsetParent !== null);
        if (focusables.length) {{
          const first = focusables[0];
          const last = focusables[focusables.length - 1];
          if (e.shiftKey && (document.activeElement === first || !drawer.contains(document.activeElement))) {{
            e.preventDefault();
            last.focus();
          }} else if (!e.shiftKey && (document.activeElement === last || !drawer.contains(document.activeElement))) {{
            e.preventDefault();
            first.focus();
          }}
        }}
      }}
    }});

    document.addEventListener('click', (e) => {{
      const playEl = e.target.closest('[data-play-ep]');
      if (playEl) {{
        playAt(Number(playEl.dataset.playEp), Number(playEl.dataset.playSec || 0));
        return;
      }}
      const shareEl = e.target.closest('[data-share-ep]');
      if (shareEl) {{
        const ep = EPISODES.find(x => x.id === Number(shareEl.dataset.shareEp));
        if (ep) {{
          const sec = activeEp && activeEp.id === ep.id ? getActiveTimeSec() : 0;
          copyTextWithFeedback(buildShareUrl(ep, sec), shareEl, 'Linkki kopioitu!');
        }}
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
        if (ep) {{
          renderDrawerBody(ep);
          const nextBtn = drawerBody.querySelector(`[data-dfilter="${{drawerFilter}}"]`);
          if (nextBtn) try {{ nextBtn.focus({{ preventScroll: true }}); }} catch (err) {{}}
        }}
        return;
      }}
      const qBtn = e.target.closest('[data-q]');
      if (qBtn) {{
        searchInput.value = qBtn.dataset.q;
        currentQuery = qBtn.dataset.q;
        syncSearchClear();
        renderList();
      }}
    }});

    document.querySelectorAll('.s-tab').forEach(btn => {{
      btn.addEventListener('click', () => {{
        document.querySelectorAll('.s-tab').forEach(b => {{
          b.classList.remove('active');
          b.setAttribute('aria-selected', 'false');
        }});
        btn.classList.add('active');
        btn.setAttribute('aria-selected', 'true');
        currentSeason = btn.dataset.season;
        renderList();
      }});
    }});
    searchInput.addEventListener('input', () => {{
      currentQuery = searchInput.value;
      syncSearchClear();
      renderList();
    }});
    searchClear.addEventListener('click', () => {{
      searchInput.value = '';
      currentQuery = '';
      syncSearchClear();
      renderList();
    }});
    document.getElementById('randomBtn').addEventListener('click', playRandomStory);

    function applyLocationHash() {{
      const h = location.hash || '';
      if (h === lastAppliedHash && openDrawerEpId) return true;
      const m = h.match(/^#ep-(\\d{{2}})(?:&t=(\\d{{1,6}}))?$/);
      if (!m) return false;
      const targetId = parseInt(m[1], 10);
      const ep = EPISODES.find(e => e.id === targetId);
      if (!ep) return false;
      lastAppliedHash = h;
      openDrawer(targetId, false);
      if (m[2] !== undefined) {{
        const sec = parseInt(m[2], 10);
        playAt(targetId, sec, undefined, {{ autoplay: false }});
      }}
      return true;
    }}

    const savedState = readSavedState();
    if (savedState) {{
      speedIdx = savedState.speedIdx;
      pSpeedBtn.textContent = `${{SPEEDS[speedIdx]}}×`;
    }}
    syncSearchClear();
    renderList();
    const openedFromHash = applyLocationHash();
    if (!openedFromHash && savedState) {{
      renderResumeBar(savedState);
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
