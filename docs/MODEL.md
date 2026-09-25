# Evoluția motorului de predicție

## Starea V7

Implementarea curentă este un model Poisson independent, regularizat, fără coeficienți de calibrare pretins antrenați. Este un reper evaluabil, nu o demonstrație a țintei de 85%. Probabilitățile piețelor provin din aceeași matrice de scoruri.

Distribuția folosește scorurile din intervalul 0–16, normalizate; ratele sunt limitate la 0.25–4.0. Media ligii are un prior de 30 de meciuri (1.5 goluri gazde, 1.2 oaspeți), iar forțele echipelor un prior de 6 observații. Acestea sunt alegeri explicite ale modelului, nu hiperparametri optimizați pe un test.

Istoricul colectat din doar două pagini de rezultate poate reprezenta neuniform liga. Absențele, loturile, transferurile și schimbările de antrenor nu sunt modelate. Schema furnizorului nu garantează separarea scorurilor după prelungiri în toate competițiile; verifică proveniența scorurilor pentru cupe înainte de studii de performanță. Datele demonstrative nu pot dovedi acuratețea pe fotbal real.

## Candidat recomandat: CatBoost

[CatBoost](https://catboost.ai/docs/en/features/categorical-features) acceptă variabile numerice și categorice. Ar putea învăța interacțiuni între forma echipelor, liga, avantajul terenului și forța adversarilor. [LightGBM](https://lightgbm.readthedocs.io/en/stable/Parameters-Tuning.html) este un candidat alternativ; regularizarea este importantă pe seturi mici.

Nu presupunem că un model mai complex este mai bun. Protocolul recomandat:

1. Colectează mai multe sezoane pentru aceleași ligi și păstrează identități stabile ale echipelor.
2. Construiește caracteristici numai din trecut: goluri/xG pe ferestre mobile, diferența Elo, forma acasă/deplasare, nivelul adversarilor și zilele de odihnă. Datele xG/cotele necesită data capturării.
3. Separă cronologic antrenarea, calibrarea și testul. Nu împărți aleator meciurile din același sezon. Grupează meciurile simultane și păstrează pauza necesară de finalizare.
4. Compară CatBoost, Poisson și un reper simplu pe aceleași meciuri și aceleași piețe. Măsoară log-loss/Brier pe toate predicțiile și rata de reușită plus acoperirea la prag fix.
5. Calibrează probabilitățile pe o perioadă separată. Alege pragurile înainte de test; nu modifica repetat modelul după rezultatele aceleiași perioade de test.
6. Rulează noul model în paralel, fără a înlocui retrospectiv jurnalul. Promovează-l doar dacă îmbunătățirea se păstrează pe o perioadă ulterioară.

Un scor de 85% pe selecții rare de șansă dublă nu este echivalent cu 85% pe 1X2 pentru toate meciurile. Raportarea trebuie să păstreze această distincție. Nu există în V7 un model CatBoost/LightGBM antrenat, un ensemble sau o garanție de randament.
