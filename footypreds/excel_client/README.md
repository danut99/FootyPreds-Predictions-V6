# FootyPreds pentru Excel

Un client Excel pentru API-ul local FootyPreds, separat de aplicația web. Excel cere
datele de la serverul pornit cu `start.ps1` și le pune în foi formatate, pentru **fotbal,
baschet și tenis**: predicțiile zilei, pagina unui meci, forma, scorul corect (fotbal),
valoarea (EV), track record-ul, recomandările AI (bilete x2, x5, x10, x100), meciurile live,
simulatorul (inclusiv strategia „scară”) și portofelul virtual. Totul se reîncarcă din
butoane, la fel ca uneltele Excel de predicții de pe piață.

Clientul are două variante:

| Variantă | Fișier | Când o folosești |
|---|---|---|
| Macro-uri VBA (recomandat) | `FootyPreds.bas` | Butoane, analiză completă FlashScore, foi formatate |
| Power Query (fără macro-uri) | `PowerQuery.md` | Tabele care se actualizează cu Date > Reîmprospătare totală |

Ambele citesc aceleași tabele de la `http://127.0.0.1:8000/api/excel/...`.

## Cerințe

- Excel 2016, 2019, 2021 sau Microsoft 365 pentru Windows, pe 32 sau 64 de biți.
  Modulul nu declară funcții Windows (`Declare`) și nu cere referințe
  (Tools > References), deci merge la fel pe 32 și pe 64 de biți.
- Serverul FootyPreds pornit pe același calculator: dublu-click pe `start.ps1`
  din rădăcina proiectului (sau în PowerShell: `.\start.ps1`). Lasă fereastra deschisă.
- Pentru meciuri reale: `RAPIDAPI_KEY` în fișierul `.env`. Fără cheie funcționează
  doar **Mod demo = DA**, cu meciuri sintetice de fotbal: predicțiile, valoarea și analiza
  unui meci (fără cereri FlashScore, fără clasament și fără salvare în registru), plus
  simulatorul pe arhivele descărcate și portofelul.

## Instalare (o singură dată)

### Varianta automată

Dacă ai Excel desktop, `pywin32` și opțiunea
*Trust access to the VBA project object model* activată:

```powershell
.venv\Scripts\python.exe -m pip install pywin32
.venv\Scripts\python.exe footypreds\excel_client\build_xlsm.py
```

Scriptul creează `footypreds\excel_client\FootyPreds.xlsm`, importă modulul și rulează
`Setup` într-un Excel ascuns (unde `Setup` nu afișează ferestre de mesaj). Verifică apoi
foile și mesajul din Panou!B15; dacă `Setup` a eșuat, scriptul afișează eroarea și pașii
manuali și nu salvează nimic. Dacă lipsește ceva, afișează pașii manuali de mai jos.

### Varianta manuală

1. Deschide Excel > **Registru de lucru necompletat**.
2. Apasă **Alt+F11** (editorul VBA) > **File > Import File...** și alege
   `footypreds\excel_client\FootyPreds.bas`.
3. Închide editorul. Apasă **Alt+F8**, alege **Setup** > **Run**.
4. **Fișier > Salvare ca** > tipul *Registru de lucru Excel cu macrocomenzi (\*.xlsm)*.
   Un `.xlsx` pierde macro-urile.

`Setup` creează foile Panou, Predictii, Meci, Forma, ScorCorect, Valoare, TrackRecord,
Recomandari, Live, Simulare, Portofel, Ajutor și foaia ascunsă Liste. Șterge doar foile
goale care nu sunt ale lui (de exemplu Foaie1 dintr-un registru nou). Îl poți rula oricând
din nou: păstrează valorile din Panou și setările din Recomandari și Simulare.

**Actualizare de la o versiune mai veche a modulului.** Importă noul `FootyPreds.bas`
(șterge întâi modulul vechi din editorul VBA) și rulează `Setup`: foile noi apar, iar Panou
primește rândul **Sport** (B3). Un buton nou apăsat într-un registru vechi creează singur
foile care lipsesc.

## Foaia Panou

| Celulă | Setare | Implicit |
|---|---|---|
| B3 | Sport | `Fotbal`; `Baschet` sau `Tenis` schimbă predicțiile, competițiile, valoarea, analiza din B13 și foaia Live |
| B4 | Adresa API | `http://127.0.0.1:8000` |
| B5 | Data meciurilor (UTC) | gol = azi în UTC, după ceasul serverului (aceeași zi ca aplicația web și Power Query); poți scrie o dată fixă, ex. `2026-09-25`. Un `=TODAY()` rămas dintr-o versiune veche este tratat tot ca „azi în UTC” |
| B6 | Competiție | `(toate)`; lista se completează la încărcarea predicțiilor |
| B7 | Calitate minimă | `D` (toate meciurile); `A` = doar cele cu cele mai multe date |
| B8 | Doar meciuri viitoare | `NU` |
| B9 | Limită meciuri | `200` (maxim 400; competițiile populare primele). Dacă ziua are mai multe meciuri, titlul foii Predictii arată „N din TOTAL meciuri” |
| B10 | Analiză completă: top N | `10` |
| B11 | Prag selecție (afișare) | `0.85` (între 0.5 și 0.99). Schimbă doar coloana *Selecție (prag Panou)* din foi; registrul prospectiv (track record) folosește mereu pragul de 85%, ca aplicația web |
| B12 | Mod demo | `NU`; `DA` = meciuri sintetice de fotbal, fără cheie RapidAPI |
| B13 | ID meci (opțional) | butonul din Panou analizează acest ID, în sportul din B3; gol = rândul selectat în foaia Predictii (în sportul cu care a fost încărcată) |
| B15 | Stare | ultimul mesaj |

## Butoane (macro-uri)

| Buton | Macro | Ce face | Endpoint |
|---|---|---|---|
| Verifică serverul | `VerificaServer` | Arată versiunea modelului, dacă e configurată cheia RapidAPI și câte rezultate are istoricul | `GET /api/excel/health` |
| Încarcă predicțiile | `IncarcaPredictii` | Umple foaia Predictii: 1X2, șansă dublă, goluri 1.5/2.5/3.5, GG/NG, xG, top 3 scoruri, pauză/final, pont, cote, valoare, formă, rata de victorii. Procente cu scală de culori, note A–D colorate, antet înghețat, filtre. Titlul spune câte meciuri are ziua după filtre (antetul `X-Total-Count`) | `GET /api/excel/predictions`, `GET /api/excel/competitions` |
| Analizează meciul (B13 / Predictii) | `AnalizaMeci` | Analiza completă FlashScore (forma din toate competițiile, H2H, clasament). Din foaia Predictii: rândul selectat. Din Panou: ID-ul din B13, iar dacă B13 e gol, rândul selectat în Predictii. Actualizează rândul și umple Meci, Forma, ScorCorect | `POST /api/excel/analyze/{id}`, `GET /api/excel/match/{id}` |
| Analiză completă: top N (C/D) | `AnalizaCompletaTop` | Rulează analiza completă pe următoarele N meciuri viitoare cu nota C sau D (rândurile vizibile după filtre) care nu au fost analizate de la ultima încărcare a predicțiilor, deci fiecare apăsare merge mai departe în zi. Progres în bara de stare; **Esc** oprește și arată câte analize au reușit. Se oprește singur la limita RapidAPI (429) | `POST /api/excel/analyze/{id}` |
| Valoare (EV pozitiv) | `IncarcaValoare` | Piețele unde probabilitatea modelului × cota − 1 > 0, sortate după EV | `GET /api/excel/value` |
| Track record | `IncarcaTrackRecord` | Registrul prospectiv: acuratețe cu interval 95%, Brier, calibrare pe benzi, fiecare selecție salvată înainte de meci | `GET /api/excel/record` |
| Recomandări AI (bilete) / Încarcă recomandările | `IncarcaRecomandari` | Foaia Recomandari: biletele AI la cotele țintă, selecțiile fiecărui bilet (cu motivul) și cele mai sigure selecții simple | `GET /api/excel/recommendations` |
| Live / Actualizează live | `ActualizeazaLive` | Foaia Live: meciurile în desfășurare ale sportului din B3, scorul, probabilitățile pe rezultatul final, sugestiile și toate piețele live | `GET /api/excel/live` |
| Rulează simularea | `RuleazaSimularea` | Foaia Simulare: rezumatul, scările, jurnalul pe zile și graficul băncii (vezi mai jos) | `GET /api/excel/simulate`, `POST`/`GET /api/excel/simulate/recent` |
| Seturi de date | `IncarcaSeturiDate` | Foaia Simulare: seturile de date disponibile și cum se descarcă cele lipsă | `GET /api/excel/simulate/datasets` |
| Portofel virtual / Actualizează portofelul | `IncarcaPortofel` | Foaia Portofel: soldul, pariurile virtuale (cu selecțiile) și mișcările de bani | `GET /api/excel/wallet` |
| Reconstruiește foile | `Setup` | Recreează foile și butoanele | — |

Dublu-click pe un rând din Predictii pornește și el analiza completă, dacă Excel permite
accesul la proiectul VBA (vezi mai sus); altfel folosește butonul.

Analiza completă folosește până la 3 cereri FlashScore per meci (H2H, clasament și
cotele tuturor piețelor; tenisul nu are clasament), păstrate în cache. Doar analizele
făcute **înainte** de meci, cu nota A–C, intră în registrul prospectiv, exact ca în
aplicația web. La baschet și tenis, foaia Meci arată fișa și piețele sportului (handicap,
total de puncte, total și scor la seturi), iar foaia ScorCorect rămâne doar pentru fotbal.

## Foile noi

**Recomandari.** Setări pe foaie: B3 *Sporturi* (`Toate`, `Fotbal`, `Baschet`, `Tenis`),
B4 *Cote țintă* (`2,5,10,100`, separate prin virgulă, fiecare între 1.2 și 1000), B5
*Regenerează* (`NU`; `DA` recalculează biletele ale căror meciuri nu au început). Ziua vine
din Panou (B5). Prima încărcare a zilei poate dura un minut (analizează meciurile și aduce
cotele din FlashScore, cu un buget fix de cereri); apoi biletele sunt citite din baza
locală. Nu există „probabilitate minimă”: optimizatorul alege cea mai probabilă combinație
de selecții reale pentru cota cerută. Un bilet imposibil apare cu starea *indisponibil* și
motivul.

**Live.** Sportul din Panou (B3). Tabelul de sus are un rând pe meci (scor, minut sau set,
probabilitățile 1/X/2 pe rezultatul final, sugestia cu cota minimă), iar dedesubt sunt toate
piețele live. Cotele din listă sunt de dinainte de meci: Excel arată cota corectă și cota
minimă, nu prețuri live. Serverul păstrează lista 30 de secunde.

**Simulare.** Setări pe foaie:

| Celulă | Setare | Implicit |
|---|---|---|
| B3 | Suma (lei) | `5`: banca de pornire (la scară, miza primei zile) |
| B4 | Set de date | `recent` (ultimele zile); `football`, `football-plus`, `tennis` (arhive), `local-football`, `local-basketball`, `local-tennis` (meciurile salvate). Butonul *Seturi de date* arată ce este disponibil |
| B5 | Sporturi (recent) | `Fotbal` (`Toate` = de 3 ori mai multe cereri FlashScore) |
| B6 | Zile recente | `14` (1–60) |
| B7 | Cotă țintă | `2` (1.2–100) |
| B8 | Strategie | `scara` = un bilet pe zi la cota țintă și tot câștigul se joacă a doua zi; `bilet` = un bilet pe zi cu miză fixă 10% din sumă; `simple` = selecții simple cu miză fixă 10% |
| B9 | Reinvestire (scara) | `1` = tot; `0.5` = jumătate din banca scării |
| B10 | Reia după pierdere | `DA` = a doua zi pornește o scară nouă cu suma inițială (totalul investit se adună) |
| B11, B12 | De la / Până la | gol = ultimul an al setului (nu se folosesc la `recent`) |
| B13 | Încasează după N zile (scara) | gol = niciodată; după N bilete reușite (câștigate sau anulate) scara se încasează și pornește alta cu suma inițială, chiar și cu B10 = `NU` |

Cu setul `recent`, macro-ul întreabă întâi câte cereri FlashScore ar folosi descărcarea
zilelor lipsă (o cerere pe zi și sport, plus 14 zile de formă; zilele salvate sunt sărite):
**Da** descarcă și simulează, **Nu** simulează doar zilele deja salvate, **Anulează** oprește.
Descărcarea așteaptă cu progres în bara de stare (**Esc** oprește); dacă se oprește la
jumătate, simularea continuă pe zilele deja salvate, cu o notă. Simularea este „oarbă”: biletul fiecărei zile se alege doar cu rezultatele de
dinainte de acea zi, apoi se află rezultatul. Rezultatul: rezumatul (prima scară, cea mai
lungă scară și vârful ei, reporniri, total investit și recuperat, zile fără bilet,
comparația cu favoritul casei), tabelul scărilor, jurnalul pe zile cu biletul fiecărei zile
și graficul. La `scara` graficul arată câștigul net cumulat (recuperat − investit), unde se
văd pierderile adunate după fiecare repornire; „Sumă inițială + câștig net” poate fi negativă.

Un singur macro rulează o dată: un clic pe alt buton cât timp unul lucrează este ignorat
(mesaj în bara de stare). Simularea și recomandările așteaptă răspunsul până la 10 minute;
un timeout spune „Serverul încă lucrează”, nu „pornește serverul”. Schimbarea sportului din
Panou (B3) resetează competiția din B6 la „(toate)” dacă aceasta nu există în noul sport.

**Portofel.** Pariurile virtuale se plasează din aplicația web (pagina Portofel); foaia
arată soldul, pariurile cu selecțiile lor și mișcările de bani. Bani fictivi.

## Tabelele API (`/api/excel/*`)

Toate răspund cu **un singur tabel**: un rând de antet cu nume stabile de coloane,
apoi datele. Numele coloanelor sunt constante în `footypreds/excel_api.py`, iar testele
din `footypreds/tests/test_excel_client.py` verifică faptul că fiecare coloană, cale,
parametru și secțiune folosite de `FootyPreds.bas` există în API.

- `?format=csv` (implicit, cu BOM UTF-8, pentru deschiderea directă în Excel) sau
  `?format=tsv` (folosit de macro-uri și de funcția Power Query). Codare UTF-8.
- Numerele au mereu **punct zecimal**, fără notație științifică; probabilitățile sunt
  între 0 și 1; valorile da/nu sunt `1`/`0`; o valoare lipsă este o celulă goală.
- O eroare este un tabel cu coloanele `error` și `status`, plus codul HTTP potrivit
  (422 parametri greșiți, 404 meci necunoscut, 429 limită RapidAPI, 503 cheie lipsă).
- `/predictions` trimite antetul `X-Total-Count`: câte meciuri rămân după filtre,
  înainte de `offset`/`limit`.
- `sport=football|basketball|tennis` (implicit `football`) la `/predictions`,
  `/competitions`, `/match`, `/analyze` și `/value`. Fotbalul păstrează coloanele istorice
  (cele noi, `sport` și siglele, sunt adăugate la sfârșit); baschetul și tenisul au coloane
  proprii. Un meci de alt sport dă 404 cu sportul corect în mesaj; `scores`, `grid` și
  `htft` există doar la fotbal (422 altfel).
- Tabelele de produs (`/recommendations`, `/live`, `/simulate`, `/wallet`) aplatizează
  API-ul JSON al aplicației web, fără reguli proprii. Au parametrul `section` (un tabel pe
  răspuns). Stările rămân în engleză (`pending`, `won`, `lost`, `void`, `unavailable`,
  `skipped`, `open`, `cashed`); macro-urile le traduc.
- Coloanele `home_logo`, `away_logo`, `league_logo` sunt adrese absolute ale imaginilor prin
  serverul local (`http://127.0.0.1:8000/api/img?u=...`); Excel le arată ca text.
- Meciurile demo (`demo-next-0` … `demo-next-5`) nu sunt salvate; `/match` și `/analyze`
  le reconstruiesc din datele sintetice (fără FlashScore, clasament gol, `saved` = 0).

| Endpoint | Parametri |
|---|---|
| `GET /api/excel/health` | — |
| `GET /api/excel/predictions` | `day`, `competition`, `limit`, `offset`, `min_grade`, `upcoming_only`, `threshold`, `demo`, `refresh` |
| `GET /api/excel/competitions` | `day`, `demo`, `refresh` |
| `GET /api/excel/match/{id}` | `section` = `summary`, `markets`, `scores`, `grid`, `htft`, `form`, `formstats`, `h2h`, `insights`, `standings`; `threshold` |
| `POST /api/excel/analyze/{id}` | `enrich` (implicit 1), `refresh`, `threshold` |
| `GET /api/excel/record` | `section` = `rows`, `metrics`, `calibration` |
| `GET /api/excel/value` | `day`, `competition`, `min_grade`, `min_ev`, `upcoming_only`, `threshold`, `demo`, `refresh`, `sport` |
| `GET /api/excel/recommendations` | `day`, `sports` (listă cu virgulă, implicit toate), `targets` (implicit `2,5,10,100`), `refresh`, `section` = `legs` (implicit), `tickets`, `singles` |
| `GET /api/excel/live` | `sport`, `refresh`, `section` = `matches` (implicit), `markets` |
| `GET /api/excel/simulate` | parametrii lui `POST /api/simulate`: `dataset`, `sport`, `sports`, `days`, `bankroll`, `strategy` (`ladder`, `flat`, `percent`, `kelly`), `mode`, `staking`, `stake`, `target_odds`, `reinvest`, `restart_on_loss`, `max_days`, `max_bets_per_day`, `start`, `end`, `seed`; `section` = `days` (implicit), `summary`, `ladders`, `legs`, `equity` |
| `GET /api/excel/simulate/datasets` | — |
| `POST /api/excel/simulate/recent` | `days` (1–60), `sports`: pornește descărcarea zilelor recente lipsă |
| `GET /api/excel/simulate/recent` | `days`, `sports` (opționale): progresul descărcării |
| `GET /api/excel/wallet` | `section` = `summary` (implicit), `bets`, `legs`, `history` |

`predictions`, `competitions`, `match` și `analyze` primesc și `sport`.

Exemplu: <http://127.0.0.1:8000/api/excel/predictions?day=2026-09-25> se deschide direct
în browser sau în Excel (Date > Din web).

## Depanare

**„Serverul FootyPreds nu răspunde”.** Serverul nu rulează sau adresa din B4 e greșită.
Pornește `start.ps1`, așteaptă mesajul `FootyPreds: http://127.0.0.1:8000` și apasă
din nou Verifică serverul. Dacă ai schimbat portul, schimbă și B4.

**Butoanele nu fac nimic / „Macrocomenzile au fost dezactivate”.** Salvează fișierul ca
`.xlsm` și activează conținutul la deschidere (bara galbenă *Activare conținut*).

**„Microsoft a blocat rularea macrocomenzilor” (bara roșie).** Fișierul descărcat de pe
internet sau primit pe e-mail are marcajul *Mark of the Web*. Închide fișierul, click
dreapta pe el în Explorer > **Proprietăți** > bifează **Deblocare** (Unblock) > OK, apoi
deschide-l din nou. Alternativ, pune-l într-un folder adăugat la *Locații de încredere*.
Fișierele create local cu `build_xlsm.py` nu au acest marcaj.

**Excel pe 64 de biți.** Nu e nevoie de nimic special: modulul nu folosește `Declare`
și creează obiectele HTTP cu `CreateObject` (MSXML2.ServerXMLHTTP 6.0, cu rezervă
WinHttp.WinHttpRequest 5.1), disponibile pe 32 și 64 de biți.

**Diacriticele apar ca semne de întrebare.** În foi apar corect (datele sunt UTF-8).
Ferestrele de mesaj (MsgBox) folosesc codepage-ul Windows, așa că mesajele lor sunt
scrise intenționat fără diacritice.

**Numere greșite sau text în loc de procente.** Macro-urile citesc numerele cu `Val()`,
independent de setările regionale (virgulă sau punct). În Power Query folosește
cultura `en-US` la conversie (vezi `PowerQuery.md`).

**„Limita RapidAPI a fost atinsă” (429).** Analizele deja făcute rămân în foi și în
cache; reîncearcă mai târziu. Predicțiile zilei nu consumă cereri noi dacă ziua e deja
în cache.

**„Serverul nu cunoaște această adresă /api/excel” (404) sau „Serverul nu are încă această
funcție”.** Serverul rulează o versiune mai veche decât clientul Excel: actualizează
proiectul și repornește `start.ps1`. Verifică serverul arată sporturile pe care le știe.

**„Meciul ... este de tenis: alege sportul tennis”.** ID-ul din B13 este al altui sport:
schimbă Sport (B3) sau reîncarcă predicțiile acelui sport.

**Simularea cu `recent` spune că zilele „se încarcă încă”.** Descărcarea continuă pe server;
apasă din nou *Rulează simularea* peste un minut. Dacă limita de cereri a fost atinsă,
mesajul apare sub rezultat și simularea rulează pe zilele deja încărcate.

## Securitate

- API-ul ascultă doar pe `127.0.0.1` (vezi `start.ps1`) și acceptă doar gazdele
  `localhost`/`127.0.0.1`. Nu îl expune în rețea și nu schimba adresa din B4 către un
  server străin: registrul trimite cereri doar la adresa din B4.
- Cheia RapidAPI rămâne în `.env` pe server; nu ajunge niciodată în Excel sau în răspunsuri.
- Cererile POST cu antet `Origin` străin sunt refuzate (403), deci o pagină web nu poate
  porni analize în numele tău. Excel nu trimite `Origin`.
- În CSV, textele care încep cu `=`, `+`, `-` sau `@` primesc un apostrof în față, ca un
  nume de echipă să nu devină formulă când deschizi fișierul cu dublu-click. TSV trimite
  valorile exacte: macro-urile scriu celulele ca text, iar funcția Power Query citește TSV.

## Pentru dezvoltatori

- `FootyPreds.bas` trebuie să rămână **ASCII** și cu terminații CRLF
  (`excel_client/.gitattributes` îl păstrează neschimbat în Git):
  editorul VBA importă fișierul cu codepage-ul ANSI. Diacriticele se scriu cu marcaje
  (`{a}` ă, `{a^}` â, `{i^}` î, `{s}` ș, `{t}` ț, majusculele `{A}`, `{A^}`, `{I^}`,
  `{S}`, `{T}`) decodate de funcția `Ro()`.
- Coloanele afișate sunt definite în funcțiile `Layout...` ca `coloana_api|Titlu|format`.
  Formate: `txt`, `int`, `num`, `odd`, `pct` (procent cu scală 0–100%), `pc0` (procent
  fără scală), `hot` (scală relativă pe coloană), `hgr` (o singură scală relativă pentru
  toate coloanele `hgr` ale tabelului, folosită de matricea scorurilor), `ev`, `grd`
  (notă A–D), `wdl`, `yn`, `win`, `mrk` (evidențiază rândul), `sts` (starea unui bilet,
  pariu sau zi simulată, tradusă și colorată) și `spt` (sporturi în română).
- VBA nu poate rula în CI. `test_excel_client.py` verifică static modulul (blocuri
  închise, variabile declarate, proceduri existente, fără legare timpurie, fără `CDbl`
  pe text) și contractul cu API-ul, pe răspunsuri reale ale aplicației de test: o zi de
  fotbal și aplicația multi-sport din `test_e2e_multisport.py` (răspunsurile FlashScore
  capturate, ceasuri înghețate, un set de date de simulare temporar).
- Rulează: `.venv\Scripts\python.exe -m pytest -q footypreds\tests\test_excel_client.py`.
