# FootyPreds în Excel fără macro-uri (Power Query)

Power Query (Date > Obținere date) poate citi direct tabelele CSV ale API-ului local.
Nu ai nevoie de macro-uri, de fișier `.xlsm` sau de setări de încredere. Actualizezi
totul cu **Date > Reîmprospătare totală** (Refresh All, `Ctrl+Alt+F5`).

Ce nu face Power Query: nu are butoane și nu formatează foile ca modulul VBA. Nici
analiza completă FlashScore a unui meci (`POST /api/excel/analyze/{id}`) nu se
potrivește aici, pentru că s-ar repeta la fiecare reîmprospătare. Pentru ea folosește
`FootyPreds.bas` sau aplicația web. Datele salvate de analiză (formă, H2H) apar apoi și
în tabelele Power Query.

Înainte de toate, pornește serverul cu `start.ps1` (`http://127.0.0.1:8000`).

## Varianta rapidă: Din web

1. **Date > Din web** (Data > From Web).
2. Lipește adresa, de exemplu:
   `http://127.0.0.1:8000/api/excel/predictions?day=2026-09-25`
3. La prima conectare alege **Anonim** (Anonymous) > **Conectare**.
4. În fereastra de previzualizare apasă **Transformare date**. Numerele din API au
   **punct zecimal**. Pe un Windows în limba română, setează conversia cu cultura
   engleză: selectează coloanele numerice > **Tip de date > Utilizând setările
   regionale...** > *Zecimal* + *Engleză (Statele Unite)*. Altfel, `0.5432` poate deveni
   `5432` sau text. Poți seta și **Fișier > Opțiuni > Registrul curent > Setări
   regionale > Engleză (Statele Unite)** pentru tot registrul.
5. **Închidere și încărcare**.

Adrese utile (toate acceptă `format=csv`, implicit):

| Tabel | Adresă |
|---|---|
| Starea serverului | `/api/excel/health` |
| Predicțiile zilei | `/api/excel/predictions?day=2026-09-25` |
| Competițiile zilei | `/api/excel/competitions?day=2026-09-25` |
| Valoare (EV pozitiv) | `/api/excel/value?day=2026-09-25` |
| Un meci | `/api/excel/match/ID?section=summary` (`markets`, `scores`, `grid`, `htft`, `form`, `formstats`, `h2h`, `insights`, `standings`) |
| Registrul prospectiv | `/api/excel/record?section=rows` (`metrics`, `calibration`) |
| Recomandări AI | `/api/excel/recommendations?day=2026-09-25` (`section=legs` implicit, `tickets`, `singles`) |
| Live | `/api/excel/live?sport=football` (`section=matches` implicit, `markets`) |
| Simulare | `/api/excel/simulate?dataset=football&strategy=ladder&bankroll=5&target_odds=2` (`section=days` implicit, `summary`, `ladders`, `legs`, `equity`) |
| Seturi de date ale simulatorului | `/api/excel/simulate/datasets` |
| Zile recente (progres) | `/api/excel/simulate/recent?days=14&sports=football,tennis` |
| Portofel virtual | `/api/excel/wallet` (`section=summary` implicit, `bets`, `legs`, `history`) |

**Sport.** Predicțiile, competițiile, valoarea și meciurile acceptă `sport=football`
(implicit), `sport=basketball` sau `sport=tennis`. Fotbalul păstrează coloanele de până acum;
baschetul și tenisul au coloanele lor (1/2, handicap și total de puncte, total și scor la seturi,
suprafață, puncte sau game-uri estimate). Un meci de alt sport decât `sport` dă eroarea 404,
iar secțiunile `scores`, `grid` și `htft` există doar pentru fotbal.

Filtre pentru predicții și valoare: `competition=england|premier league`
(ID-ul din tabelul de competiții), `min_grade=C`, `upcoming_only=1`, `limit=400`,
`demo=1` (meciuri sintetice de fotbal, fără cheie RapidAPI).

Coloanele `home_logo`, `away_logo` și `league_logo` conțin adresa imaginii prin serverul
local (`http://127.0.0.1:8000/api/img?u=...`); Excel le afișează ca text.

În CSV, un text care începe cu `=`, `+`, `-` sau `@` (de exemplu o echipă numită
`=Steaua`) primește un apostrof în față, ca să nu devină formulă când fișierul e
deschis cu dublu-click. Varianta rapidă păstrează apostroful în tabel. Funcția
`FootyPreds` de mai jos cere `format=tsv`, care trimite valorile exact, fără apostrof.

## Varianta completă: interogări cu parametri

Creează interogările de mai jos din **Date > Obținere date > Din alte surse >
Interogare necompletată**, apoi **Editor complex** (Advanced Editor) și lipește codul.
Numele interogării este cel din titlu (click dreapta > Redenumire).

### 1. `ApiUrl` (parametru)

```m
"http://127.0.0.1:8000" meta [IsParameterQuery = true, Type = "Text", IsParameterQueryRequired = true]
```

### 2. `Zi` (ziua meciurilor, UTC)

Lasă `Fix` gol pentru ziua de azi sau scrie o dată, de exemplu `"2026-09-25"`.

```m
let
    Fix = "",
    Azi = Date.ToText(Date.From(DateTimeZone.RemoveZone(DateTimeZone.UtcNow())), "yyyy-MM-dd"),
    Rezultat = if Fix = "" then Azi else Fix
in
    Rezultat
```

### 3. `FootyPreds` (funcția care citește orice tabel)

Cere tabelul în TSV (valorile exacte, fără apostroful de protecție din CSV), transformă
erorile API (coloana `error`) în erori Power Query lizibile și convertește numerele cu
cultura `en-US`, indiferent de setările Windows. Un răspuns care nu e tabelul API (un
server pornit înainte de clientul Excel, sau o adresă cu numele calculatorului în loc
de `127.0.0.1`) dă un mesaj clar în loc de „The field 'error' of the record wasn't found”.

```m
(cale as text, optional parametri as nullable record) as table =>
let
    Interogare = Record.Combine({if parametri = null then [] else parametri, [format = "tsv"]}),
    Raspuns = Web.Contents(
        Text.TrimEnd(ApiUrl, "/"),
        [
            RelativePath = cale,
            Query = Interogare,
            ManualStatusHandling = {400, 403, 404, 405, 409, 422, 429, 500, 502, 503}
        ]
    ),
    Stare = Value.Metadata(Raspuns)[Response.Status],
    Tabel = Table.PromoteHeaders(
        Csv.Document(Raspuns, [Delimiter = "#(tab)", Encoding = 65001, QuoteStyle = QuoteStyle.None]),
        [PromoteAllScalars = true]
    ),
    ColoaneText = {
        "match_id", "source", "date_utc", "time_utc", "kickoff_utc", "date_local", "time_local",
        "country", "competition", "competition_id", "home", "away", "status", "grade",
        "score_1", "score_2", "score_3", "htft_1", "htft_label_1", "tip_key", "tip_label",
        "selection_key", "selection_label", "value_key", "value_label", "form_home",
        "form_away", "result", "summary", "reason", "version", "warnings", "key", "label",
        "group", "score", "side", "team", "date", "venue", "opponent", "window", "sequence",
        "text", "role", "created_utc", "market_key", "market_label", "range", "error",
        "excel_api", "server_time_utc", "server_time_local", "sports", "sport",
        "main_3_key", "main_3_label", "main_4_key", "main_4_label", "surface",
        "home_logo", "away_logo", "league_logo", "day", "ticket_status", "selections",
        "rationale", "generated_at", "disclaimer", "suggestion", "suggestion_key",
        "suggestion_kind", "suggestion_why", "suggestions", "period", "stage", "clock",
        "pre_match_source", "notes", "updated_at", "odds_note", "why", "dataset",
        "dataset_label", "strategy", "mode", "staking", "start", "end", "stopped",
        "first_run_status", "baseline_label", "method", "id", "hint", "message",
        "currency", "notice", "settled_utc", "bet_id", "at_utc", "type"
    },
    Numerice = List.Difference(Table.ColumnNames(Tabel), ColoaneText),
    Tipuri = Table.TransformColumnTypes(
        Tabel, List.Transform(Numerice, each {_, type number}), "en-US"
    ),
    Mesaj =
        try Text.From(Tabel{0}[error])
        otherwise "Răspuns neașteptat (server vechi sau adresă greșită). "
            & "Repornește start.ps1 și folosește http://127.0.0.1:8000 în ApiUrl.",
    Rezultat =
        if Stare = 200 then Tipuri
        else error Error.Record("FootyPreds", Mesaj, [HTTP = Stare])
in
    Rezultat
```

### 4. Tabelele

Fiecare interogare de mai jos se încarcă într-o foaie (click dreapta > **Încărcare în...**
> Tabel). Valorile din `Query` sunt text.

**Predictii**

```m
let
    Sursa = FootyPreds("api/excel/predictions", [day = Zi, limit = "400"])
in
    Sursa
```

Doar o competiție și nota minimă C, doar meciurile viitoare:

```m
let
    Sursa = FootyPreds(
        "api/excel/predictions",
        [day = Zi, competition = "england|premier league", min_grade = "C", upcoming_only = "1"]
    )
in
    Sursa
```

**Competitii**

```m
let
    Sursa = FootyPreds("api/excel/competitions", [day = Zi])
in
    Sursa
```

**Valoare**

```m
let
    Sursa = FootyPreds("api/excel/value", [day = Zi, min_grade = "C"])
in
    Sursa
```

**TrackRecord**, **Metrici** și **Calibrare**

```m
let
    Sursa = FootyPreds("api/excel/record", [section = "rows"])
in
    Sursa
```

```m
let
    Sursa = FootyPreds("api/excel/record", [section = "metrics"])
in
    Sursa
```

```m
let
    Sursa = FootyPreds("api/excel/record", [section = "calibration"])
in
    Sursa
```

**Meci** (o secțiune a unui meci din ziua încărcată)

Creează și parametrul `IdMeci` ca la `ApiUrl` (tip Text), cu ID-ul din coloana
`match_id` a tabelului Predictii.

```m
let
    Sursa = FootyPreds(
        "api/excel/match/" & Uri.EscapeDataString(IdMeci), [section = "scores"]
    )
in
    Sursa
```

Secțiuni: `summary`, `markets`, `scores`, `grid`, `htft`, `form`, `formstats`, `h2h`,
`insights`, `standings`. `standings` poate folosi o cerere FlashScore dacă meciul nu a
fost încă analizat. Pentru baschet sau tenis adaugă sportul (fără `scores`, `grid`, `htft`):

```m
let
    Sursa = FootyPreds(
        "api/excel/match/" & Uri.EscapeDataString(IdMeci), [section = "markets", sport = "tennis"]
    )
in
    Sursa
```

**Predictii baschet** / **Predictii tenis**

```m
let
    Sursa = FootyPreds("api/excel/predictions", [day = Zi, sport = "basketball", limit = "400"])
in
    Sursa
```

```m
let
    Sursa = FootyPreds("api/excel/predictions", [day = Zi, sport = "tennis", limit = "400"])
in
    Sursa
```

**Recomandari** (biletele AI x2, x5, x10, x100 și selecțiile simple)

Un rând pe selecție (cu coloanele biletului: `target`, `ticket_status`,
`ticket_total_odds`, ...), apoi rezumatul biletelor și selecțiile simple. Prima cerere a zilei
generează recomandările (poate dura); următoarele citesc biletele salvate. `sports` și
`targets` sunt opționale (implicit toate sporturile și `2,5,10,100`). Nu adăuga
`refresh = "1"` într-o interogare care se reîmprospătează singură: ar regenera biletele la
fiecare reîmprospătare.

```m
let
    Sursa = FootyPreds("api/excel/recommendations", [day = Zi])
in
    Sursa
```

```m
let
    Sursa = FootyPreds(
        "api/excel/recommendations",
        [day = Zi, section = "tickets", sports = "football,tennis", targets = "2,5,10,100"]
    )
in
    Sursa
```

```m
let
    Sursa = FootyPreds("api/excel/recommendations", [day = Zi, section = "singles"])
in
    Sursa
```

**Live** (un rând pe meci; `section = "markets"` dă toate piețele live)

Probabilitățile sunt pe rezultatul final. Cotele din listă sunt de dinainte de meci, deci
tabelul arată cota corectă și cota minimă (`suggestion_min_odds`), nu prețuri live. Serverul
păstrează lista 30 de secunde, așa că o reîmprospătare la un minut este suficientă.

```m
let
    Sursa = FootyPreds("api/excel/live", [sport = "football"])
in
    Sursa
```

```m
let
    Sursa = FootyPreds("api/excel/live", [sport = "tennis", section = "markets"])
in
    Sursa
```

**Simulare** (scara: tot câștigul se joacă a doua zi)

Parametrii sunt cei ai `POST /api/simulate`: `dataset` (`football`, `football-plus`,
`tennis`, `local-football`, `local-basketball`, `local-tennis`, `recent`), `bankroll`,
`strategy` (`ladder`, sau `flat` cu `mode = "ticket"` / `"singles"`), `target_odds`,
`reinvest` (0–1, implicit 1 = tot), `restart_on_loss` (`1`/`0`), `max_days`, `start`, `end`,
`stake`; pentru `recent`: `days` (1–60) și `sports`. Secțiuni: `days` (jurnalul pe zile,
implicit), `summary`, `ladders`, `legs`, `equity`. Toate secțiunile aceleiași simulări sunt
calculate o singură dată (serverul le păstrează un minut).

```m
let
    Sursa = FootyPreds(
        "api/excel/simulate",
        [dataset = "football", strategy = "ladder", bankroll = "5", target_odds = "2",
         reinvest = "1", restart_on_loss = "1", section = "summary"]
    )
in
    Sursa
```

```m
let
    Sursa = FootyPreds(
        "api/excel/simulate",
        [dataset = "football", strategy = "ladder", bankroll = "5", target_odds = "2"]
    )
in
    Sursa
```

Ultimele zile (`dataset = "recent"`): zilele lipsă se descarcă din FlashScore, câte o cerere
pe zi și sport. Power Query nu pornește descărcarea (ar face-o la fiecare reîmprospătare):
apasă **Rulează simularea** în foaia Simulare (modulul VBA) sau folosește pagina Simulator a
aplicației web, apoi citește aici progresul și rezultatul.

```m
let
    Sursa = FootyPreds("api/excel/simulate/recent", [days = "14", sports = "football,tennis"])
in
    Sursa
```

```m
let
    Sursa = FootyPreds(
        "api/excel/simulate",
        [dataset = "recent", days = "14", sports = "football,tennis", strategy = "ladder",
         bankroll = "5", target_odds = "2"]
    )
in
    Sursa
```

**Seturi de date**

```m
let
    Sursa = FootyPreds("api/excel/simulate/datasets")
in
    Sursa
```

**Portofel** (bani virtuali; pariurile se plasează din aplicația web)

```m
let
    Sursa = FootyPreds("api/excel/wallet", [section = "bets"])
in
    Sursa
```

Secțiuni: `summary` (sold, depus, profit), `bets`, `legs` (selecțiile fiecărui pariu),
`history` (mișcările de bani).

### 5. Reîmprospătare

- **Date > Reîmprospătare totală** (Refresh All, `Ctrl+Alt+F5`) actualizează toate tabelele.
- Pentru actualizare automată: **Date > Interogări și conexiuni** > click dreapta pe
  interogare > **Proprietăți** > bifează *Reîmprospătare la fiecare 10 minute* și/sau
  *Reîmprospătare date la deschiderea fișierului*.
- Pentru altă zi, schimbă `Fix` în interogarea `Zi` (sau parametrul `ApiUrl` pentru alt
  port) și apasă Refresh All.

## Probleme frecvente

- **DataSource.Error / „Nu se poate conecta”**: serverul nu rulează. Pornește `start.ps1`.
- **Expression.Error cu un mesaj în română**: mesajul vine din coloana `error` a API-ului,
  de exemplu o dată greșită (`AAAA-LL-ZZ`), un ID de meci care nu e în baza locală, un meci
  de alt sport (adaugă `sport = "tennis"` sau `"basketball"`), un set de date nedescărcat
  sau limita RapidAPI.
- **„Serverul nu are încă această funcție”**: serverul pornit este mai vechi decât clientul;
  actualizează proiectul și repornește `start.ps1`.
- **„Răspuns neașteptat (server vechi sau adresă greșită)”**: serverul pornit nu are încă
  `/api/excel` (repornește `start.ps1`) sau `ApiUrl` folosește numele calculatorului ori
  IP-ul din rețea; serverul acceptă doar `127.0.0.1` și `localhost`.
- **Un apostrof în fața unui nume** (`'=Steaua`): tabelul vine din varianta rapidă (CSV).
  Folosește funcția `FootyPreds` (TSV).
- **Numere uriașe sau text în coloanele de procente**: conversia nu a folosit `en-US`
  (vezi pasul 4 al variantei rapide sau funcția `FootyPreds`).
- **Formula.Firewall** când combini interogările cu celule din registru: **Date > Obținere
  date > Opțiuni interogare > Registrul curent > Confidențialitate > Ignorare niveluri de
  confidențialitate**, doar pentru acest registru local.
