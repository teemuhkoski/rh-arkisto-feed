Analysoi liitteenä oleva Radio Helsingin "Rane Raitsikka ja Tokela" -radio-ohjelman .srt-tekstitystiedosto ja jaa koko lähetys kronologisiin lukuihin (chapters).

TÄRKEÄT SÄÄNNÖT ANALYYSIIN:
1. Musiikkikappaleiden tunnistus: Whisper tuottaa musiikin aikana tyypillisesti hallusinaatioita (esim. "Kiitos kun katsoit videon!", "Kiitos.", "Yle Areena", "Tervetuloa!") tai yksittäisiä laulunsanojen pätkiä. Tunnista jokaisen biisin tarkka alkamisaika (kohta, jossa juonto tai mainos loppuu ja musiikki alkaa) ja päättele esittäjä sekä kappaleen nimi juontajien ennen tai jälkeen biisiä sanomista tiedoista sekä lyriikkapätkistä.
2. Musiikin ja puheen erottelu: Merkitse jokainen musiikkikappale omaksi luvukseen muodossa `Musiikki: Artisti – Kappale` (tai `Musiikki: Artisti` jos tarkka kappale ei selviä). Merkitse puheosuudet ja mainoskatkot omiksi luvuikseen tiivistämällä konkreettiset aiheet, bändit, henkilöt, paikat ja tarinat (älä käytä ympäripyöreitä otsikoita kuten "keskustelua musiikista").
3. Nimien ja slangin korjaus: Korjaa Whisperin litterointivirheet (esim. Rane Raitsika -> Rane Raitsikka, Tokala -> Tokela, Leba -> Lepakko, skeba/skitta -> kitara, Custom Sounds ja Custom Boards).
4. Aikaleimat: Käytä tarkkaa muotoa `HH:MM:SS` tekstilistassa ja laske `chapters.json`-tiedostoon tarkka sekuntimäärä (`tunnit*3600 + minuutit*60 + sekunnit`) kokonaislukuna.

TUOTA VASTAUS TÄSMÄLLEEN NÄILLÄ KOLMELLA OSIOLLA:

## 1. RSS-kuvauksen aikaleimalista
Pelkkä tekstilista koodiblokissa (`text`), jossa jokainen rivi on muodossa `HH:MM:SS Otsikko`.

## 2. chapters_XX.json
Validi Podcasting 2.0 -muotoinen JSON-koodiblokki (`json`), jossa `"version": "1.2.0"`, `"title": "Rane Raitsikka ja Tokela – Kausi X, Jakso YY"` (päätelty lähetyspäivän kausijaosta) ja `"chapters"`-taulukossa objektit `{"startTime": 0, "title": "..."}`.

## 0. Jaksokuvaus (vain jos alkuperäinen kuvaus puuttuu tai on geneerinen)
Jos manifestin kuvaus puuttuu tai on vain yleinen mainoslause (esim. jaksot 31, 37, 39, 42–45), kirjoita alkuun osio `## 0. Jaksokuvaus` ja siihen napakka, faktapohjainen 2–4 virkkeen esittelyteksti litteroinnin pohjalta.

## 3. Jakson kiinnostavimmat kohdat
5–6 nostoa lähetyksen parhaista tarinoista, rock-historiasta, studiotekniikasta tai erikoisista sattumuksista aikaleimoineen (`HH:MM:SS`).
