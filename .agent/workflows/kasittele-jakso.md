---
description: Käsittele uudet litteroidut .srt-jaksot, generoi chapters/chapters_XX.json, siivotut .vtt-tekstitykset ja päivitä feed.xml + index.html
---

1. Tarkista `Rane_Raitsikka_ja_Tokela/`-kansion `.srt`-tiedostot ja vertaa niitä `chapters/chapters_*.json`-tiedostoihin löytääksesi vielä käsittelemättömät jaksot.
2. Lue `GEMINI.md`, `docs/prompt_chapters.md` ja `data/manifest_rane-raitsikka-tokela.json`.
3. Käsittele pyydetyt tai uudet `.srt`-tiedostot suoraan työtilassa (älä käytä maksullista ulkoista API-skriptiä):
   - Luo jokaiselle jaksolle `chapters/chapters_XX.json` ja tallenna analyysi tiedostoon `analyysit/jaksoXX_analyysi.md`.
   - Käytä otsikoinnissa `Kausi X, Jakso YY` -muotoa.
   - Jos jaksolta puuttuu kuvaus manifestista (tai se on geneerinen), kirjoita `## 0. Jaksokuvaus` -osio tiedostoon `analyysit/jaksoXX_analyysi.md` tai lisää kuvaus tiedostoon `data/episode_descriptions.json`.
4. Aja `./scripts/update_feed_and_repo.sh --dry-run` (generoi `transcripts/transcript_XX.vtt`, `feed.xml` ja `index.html`) ja tarkista muutokset ennen julkaisua komennolla `./scripts/update_feed_and_repo.sh`.
