# Rane Raitsikka ja Tokela – Podcast-arkisto ja RSS-julkaisu

## Projektin tavoite ja arkkitehtuuri
Tässä repositoriossa koostetaan Radio Helsingin *Rane Raitsikka ja Tokela* -ohjelman jaksoista Podcasting 2.0 -yhteensopiva RSS-syöte (`feed.xml`) sekä hakukelpoinen arkistosivu (`index.html`) GitHub Pagesiin.
- **Äänitiedostot:** Linkitetään suoraan Radio Helsingin S3-osoitteisiin (`data/manifest_rane-raitsikka-tokela.json` sisältää kaikkien 45 jakson metatiedot, päivämäärät ja URL-osoitteet). Älä koskaan lataa tai puske MP3-tiedostoja Git-repositorioon (`.gitignore` suojaa `Rane_Raitsikka_ja_Tokela/`-kansion).
- **Yksityisyys:** Syöte sisältää `<itunes:block>yes</itunes:block>`- ja `<podcast:locked>yes</podcast:locked>`-tagit, jotta se pysyy suoralla RSS-linkillä tilattavana arkistona eikä indeksoidu julkisiin hakemistoihin.
- **Julkaisu:** Syötettä julkaistaan kausittain tai erissä sitä mukaa, kun jaksojen Whisper-litteroinnit (`Rane_Raitsikka_ja_Tokela/*.srt`) valmistuvat.

## Kansiorakenne
- `feed.xml`, `index.html`, `cover.jpg`: Julkaistava RSS-syöte, verkkosivu ja kansikuva (juuressa GitHub Pagesia varten).
- `chapters/chapters_XX.json`: Podcasting 2.0 -luvut (juokseva tiedostonumerointi `01`–`45`).
- `transcripts/transcript_XX.vtt`: Siivotut WebVTT-tekstitykset (`scripts/clean_transcripts.py`).
- `analyysit/jaksoXX_analyysi.md`: Jaksojen markdown-analyysit ja kiinnostavimmat nostot.
- `data/`: `manifest_rane-raitsikka-tokela.json`, `audio_metadata.json` (kestot ja S3 `Content-Length` -tavukoot), `episode_descriptions.json` (generoidut esittelytekstit puuttuville jaksoille).
- `scripts/`: `build_feed.py`, `clean_transcripts.py`, `update_feed_and_repo.sh`, `publish_pages.sh`, `litteroi_jaksot.py`, `hae_jaksot.py` ja `legacy/`.
- `docs/prompt_chapters.md`: Yksityiskohtaiset ohjeet `.srt`-litterointien jäsentämiseen.
- `Rane_Raitsikka_ja_Tokela/`: Paikalliset `.mp3`- ja raaka-`.srt`-tiedostot (`.gitignore`ssa).

## Nimeämiskäytäntö (`Kausi X, Jakso Y`)
- Käytä kaikissa otsikoissa (`feed.xml`:n `<title>` ja `chapters/chapters_XX.json`:n `"title"`) muotoa **`Kausi X, Jakso YY`** pelkän juoksevan jaksonumeron sijaan (esim. `Rane Raitsikka ja Tokela – Kausi 1, Jakso 01` ja `Kausi 1, Jakso 01 (2017-03-01)`).
- Virallinen kausijako:
  - **Kausi 1 (kevät 2017):** `1.3.2017 – 24.5.2017` → tiedostot `01`–`13` (`Kausi 1, Jakso 01`–`13`)
  - **Kausi 2 (syksy 2017 – alkuvuosi 2018):** `30.8.2017 – 3.1.2018` → tiedostot `14`–`32` (`Kausi 2, Jakso 01`–`19`)
  - **Kausi 3 (syksy 2018):** `26.9.2018 – 19.12.2018` → tiedostot `33`–`45` (`Kausi 3, Jakso 01`–`13`)

## Lukujen (chapters) ja kuvausten generointisäännöt
Kun käsittelet uuden `.srt`-tiedoston (noudata lisäksi `docs/prompt_chapters.md`-tiedoston sääntöjä):
1. **Musiikin ja puheen erottelu:** Erottele jokainen soitettu kappale omaksi luvukseen muodossa `Musiikki: Artisti – Kappale` (tai `Musiikki: Artisti`). Tunnista biisien alkukohdat Whisperin musiikkihallusinaatioiden (*"Kiitos kun katsoit videon!"*, *"Yle Areena"*) ja juontojen perusteella.
2. **Puheosuudet:** Tiivistä keskustelut ja mainoskatkot konkreettisilla aiheilla, bändeillä, henkilöillä ja tarinoilla (ei ympäripyöreitä otsikoita).
3. **Kielenhuolto:** Korjaa Whisperin litterointivirheet (esim. *Rane Raitsika* -> *Rane Raitsikka*, *Tokala* -> *Tokela*, *Leba* -> *Lepakko*, *Custom Sounds ja Custom Boards*).
4. **Tiedostot ja julkaisuputki:**
   - Tallenna luvut tiedostoon `chapters/chapters_XX.json` (Podcasting 2.0 JSON v1.2.0, `startTime` kokonaissekunteina) ja analyysi tiedostoon `analyysit/jaksoXX_analyysi.md`.
   - **Puuttuvat/geneeriset jaksokuvaukset (esim. jaksot 31, 37, 39, 42–45):** Kirjoita napakka, faktapohjainen 2–4 virkkeen esittelyteksti `.srt`-litteroinnin pohjalta joko `analyysit/jaksoXX_analyysi.md`-tiedoston `## 0. Jaksokuvaus`-osioon tai tiedostoon `data/episode_descriptions.json`.
   - Aja `scripts/update_feed_and_repo.sh --dry-run` (generoi siivotut `.vtt`-tekstitykset, `feed.xml`:n ja `index.html`:n) ennen varsinaista julkaisua `scripts/update_feed_and_repo.sh`.
