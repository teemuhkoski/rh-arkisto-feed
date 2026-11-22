import argparse
import asyncio
import html
import io
import json
import re
import subprocess
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import requests
from mutagen.id3 import APIC, COMM, ID3, TALB, TDRC, TIT2, TPE1, TRCK, delete as delete_id3
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, MP4Cover
from PIL import Image
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

DEFAULT_URL = "https://www.radiohelsinki.fi/ohjelma/rane-raitsikka-tokela/"


def build_http_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=6,
        connect=6,
        read=6,
        backoff_factor=1.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "HEAD"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def strip_html_clean(fragment: str) -> str:
    fragment = re.sub(r"<(style|script)[^>]*>.*?</\1>", "", fragment, flags=re.I | re.S)
    fragment = re.sub(r"<[^>]+>", " ", fragment)
    fragment = html.unescape(fragment)
    return re.sub(r"\s+", " ", fragment).strip()


def slugify_filename(text: str) -> str:
    text = text.replace("&", "ja")
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^\w\s-]", "", text).strip()
    text = re.sub(r"[-\s]+", "_", text)
    return (text[:80].rstrip("_")) or "Radio_Helsinki_Ohjelma"


def extract_url_slug(show_url: str) -> str:
    path = urlparse(show_url).path.strip("/")
    parts = [p for p in path.split("/") if p]
    return parts[-1] if parts else "radio-helsinki-ohjelma"


def fetch_show_info(session: requests.Session, show_url: str) -> tuple[str, str | None]:
    print(f"Haetaan ohjelman perustiedot: {show_url}")
    page_html = session.get(show_url, timeout=15).text

    title = ""
    og_title = re.search(
        r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']',
        page_html,
        re.I,
    ) or re.search(
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:title["\']',
        page_html,
        re.I,
    )
    if og_title:
        title = strip_html_clean(og_title.group(1))

    if not title:
        title_tag = re.search(r"<title[^>]*>(.*?)</title>", page_html, re.I | re.S)
        if title_tag:
            title = strip_html_clean(title_tag.group(1))

    title = re.sub(r"\s*[-|–—]\s*Radio Helsinki\s*$", "", title, flags=re.I).strip()

    if not title or title.lower() == "radio helsinki":
        for h1_match in re.finditer(r"<h1[^>]*>(.*?)</h1>", page_html, re.I | re.S):
            candidate = strip_html_clean(h1_match.group(1))
            if candidate and candidate.lower() != "radio helsinki":
                title = candidate
                break

    if not title or title.lower() == "radio helsinki":
        title = extract_url_slug(show_url).replace("-", " ").title()

    cover_url = None
    og_img = re.search(
        r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
        page_html,
        re.I,
    ) or re.search(
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
        page_html,
        re.I,
    )
    if og_img:
        cover_url = html.unescape(og_img.group(1).strip())

    return title, cover_url


def clean_description(raw: str) -> str:
    text = re.sub(r"\s+", " ", raw).strip()

    leading_noise = re.compile(
        r"^(?:"
        r"play\s*/\s*pause|"
        r"play|pause|kuuntele|"
        r"jaa\s+jakso|jaa|kopioi\s+linkki|"
        r"lue\s+lisää|näytä\s+vähemmän|"
        r"\d{1,2}\.\d{1,2}\.\d{4}(?:\s+(?:klo\s*)?\d{1,2}[.:]\d{2})?"
        r")[\s:\-–/]*",
        re.I,
    )
    while True:
        updated = leading_noise.sub("", text).strip()
        if updated == text:
            break
        text = updated

    text = re.sub(r"\bplay\s*/\s*pause\b", " ", text, flags=re.I)
    text = re.sub(r"\b(?:jaa\s+jakso|kopioi\s+linkki|lue\s+lisää|näytä\s+vähemmän)\b", " ", text, flags=re.I)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def extract_short_topics(desc: str) -> str:
    if ":" in desc:
        head, tail = desc.split(":", 1)
        if len(head) < 140 and len(tail.strip()) > 15:
            return tail.strip()
    return desc


def prepare_square_cover(
    session: requests.Session,
    out_dir: Path,
    slug: str,
    cover_url: str | None,
) -> bytes:
    raw_bytes = None
    candidates = [
        out_dir / "cover.jpg",
        out_dir / "cover.png",
        Path(f"cover_{slug}.jpg"),
        Path(f"cover_{slug}.png"),
    ]
    for p in candidates:
        if p.exists():
            print(f"Käytetään paikallista kansikuvaa: {p}")
            raw_bytes = p.read_bytes()
            break

    if raw_bytes is None:
        if not cover_url:
            raise RuntimeError(
                f"Kansikuvaa ei löytynyt sivulta. Tallenna oma kuva nimellä {out_dir / 'cover.jpg'}."
            )
        print(f"Ladataan kansikuva: {cover_url}")
        resp = session.get(cover_url, timeout=15)
        resp.raise_for_status()
        raw_bytes = resp.content

    img = Image.open(io.BytesIO(raw_bytes)).convert("RGB")
    w, h = img.size
    side = min(w, h)
    left = (w - side) // 2
    top = (h - side) // 2
    img = img.crop((left, top, left + side, top + side))
    img = img.resize((1000, 1000), Image.Resampling.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90, optimize=True)
    jpeg_bytes = buf.getvalue()

    (out_dir / "cover.jpg").write_bytes(jpeg_bytes)
    return jpeg_bytes


def format_card_text(raw_text: str) -> str:
    noise_exact = {
        "play",
        "pause",
        "play / pause",
        "play/pause",
        "jaa jakso",
        "jaa",
        "linkki",
        "kopioi linkki",
        "haetaan",
        "paina tästä",
        "lue lisää",
        "näytä vähemmän",
        "kuuntele",
    }
    lines = []
    for ln in raw_text.splitlines():
        s = ln.strip()
        if not s:
            continue
        if s.lower() in noise_exact:
            continue
        if re.match(r"^\d{1,2}:\d{2}(:\d{2})?$", s):
            continue
        lines.append(s)
    return clean_description(" ".join(lines))


async def extract_cards_from_page(page, show_url: str) -> list[dict]:
    print("Avataan ohjelmasivu selaimessa...")
    await page.goto(show_url, wait_until="networkidle")

    for selector in [
        "#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll",
        "button:has-text('Hyväksy')",
        "button:has-text('Allow')",
    ]:
        btn = page.locator(selector).first
        if await btn.is_visible():
            await btn.click()
            await page.wait_for_timeout(500)
            break

    # Ladataan kaikki vanhemmat jaksot näkyviin vierittämällä ja klikkaamalla "Näytä lisää"
    clicks = 0
    while True:
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(400)

        more_btn = page.locator(
            "button:has-text('Näytä lisää'), a:has-text('Näytä lisää'), "
            "button:has-text('Lataa lisää'), a:has-text('Lataa lisää')"
        ).first
        if not await more_btn.is_visible():
            break

        prev_dates = await page.evaluate(
            r"() => (document.body.innerText.match(/\b\d{1,2}\.\d{1,2}\.\d{4}\b/g) || []).length"
        )
        await more_btn.scroll_into_view_if_needed()
        await more_btn.click()
        clicks += 1

        for _ in range(30):
            await page.wait_for_timeout(250)
            cur_dates = await page.evaluate(
                r"() => (document.body.innerText.match(/\b\d{1,2}\.\d{1,2}\.\d{4}\b/g) || []).length"
            )
            if cur_dates > prev_dates:
                break

        await page.wait_for_timeout(300)

    if clicks:
        print(f"Avattiin lisäsivuja: {clicks} kpl")

    # Avataan mahdolliset yksittäisten jaksojen "Lue lisää" -kuvaustekstit
    await page.evaluate("""() => {
        const toggles = Array.from(document.querySelectorAll('button, [role="button"], a'));
        for (const btn of toggles) {
            const txt = (btn.innerText || '').trim().toLowerCase();
            if (txt === 'lue lisää' || txt === 'näytä enemmän') {
                btn.click();
            }
        }
    }""")
    await page.wait_for_timeout(400)

    # Haetaan sisimmät jaksokortit (joissa on päivämäärä ja soittonappi, mutta ei sisäkkäisiä kortteja)
    raw_items = await page.evaluate("""() => {
        const dateRegex = /\\b(\\d{1,2}\\.\\d{1,2}\\.\\d{4})\\b/;
        const isEpisodeCard = (el) => {
            const text = (el.innerText || '').trim();
            const match = text.match(dateRegex);
            return Boolean(
                match &&
                text.length < 6000 &&
                el.querySelector('button, [role="button"], .play')
            );
        };

        const candidates = Array.from(document.querySelectorAll('article, li, div')).filter(isEpisodeCard);
        const innermost = candidates.filter(
            el => !Array.from(el.querySelectorAll('article, li, div')).some(isEpisodeCard)
        );

        const found = [];
        const seenDates = new Set();
        for (const baseEl of innermost) {
            let el = baseEl;
            let cur = baseEl.parentElement;
            while (cur && !['BODY', 'MAIN', 'HTML'].includes(cur.tagName)) {
                if (innermost.filter(x => cur.contains(x)).length > 1) break;
                el = cur;
                cur = cur.parentElement;
            }
            const text = (el.innerText || '').trim();
            const match = text.match(dateRegex);
            if (match && !seenDates.has(match[1])) {
                seenDates.add(match[1]);
                el.setAttribute('data-scraper-id', String(found.length));
                found.push({
                    id: found.length,
                    date: match[1],
                    text: text
                });
            }
        }
        return found;
    }""")

    return raw_items


async def capture_audio_for_card(page, item_id: int) -> str | None:
    captured_url = None

    def handle_request(req):
        nonlocal captured_url
        u = req.url
        if req.resource_type == "media" or re.search(r"\.(mp3|m4a|aac|m3u8)(\?|$)", u, re.I):
            captured_url = u

    async def handle_response(res):
        nonlocal captured_url
        if captured_url:
            return
        ct = res.headers.get("content-type", "")
        if "json" in ct:
            try:
                body = await res.text()
                m = re.search(r'https?:\\/\\/[^"\']+\.(?:mp3|m4a|aac|m3u8)[^"\']*', body, re.I)
                if m:
                    captured_url = m.group(0).replace("\\/", "/")
            except Exception:
                pass

    page.on("request", handle_request)
    page.on("response", handle_response)

    try:
        container = page.locator(f"[data-scraper-id='{item_id}']").first
        play_btn = container.locator("button, [role='button'], .play").first
        await play_btn.scroll_into_view_if_needed()
        await play_btn.click(force=True)
        for _ in range(45):
            if captured_url:
                break
            await page.wait_for_timeout(200)
    finally:
        page.remove_listener("request", handle_request)
        page.remove_listener("response", handle_response)

    return captured_url


async def collect_episode_urls(
    show_url: str,
    manifest_file: Path,
    force_rescrape: bool = False,
) -> list[dict]:
    if manifest_file.exists() and not force_rescrape:
        episodes = json.loads(manifest_file.read_text(encoding="utf-8"))
        for ep in episodes:
            ep["description"] = clean_description(ep.get("description", ""))
        episodes.sort(key=lambda x: datetime.strptime(x["date"], "%d.%m.%Y"))
        print(f"Luetaan {len(episodes)} jaksoa paikallisesta tiedostosta {manifest_file}...")
        return episodes

    existing_by_date: dict[str, dict] = {}

    episodes: list[dict] = []
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(locale="fi-FI")
        page = await context.new_page()

        raw_items = await extract_cards_from_page(page, show_url)
        if raw_items:
            sorted_dates = sorted(
                (item["date"] for item in raw_items),
                key=lambda d: datetime.strptime(d, "%d.%m.%Y"),
            )
            print(f"Löydettiin sivulta {len(raw_items)} jaksoa ({sorted_dates[0]} – {sorted_dates[-1]}).")
        else:
            print("Löydettiin sivulta 0 jaksoa.")

        for item in raw_items:
            date_str = item["date"]
            cleaned_text = format_card_text(item["text"])

            if date_str in existing_by_date:
                ep = existing_by_date[date_str]
                if cleaned_text:
                    ep["description"] = cleaned_text
                episodes.append(ep)
                continue

            captured_url = await capture_audio_for_card(page, item["id"])
            if captured_url:
                print(f"  [UUSI] {date_str} -> {cleaned_text[:55]}...")
                episodes.append(
                    {
                        "date": date_str,
                        "description": cleaned_text,
                        "url": captured_url,
                    }
                )
            else:
                print(f"  [EI ÄÄNTÄ] {date_str} – sivulla ei ole kuunneltavaa äänitiedostoa")

        await browser.close()

    seen = {ep["date"] for ep in episodes}
    for date_str, ep in existing_by_date.items():
        if date_str not in seen:
            episodes.append(ep)

    episodes.sort(key=lambda x: datetime.strptime(x["date"], "%d.%m.%Y"))
    manifest_file.write_text(json.dumps(episodes, indent=2, ensure_ascii=False), encoding="utf-8")
    return episodes


def realign_existing_files(out_dir: Path, episodes: list[dict], safe_show_name: str) -> None:
    staged: list[tuple[Path, Path]] = []
    for idx, ep in enumerate(episodes, start=1):
        dt = datetime.strptime(ep["date"], "%d.%m.%Y")
        iso_date = dt.strftime("%Y-%m-%d")
        is_mp3 = bool(re.search(r"\.mp3(\?|$)", ep["url"], re.I))
        ext = "mp3" if is_mp3 else "m4a"
        target = out_dir / f"{idx:02d}_{iso_date}_{safe_show_name}.{ext}"
        if target.exists():
            continue

        matches = list(out_dir.glob(f"*_{iso_date}_{safe_show_name}.{ext}"))
        if matches:
            src = matches[0]
            tmp = out_dir / f".realign_{idx:02d}_{iso_date}_{safe_show_name}.{ext}"
            src.rename(tmp)
            staged.append((tmp, target))

    for tmp, target in staged:
        tmp.rename(target)
        print(f"Uudelleennimettiin: -> {target.name}")


def build_album_name(show_title: str, episodes: list[dict]) -> str:
    years = [datetime.strptime(ep["date"], "%d.%m.%Y").year for ep in episodes]
    y_min, y_max = min(years), max(years)
    year_span = f"{y_min}" if y_min == y_max else f"{y_min}–{y_max}"
    return f"{show_title} ({year_span})"


def find_first_mp3_frame_offset(mp3_path: Path) -> int | None:
    """Etsii ensimmäisen aidon MPEG Layer III -äänikehyksen tavusijainnin."""
    bitrates_v1_l3 = [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0]
    samplerates_v1 = [44100, 48000, 32000, 0]

    def frame_length(buf: bytes, pos: int) -> int:
        if pos + 4 > len(buf):
            return 0
        b0, b1, b2 = buf[pos], buf[pos + 1], buf[pos + 2]
        if b0 != 0xFF or (b1 & 0xFE) != 0xFA:
            return 0
        br = bitrates_v1_l3[(b2 >> 4) & 0x0F]
        sr = samplerates_v1[(b2 >> 2) & 0x03]
        pad = (b2 >> 1) & 0x01
        if not br or not sr:
            return 0
        return (144 * br * 1000) // sr + pad

    with open(mp3_path, "rb") as f:
        data = f.read(2 * 1024 * 1024)

    for pos in range(len(data) - 8):
        fl1 = frame_length(data, pos)
        if fl1 > 0 and frame_length(data, pos + fl1) > 0:
            return pos
    return None


def download_and_tag(args: tuple) -> None:
    idx, total, ep, cover_bytes, out_dir, safe_show_name, show_title, album_name = args
    dt = datetime.strptime(ep["date"], "%d.%m.%Y")
    iso_date = dt.strftime("%Y-%m-%d")
    base_name = f"{idx:02d}_{iso_date}_{safe_show_name}"
    url = ep["url"]

    is_mp3 = bool(re.search(r"\.mp3(\?|$)", url, re.I))
    ext = "mp3" if is_mp3 else "m4a"
    out_path = out_dir / f"{base_name}.{ext}"
    tmp_path = out_dir / f"{base_name}.{ext}.part"

    if out_path.exists() and out_path.stat().st_size >= 100_000:
        print(f"Päivitetään metatiedot ({idx}/{total}): {out_path.name}")
    else:
        if out_path.exists():
            out_path.unlink()
        if tmp_path.exists():
            tmp_path.unlink()

        print(f"Ladataan ({idx}/{total}): {out_path.name}")
        if is_mp3:
            session = build_http_session()
            with session.get(url, stream=True, timeout=(15, 60)) as r:
                r.raise_for_status()
                expected_len = int(r.headers.get("Content-Length", 0))
                downloaded = 0
                with open(tmp_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1024 * 256):
                        if chunk:
                            f.write(chunk)
                            downloaded += len(chunk)
                if expected_len and downloaded < expected_len:
                    raise IOError(f"Lataus jäi kesken ({downloaded}/{expected_len} tavua)")
        else:
            cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", url, "-c", "copy", str(tmp_path)]
            res = subprocess.run(cmd)
            if res.returncode != 0:
                subprocess.run(
                    ["ffmpeg", "-y", "-loglevel", "error", "-i", url, "-c:a", "aac", "-b:a", "192k", str(tmp_path)],
                    check=True,
                )

        tmp_path.replace(out_path)

    full_desc = clean_description(ep["description"])
    topics = extract_short_topics(full_desc)
    display_title = f"{idx:02d} ({ep['date']}) – {topics}" if topics else f"Jakso {idx:02d} ({ep['date']})"

    if ext == "mp3":
        first_frame = find_first_mp3_frame_offset(out_path)
        if first_frame is not None and first_frame > 0:
            raw_audio = out_path.read_bytes()[first_frame:]
            out_path.write_bytes(raw_audio)

        tags = ID3()
        tags.add(TIT2(encoding=1, text=display_title))
        tags.add(TALB(encoding=1, text=album_name))
        tags.add(TPE1(encoding=1, text=show_title))
        tags.add(TRCK(encoding=1, text=f"{idx}/{total}"))
        tags.add(TDRC(encoding=1, text=iso_date))
        tags.add(COMM(encoding=1, lang="eng", desc="", text=full_desc or display_title))
        tags.add(
            APIC(
                encoding=0,
                mime="image/jpeg",
                type=3,
                desc="Cover",
                data=cover_bytes,
            )
        )
        tags.save(out_path, v1=0, v2_version=3)

        new_first_frame = find_first_mp3_frame_offset(out_path)
        if new_first_frame is not None and new_first_frame > 10:
            data = out_path.read_bytes()
            pos = 10
            while pos + 10 <= new_first_frame:
                if data[pos:pos+4] == b"\x00\x00\x00\x00":
                    break
                fsize = int.from_bytes(data[pos+4:pos+8], "big")
                pos += 10 + fsize
            if pos < new_first_frame:
                body_size = pos - 10
                syncsafe = bytes([
                    (body_size >> 21) & 0x7F,
                    (body_size >> 14) & 0x7F,
                    (body_size >> 7) & 0x7F,
                    body_size & 0x7F,
                ])
                out_path.write_bytes(b"ID3" + data[3:6] + syncsafe + data[10:pos] + data[new_first_frame:])
    else:
        audio = MP4(out_path)
        audio["\xa9nam"] = [display_title]
        audio["\xa9alb"] = [album_name]
        audio["\xa9ART"] = [show_title]
        audio["\xa9day"] = [iso_date]
        audio["\xa9cmt"] = [full_desc or display_title]
        audio["trkn"] = [(idx, total)]
        audio["covr"] = [MP4Cover(cover_bytes, imageformat=MP4Cover.FORMAT_JPEG)]
        audio.save()


def write_episode_index(
    episodes: list[dict],
    out_dir: Path,
    show_url: str,
    show_title: str,
    safe_show_name: str,
) -> Path:
    lines = [
        f"# {show_title} – Jaksolista",
        "",
        f"- **Lähde:** {show_url}",
        f"- **Jaksoja yhteensä:** {len(episodes)} kpl (kronologisessa järjestyksessä)",
        "",
        "---",
        "",
    ]

    for idx, ep in enumerate(episodes, start=1):
        dt = datetime.strptime(ep["date"], "%d.%m.%Y")
        iso_date = dt.strftime("%Y-%m-%d")
        ext = "mp3" if re.search(r"\.mp3(\?|$)", ep["url"], re.I) else "m4a"
        filename = f"{idx:02d}_{iso_date}_{safe_show_name}.{ext}"

        clean_desc = clean_description(ep["description"]) or "*(Ei erillistä jaksokuvausta)*"

        lines.append(f"### {idx:02d}. {ep['date']} (`{filename}`)")
        lines.append("")
        lines.append(clean_desc)
        lines.append("")

    index_path = out_dir / "JAKSOLISTA.md"
    index_path.write_text("\n".join(lines), encoding="utf-8")
    return index_path


async def main():
    parser = argparse.ArgumentParser(
        description="Lataa Radio Helsingin ohjelman on-demand-jaksot BookPlayer-valmiina pakettina."
    )
    parser.add_argument(
        "url",
        nargs="?",
        default=DEFAULT_URL,
        help="Radio Helsingin ohjelmasivun URL-osoite",
    )
    parser.add_argument(
        "--uudelleen-haku",
        action="store_true",
        help="Hakee kaikki jaksolinkit ja kuvaukset uudelleen sivulta",
    )
    args = parser.parse_args()
    show_url = args.url.strip()

    session = build_http_session()
    slug = extract_url_slug(show_url)
    if slug == "rane-raitsikka-tokela" and Path("Rane_Raitsikka_ja_Tokela/cover.jpg").exists() and not args.uudelleen_haku:
        show_title, cover_url = "Rane Raitsikka & Tokela", None
    else:
        show_title, cover_url = fetch_show_info(session, show_url)
    safe_show_name = slugify_filename(show_title)

    out_dir = Path(safe_show_name)
    manifest_file = Path(f"manifest_{slug}.json")

    legacy_manifest = Path("manifest.json")
    if slug == "rane-raitsikka-tokela" and legacy_manifest.exists() and not manifest_file.exists():
        legacy_manifest.rename(manifest_file)

    print(f"Ohjelma:  {show_title}")
    print(f"Kansio:   {out_dir}/")
    print(f"Manifest: {manifest_file}\n")

    out_dir.mkdir(exist_ok=True)
    cover_bytes = prepare_square_cover(session, out_dir, slug, cover_url)
    episodes = await collect_episode_urls(show_url, manifest_file, force_rescrape=args.uudelleen_haku)
    if not episodes:
        print("Jaksoja ei löytynyt. Tarkista sivun rakenne.")
        return

    realign_existing_files(out_dir, episodes, safe_show_name)

    album_name = build_album_name(show_title, episodes)
    tasks = [
        (i + 1, len(episodes), ep, cover_bytes, out_dir, safe_show_name, show_title, album_name)
        for i, ep in enumerate(episodes)
    ]
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(download_and_tag, tasks))

    index_path = write_episode_index(episodes, out_dir, show_url, show_title, safe_show_name)
    print(f"Jaksolista tallennettu: {index_path.resolve()}")
    print(f"\nValmis! {len(episodes)} jaksoa käsitelty kansioon: {out_dir.resolve()}")


if __name__ == "__main__":
    asyncio.run(main())
    