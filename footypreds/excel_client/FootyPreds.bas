Attribute VB_Name = "FootyPreds"
'==============================================================================
' FootyPreds - client Excel pentru API-ul local FootyPreds
'
' Instalare: Alt+F11 > File > Import File... > FootyPreds.bas, apoi Alt+F8 > Setup.
' Serverul trebuie sa ruleze (start.ps1) la adresa din foaia Panou, celula B4
' (implicit http://127.0.0.1:8000). Datele vin din /api/excel/* in format TSV.
' Sporturi: fotbal, baschet si tenis (Panou B3).
'
' Reguli pentru cine modifica fisierul (verificate automat de
' footypreds/tests/test_excel_client.py):
'   * doar caractere ASCII: editorul VBA importa fisierul cu codepage-ul ANSI.
'     Diacriticele se scriu cu marcaje ({a} {A} {a^} {i^} {I^} {s} {S} {t} {T})
'     si sunt convertite de functia Ro();
'   * fara referinte (Tools > References): numai CreateObject (late binding);
'   * numerele din API au punct zecimal: se citesc cu Val(), niciodata cu CDbl;
'   * fiecare coloana citita trebuie sa existe in footypreds/excel_api.py.
'==============================================================================
Option Explicit

Private Const APP_TITLE As String = "FootyPreds"
Private Const DEFAULT_URL As String = "http://127.0.0.1:8000"
Private Const ALL_SPORTS As String = "football,basketball,tennis"

Private Const SH_PANEL As String = "Panou"
Private Const SH_PRED As String = "Predictii"
Private Const SH_MATCH As String = "Meci"
Private Const SH_FORM As String = "Forma"
Private Const SH_SCORE As String = "ScorCorect"
Private Const SH_VALUE As String = "Valoare"
Private Const SH_RECORD As String = "TrackRecord"
Private Const SH_RECO As String = "Recomandari"
Private Const SH_LIVE As String = "Live"
Private Const SH_SIM As String = "Simulare"
Private Const SH_WALLET As String = "Portofel"
Private Const SH_HELP As String = "Ajutor"
Private Const SH_LISTS As String = "Liste"

' Predictii: titlu pe randul 1, antet pe randul 2, meciuri de la randul 3.
Private Const PRED_HEADER As Long = 2

' Celulele din foaia Panou.
Private Const CELL_SPORT As String = "B3"
Private Const CELL_URL As String = "B4"
Private Const CELL_DATE As String = "B5"
Private Const CELL_COMP As String = "B6"
Private Const CELL_GRADE As String = "B7"
Private Const CELL_UPCOMING As String = "B8"
Private Const CELL_LIMIT As String = "B9"
Private Const CELL_TOPN As String = "B10"
Private Const CELL_THRESHOLD As String = "B11"
Private Const CELL_DEMO As String = "B12"
Private Const CELL_MATCH As String = "B13"
Private Const CELL_STATUS As String = "B15"

' Foaia ascunsa Liste: sportul predictiilor incarcate (analiza unui rand il foloseste).
Private Const LIST_LOADED_SPORT As String = "G1"
' Sportul listei de competitii din Liste!A:B (Panou B6 se reseteaza cand sportul difera).
Private Const LIST_COMP_SPORT As String = "G2"

' Foaia Recomandari: setari pe randurile 3-5, butonul pe randul 6, rezultate de la randul 8.
Private Const RECO_SPORTS As String = "B3"
Private Const RECO_TARGETS As String = "B4"
Private Const RECO_REFRESH As String = "B5"
Private Const RECO_FIRST_ROW As Long = 8

' Foaia Live si foaia Portofel: butonul pe randul 2, rezultate de la randul 4.
Private Const LIVE_FIRST_ROW As Long = 4
Private Const WALLET_FIRST_ROW As Long = 4

' Foaia Simulare: setari pe randurile 3-12, butoane pe randul 13, rezultate de la randul 15.
Private Const SIM_AMOUNT As String = "B3"
Private Const SIM_DATASET As String = "B4"
Private Const SIM_SPORTS As String = "B5"
Private Const SIM_DAYS As String = "B6"
Private Const SIM_ODDS As String = "B7"
Private Const SIM_STRATEGY As String = "B8"
Private Const SIM_REINVEST As String = "B9"
Private Const SIM_RESTART As String = "B10"
Private Const SIM_START As String = "B11"
Private Const SIM_END As String = "B12"
Private Const SIM_MAXDAYS As String = "B13"
Private Const SIM_FIRST_ROW As Long = 16
' Cat asteapta simularea incarcarea zilelor recente (secunde).
Private Const RECENT_MAX_WAIT As Long = 600

Private Const ERR_SERVER As Long = vbObjectError + 1001
Private Const ERR_API As Long = vbObjectError + 1002
Private Const ERR_INPUT As Long = vbObjectError + 1003

Private Const CONNECT_TIMEOUT_MS As Long = 5000
Private Const RECEIVE_TIMEOUT_MS As Long = 180000
' Simularea si recomandarile pot calcula minute intregi la prima rulare.
Private Const LONG_RECEIVE_TIMEOUT_MS As Long = 600000
' WinHTTP / ServerXMLHTTP: ERROR_WINHTTP_TIMEOUT (&H80072EE2).
Private Const HTTP_TIMEOUT_ERROR As Long = -2147012894
Private Const MAX_COLUMN_WIDTH As Double = 45

' Dimensiunea ultimului tabel scris de WriteTable.
' Un macro ruleaza deja (DoEvents lasa butoanele sa porneasca altul): vezi IsBusy.
Private m_busy As Boolean
Private m_rows As Long
Private m_cols As Long
' X-Total-Count al ultimului raspuns (-1 daca lipseste): meciurile zilei dupa filtre.
Private m_total As Long
' ID-urile analizate complet de la ultima incarcare a predictiilor ("|id1|id2|").
Private m_analyzed As String

'==============================================================================
' Macro-uri publice (butoane)
'==============================================================================

Public Sub Setup()
    Dim failNumber As Long
    Dim failText As String
    If IsBusy() Then Exit Sub
    On Error GoTo Fail
    BeginWork Ro("Se creeaz{a} foile...")
    BuildWorkbook
    EndWork Ro("Foile sunt gata. Porne{s}te serverul (start.ps1), apoi ap{a}s{a} Verific{a} serverul.")
    ThisWorkbook.Worksheets(SH_PANEL).Activate
    Exit Sub
Fail:
    failNumber = Err.Number
    failText = Err.Description
    EndWork
    ShowError ErrorText(failNumber, failText)
End Sub

Public Sub VerificaServer()
    Dim failNumber As Long
    Dim failText As String
    Dim body As String
    Dim message As String
    If IsBusy() Then Exit Sub
    On Error GoTo Fail
    EnsureReady
    BeginWork Ro("Se verific{a} serverul...")
    body = ApiTable("GET", "/api/excel/health", "")
    message = Ro("Serverul FootyPreds func{t}ioneaz{a}.") & vbLf & vbLf
    message = message & Ro("Adres{a}: ") & BaseUrl() & vbLf
    message = message & "Model: " & FieldOf(body, "version") & vbLf
    message = message & "API Excel: v" & FieldOf(body, "excel_api") & vbLf
    message = message & "Sporturi: " & SportsText(FieldOf(body, "sports")) & vbLf
    message = message & Ro("Cheie RapidAPI configurat{a}: ") & YesNo(FieldOf(body, "api_configured")) & vbLf
    message = message & Ro("Rezultate {i^}n istoric: ") & FieldOf(body, "history_matches") & vbLf
    message = message & "Zile sincronizate: " & FieldOf(body, "synced_days") & vbLf
    message = message & "Ora serverului: " & FieldOf(body, "server_time_local")
    If Len(FieldOf(body, "sports")) = 0 Then
        message = message & vbLf & vbLf & Ro("Serverul este o versiune veche (doar fotbal): actualizeaz{a} proiectul {s}i reporne{s}te start.ps1.")
    End If
    If FieldOf(body, "api_configured") <> "1" Then
        message = message & vbLf & vbLf & Ro("Adaug{a} RAPIDAPI_KEY {i^}n fi{s}ierul .env {s}i reporne{s}te ")
        message = message & Ro("serverul. F{a}r{a} cheie func{t}ioneaz{a} doar Mod demo = DA.")
    End If
    EndWork Ro("Serverul r{a}spunde (") & FieldOf(body, "version") & ")."
    MsgBox Plain(message), vbInformation, APP_TITLE
    Exit Sub
Fail:
    failNumber = Err.Number
    failText = Err.Description
    EndWork
    ShowError ErrorText(failNumber, failText)
End Sub

Public Sub IncarcaPredictii()
    Dim failNumber As Long
    Dim failText As String
    Dim ws As Worksheet
    Dim body As String
    Dim dayText As String
    Dim sport As String
    Dim matchCount As Long
    Dim totalCount As Long
    Dim counted As String
    If IsBusy() Then Exit Sub
    On Error GoTo Fail
    EnsureReady
    dayText = PanelDay()
    sport = PanelSport()
    BeginWork Ro("Se {i^}ncarc{a} predic{t}iile (") & SportLabel(sport) & Ro(") pentru ") & dayText & "..."
    body = ApiTable("GET", "/api/excel/predictions", PredictionsQuery(dayText, sport))
    totalCount = m_total
    m_analyzed = ""
    SetLoadedSport sport
    Set ws = PrepareSheet(SH_PRED)
    WriteTable ws, 1, 1, Ro("Predic{t}ii ") & dayText, body, PredictionLayout(sport)
    matchCount = m_rows
    counted = matchCount & " meciuri"
    ' Limita din B9 taie ziua: spune cate meciuri lipsesc in loc sa le ascunda in tacere.
    If totalCount > matchCount Then
        counted = matchCount & " din " & totalCount & Ro(" meciuri (m{a}re{s}te Limita din Panou B9 sau filtreaz{a} dup{a} competi{t}ie)")
    End If
    If matchCount = 0 And Len(PanelCompetition()) > 0 Then
        counted = counted & Ro(" (filtru de competi{t}ie activ {i^}n Panou B6)")
    End If
    ws.Range("A1").Value = Ro("Predic{t}ii ") & SportLabel(sport) & " " & dayText & ": " & counted
    FinishPredictions ws, matchCount
    LoadCompetitions dayText, sport
    EndWork Ro("Predic{t}ii {i^}nc{a}rcate: ") & counted & " (" & SportLabel(sport) & ", " & dayText & ")."
    ws.Activate
    Exit Sub
Fail:
    failNumber = Err.Number
    failText = Err.Description
    EndWork
    ShowError ErrorText(failNumber, failText)
End Sub

Public Sub AnalizaMeci()
    Dim failNumber As Long
    Dim failText As String
    Dim matchId As String
    Dim sport As String
    Dim rowNumber As Long
    Dim onPredictions As Boolean
    If IsBusy() Then Exit Sub
    On Error GoTo Fail
    EnsureReady
    If TypeName(ActiveSheet) = "Worksheet" Then onPredictions = (ActiveSheet.Name = SH_PRED)
    ' Din Panou: ID-ul din B13 are prioritate; altfel randul selectat in foaia Predictii.
    If Not onPredictions Then matchId = PanelMatchId()
    sport = PanelSport()
    If Len(matchId) = 0 Then
        If Not onPredictions Then ThisWorkbook.Worksheets(SH_PRED).Activate
        If ActiveCell.Row > PRED_HEADER Then
            rowNumber = ActiveCell.Row
            matchId = PredictionCell(rowNumber, "match_id")
            sport = LoadedSport()
        End If
    End If
    If Len(matchId) = 0 Then
        rowNumber = 0
        matchId = PanelMatchId()
        sport = PanelSport()
    End If
    If Len(matchId) = 0 Then
        ShowError Ro("Selecteaz{a} un r{a^}nd cu un meci {i^}n foaia Predictii sau scrie ID-ul meciului {i^}n Panou (B13).")
        Exit Sub
    End If
    RunAnalysis matchId, rowNumber, sport
    Exit Sub
Fail:
    failNumber = Err.Number
    failText = Err.Description
    EndWork
    ShowError ErrorText(failNumber, failText)
End Sub

' Apelat de dublu-click pe un rand din Predictii (cod instalat de Setup, daca este permis).
Public Sub AnalizaMeciDinRand(ByVal rowNumber As Long)
    Dim failNumber As Long
    Dim failText As String
    Dim matchId As String
    If IsBusy() Then Exit Sub
    On Error GoTo Fail
    If rowNumber <= PRED_HEADER Then Exit Sub
    matchId = PredictionCell(rowNumber, "match_id")
    If Len(matchId) = 0 Then Exit Sub
    RunAnalysis matchId, rowNumber, LoadedSport()
    Exit Sub
Fail:
    failNumber = Err.Number
    failText = Err.Description
    EndWork
    ShowError ErrorText(failNumber, failText)
End Sub

Public Sub AnalizaCompletaTop()
    Dim failNumber As Long
    Dim failText As String
    Dim ws As Worksheet
    Dim lastRow As Long
    Dim r As Long
    Dim topN As Long
    Dim attempted As Long
    Dim succeeded As Long
    Dim failed As Long
    Dim skipped As Long
    Dim httpStatus As Long
    Dim matchId As String
    Dim grade As String
    Dim sport As String
    Dim body As String
    Dim message As String
    Dim stopReason As String
    Dim question As String
    If IsBusy() Then Exit Sub
    On Error GoTo Fail
    EnsureReady
    Set ws = ThisWorkbook.Worksheets(SH_PRED)
    sport = LoadedSport()
    lastRow = ws.Cells(ws.Rows.Count, LayoutPos(PredictionLayout(sport), "match_id")).End(xlUp).Row
    If lastRow <= PRED_HEADER Then
        ShowError Ro("{I^}ncarc{a} mai {i^}nt{a^}i predic{t}iile (butonul {I^}ncarc{a} predic{t}iile).")
        Exit Sub
    End If
    topN = PanelLong(CELL_TOPN, 10, 1, 100)
    question = Ro("Analiza complet{a} ruleaz{a} pentru primele ") & topN
    question = question & Ro(" meciuri viitoare cu nota C sau D (r{a^}ndurile vizibile). ")
    question = question & Ro("Fiecare meci folose{s}te p{a^}n{a} la 3 cereri FlashScore (H2H, clasament, cote). ")
    question = question & Ro("Meciurile analizate deja de la ultima {i^}nc{a}rcare a predic{t}iilor sunt s{a}rite.")
    question = question & vbLf & vbLf & Ro("Continui? (Esc opre{s}te analiza)")
    If MsgBox(Plain(question), vbQuestion + vbYesNo, APP_TITLE) <> vbYes Then Exit Sub
    BeginWork Ro("Analiz{a} complet{a} top ") & topN & "..."
    For r = PRED_HEADER + 1 To lastRow
        If attempted >= topN Then Exit For
        If Not ws.Rows(r).Hidden Then
            grade = PredictionCell(r, "grade")
            If (grade = "C" Or grade = "D") And PredictionCell(r, "upcoming") = "DA" Then
                matchId = PredictionCell(r, "match_id")
                If WasAnalyzed(matchId) Then
                    skipped = skipped + 1
                Else
                    attempted = attempted + 1
                    Application.StatusBar = APP_TITLE & ": " & Ro("analiz{a} ") & attempted & "/" & topN & " - " _
                        & PredictionCell(r, "home") & " - " & PredictionCell(r, "away") & Ro(" (Esc opre{s}te)")
                    DoEvents
                    body = ApiTry("POST", "/api/excel/analyze/" & UrlEncode(matchId), AnalysisQuery(sport), httpStatus, message)
                    If httpStatus = 200 Then
                        UpdatePredictionRow r, body
                        MarkAnalyzed matchId
                        succeeded = succeeded + 1
                    ElseIf httpStatus = 429 Or httpStatus = 503 Then
                        stopReason = message
                        Exit For
                    Else
                        ' Un meci care esueaza (ex. 404) nu blocheaza apasarile urmatoare.
                        MarkAnalyzed matchId
                        failed = failed + 1
                    End If
                End If
            End If
        End If
    Next r
    message = Ro("Analize complete reu{s}ite: ") & succeeded & Ro(", e{s}uate: ") & failed & "."
    If attempted = 0 Then
        message = Ro("Nu exist{a} meciuri viitoare cu nota C sau D printre r{a^}ndurile vizibile.")
        If skipped > 0 Then
            message = Ro("Toate cele ") & skipped & Ro(" meciuri viitoare C/D vizibile au fost deja analizate. ")
            message = message & Ro("Re{i^}ncarc{a} predic{t}iile ca s{a} le analizezi din nou.")
        End If
    End If
    If Len(stopReason) > 0 Then message = message & vbLf & Ro("Oprit: ") & stopReason
    EndWork message
    ws.Activate
    MsgBox Plain(message), vbInformation, APP_TITLE
    Exit Sub
Fail:
    failNumber = Err.Number
    failText = ErrorText(failNumber, Err.Description)
    EndWork
    If attempted > 0 Then
        failText = failText & vbLf & Ro("Analize complete reu{s}ite: ") & succeeded & Ro(", e{s}uate: ") & failed & "."
    End If
    ShowError failText
End Sub

Public Sub IncarcaValoare()
    Dim failNumber As Long
    Dim failText As String
    Dim ws As Worksheet
    Dim body As String
    Dim dayText As String
    Dim sport As String
    Dim nextRow As Long
    If IsBusy() Then Exit Sub
    On Error GoTo Fail
    EnsureReady
    dayText = PanelDay()
    sport = PanelSport()
    BeginWork Ro("Se caut{a} valoarea (EV pozitiv) pentru ") & dayText & "..."
    body = ApiTable("GET", "/api/excel/value", BoardQuery(dayText, sport))
    Set ws = PrepareSheet(SH_VALUE)
    nextRow = WriteTable(ws, 1, 1, Ro("Valoare (EV pozitiv) ") & SportLabel(sport) & " " & dayText, body, LayoutValue())
    ws.Cells(nextRow, 1).Value = Ro("EV = probabilitate x cot{a} - 1. Estimare statistic{a}, nu profit garantat. 18+.")
    ws.Cells(nextRow, 1).Font.Italic = True
    FreezeBelow ws, 2, 0
    EndWork Ro("Valoare: ") & m_rows & Ro(" pie{t}e cu EV pozitiv (") & dayText & ")."
    Exit Sub
Fail:
    failNumber = Err.Number
    failText = Err.Description
    EndWork
    ShowError ErrorText(failNumber, failText)
End Sub

Public Sub IncarcaTrackRecord()
    Dim failNumber As Long
    Dim failText As String
    Dim ws As Worksheet
    Dim metrics As String
    Dim calibration As String
    Dim ledger As String
    Dim metricsEnd As Long
    Dim calibrationEnd As Long
    Dim nextRow As Long
    If IsBusy() Then Exit Sub
    On Error GoTo Fail
    EnsureReady
    BeginWork Ro("Se {i^}ncarc{a} registrul de predic{t}ii...")
    metrics = ApiTable("GET", "/api/excel/record", "section=metrics")
    calibration = ApiTable("GET", "/api/excel/record", "section=calibration")
    ledger = ApiTable("GET", "/api/excel/record", "section=rows")
    Set ws = PrepareSheet(SH_RECORD)
    metricsEnd = WriteRecord(ws, 1, 1, Ro("Registru prospectiv (selec{t}ii salvate {i^}nainte de meci)"), metrics, LayoutMetrics())
    calibrationEnd = WriteTable(ws, 1, 4, Ro("Calibrare pe benzi de probabilitate"), calibration, LayoutCalibration())
    nextRow = metricsEnd
    If calibrationEnd > nextRow Then nextRow = calibrationEnd
    WriteTable ws, nextRow, 1, Ro("Selec{t}ii (cele mai noi primele)"), ledger, LayoutRecord()
    EndWork Ro("Track record {i^}nc{a}rcat: ") & m_rows & Ro(" selec{t}ii.")
    Exit Sub
Fail:
    failNumber = Err.Number
    failText = Err.Description
    EndWork
    ShowError ErrorText(failNumber, failText)
End Sub

Public Sub IncarcaRecomandari()
    Dim failNumber As Long
    Dim failText As String
    Dim ws As Worksheet
    Dim dayText As String
    Dim tickets As String
    Dim legs As String
    Dim singles As String
    Dim warnings As String
    Dim nextRow As Long
    If IsBusy() Then Exit Sub
    On Error GoTo Fail
    EnsureReady
    dayText = PanelDay()
    BeginWork Ro("Se preg{a}tesc recomand{a}rile AI pentru ") & dayText & Ro(" (prima dat{a} poate dura un minut)...")
    ' Doar prima cerere regenereaza (Regenereaza = DA); celelalte citesc biletele salvate.
    tickets = ApiTable("GET", "/api/excel/recommendations", RecoQuery(dayText, SheetYes(SH_RECO, RECO_REFRESH)) & "&section=tickets")
    legs = ApiTable("GET", "/api/excel/recommendations", RecoQuery(dayText, False) & "&section=legs")
    singles = ApiTable("GET", "/api/excel/recommendations", RecoQuery(dayText, False) & "&section=singles")
    Set ws = ThisWorkbook.Worksheets(SH_RECO)
    ClearFrom ws, RECO_FIRST_ROW
    nextRow = WriteTable(ws, RECO_FIRST_ROW, 1, Ro("Bilete AI pentru ") & dayText & Ro(" (cota {t}int{a} x2, x5, x10, x100)"), tickets, LayoutRecoTickets())
    nextRow = WriteTable(ws, nextRow, 1, Ro("Selec{t}iile fiec{a}rui bilet"), legs, LayoutRecoLegs())
    nextRow = WriteTable(ws, nextRow, 1, Ro("Cele mai sigure selec{t}ii simple"), singles, LayoutRecoSingles())
    warnings = FieldOf(tickets, "warnings")
    If Len(warnings) > 0 Then
        WriteNote ws, nextRow, Ro("Aten{t}ie: ") & warnings
        nextRow = nextRow + 1
    End If
    WriteNote ws, nextRow, Ro("Probabilitatea biletului este produsul probabilit{a}{t}ilor selec{t}iilor. Estim{a}ri statistice, nu garan{t}ii. 18+.")
    EndWork Ro("Recomand{a}ri {i^}nc{a}rcate pentru ") & dayText & "."
    ws.Activate
    Exit Sub
Fail:
    failNumber = Err.Number
    failText = Err.Description
    EndWork
    ShowError ErrorText(failNumber, failText)
End Sub

Public Sub ActualizeazaLive()
    Dim failNumber As Long
    Dim failText As String
    Dim ws As Worksheet
    Dim sport As String
    Dim body As String
    Dim markets As String
    Dim oddsNote As String
    Dim nextRow As Long
    Dim liveCount As Long
    If IsBusy() Then Exit Sub
    On Error GoTo Fail
    EnsureReady
    sport = PanelSport()
    BeginWork Ro("Se actualizeaz{a} meciurile live (") & SportLabel(sport) & ")..."
    body = ApiTable("GET", "/api/excel/live", "sport=" & sport & "&section=matches")
    markets = ApiTable("GET", "/api/excel/live", "sport=" & sport & "&section=markets")
    Set ws = ThisWorkbook.Worksheets(SH_LIVE)
    ClearFrom ws, LIVE_FIRST_ROW
    nextRow = WriteTable(ws, LIVE_FIRST_ROW, 1, "Live " & SportLabel(sport) & " (" & Format$(Now, "hh:nn:ss") & ")", body, LayoutLive())
    liveCount = m_rows
    oddsNote = FieldOf(body, "odds_note")
    If Len(oddsNote) = 0 Then oddsNote = Ro("Cotele din list{a} sunt de dinainte de meci; afi{s}{a}m cota corect{a} {s}i cota minim{a}, nu pre{t}uri live.")
    WriteNote ws, nextRow - 1, oddsNote
    nextRow = WriteTable(ws, nextRow + 1, 1, Ro("Toate pie{t}ele live (probabilit{a}{t}i pe rezultatul final)"), markets, LayoutLiveMarkets())
    WriteNote ws, nextRow, Ro("Estim{a}ri statistice, nu garan{t}ii. 18+. Datele live se re{i^}mprosp{a}teaz{a} la cel mult 30 de secunde.")
    EndWork Ro("Live actualizat: ") & liveCount & " meciuri (" & SportLabel(sport) & ")."
    ws.Activate
    Exit Sub
Fail:
    failNumber = Err.Number
    failText = Err.Description
    EndWork
    ShowError ErrorText(failNumber, failText)
End Sub

Public Sub RuleazaSimularea()
    Dim failNumber As Long
    Dim failText As String
    Dim ws As Worksheet
    Dim query As String
    Dim summary As String
    Dim ladders As String
    Dim daysLog As String
    Dim recentNote As String
    Dim warnings As String
    Dim nextRow As Long
    Dim ladderEnd As Long
    Dim daysTop As Long
    Dim dayCount As Long
    Dim equity As String
    Dim equityCount As Long
    Dim finishText As String
    If IsBusy() Then Exit Sub
    On Error GoTo Fail
    EnsureReady
    query = SimulationQuery()
    BeginWork Ro("Simulare {i^}n curs (") & SimStrategy() & ", " & SimDataset() & ")..."
    If SimDataset() = "recent" Then recentNote = PrepareRecentDays(SimDays(), SimSports())
    Application.StatusBar = APP_TITLE & ": " & Ro("simulare {i^}n curs (prima rulare pe un set poate dura c{a^}teva minute)...")
    summary = ApiTable("GET", "/api/excel/simulate", query & "&section=summary")
    ladders = ApiTable("GET", "/api/excel/simulate", query & "&section=ladders")
    daysLog = ApiTable("GET", "/api/excel/simulate", query & "&section=days")
    Set ws = ThisWorkbook.Worksheets(SH_SIM)
    ClearFrom ws, SIM_FIRST_ROW - 1
    nextRow = WriteRecord(ws, SIM_FIRST_ROW, 1, Ro("Rezultatul simul{a}rii"), summary, LayoutSimSummary())
    ladderEnd = WriteTable(ws, SIM_FIRST_ROW, 4, Ro("Sc{a}ri (fiecare pornire cu suma ini{t}ial{a})"), ladders, LayoutSimLadders())
    If ladderEnd > nextRow Then nextRow = ladderEnd
    daysTop = nextRow
    nextRow = WriteTable(ws, daysTop, 1, Ro("Jurnal pe zile (biletul fixat {i^}nainte de rezultate)"), daysLog, LayoutSimDays())
    dayCount = m_rows
    If SimStrategy() = "scara" Then
        ' Scara: banca scarii revine la suma initiala la fiecare repornire, deci graficul arata
        ' castigul net cumulat (recuperat - investit), unde se vad pierderile adunate.
        equity = ApiTable("GET", "/api/excel/simulate", query & "&section=equity")
        WriteTable ws, SIM_FIRST_ROW, 26, Ro("C{a^}{s}tig net cumulat"), equity, LayoutSimEquity()
        equityCount = m_rows
        If equityCount > 1 Then
            AddEquityChart ws, SIM_FIRST_ROW + 2, equityCount, 26 + LayoutPos(LayoutSimEquity(), "net") - 1, 26 + LayoutPos(LayoutSimEquity(), "date") - 1, _
                ws.Cells(SIM_FIRST_ROW, 16).Left, ws.Cells(SIM_FIRST_ROW, 16).Top, Ro("C{a^}{s}tig net cumulat")
        End If
    ElseIf dayCount > 1 Then
        AddEquityChart ws, daysTop + 2, dayCount, LayoutPos(LayoutSimDays(), "bankroll_after"), LayoutPos(LayoutSimDays(), "date"), _
            ws.Cells(SIM_FIRST_ROW, 16).Left, ws.Cells(SIM_FIRST_ROW, 16).Top, Ro("Evolu{t}ia b{a}ncii")
    End If
    warnings = FieldOf(summary, "warnings")
    If Len(recentNote) > 0 Then
        WriteNote ws, nextRow, recentNote
        nextRow = nextRow + 1
    End If
    If Len(warnings) > 0 Then
        WriteNote ws, nextRow, Ro("Aten{t}ie: ") & warnings
        nextRow = nextRow + 1
    End If
    WriteNote ws, nextRow, Ro("Simulare cu bani virtuali pe meciuri din trecut. Estim{a}ri statistice, nu garan{t}ii. 18+.")
    If SimStrategy() = "scara" Then
        finishText = Ro("Simulare gata: investit ") & FieldOf(summary, "total_invested") & Ro(", recuperat ") _
            & FieldOf(summary, "total_returned") & Ro(", c{a^}{s}tig net ") & FieldOf(summary, "net")
    Else
        finishText = Ro("Simulare gata: final ") & FieldOf(summary, "final") & ", profit " & FieldOf(summary, "profit")
    End If
    EndWork finishText & " (" & dayCount & Ro(" r{a^}nduri).")
    ws.Activate
    Exit Sub
Fail:
    failNumber = Err.Number
    failText = Err.Description
    EndWork
    ShowError ErrorText(failNumber, failText)
End Sub

Public Sub IncarcaSeturiDate()
    Dim failNumber As Long
    Dim failText As String
    Dim ws As Worksheet
    Dim body As String
    Dim nextRow As Long
    If IsBusy() Then Exit Sub
    On Error GoTo Fail
    EnsureReady
    BeginWork Ro("Se citesc seturile de date ale simulatorului...")
    body = ApiTable("GET", "/api/excel/simulate/datasets", "")
    Set ws = ThisWorkbook.Worksheets(SH_SIM)
    ClearFrom ws, SIM_FIRST_ROW
    nextRow = WriteTable(ws, SIM_FIRST_ROW, 1, Ro("Seturi de date (ID-ul se scrie {i^}n B4)"), body, LayoutSimDatasets())
    WriteNote ws, nextRow, Ro("recent = ultimele zile din baza local{a}: zilele lips{a} se descarc{a} automat la rulare.")
    EndWork Ro("Seturi de date: ") & m_rows & "."
    ws.Activate
    Exit Sub
Fail:
    failNumber = Err.Number
    failText = Err.Description
    EndWork
    ShowError ErrorText(failNumber, failText)
End Sub

Public Sub IncarcaPortofel()
    Dim failNumber As Long
    Dim failText As String
    Dim ws As Worksheet
    Dim summary As String
    Dim bets As String
    Dim history As String
    Dim summaryEnd As Long
    Dim nextRow As Long
    If IsBusy() Then Exit Sub
    On Error GoTo Fail
    EnsureReady
    BeginWork Ro("Se {i^}ncarc{a} portofelul virtual...")
    summary = ApiTable("GET", "/api/excel/wallet", "section=summary")
    bets = ApiTable("GET", "/api/excel/wallet", "section=bets")
    history = ApiTable("GET", "/api/excel/wallet", "section=history")
    Set ws = ThisWorkbook.Worksheets(SH_WALLET)
    ClearFrom ws, WALLET_FIRST_ROW
    summaryEnd = WriteRecord(ws, WALLET_FIRST_ROW, 1, Ro("Portofel virtual"), summary, LayoutWalletSummary())
    nextRow = WriteTable(ws, WALLET_FIRST_ROW, 4, Ro("Pariuri (cele mai noi primele)"), bets, LayoutWalletBets())
    nextRow = WriteTable(ws, nextRow, 4, Ro("Mi{s}c{a}ri de bani"), history, LayoutWalletHistory())
    If summaryEnd > nextRow Then nextRow = summaryEnd
    WriteNote ws, nextRow, Ro("Bani fictivi. Pariurile se plaseaz{a} din aplica{t}ia web (Portofel); Excel doar le afi{s}eaz{a}. 18+.")
    EndWork Ro("Portofel: sold ") & FieldOf(summary, "balance") & " " & FieldOf(summary, "currency") & "."
    ws.Activate
    Exit Sub
Fail:
    failNumber = Err.Number
    failText = Err.Description
    EndWork
    ShowError ErrorText(failNumber, failText)
End Sub

'==============================================================================
' Analiza unui meci
'==============================================================================

Private Sub RunAnalysis(ByVal matchId As String, ByVal predRow As Long, ByVal sport As String)
    Dim failNumber As Long
    Dim failText As String
    Dim body As String
    Dim warnings As String
    Dim message As String
    On Error GoTo Fail
    BeginWork Ro("Analiz{a} complet{a} (FlashScore: form{a}, H2H, clasament, cote)...")
    body = ApiTable("POST", "/api/excel/analyze/" & UrlEncode(matchId), AnalysisQuery(sport))
    MarkAnalyzed matchId
    If predRow > 0 Then UpdatePredictionRow predRow, body
    FillMatchSheets matchId, body, sport
    warnings = FieldOf(body, "warnings")
    message = Ro("Analiz{a} gata: ") & FieldOf(body, "home") & " - " & FieldOf(body, "away")
    If Len(warnings) > 0 Then message = message & " | " & warnings
    EndWork message
    ThisWorkbook.Worksheets(SH_MATCH).Activate
    Exit Sub
Fail:
    failNumber = Err.Number
    failText = Err.Description
    EndWork
    ShowError ErrorText(failNumber, failText)
End Sub

Private Sub FillMatchSheets(ByVal matchId As String, ByVal summary As String, ByVal sport As String)
    Dim ws As Worksheet
    Dim body As String
    Dim message As String
    Dim httpStatus As Long
    Dim nextRow As Long
    Dim headline As String

    ' Foaia Meci: fisa meciului, toate pietele, pauza/final (fotbal) si observatii.
    Set ws = PrepareSheet(SH_MATCH)
    ws.Range("A1:A3").NumberFormat = "@"
    ws.Range("A1").Value = FieldOf(summary, "home") & " - " & FieldOf(summary, "away")
    ws.Range("A1").Font.Size = 16
    ws.Range("A1").Font.Bold = True
    headline = SportLabel(sport) & "  |  " & FieldOf(summary, "competition") & "  |  " & FieldOf(summary, "date_local") & " "
    headline = headline & FieldOf(summary, "time_local") & "  |  " & Ro("Calitate ") & FieldOf(summary, "grade")
    ws.Range("A2").Value = headline
    ws.Range("A3").Value = FieldOf(summary, "summary")
    ws.Range("A3").Font.Italic = True
    ws.Columns(1).ColumnWidth = 34
    ws.Columns(2).ColumnWidth = 30
    WriteRecord ws, 5, 1, Ro("Fi{s}a meciului"), summary, MatchCardLayout(sport)
    body = ApiTable("GET", "/api/excel/match/" & UrlEncode(matchId), MatchQuery("markets", sport))
    WriteTable ws, 5, 4, Ro("Toate pie{t}ele"), body, LayoutMarkets()
    nextRow = 5
    If sport = "football" Then
        body = ApiTable("GET", "/api/excel/match/" & UrlEncode(matchId), MatchQuery("htft", sport))
        nextRow = WriteTable(ws, 5, 13, Ro("Pauz{a} / Final"), body, LayoutHtft())
    End If
    body = ApiTable("GET", "/api/excel/match/" & UrlEncode(matchId), MatchQuery("insights", sport))
    WriteTable ws, nextRow, 13, Ro("Observa{t}ii"), body, LayoutInsights()

    ' Foaia Forma: statistici, ultimele meciuri, H2H si clasament.
    Set ws = PrepareSheet(SH_FORM)
    body = ApiTable("GET", "/api/excel/match/" & UrlEncode(matchId), MatchQuery("formstats", sport))
    nextRow = WriteTable(ws, 1, 1, Ro("Form{a} (toate competi{t}iile): ") & FieldOf(summary, "home") & " - " & FieldOf(summary, "away"), body, LayoutFormStats())
    body = ApiTable("GET", "/api/excel/match/" & UrlEncode(matchId), MatchQuery("form", sport))
    nextRow = WriteTable(ws, nextRow, 1, "Ultimele meciuri", body, LayoutForm())
    body = ApiTable("GET", "/api/excel/match/" & UrlEncode(matchId), MatchQuery("h2h", sport))
    nextRow = WriteTable(ws, nextRow, 1, Ro("Meciuri directe (H2H)"), body, LayoutH2H())
    body = ApiTry("GET", "/api/excel/match/" & UrlEncode(matchId), MatchQuery("standings", sport), httpStatus, message)
    If httpStatus = 200 Then
        WriteTable ws, nextRow, 1, "Clasament", body, LayoutStandings()
    Else
        ws.Cells(nextRow, 1).Value = Ro("Clasament indisponibil: ") & message
        ws.Cells(nextRow, 1).Font.Italic = True
    End If

    ' Foaia ScorCorect: top 10 scoruri si matricea 0-5 x 0-5 (doar fotbal).
    Set ws = PrepareSheet(SH_SCORE)
    If sport <> "football" Then
        ws.Range("A1").Value = Ro("Scorul corect {s}i matricea scorurilor exist{a} doar pentru fotbal. Pentru ") _
            & SportLabel(sport) & Ro(" vezi pie{t}ele din foaia Meci (ex. scorul la seturi).")
        ws.Range("A1").Font.Italic = True
        Exit Sub
    End If
    body = ApiTable("GET", "/api/excel/match/" & UrlEncode(matchId), MatchQuery("scores", sport))
    WriteTable ws, 1, 1, Ro("Scor corect: ") & FieldOf(summary, "home") & " - " & FieldOf(summary, "away"), body, LayoutScores()
    body = ApiTable("GET", "/api/excel/match/" & UrlEncode(matchId), MatchQuery("grid", sport))
    WriteTable ws, 1, 7, Ro("Matricea scorurilor (gazde pe r{a^}nduri, oaspe{t}i pe coloane)"), body, LayoutGrid()
End Sub

Private Sub UpdatePredictionRow(ByVal rowNumber As Long, ByVal body As String)
    Dim ws As Worksheet
    Dim headers() As String
    Dim data() As String
    Dim keys() As String
    Dim titles() As String
    Dim fmts() As String
    Dim out() As Variant
    Dim n As Long
    Dim m As Long
    Dim j As Long
    Dim src As Long
    Dim idCol As Long

    n = ParseTsv(body, headers, data)
    If n < 1 Then Exit Sub
    idCol = FindCol(headers, "match_id")
    If idCol < 0 Then Exit Sub
    ' Nu suprascrie alt meci daca lista a fost reincarcata intre timp.
    If PredictionCell(rowNumber, "match_id") <> data(1, idCol) Then Exit Sub
    Set ws = ThisWorkbook.Worksheets(SH_PRED)
    m = ParseLayout(PredictionLayout(LoadedSport()), keys, titles, fmts)
    ReDim out(1 To 1, 1 To m)
    For j = 1 To m
        src = FindCol(headers, keys(j))
        If src >= 0 Then out(1, j) = Convert(data(1, src), fmts(j))
    Next j
    ws.Range(ws.Cells(rowNumber, 1), ws.Cells(rowNumber, m)).Value = out
    ' Aceeasi evidentiere ca HighlightMarked: fundal galben si text ingrosat, sau niciuna.
    For j = 1 To m
        If fmts(j) = "mrk" Then
            With ws.Range(ws.Cells(rowNumber, 1), ws.Cells(rowNumber, m))
                If Len(CStr(out(1, j))) > 0 Then
                    .Interior.Color = RGB(255, 250, 205)
                    .Font.Bold = True
                Else
                    .Interior.Pattern = xlNone
                    .Font.Bold = False
                End If
            End With
        End If
    Next j
End Sub

Private Function WasAnalyzed(ByVal matchId As String) As Boolean
    WasAnalyzed = InStr(1, m_analyzed, "|" & matchId & "|", vbBinaryCompare) > 0
End Function

Private Sub MarkAnalyzed(ByVal matchId As String)
    If Len(matchId) = 0 Or WasAnalyzed(matchId) Then Exit Sub
    If Len(m_analyzed) = 0 Then m_analyzed = "|"
    m_analyzed = m_analyzed & matchId & "|"
End Sub

Private Function PredictionCell(ByVal rowNumber As Long, ByVal key As String) As String
    Dim col As Long
    col = LayoutPos(PredictionLayout(LoadedSport()), key)
    If col = 0 Then Exit Function
    PredictionCell = Trim$(SafeText(ThisWorkbook.Worksheets(SH_PRED).Cells(rowNumber, col).Value))
End Function

Private Sub FinishPredictions(ByVal ws As Worksheet, ByVal matchCount As Long)
    If ws.AutoFilterMode Then ws.AutoFilterMode = False
    If matchCount > 0 Then
        ws.Range(ws.Cells(PRED_HEADER, 1), ws.Cells(PRED_HEADER + matchCount, m_cols)).AutoFilter
    End If
    ws.Rows(1).RowHeight = 30
    ws.Rows(PRED_HEADER).RowHeight = 30
    ws.Range("A1").Font.Size = 14
    FreezeBelow ws, PRED_HEADER, 5
End Sub

Private Sub LoadCompetitions(ByVal dayText As String, ByVal sport As String)
    Dim ws As Worksheet
    Dim body As String
    Dim message As String
    Dim query As String
    Dim httpStatus As Long
    Dim headers() As String
    Dim data() As String
    Dim out() As Variant
    Dim n As Long
    Dim i As Long
    Dim idCol As Long
    Dim nameCol As Long
    Dim countryCol As Long
    Dim label As String
    On Error GoTo Quiet
    query = "day=" & dayText & "&sport=" & sport
    If PanelYes(CELL_DEMO) Then query = query & "&demo=1"
    body = ApiTry("GET", "/api/excel/competitions", query, httpStatus, message)
    If httpStatus <> 200 Then Exit Sub
    n = ParseTsv(body, headers, data)
    idCol = FindCol(headers, "competition_id")
    nameCol = FindCol(headers, "competition")
    countryCol = FindCol(headers, "country")
    If idCol < 0 Or nameCol < 0 Or countryCol < 0 Then Exit Sub
    ReDim out(1 To n + 1, 1 To 2)
    out(1, 1) = Ro("(toate)")
    out(1, 2) = ""
    For i = 1 To n
        label = data(i, nameCol)
        If Len(data(i, countryCol)) > 0 Then label = label & " (" & data(i, countryCol) & ")"
        out(i + 1, 1) = label
        out(i + 1, 2) = data(i, idCol)
    Next i
    Set ws = ThisWorkbook.Worksheets(SH_LISTS)
    ws.Range("A:B").ClearContents
    ws.Range(ws.Cells(1, 1), ws.Cells(n + 1, 2)).NumberFormat = "@"
    ws.Range(ws.Cells(1, 1), ws.Cells(n + 1, 2)).Value = out
    ws.Range(LIST_COMP_SPORT).NumberFormat = "@"
    ws.Range(LIST_COMP_SPORT).Value = sport
    SetListValidation ThisWorkbook.Worksheets(SH_PANEL).Range(CELL_COMP), "=" & SH_LISTS & "!$A$1:$A$" & (n + 1), False
    ' Eticheta din B6 care nu exista in lista noua (alt sport sau alta zi) revine la (toate).
    label = Trim$(SafeText(PanelValue(CELL_COMP)))
    If Len(label) > 0 And label <> Ro("(toate)") Then
        If Application.WorksheetFunction.CountIf(ws.Range(ws.Cells(1, 1), ws.Cells(n + 1, 1)), label) = 0 Then
            If InStr(label, "|") = 0 And InStr(label, ":") = 0 Then ResetCompetition
        End If
    End If
Quiet:
    ' Lista e optionala, dar Esc (eroarea 18) trebuie sa opreasca macro-ul apelant.
    If Err.Number = 18 Then Err.Raise 18
End Sub

'==============================================================================
' Simulare, recomandari: parametri
'==============================================================================

' Incarca zilele recente lipsa (o cerere FlashScore pe zi si sport) si asteapta sfarsitul.
Private Function PrepareRecentDays(ByVal dayCount As Long, ByVal sports As String) As String
    Dim body As String
    Dim query As String
    Dim loadState As String
    Dim waited As Long
    Dim planned As Long
    Dim answer As Long
    Dim question As String
    query = "days=" & dayCount & "&sports=" & UrlEncode(sports)
    body = ApiTable("GET", "/api/excel/simulate/recent", query)
    loadState = FieldOf(body, "status")
    If loadState <> "running" Then
        planned = CLng(Val(FieldOf(body, "planned")))
        If planned <= 0 Then Exit Function
        question = Ro("Se folosesc p{a^}n{a} la ") & planned & Ro(" cereri FlashScore (o cerere pe zi {s}i sport, ")
        question = question & Ro("inclusiv zilele de form{a} de dinainte). Zilele salvate sunt s{a}rite.") & vbLf & vbLf
        question = question & Ro("Da = {i^}ncarc{a} {s}i simuleaz{a}; Nu = simuleaz{a} doar zilele deja {i^}nc{a}rcate; Anuleaz{a} = oprire.")
        answer = vbNo
        ' Excel ascuns (build_xlsm.py): nicio cerere cheltuita fara acord.
        If Application.Visible Then answer = MsgBox(Plain(question), vbQuestion + vbYesNoCancel, APP_TITLE)
        If answer = vbCancel Then Err.Raise ERR_INPUT, APP_TITLE, Ro("Simulare anulat{a}.")
        If answer <> vbYes Then
            PrepareRecentDays = Ro("Zilele lips{a} nu au fost {i^}nc{a}rcate; simularea folose{s}te doar zilele salvate.")
            Exit Function
        End If
        body = ApiTable("POST", "/api/excel/simulate/recent", query)
        loadState = FieldOf(body, "status")
    End If
    Do While loadState = "running"
        If waited >= RECENT_MAX_WAIT Then
            Err.Raise ERR_API, APP_TITLE, Ro("Zilele recente se {i^}ncarc{a} {i^}nc{a} (") & FieldOf(body, "done") & "/" _
                & FieldOf(body, "total") & Ro("). Mai ap{a}s{a} o dat{a} Ruleaz{a} simularea peste un minut.")
        End If
        Application.StatusBar = APP_TITLE & ": " & Ro("zile recente ") & FieldOf(body, "done") & "/" & FieldOf(body, "total") _
            & " - " & FieldOf(body, "message") & Ro(" (Esc opre{s}te)")
        DoEvents
        Application.Wait Now + TimeSerial(0, 0, 2)
        waited = waited + 2
        body = ApiTable("GET", "/api/excel/simulate/recent", query)
        loadState = FieldOf(body, "status")
    Loop
    If loadState = "failed" Or loadState = "interrupted" Then
        ' Zilele deja salvate raman: simularea continua pe ele, cu mesajul serverului ca nota.
        If Val(FieldOf(body, "days_loaded")) > 0 Or Val(FieldOf(body, "matches")) > 0 Then
            PrepareRecentDays = Ro("{I^}nc{a}rcarea nu s-a terminat: ") & FieldOf(body, "message")
            Exit Function
        End If
        Err.Raise ERR_API, APP_TITLE, Ro("Zilele recente nu s-au putut {i^}nc{a}rca: ") & FieldOf(body, "message")
    End If
    ' "partial": limita de cereri a fost atinsa; simularea ruleaza pe zilele deja incarcate.
    If loadState = "partial" Then PrepareRecentDays = FieldOf(body, "message")
End Function

Private Function SimulationQuery() As String
    Dim query As String
    Dim datasetId As String
    Dim fromDay As String
    Dim toDay As String
    Dim amount As Double
    amount = SimAmount()
    datasetId = SimDataset()
    query = "bankroll=" & NumToStr(amount) & "&dataset=" & UrlEncode(datasetId)
    If datasetId = "recent" Then
        query = query & "&days=" & SimDays() & "&sports=" & UrlEncode(SimSports())
    Else
        fromDay = SheetDay(SH_SIM, SIM_START)
        toDay = SheetDay(SH_SIM, SIM_END)
        If Len(fromDay) > 0 Then query = query & "&start=" & fromDay
        If Len(toDay) > 0 Then query = query & "&end=" & toDay
    End If
    Select Case SimStrategy()
        Case "bilet"
            query = query & "&strategy=flat&mode=ticket&target_odds=" & NumToStr(SimOdds()) & "&stake=" & NumToStr(amount / 10)
        Case "simple"
            query = query & "&strategy=flat&mode=singles&stake=" & NumToStr(amount / 10)
        Case Else
            query = query & "&strategy=ladder&target_odds=" & NumToStr(SimOdds()) & "&reinvest=" & NumToStr(SimReinvest())
            If SimMaxDays() > 0 Then query = query & "&max_days=" & SimMaxDays()
            If SheetYes(SH_SIM, SIM_RESTART) Then
                query = query & "&restart_on_loss=1"
            Else
                query = query & "&restart_on_loss=0"
            End If
    End Select
    SimulationQuery = query
End Function

Private Function SimAmount() As Double
    Dim amount As Double
    amount = SheetNumber(SH_SIM, SIM_AMOUNT, 5)
    If amount <= 0 Then
        Err.Raise ERR_INPUT, APP_TITLE, Ro("Suma din foaia Simulare (B3) trebuie s{a} fie pozitiv{a}, de ex. 5.")
    End If
    SimAmount = amount
End Function

Private Function SimDataset() As String
    Dim text As String
    text = LCase$(SheetText(SH_SIM, SIM_DATASET))
    If Len(text) = 0 Then text = "recent"
    SimDataset = text
End Function

Private Function SimSports() As String
    SimSports = SportsChoice(SheetText(SH_SIM, SIM_SPORTS))
End Function

Private Function SimDays() As Long
    SimDays = SheetLong(SH_SIM, SIM_DAYS, 14, 1, 60)
End Function

' Incaseaza scara dupa N bilete reusite (0 = niciodata).
Private Function SimMaxDays() As Long
    SimMaxDays = SheetLong(SH_SIM, SIM_MAXDAYS, 0, 0, 365)
End Function

Private Function SimOdds() As Double
    Dim amount As Double
    amount = SheetNumber(SH_SIM, SIM_ODDS, 2)
    If amount < 1.2 Then amount = 1.2
    If amount > 100 Then amount = 100
    SimOdds = amount
End Function

Private Function SimReinvest() As Double
    Dim amount As Double
    amount = SheetNumber(SH_SIM, SIM_REINVEST, 1)
    If amount > 1 Then amount = amount / 100
    If amount <= 0 Then amount = 1
    If amount < 0.01 Then amount = 0.01
    If amount > 1 Then amount = 1
    SimReinvest = amount
End Function

' "scara" (implicit), "bilet" sau "simple"; accepta si diacritice / majuscule.
Private Function SimStrategy() As String
    Dim text As String
    text = LCase$(Plain(SheetText(SH_SIM, SIM_STRATEGY)))
    If Left$(text, 3) = "bil" Then
        SimStrategy = "bilet"
    ElseIf Left$(text, 3) = "sim" Then
        SimStrategy = "simple"
    Else
        SimStrategy = "scara"
    End If
End Function

Private Function RecoQuery(ByVal dayText As String, ByVal refresh As Boolean) As String
    Dim query As String
    query = "day=" & dayText & "&sports=" & UrlEncode(SportsChoice(SheetText(SH_RECO, RECO_SPORTS)))
    query = query & "&targets=" & UrlEncode(RecoTargets())
    If refresh Then query = query & "&refresh=1"
    RecoQuery = query
End Function

' "2,5,10,100" din foaia Recomandari (B4); ";" si spatiile sunt acceptate.
Private Function RecoTargets() As String
    Dim text As String
    text = Replace(Replace(SheetText(SH_RECO, RECO_TARGETS), ";", ","), " ", "")
    If Len(text) = 0 Then text = "2,5,10,100"
    RecoTargets = text
End Function

'==============================================================================
' Sporturi
'==============================================================================

Private Function SportKey(ByVal label As String) As String
    Select Case LCase$(Trim$(label))
        Case "baschet", "basketball"
            SportKey = "basketball"
        Case "tenis", "tennis"
            SportKey = "tennis"
        Case Else
            SportKey = "football"
    End Select
End Function

Private Function SportLabel(ByVal sport As String) As String
    Select Case sport
        Case "basketball"
            SportLabel = "Baschet"
        Case "tennis"
            SportLabel = "Tenis"
        Case Else
            SportLabel = "Fotbal"
    End Select
End Function

' "football,tennis" -> "Fotbal, Tenis"; "multi" (setul recent) -> "Mai multe"; "" -> "".
Private Function SportsText(ByVal sports As String) As String
    Dim parts() As String
    Dim i As Long
    Dim out As String
    Dim sport As String
    If Len(sports) = 0 Then Exit Function
    parts = Split(sports, ",")
    For i = 0 To UBound(parts)
        sport = Trim$(parts(i))
        If Len(out) > 0 Then out = out & ", "
        If sport = "football" Or sport = "basketball" Or sport = "tennis" Then
            out = out & SportLabel(sport)
        ElseIf sport = "multi" Then
            out = out & "Mai multe"
        Else
            out = out & sport
        End If
    Next i
    SportsText = out
End Function

' "Toate" (sau gol) -> toate sporturile; altfel un singur sport.
Private Function SportsChoice(ByVal label As String) As String
    If Len(label) = 0 Or LCase$(label) = "toate" Then
        SportsChoice = ALL_SPORTS
    Else
        SportsChoice = SportKey(label)
    End If
End Function

Private Function PanelSport() As String
    PanelSport = SportKey(SafeText(PanelValue(CELL_SPORT)))
End Function

' Sportul foii Predictii (scris la incarcare): analiza unui rand foloseste acelasi sport.
Private Function LoadedSport() As String
    Dim sport As String
    sport = Trim$(SafeText(ThisWorkbook.Worksheets(SH_LISTS).Range(LIST_LOADED_SPORT).Value))
    If Len(sport) = 0 Then sport = "football"
    LoadedSport = SportKey(sport)
End Function

Private Sub SetLoadedSport(ByVal sport As String)
    With ThisWorkbook.Worksheets(SH_LISTS).Range(LIST_LOADED_SPORT)
        .NumberFormat = "@"
        .Value = sport
    End With
End Sub

Private Function PredictionLayout(ByVal sport As String) As String
    Select Case sport
        Case "basketball"
            PredictionLayout = LayoutPredictionsBasketball()
        Case "tennis"
            PredictionLayout = LayoutPredictionsTennis()
        Case Else
            PredictionLayout = LayoutPredictions()
    End Select
End Function

Private Function MatchCardLayout(ByVal sport As String) As String
    Select Case sport
        Case "basketball"
            MatchCardLayout = LayoutMatchCardBasketball()
        Case "tennis"
            MatchCardLayout = LayoutMatchCardTennis()
        Case Else
            MatchCardLayout = LayoutMatchCard()
    End Select
End Function

'==============================================================================
' Coloane afisate: "coloana_api|Titlu|format;..." (formatele sunt in Convert)
'==============================================================================

Private Function LayoutPredictions() As String
    Dim s As String
    s = "time_local|Ora|txt;date_local|Data|txt;competition|Competi{t}ie|txt;"
    s = s & "home|Gazde|txt;away|Oaspe{t}i|txt;grade|Calitate|grd;confidence|{I^}ncredere|int;"
    s = s & "p_1|1|pct;p_x|X|pct;p_2|2|pct;p_1x|1X|pct;p_x2|X2|pct;p_12|12|pct;"
    s = s & "p_over15|Peste 1.5|pct;p_over25|Peste 2.5|pct;p_under25|Sub 2.5|pct;p_over35|Peste 3.5|pct;"
    s = s & "p_btts|GG|pct;p_no_btts|NG|pct;xg_home|xG gazde|num;xg_away|xG oaspe{t}i|num;"
    s = s & "score_1|Scor 1|txt;p_score_1|P scor 1|pc0;score_2|Scor 2|txt;p_score_2|P scor 2|pc0;"
    s = s & "score_3|Scor 3|txt;p_score_3|P scor 3|pc0;htft_1|Pauz{a}/Final|txt;p_htft_1|P pauz{a}/final|pc0;"
    s = s & "tip_label|Pont principal|txt;tip_p|Prob. pont|pct;selection_label|Selec{t}ie (prag Panou)|mrk;"
    s = s & "odds_1|Cot{a} 1|odd;odds_x|Cot{a} X|odd;odds_2|Cot{a} 2|odd;value_label|Valoare|txt;value_ev|EV|ev;"
    s = s & "form_home|Form{a} gazde|txt;form_away|Form{a} oaspe{t}i|txt;"
    s = s & "ppg_home|Puncte/meci gazde|num;ppg_away|Puncte/meci oaspe{t}i|num;"
    s = s & "win_rate_home|% victorii gazde|pc0;win_rate_away|% victorii oaspe{t}i|pc0;"
    s = s & "result|Rezultat|txt;tip_won|Pont c{a^}{s}tigat|win;upcoming|Viitor|yn;status|Stare|txt;"
    s = s & "match_id|ID meci|txt;home_logo|Sigl{a} gazde (URL)|txt;away_logo|Sigl{a} oaspe{t}i (URL)|txt"
    LayoutPredictions = s
End Function

Private Function LayoutPredictionsBasketball() As String
    Dim s As String
    s = "time_local|Ora|txt;date_local|Data|txt;competition|Competi{t}ie|txt;"
    s = s & "home|Gazde|txt;away|Oaspe{t}i|txt;grade|Calitate|grd;confidence|{I^}ncredere|int;"
    s = s & "p_1|1 (cu prelungiri)|pct;p_2|2 (cu prelungiri)|pct;odds_1|Cot{a} 1|odd;odds_2|Cot{a} 2|odd;"
    s = s & "main_3_label|Handicap|txt;main_3_p|P handicap|pct;main_4_label|Total puncte|txt;main_4_p|P total|pct;"
    s = s & "exp_home|Puncte gazde|num;exp_away|Puncte oaspe{t}i|num;exp_total|Total estimat|num;"
    s = s & "exp_margin|Diferen{t}{a} estimat{a}|num;p_overtime|P prelungiri|pc0;"
    s = s & "tip_label|Pont principal|txt;tip_p|Prob. pont|pct;tip_odds|Cot{a} pont|odd;"
    s = s & "selection_label|Selec{t}ie (prag Panou)|mrk;value_label|Valoare|txt;value_ev|EV|ev;"
    s = s & "form_home|Form{a} gazde|txt;form_away|Form{a} oaspe{t}i|txt;"
    s = s & "result|Rezultat|txt;tip_won|Pont c{a^}{s}tigat|win;upcoming|Viitor|yn;status|Stare|txt;"
    s = s & "match_id|ID meci|txt;home_logo|Sigl{a} gazde (URL)|txt;away_logo|Sigl{a} oaspe{t}i (URL)|txt"
    LayoutPredictionsBasketball = s
End Function

Private Function LayoutPredictionsTennis() As String
    Dim s As String
    s = "time_local|Ora|txt;date_local|Data|txt;competition|Turneu|txt;"
    s = s & "home|Juc{a}tor 1|txt;away|Juc{a}tor 2|txt;grade|Calitate|grd;confidence|{I^}ncredere|int;"
    s = s & "p_1|1|pct;p_2|2|pct;odds_1|Cot{a} 1|odd;odds_2|Cot{a} 2|odd;"
    s = s & "main_3_label|Total seturi|txt;main_3_p|P total seturi|pct;main_4_label|Scor la seturi|txt;main_4_p|P scor seturi|pct;"
    s = s & "surface|Suprafa{t}{a}|txt;best_of|Seturi (maxim)|int;set_win|Set c{a^}{s}tigat de J1|pc0;exp_games|Game-uri estimate|num;"
    s = s & "tip_label|Pont principal|txt;tip_p|Prob. pont|pct;tip_odds|Cot{a} pont|odd;"
    s = s & "selection_label|Selec{t}ie (prag Panou)|mrk;value_label|Valoare|txt;value_ev|EV|ev;"
    s = s & "form_home|Form{a} J1|txt;form_away|Form{a} J2|txt;"
    s = s & "result|Rezultat (seturi)|txt;tip_won|Pont c{a^}{s}tigat|win;upcoming|Viitor|yn;status|Stare|txt;"
    s = s & "match_id|ID meci|txt;home_logo|Steag J1 (URL)|txt;away_logo|Steag J2 (URL)|txt"
    LayoutPredictionsTennis = s
End Function

Private Function LayoutMatchCard() As String
    Dim s As String
    s = "competition|Competi{t}ie|txt;date_local|Data|txt;time_local|Ora|txt;home|Gazde|txt;"
    s = s & "away|Oaspe{t}i|txt;status|Stare|txt;grade|Calitate date (A-D)|grd;"
    s = s & "confidence|{I^}ncredere (0-100)|int;xg_home|xG gazde|num;xg_away|xG oaspe{t}i|num;"
    s = s & "p_1|Victorie gazde (1)|pc0;p_x|Egal (X)|pc0;p_2|Victorie oaspe{t}i (2)|pc0;"
    s = s & "p_1x|Gazde sau egal (1X)|pc0;p_x2|Egal sau oaspe{t}i (X2)|pc0;p_12|F{a}r{a} egal (12)|pc0;"
    s = s & "p_over15|Peste 1.5 goluri|pc0;p_over25|Peste 2.5 goluri|pc0;p_under25|Sub 2.5 goluri|pc0;"
    s = s & "p_over35|Peste 3.5 goluri|pc0;p_btts|Ambele marcheaz{a} (GG)|pc0;p_no_btts|Nu marcheaz{a} ambele (NG)|pc0;"
    s = s & "p_ht_1|Pauz{a}: gazde|pc0;p_ht_x|Pauz{a}: egal|pc0;p_ht_2|Pauz{a}: oaspe{t}i|pc0;"
    s = s & "p_ht_over05|Pauz{a}: peste 0.5 goluri|pc0;score_1|Scor probabil|txt;p_score_1|Prob. scor|pc0;"
    s = s & "htft_label_1|Pauz{a}/Final probabil|txt;p_htft_1|Prob. pauz{a}/final|pc0;"
    s = s & "tip_label|Pont principal|txt;tip_p|Prob. pont|pc0;"
    s = s & "selection_label|Selec{t}ie (prag Panou)|txt;selection_p|Prob. selec{t}ie|pc0;"
    s = s & "odds_1|Cot{a} 1|odd;odds_x|Cot{a} X|odd;odds_2|Cot{a} 2|odd;"
    s = s & "fair_1|Cot{a} corect{a} 1|odd;fair_x|Cot{a} corect{a} X|odd;fair_2|Cot{a} corect{a} 2|odd;"
    s = s & "value_label|Cea mai bun{a} valoare|txt;value_ev|EV valoare|ev;"
    s = s & "form_home|Form{a} gazde (ultimele 5)|txt;form_away|Form{a} oaspe{t}i (ultimele 5)|txt;"
    s = s & "sample_home|Meciuri analizate gazde|int;sample_away|Meciuri analizate oaspe{t}i|int;"
    s = s & "h2h_played|Meciuri directe|int;h2h_home_wins|H2H victorii gazde|int;h2h_draws|H2H egaluri|int;"
    s = s & "h2h_away_wins|H2H victorii oaspe{t}i|int;h2h_goals_avg|H2H goluri/meci|num;"
    s = s & "days_since_home|Zile de la ultimul meci (gazde)|int;days_since_away|Zile de la ultimul meci (oaspe{t}i)|int;"
    s = s & "saved|Salvat {i^}n registru|yn;retrospective|Analiz{a} retrospectiv{a}|yn;warnings|Avertismente|txt;"
    s = s & "reason|Motiv|txt;version|Versiune model|txt;match_id|ID meci|txt"
    LayoutMatchCard = s
End Function

Private Function LayoutMatchCardBasketball() As String
    Dim s As String
    s = "competition|Competi{t}ie|txt;date_local|Data|txt;time_local|Ora|txt;home|Gazde|txt;"
    s = s & "away|Oaspe{t}i|txt;status|Stare|txt;grade|Calitate date (A-D)|grd;confidence|{I^}ncredere (0-100)|int;"
    s = s & "p_1|Victorie gazde (cu prelungiri)|pc0;p_2|Victorie oaspe{t}i (cu prelungiri)|pc0;"
    s = s & "odds_1|Cot{a} 1|odd;odds_2|Cot{a} 2|odd;fair_1|Cot{a} corect{a} 1|odd;fair_2|Cot{a} corect{a} 2|odd;"
    s = s & "main_3_label|Handicap|txt;main_3_p|Prob. handicap|pc0;main_3_odds|Cot{a} handicap|odd;"
    s = s & "main_4_label|Total puncte|txt;main_4_p|Prob. total|pc0;main_4_odds|Cot{a} total|odd;"
    s = s & "exp_home|Puncte estimate gazde|num;exp_away|Puncte estimate oaspe{t}i|num;exp_total|Total estimat|num;"
    s = s & "exp_margin|Diferen{t}{a} estimat{a}|num;p_overtime|Probabilitate prelungiri|pc0;"
    s = s & "tip_label|Pont principal|txt;tip_p|Prob. pont|pc0;tip_odds|Cot{a} pont|odd;"
    s = s & "selection_label|Selec{t}ie (prag Panou)|txt;selection_p|Prob. selec{t}ie|pc0;"
    s = s & "value_label|Cea mai bun{a} valoare|txt;value_ev|EV valoare|ev;"
    s = s & "form_home|Form{a} gazde|txt;form_away|Form{a} oaspe{t}i|txt;"
    s = s & "sample_home|Meciuri analizate gazde|int;sample_away|Meciuri analizate oaspe{t}i|int;"
    s = s & "h2h_played|Meciuri directe|int;h2h_home_wins|H2H victorii gazde|int;h2h_away_wins|H2H victorii oaspe{t}i|int;"
    s = s & "days_since_home|Zile de la ultimul meci (gazde)|int;days_since_away|Zile de la ultimul meci (oaspe{t}i)|int;"
    s = s & "saved|Salvat {i^}n registru|yn;retrospective|Analiz{a} retrospectiv{a}|yn;warnings|Avertismente|txt;"
    s = s & "reason|Motiv|txt;version|Versiune model|txt;match_id|ID meci|txt"
    LayoutMatchCardBasketball = s
End Function

Private Function LayoutMatchCardTennis() As String
    Dim s As String
    s = "competition|Turneu|txt;date_local|Data|txt;time_local|Ora|txt;home|Juc{a}tor 1|txt;"
    s = s & "away|Juc{a}tor 2|txt;status|Stare|txt;grade|Calitate date (A-D)|grd;confidence|{I^}ncredere (0-100)|int;"
    s = s & "p_1|Victorie J1|pc0;p_2|Victorie J2|pc0;"
    s = s & "odds_1|Cot{a} 1|odd;odds_2|Cot{a} 2|odd;fair_1|Cot{a} corect{a} 1|odd;fair_2|Cot{a} corect{a} 2|odd;"
    s = s & "main_3_label|Total seturi|txt;main_3_p|Prob. total seturi|pc0;main_3_odds|Cot{a} total seturi|odd;"
    s = s & "main_4_label|Scor la seturi probabil|txt;main_4_p|Prob. scor seturi|pc0;main_4_odds|Cot{a} scor seturi|odd;"
    s = s & "surface|Suprafa{t}{a}|txt;best_of|Seturi (maxim)|int;set_win|Set c{a^}{s}tigat de J1|pc0;exp_games|Game-uri estimate|num;"
    s = s & "tip_label|Pont principal|txt;tip_p|Prob. pont|pc0;tip_odds|Cot{a} pont|odd;"
    s = s & "selection_label|Selec{t}ie (prag Panou)|txt;selection_p|Prob. selec{t}ie|pc0;"
    s = s & "value_label|Cea mai bun{a} valoare|txt;value_ev|EV valoare|ev;"
    s = s & "form_home|Form{a} J1|txt;form_away|Form{a} J2|txt;"
    s = s & "sample_home|Meciuri analizate J1|int;sample_away|Meciuri analizate J2|int;"
    s = s & "h2h_played|Meciuri directe|int;h2h_home_wins|H2H victorii J1|int;h2h_away_wins|H2H victorii J2|int;"
    s = s & "days_since_home|Zile de la ultimul meci (J1)|int;days_since_away|Zile de la ultimul meci (J2)|int;"
    s = s & "saved|Salvat {i^}n registru|yn;retrospective|Analiz{a} retrospectiv{a}|yn;warnings|Avertismente|txt;"
    s = s & "reason|Motiv|txt;version|Versiune model|txt;match_id|ID meci|txt"
    LayoutMatchCardTennis = s
End Function

Private Function LayoutMarkets() As String
    Dim s As String
    s = "label|Pia{t}{a}|txt;group|Grup|txt;probability|Probabilitate|pct;fair_odds|Cot{a} corect{a}|odd;"
    s = s & "odds|Cot{a}|odd;ev|EV|ev;is_tip|Pont|yn;is_selection|Selec{t}ie (prag Panou)|mrk"
    LayoutMarkets = s
End Function

Private Function LayoutHtft() As String
    LayoutHtft = "label|Pauz{a} / Final|txt;probability|Probabilitate|pct;fair_odds|Cot{a} corect{a}|odd"
End Function

Private Function LayoutInsights() As String
    LayoutInsights = "n|Nr.|int;text|Observa{t}ie|txt"
End Function

Private Function LayoutScores() As String
    LayoutScores = "rank|Loc|int;score|Scor|txt;probability|Probabilitate|hot;fair_odds|Cot{a} corect{a}|odd"
End Function

Private Function LayoutGrid() As String
    Dim s As String
    ' hgr: o singura scala de culori pe toata matricea (nu cate una pe coloana).
    s = "home_goals|Gazde \ Oaspe{t}i|int;away_0|0|hgr;away_1|1|hgr;away_2|2|hgr;"
    s = s & "away_3|3|hgr;away_4|4|hgr;away_5|5|hgr"
    LayoutGrid = s
End Function

Private Function LayoutFormStats() As String
    Dim s As String
    s = "side|Rol|txt;team|Echip{a}|txt;window|Interval|txt;played|Meciuri|int;wins|V|int;draws|E|int;"
    s = s & "losses|{I^}|int;points_per_game|Puncte/meci|num;win_rate|% victorii|pc0;"
    s = s & "scored_avg|Marcate/meci|num;conceded_avg|Primite/meci|num;"
    s = s & "over15|Peste 1.5|pc0;over25|Peste 2.5|pc0;btts|GG|pc0;clean_sheets|F{a}r{a} gol primit|pc0;"
    s = s & "failed_to_score|F{a}r{a} gol marcat|pc0;sequence|Ultimele 5|txt;"
    s = s & "streak_unbeaten|Serie f{a}r{a} {i^}nfr{a^}ngere|int;streak_wins|Serie de victorii|int;"
    s = s & "streak_winless|Serie f{a}r{a} victorie|int;days_since_last|Zile de la ultimul meci|int;"
    s = s & "matches_last_30_days|Meciuri {i^}n 30 de zile|int"
    LayoutFormStats = s
End Function

Private Function LayoutForm() As String
    Dim s As String
    s = "side|Rol|txt;team|Echip{a}|txt;n|Nr.|int;date|Data|txt;competition|Competi{t}ie|txt;"
    s = s & "venue|A/D|txt;opponent|Adversar|txt;score|Scor|txt;goals_for|Marcate|int;"
    s = s & "goals_against|Primite|int;result|Rezultat|wdl"
    LayoutForm = s
End Function

Private Function LayoutH2H() As String
    Dim s As String
    s = "date|Data|txt;competition|Competi{t}ie|txt;home|Gazde|txt;away|Oaspe{t}i|txt;"
    s = s & "score|Scor|txt;result|Rezultat pentru gazdele de azi|wdl"
    LayoutH2H = s
End Function

Private Function LayoutStandings() As String
    Dim s As String
    s = "position|Loc|int;team|Echip{a}|txt;played|M|int;wins|V|int;draws|E|int;losses|{I^}|int;"
    s = s & "scored|GM|int;conceded|GP|int;goal_diff|Golaveraj|int;points|Puncte|int;role|Meciul analizat|mrk"
    LayoutStandings = s
End Function

Private Function LayoutValue() As String
    Dim s As String
    s = "date_local|Data|txt;time_local|Ora|txt;competition|Competi{t}ie|txt;home|Gazde|txt;"
    s = s & "away|Oaspe{t}i|txt;grade|Calitate|grd;market_label|Pia{t}{a}|txt;probability|Probabilitate|pct;"
    s = s & "fair_odds|Cot{a} corect{a}|odd;odds|Cot{a}|odd;edge|Avantaj|ev;ev|EV|ev;match_id|ID meci|txt"
    LayoutValue = s
End Function

Private Function LayoutMetrics() As String
    Dim s As String
    s = "total_matches|Meciuri evaluate|int;selected|Selec{t}ii|int;settled|Decise|int;"
    s = s & "wins|C{a^}{s}tigate|int;pending|{I^}n a{s}teptare|int;coverage|Acoperire|pc0;"
    s = s & "accuracy|Acurate{t}e|pc0;ci_low|Interval 95%: minim|pc0;ci_high|Interval 95%: maxim|pc0;"
    s = s & "brier|Scor Brier|num;target_supported|Obiectiv 85% confirmat|yn"
    LayoutMetrics = s
End Function

Private Function LayoutCalibration() As String
    Dim s As String
    s = "range|Band{a}|txt;count|Selec{t}ii|int;predicted|Probabilitate medie|pc0;"
    s = s & "actual|Rat{a} real{a}|pc0"
    LayoutCalibration = s
End Function

Private Function LayoutRecord() As String
    Dim s As String
    s = "date_utc|Data (UTC)|txt;time_utc|Ora (UTC)|txt;sport|Sport|spt;competition|Competi{t}ie|txt;home|Gazde|txt;"
    s = s & "away|Oaspe{t}i|txt;selection_label|Selec{t}ie|txt;probability|Probabilitate|pct;"
    s = s & "fair_odds|Cot{a} corect{a}|odd;grade|Calitate|grd;status|Stare|txt;score|Scor|txt;"
    s = s & "won|C{a^}{s}tigat|win;created_utc|Salvat la (UTC)|txt;match_id|ID meci|txt"
    LayoutRecord = s
End Function

Private Function LayoutRecoTickets() As String
    Dim s As String
    s = "target|Cot{a} {t}int{a}|num;status|Stare|sts;total_odds|Cot{a} total{a}|odd;"
    s = s & "probability|Probabilitate estimat{a}|pct;legs|Selec{t}ii|int;sports|Sporturi|spt;"
    s = s & "selections|Biletul|txt;rationale|De ce acest bilet|txt;reason|Motiv (indisponibil)|txt;"
    s = s & "payout_odds|Cot{a} pl{a}tit{a}|odd"
    LayoutRecoTickets = s
End Function

Private Function LayoutRecoLegs() As String
    Dim s As String
    s = "target|Bilet (cot{a} {t}int{a})|num;leg|Nr.|int;date_local|Data|txt;time_local|Ora|txt;"
    s = s & "sport|Sport|spt;competition|Competi{t}ie|txt;home|Gazde / J1|txt;away|Oaspe{t}i / J2|txt;"
    s = s & "label|Pariu|txt;odds|Cot{a}|odd;probability|Probabilitate|pct;grade|Calitate|grd;"
    s = s & "status|Stare|sts;score|Scor|txt;reason|De ce|txt;home_logo|Sigl{a} gazde (URL)|txt;"
    s = s & "away_logo|Sigl{a} oaspe{t}i (URL)|txt;match_id|ID meci|txt"
    LayoutRecoLegs = s
End Function

Private Function LayoutRecoSingles() As String
    Dim s As String
    s = "rank|Loc|int;date_local|Data|txt;time_local|Ora|txt;sport|Sport|spt;competition|Competi{t}ie|txt;"
    s = s & "home|Gazde / J1|txt;away|Oaspe{t}i / J2|txt;label|Pariu|txt;odds|Cot{a}|odd;"
    s = s & "probability|Probabilitate|pct;ev|EV|ev;grade|Calitate|grd;status|Stare|sts;reason|De ce|txt;"
    s = s & "home_logo|Sigl{a} gazde (URL)|txt;away_logo|Sigl{a} oaspe{t}i (URL)|txt;match_id|ID meci|txt"
    LayoutRecoSingles = s
End Function

Private Function LayoutLive() As String
    Dim s As String
    s = "competition|Competi{t}ie|txt;home|Gazde / J1|txt;away|Oaspe{t}i / J2|txt;"
    s = s & "score_home|Scor gazde|int;score_away|Scor oaspe{t}i|int;clock|Minut / set|txt;stage|Etap{a}|txt;"
    s = s & "p_1|1 (final)|pct;p_x|X (final)|pct;p_2|2 (final)|pct;suggestion|Sugestie|txt;"
    s = s & "suggestion_p|Prob. sugestie|pct;suggestion_min_odds|Cot{a} minim{a}|odd;suggestion_kind|Tip|txt;"
    s = s & "suggestions|Toate sugestiile|txt;summary|Rezumat|txt;notes|Note|txt;"
    s = s & "home_logo|Sigl{a} gazde (URL)|txt;away_logo|Sigl{a} oaspe{t}i (URL)|txt;match_id|ID meci|txt"
    LayoutLive = s
End Function

Private Function LayoutLiveMarkets() As String
    Dim s As String
    s = "home|Gazde / J1|txt;away|Oaspe{t}i / J2|txt;minute|Minut|int;score|Scor|txt;label|Pia{t}{a}|txt;"
    s = s & "group|Grup|txt;probability|Probabilitate|pct;fair_odds|Cot{a} corect{a}|odd;"
    s = s & "reliable|De {i^}ncredere|yn;why|Explica{t}ie|txt;match_id|ID meci|txt"
    LayoutLiveMarkets = s
End Function

Private Function LayoutSimSummary() As String
    Dim s As String
    s = "dataset_label|Set de date|txt;strategy|Strategie|txt;mode|Mod|txt;target_odds|Cot{a} {t}int{a}|num;"
    s = s & "start|De la|txt;end|P{a^}n{a} la|txt;initial|Suma ini{t}ial{a}|num;"
    s = s & "total_invested|Total investit (toate sc{a}rile)|num;total_returned|Total recuperat|num;"
    s = s & "net|C{a^}{s}tig net (recuperat - investit)|num;final|Suma ini{t}ial{a} + c{a^}{s}tig net|num;"
    s = s & "profit|Profit|num;roi|ROI|ev;bets|Bilete / pariuri|int;won|C{a^}{s}tigate|int;"
    s = s & "lost|Pierdute|int;void|Anulate|int;hit_rate|Rat{a} de reu{s}it{a}|pc0;"
    s = s & "first_run_days|Prima scar{a}: zile c{a^}{s}tigate|int;first_run_peak|Prima scar{a}: v{a^}rf|num;"
    s = s & "longest_streak|Cea mai lung{a} scar{a} (zile)|int;longest_streak_peak|V{a^}rful ei|num;"
    s = s & "restarts|Reporniri|int;days_without_ticket|Zile f{a}r{a} bilet|int;"
    s = s & "max_drawdown|Sc{a}dere maxim{a}|pc0;baseline_label|Compara{t}ie|txt;"
    s = s & "baseline_profit|Profit comparat|num;baseline_roi|ROI comparat|ev;warnings|Avertismente|txt;"
    s = s & "disclaimer|Aten{t}ie|txt"
    LayoutSimSummary = s
End Function

Private Function LayoutSimLadders() As String
    Dim s As String
    s = "n|Scara|int;start|Start|txt;end|Sf{a^}r{s}it|txt;days|Zile c{a^}{s}tigate|int;tickets|Bilete|int;"
    s = s & "peak|V{a^}rf|num;final|Final|num;status|Stare|sts"
    LayoutSimLadders = s
End Function

Private Function LayoutSimDays() As String
    Dim s As String
    s = "date|Data|txt;result|Rezultat|sts;stake|Miz{a}|num;odds|Cot{a}|odd;probability|Probabilitate|pct;"
    s = s & "payout|Plat{a}|num;bankroll_after|Banc{a} dup{a}|num;ladder_index|Scara|int;"
    s = s & "streak_day|Bilet nr. {i^}n scar{a}|int;legs_count|Selec{t}ii|int;selections|Biletul|txt;reason|Motiv|txt"
    LayoutSimDays = s
End Function

Private Function LayoutSimEquity() As String
    LayoutSimEquity = "date|Data|txt;net|C{a^}{s}tig net cumulat|num;bankroll|Banca sc{a}rii|num;ladder_index|Scara|int"
End Function

Private Function LayoutSimDatasets() As String
    Dim s As String
    s = "id|ID (pentru B4)|txt;label|Set de date|txt;sport|Sport|spt;matches|Meciuri|int;bettable|Cu cote|int;"
    s = s & "start|De la|txt;end|P{a^}n{a} la|txt;available|Disponibil|yn;source|Surs{a}|txt;hint|Cum se ob{t}ine|txt"
    LayoutSimDatasets = s
End Function

Private Function LayoutWalletSummary() As String
    Dim s As String
    s = "currency|Moned{a}|txt;balance|Sold|num;deposited|Depus|num;staked_open|Miz{a} {i^}n joc|num;"
    s = s & "profit|Profit|num;open|Deschise|int;won|C{a^}{s}tigate|int;lost|Pierdute|int;void|Anulate|int;"
    s = s & "bets|Total pariuri|int;notice|Aten{t}ie|txt"
    LayoutWalletSummary = s
End Function

Private Function LayoutWalletBets() As String
    Dim s As String
    s = "created_utc|Creat (UTC)|txt;label|Pariu|txt;source|Surs{a}|txt;stake|Miz{a}|num;total_odds|Cot{a}|odd;"
    s = s & "status|Stare|sts;payout|Plat{a}|num;legs_count|Selec{t}ii|int;selections|Detalii|txt;id|ID|txt"
    LayoutWalletBets = s
End Function

Private Function LayoutWalletHistory() As String
    LayoutWalletHistory = "at_utc|Moment (UTC)|txt;type|Tip|txt;amount|Sum{a}|num;balance|Sold dup{a}|num;bet_id|Pariu|txt"
End Function

'==============================================================================
' Interogari catre API
'==============================================================================

Private Function BoardQuery(ByVal dayText As String, ByVal sport As String) As String
    Dim query As String
    Dim competition As String
    query = "day=" & dayText & "&sport=" & sport & "&min_grade=" & PanelGrade() & "&threshold=" & NumToStr(PanelThreshold())
    If PanelYes(CELL_UPCOMING) Then query = query & "&upcoming_only=1"
    If PanelYes(CELL_DEMO) Then query = query & "&demo=1"
    competition = PanelCompetition()
    If Len(competition) > 0 Then query = query & "&competition=" & UrlEncode(competition)
    BoardQuery = query
End Function

Private Function PredictionsQuery(ByVal dayText As String, ByVal sport As String) As String
    PredictionsQuery = BoardQuery(dayText, sport) & "&limit=" & PanelLong(CELL_LIMIT, 200, 1, 400)
End Function

Private Function AnalysisQuery(ByVal sport As String) As String
    AnalysisQuery = "enrich=1&threshold=" & NumToStr(PanelThreshold()) & "&sport=" & sport
End Function

Private Function MatchQuery(ByVal section As String, ByVal sport As String) As String
    MatchQuery = "section=" & section & "&threshold=" & NumToStr(PanelThreshold()) & "&sport=" & sport
End Function

'==============================================================================
' HTTP (late binding, fara referinte)
'==============================================================================

' Returneaza tabelul TSV sau opreste macro-ul cu mesajul de eroare al serverului.
Private Function ApiTable(ByVal method As String, ByVal apiPath As String, ByVal query As String) As String
    Dim httpStatus As Long
    Dim message As String
    Dim body As String
    body = ApiTry(method, apiPath, query, httpStatus, message)
    If httpStatus <> 200 And httpStatus <> 202 Then Err.Raise ERR_API, APP_TITLE, message
    ApiTable = body
End Function

' Ca ApiTable, dar intoarce statusul HTTP si mesajul in loc sa opreasca macro-ul.
Private Function ApiTry(ByVal method As String, ByVal apiPath As String, ByVal query As String, _
                        ByRef httpStatus As Long, ByRef message As String) As String
    Dim url As String
    Dim body As String
    url = BaseUrl() & apiPath & "?format=tsv"
    If Len(query) > 0 Then url = url & "&" & query
    body = HttpCall(method, url, httpStatus)
    message = ""
    If httpStatus <> 200 And httpStatus <> 202 Then message = ErrorFromTable(body, httpStatus)
    ApiTry = body
End Function

Private Function HttpCall(ByVal method As String, ByVal url As String, ByRef httpStatus As Long) As String
    Dim http As Object
    Dim failure As String
    Dim failNumber As Long
    Dim totalText As String
    httpStatus = 0
    m_total = -1
    Set http = NewHttp()
    On Error Resume Next
    http.setTimeouts CONNECT_TIMEOUT_MS, CONNECT_TIMEOUT_MS, 30000, ReceiveTimeout(url)
    If IsLocalUrl(url) Then http.setProxy 1
    Err.Clear
    http.Open method, url, False
    http.setRequestHeader "Accept", "text/tab-separated-values"
    If method = "POST" Then
        http.setRequestHeader "Content-Type", "application/x-www-form-urlencoded"
        http.send ""
    Else
        http.send
    End If
    ' Esc apasat in timpul cererii ajunge aici ca eroarea 18 (EnableCancelKey = xlErrorHandler).
    If Err.Number <> 0 Then
        failNumber = Err.Number
        failure = Err.Description
        On Error GoTo 0
        RaiseHttpFailure failNumber, failure
    End If
    httpStatus = http.Status
    If Err.Number <> 0 Then
        failNumber = Err.Number
        failure = Err.Description
        On Error GoTo 0
        RaiseHttpFailure failNumber, failure
    End If
    totalText = http.getResponseHeader("X-Total-Count")
    If Err.Number = 18 Then
        On Error GoTo 0
        Err.Raise 18
    End If
    If Err.Number = 0 And Len(totalText) > 0 Then m_total = CLng(Val(totalText))
    Err.Clear
    On Error GoTo 0
    HttpCall = Utf8Decode(http.responseBody)
End Function

' Apelat doar dupa On Error GoTo 0 (altfel On Error Resume Next din HttpCall ar inghiti eroarea).
Private Sub RaiseHttpFailure(ByVal failNumber As Long, ByVal failure As String)
    If failNumber = 18 Then Err.Raise 18
    ' Un timeout nu inseamna server oprit: serverul raspunde, dar inca lucreaza.
    If failNumber = HTTP_TIMEOUT_ERROR Then
        Err.Raise ERR_API, APP_TITLE, Ro("Serverul {i^}nc{a} lucreaz{a} (prima rulare poate dura c{a^}teva minute); ap{a}s{a} din nou peste un minut.")
    End If
    Err.Raise ERR_SERVER, APP_TITLE, ServerDownMessage(failure)
End Sub

' Simularea si recomandarile (calcule lungi la prima rulare) asteapta mai mult.
Private Function ReceiveTimeout(ByVal url As String) As Long
    If InStr(1, url, "/api/excel/simulate", vbTextCompare) > 0 Or InStr(1, url, "/api/excel/recommendations", vbTextCompare) > 0 Then
        ReceiveTimeout = LONG_RECEIVE_TIMEOUT_MS
    Else
        ReceiveTimeout = RECEIVE_TIMEOUT_MS
    End If
End Function

Private Function NewHttp() As Object
    Dim http As Object
    On Error Resume Next
    Set http = CreateObject("MSXML2.ServerXMLHTTP.6.0")
    If http Is Nothing Then Set http = CreateObject("WinHttp.WinHttpRequest.5.1")
    On Error GoTo 0
    If http Is Nothing Then
        Err.Raise ERR_SERVER, APP_TITLE, Ro("Windows nu are componenta HTTP (MSXML2 / WinHttp). Reinstaleaz{a} Office.")
    End If
    Set NewHttp = http
End Function

Private Function IsLocalUrl(ByVal url As String) As Boolean
    Dim lower As String
    lower = LCase$(url)
    IsLocalUrl = (InStr(1, lower, "://127.0.0.1") > 0 Or InStr(1, lower, "://localhost") > 0)
End Function

' Decodeaza raspunsul ca UTF-8 (diacriticele raman corecte pe orice Windows).
Private Function Utf8Decode(ByVal bytes As Variant) As String
    Dim stream As Object
    If Not IsArray(bytes) Then Exit Function
    If UBound(bytes) < LBound(bytes) Then Exit Function
    Set stream = CreateObject("ADODB.Stream")
    stream.Type = 1
    stream.Open
    stream.Write bytes
    stream.Position = 0
    stream.Type = 2
    stream.Charset = "utf-8"
    Utf8Decode = stream.ReadText
    stream.Close
End Function

Private Function ErrorFromTable(ByVal body As String, ByVal httpStatus As Long) As String
    Dim headers() As String
    Dim data() As String
    Dim n As Long
    Dim col As Long
    On Error GoTo Raw
    n = ParseTsv(body, headers, data)
    col = FindCol(headers, "error")
    If n > 0 And col >= 0 Then
        ErrorFromTable = data(1, col) & " (HTTP " & httpStatus & ")"
        Exit Function
    End If
Raw:
    If Err.Number = 18 Then Err.Raise 18
    If httpStatus = 404 Then
        ErrorFromTable = Ro("Serverul nu cunoa{s}te aceast{a} adres{a} /api/excel (HTTP 404). Actualizeaz{a} proiectul {s}i reporne{s}te start.ps1.")
    Else
        ErrorFromTable = "HTTP " & httpStatus & ": " & Left$(Replace(Replace(body, vbCr, " "), vbLf, " "), 300)
    End If
End Function

Private Function ServerDownMessage(ByVal detail As String) As String
    Dim message As String
    message = Ro("Serverul FootyPreds nu r{a}spunde la ") & BaseUrl() & "." & vbLf & vbLf
    message = message & Ro("Porne{s}te serverul: dublu-click pe start.ps1 din folderul proiectului ")
    message = message & Ro("(sau {i^}n PowerShell: .\start.ps1) {s}i las{a} fereastra deschis{a}. ")
    message = message & Ro("Verific{a} apoi adresa din Panou (B4).") & vbLf & vbLf
    message = message & "Detalii: " & Trim$(detail)
    ServerDownMessage = message
End Function

Private Function BaseUrl() As String
    Dim url As String
    url = Trim$(SafeText(ThisWorkbook.Worksheets(SH_PANEL).Range(CELL_URL).Value))
    If Len(url) = 0 Then url = DEFAULT_URL
    Do While Right$(url, 1) = "/"
        url = Left$(url, Len(url) - 1)
    Loop
    If LCase$(Left$(url, 7)) <> "http://" And LCase$(Left$(url, 8)) <> "https://" Then
        Err.Raise ERR_INPUT, APP_TITLE, Ro("Adresa API din Panou (B4) trebuie s{a} {i^}nceap{a} cu http://, de ex. ") & DEFAULT_URL
    End If
    BaseUrl = url
End Function

Private Function UrlEncode(ByVal text As String) As String
    Dim i As Long
    Dim code As Long
    Dim low As Long
    Dim out As String
    Dim ch As String
    i = 1
    Do While i <= Len(text)
        ch = Mid$(text, i, 1)
        code = AscW(ch)
        If code < 0 Then code = code + 65536
        If (code >= 48 And code <= 57) Or (code >= 65 And code <= 90) Or (code >= 97 And code <= 122) Then
            out = out & ch
        ElseIf code = 45 Or code = 46 Or code = 95 Or code = 126 Then
            out = out & ch
        ElseIf code < 128 Then
            out = out & "%" & Hex2(code)
        ElseIf code < 2048 Then
            out = out & "%" & Hex2(192 + code \ 64) & "%" & Hex2(128 + (code And 63))
        ElseIf code >= 55296 And code <= 56319 And i < Len(text) Then
            low = AscW(Mid$(text, i + 1, 1))
            If low < 0 Then low = low + 65536
            code = 65536 + (code - 55296) * 1024 + (low - 56320)
            out = out & "%" & Hex2(240 + code \ 262144) & "%" & Hex2(128 + ((code \ 4096) And 63))
            out = out & "%" & Hex2(128 + ((code \ 64) And 63)) & "%" & Hex2(128 + (code And 63))
            i = i + 1
        Else
            out = out & "%" & Hex2(224 + code \ 4096) & "%" & Hex2(128 + ((code \ 64) And 63))
            out = out & "%" & Hex2(128 + (code And 63))
        End If
        i = i + 1
    Loop
    UrlEncode = out
End Function

Private Function Hex2(ByVal amount As Long) As String
    Hex2 = Right$("0" & Hex$(amount), 2)
End Function

'==============================================================================
' TSV -> foi de calcul
'==============================================================================

' headers(0..k-1) si data(1..n, 0..k-1); intoarce n (numarul de randuri de date).
Private Function ParseTsv(ByVal body As String, ByRef headers() As String, ByRef data() As String) As Long
    Dim lines() As String
    Dim parts() As String
    Dim i As Long
    Dim j As Long
    Dim n As Long
    Dim colCount As Long
    body = Replace(body, vbCrLf, vbLf)
    body = Replace(body, vbCr, vbLf)
    If Len(body) > 0 Then
        If Left$(body, 1) = ChrW(&HFEFF) Then body = Mid$(body, 2)
    End If
    If Len(Trim$(body)) = 0 Then
        Err.Raise ERR_API, APP_TITLE, Ro("Serverul a trimis un r{a}spuns gol.")
    End If
    lines = Split(body, vbLf)
    headers = Split(lines(0), vbTab)
    colCount = UBound(headers) + 1
    n = UBound(lines)
    Do While n > 0
        If Len(lines(n)) > 0 Then Exit Do
        n = n - 1
    Loop
    If n > 0 Then
        ReDim data(1 To n, 0 To colCount - 1)
        For i = 1 To n
            parts = Split(lines(i), vbTab)
            For j = 0 To UBound(parts)
                If j < colCount Then data(i, j) = parts(j)
            Next j
        Next i
    Else
        ReDim data(1 To 1, 0 To colCount - 1)
    End If
    ParseTsv = n
End Function

Private Function FindCol(ByRef headers() As String, ByVal key As String) As Long
    Dim j As Long
    FindCol = -1
    For j = LBound(headers) To UBound(headers)
        If headers(j) = key Then
            FindCol = j
            Exit Function
        End If
    Next j
End Function

' Valoarea unei coloane din primul rand al unui tabel TSV ("" daca lipseste).
Private Function FieldOf(ByVal body As String, ByVal key As String) As String
    Dim headers() As String
    Dim data() As String
    Dim col As Long
    If ParseTsv(body, headers, data) < 1 Then Exit Function
    col = FindCol(headers, key)
    If col >= 0 Then FieldOf = data(1, col)
End Function

Private Function ParseLayout(ByVal layout As String, ByRef keys() As String, ByRef titles() As String, _
                             ByRef fmts() As String) As Long
    Dim items() As String
    Dim parts() As String
    Dim i As Long
    Dim n As Long
    items = Split(layout, ";")
    ReDim keys(1 To UBound(items) + 1)
    ReDim titles(1 To UBound(items) + 1)
    ReDim fmts(1 To UBound(items) + 1)
    For i = 0 To UBound(items)
        If Len(Trim$(items(i))) > 0 Then
            parts = Split(items(i), "|")
            n = n + 1
            keys(n) = Trim$(parts(0))
            titles(n) = keys(n)
            fmts(n) = "txt"
            If UBound(parts) >= 1 Then titles(n) = Ro(parts(1))
            If UBound(parts) >= 2 Then fmts(n) = Trim$(parts(2))
        End If
    Next i
    ParseLayout = n
End Function

' Pozitia (1..m) a unei coloane API in layout; 0 daca lipseste.
Private Function LayoutPos(ByVal layout As String, ByVal key As String) As Long
    Dim keys() As String
    Dim titles() As String
    Dim fmts() As String
    Dim m As Long
    Dim j As Long
    m = ParseLayout(layout, keys, titles, fmts)
    For j = 1 To m
        If keys(j) = key Then
            LayoutPos = j
            Exit Function
        End If
    Next j
End Function

' Textul din API -> valoarea din celula. Numerele se citesc cu Val (punct zecimal).
Private Function Convert(ByVal raw As String, ByVal fmt As String) As Variant
    Select Case fmt
        Case "pct", "pc0", "hot", "hgr", "num", "odd", "ev"
            If Len(raw) = 0 Then
                Convert = Empty
            Else
                Convert = Val(raw)
            End If
        Case "int"
            If Len(raw) = 0 Then
                Convert = Empty
            Else
                Convert = CLng(Val(raw))
            End If
        Case "yn", "win"
            Convert = YesNo(raw)
        Case "sts"
            Convert = StatusText(raw)
        Case "spt"
            Convert = SportsText(raw)
        Case "mrk"
            If raw = "0" Then
                Convert = ""
            ElseIf raw = "1" Then
                Convert = "DA"
            Else
                Convert = raw
            End If
        Case Else
            Convert = raw
    End Select
End Function

Private Function YesNo(ByVal raw As String) As String
    If raw = "1" Then
        YesNo = "DA"
    ElseIf raw = "0" Then
        YesNo = "NU"
    Else
        YesNo = raw
    End If
End Function

' Starea unui bilet, pariu, zi simulata sau scari -> romana.
Private Function StatusText(ByVal raw As String) As String
    Select Case raw
        Case "pending"
            StatusText = Ro("{i^}n a{s}teptare")
        Case "won"
            StatusText = Ro("c{a^}{s}tigat")
        Case "lost"
            StatusText = "pierdut"
        Case "void"
            StatusText = "anulat"
        Case "unavailable"
            StatusText = "indisponibil"
        Case "skipped"
            StatusText = Ro("f{a}r{a} bilet")
        Case "open"
            StatusText = Ro("{i^}n curs")
        Case "cashed"
            StatusText = Ro("{i^}ncasat")
        Case Else
            StatusText = raw
    End Select
End Function

Private Function NumberFormatOf(ByVal fmt As String) As String
    Select Case fmt
        Case "pct", "pc0", "hot", "hgr"
            NumberFormatOf = "0.0%"
        Case "num", "odd"
            NumberFormatOf = "0.00"
        Case "int"
            NumberFormatOf = "0"
        Case "ev"
            NumberFormatOf = "[Color10]+0.0%;[Red]-0.0%;0.0%"
        Case Else
            NumberFormatOf = "@"
    End Select
End Function

' Scrie un tabel TSV (titlu optional, antet, date) si intoarce primul rand liber dupa el.
Private Function WriteTable(ByVal ws As Worksheet, ByVal firstRow As Long, ByVal firstCol As Long, _
                            ByVal title As String, ByVal body As String, ByVal layout As String) As Long
    Dim headers() As String
    Dim data() As String
    Dim keys() As String
    Dim titles() As String
    Dim fmts() As String
    Dim out() As Variant
    Dim n As Long
    Dim m As Long
    Dim i As Long
    Dim j As Long
    Dim src As Long
    Dim headRow As Long
    Dim heatFirst As Long
    Dim heatLast As Long
    Dim target As Range

    n = ParseTsv(body, headers, data)
    m = ParseLayout(layout, keys, titles, fmts)
    headRow = firstRow
    If Len(title) > 0 Then
        With ws.Cells(firstRow, firstCol)
            .NumberFormat = "@"
            .Value = title
            .Font.Bold = True
            .Font.Size = 12
        End With
        headRow = firstRow + 1
    End If
    ReDim out(1 To n + 1, 1 To m)
    For j = 1 To m
        out(1, j) = titles(j)
        src = FindCol(headers, keys(j))
        If src >= 0 Then
            For i = 1 To n
                out(i + 1, j) = Convert(data(i, src), fmts(j))
            Next i
        End If
    Next j
    ' Formatul text se pune inainte de valori: "1-0" nu devine data, "=x" nu devine formula.
    ws.Range(ws.Cells(headRow, firstCol), ws.Cells(headRow, firstCol + m - 1)).NumberFormat = "@"
    If n > 0 Then
        For j = 1 To m
            Set target = ws.Range(ws.Cells(headRow + 1, firstCol + j - 1), ws.Cells(headRow + n, firstCol + j - 1))
            target.NumberFormat = NumberFormatOf(fmts(j))
        Next j
    End If
    ws.Range(ws.Cells(headRow, firstCol), ws.Cells(headRow + n, firstCol + m - 1)).Value = out
    StyleHeader ws.Range(ws.Cells(headRow, firstCol), ws.Cells(headRow, firstCol + m - 1))
    If n > 0 Then
        For j = 1 To m
            Set target = ws.Range(ws.Cells(headRow + 1, firstCol + j - 1), ws.Cells(headRow + n, firstCol + j - 1))
            DecorateColumn target, fmts(j)
            If fmts(j) = "mrk" Then HighlightMarked ws, headRow, firstCol, n, m, j, out
            If fmts(j) = "hgr" Then
                If heatFirst = 0 Then heatFirst = j
                heatLast = j
            End If
        Next j
        ' Coloanele "hgr" (matricea scorurilor) primesc o singura scala, comuna.
        If heatFirst > 0 Then
            AddScale ws.Range(ws.Cells(headRow + 1, firstCol + heatFirst - 1), ws.Cells(headRow + n, firstCol + heatLast - 1)), True
        End If
    Else
        ws.Cells(headRow + 1, firstCol).Value = Ro("(nicio {i^}nregistrare)")
        ws.Cells(headRow + 1, firstCol).Font.Italic = True
    End If
    FitColumns ws, firstCol, m, headRow, n
    m_rows = n
    m_cols = m
    If n > 0 Then
        WriteTable = headRow + n + 2
    Else
        WriteTable = headRow + 3
    End If
End Function

' Scrie primul rand al unui tabel TSV vertical: eticheta in stanga, valoarea in dreapta.
Private Function WriteRecord(ByVal ws As Worksheet, ByVal firstRow As Long, ByVal firstCol As Long, _
                             ByVal title As String, ByVal body As String, ByVal layout As String) As Long
    Dim headers() As String
    Dim data() As String
    Dim keys() As String
    Dim titles() As String
    Dim fmts() As String
    Dim out() As Variant
    Dim n As Long
    Dim m As Long
    Dim j As Long
    Dim src As Long
    Dim startRow As Long

    n = ParseTsv(body, headers, data)
    m = ParseLayout(layout, keys, titles, fmts)
    startRow = firstRow
    If Len(title) > 0 Then
        With ws.Cells(firstRow, firstCol)
            .NumberFormat = "@"
            .Value = title
            .Font.Bold = True
            .Font.Size = 12
        End With
        startRow = firstRow + 1
    End If
    ReDim out(1 To m, 1 To 2)
    For j = 1 To m
        out(j, 1) = titles(j)
        src = FindCol(headers, keys(j))
        If n > 0 And src >= 0 Then out(j, 2) = Convert(data(1, src), fmts(j))
        ws.Cells(startRow + j - 1, firstCol + 1).NumberFormat = NumberFormatOf(fmts(j))
    Next j
    ws.Range(ws.Cells(startRow, firstCol), ws.Cells(startRow + m - 1, firstCol)).NumberFormat = "@"
    ws.Range(ws.Cells(startRow, firstCol), ws.Cells(startRow + m - 1, firstCol + 1)).Value = out
    ws.Range(ws.Cells(startRow, firstCol), ws.Cells(startRow + m - 1, firstCol)).Font.Bold = True
    ws.Range(ws.Cells(startRow, firstCol + 1), ws.Cells(startRow + m - 1, firstCol + 1)).HorizontalAlignment = xlLeft
    For j = 1 To m
        If fmts(j) = "grd" Then ws.Cells(startRow + j - 1, firstCol + 1).Interior.Color = GradeColor(CStr(out(j, 2)))
    Next j
    FitColumns ws, firstCol, 2, startRow, m - 1
    WriteRecord = startRow + m + 1
End Function

' O nota pe un rand (text cursiv, gri): avertismente, cota live, 18+.
Private Sub WriteNote(ByVal ws As Worksheet, ByVal r As Long, ByVal text As String)
    With ws.Cells(r, 1)
        .NumberFormat = "@"
        .Value = text
        .Font.Italic = True
        .Font.Color = RGB(110, 110, 110)
    End With
End Sub

Private Sub StyleHeader(ByVal target As Range)
    target.Interior.Color = RGB(22, 33, 29)
    target.Font.Color = RGB(232, 255, 156)
    target.Font.Bold = True
    target.HorizontalAlignment = xlCenter
    target.VerticalAlignment = xlCenter
End Sub

Private Sub DecorateColumn(ByVal target As Range, ByVal fmt As String)
    Select Case fmt
        Case "pct"
            AddScale target, False
        Case "hot"
            AddScale target, True
        Case "grd"
            AddEqualsFill target, "A", GradeColor("A")
            AddEqualsFill target, "B", GradeColor("B")
            AddEqualsFill target, "C", GradeColor("C")
            AddEqualsFill target, "D", GradeColor("D")
            target.HorizontalAlignment = xlCenter
        Case "wdl"
            AddEqualsFill target, "W", RGB(198, 239, 206)
            AddEqualsFill target, "D", RGB(242, 242, 242)
            AddEqualsFill target, "L", RGB(255, 199, 206)
            target.HorizontalAlignment = xlCenter
        Case "win"
            AddEqualsFill target, "DA", RGB(198, 239, 206)
            AddEqualsFill target, "NU", RGB(255, 199, 206)
            target.HorizontalAlignment = xlCenter
        Case "sts"
            AddEqualsFill target, StatusText("won"), RGB(198, 239, 206)
            AddEqualsFill target, StatusText("cashed"), RGB(198, 239, 206)
            AddEqualsFill target, StatusText("lost"), RGB(255, 199, 206)
            AddEqualsFill target, StatusText("void"), RGB(242, 242, 242)
            AddEqualsFill target, StatusText("unavailable"), RGB(242, 242, 242)
            target.HorizontalAlignment = xlCenter
        Case "yn"
            target.HorizontalAlignment = xlCenter
    End Select
End Sub

Private Function GradeColor(ByVal grade As String) As Long
    Select Case grade
        Case "A"
            GradeColor = RGB(198, 239, 206)
        Case "B"
            GradeColor = RGB(226, 240, 217)
        Case "C"
            GradeColor = RGB(255, 242, 204)
        Case "D"
            GradeColor = RGB(248, 203, 173)
        Case Else
            GradeColor = RGB(255, 255, 255)
    End Select
End Function

Private Sub AddScale(ByVal target As Range, ByVal relative As Boolean)
    Dim rule As Object
    Set rule = target.FormatConditions.AddColorScale(ColorScaleType:=2)
    If relative Then
        rule.ColorScaleCriteria(1).Type = xlConditionValueLowestValue
        rule.ColorScaleCriteria(2).Type = xlConditionValueHighestValue
    Else
        rule.ColorScaleCriteria(1).Type = xlConditionValueNumber
        rule.ColorScaleCriteria(1).Value = 0
        rule.ColorScaleCriteria(2).Type = xlConditionValueNumber
        rule.ColorScaleCriteria(2).Value = 1
    End If
    rule.ColorScaleCriteria(1).FormatColor.Color = RGB(255, 255, 255)
    rule.ColorScaleCriteria(2).FormatColor.Color = RGB(99, 190, 123)
End Sub

Private Sub AddEqualsFill(ByVal target As Range, ByVal expected As String, ByVal fillColor As Long)
    Dim rule As Object
    Set rule = target.FormatConditions.Add(Type:=xlCellValue, Operator:=xlEqual, Formula1:="=""" & expected & """")
    rule.Interior.Color = fillColor
End Sub

' Randurile cu o valoare in coloana "mrk" (selectie, echipa analizata) sunt evidentiate.
Private Sub HighlightMarked(ByVal ws As Worksheet, ByVal headRow As Long, ByVal firstCol As Long, _
                            ByVal n As Long, ByVal m As Long, ByVal col As Long, ByRef out() As Variant)
    Dim i As Long
    For i = 1 To n
        If Len(CStr(out(i + 1, col))) > 0 Then
            With ws.Range(ws.Cells(headRow + i, firstCol), ws.Cells(headRow + i, firstCol + m - 1))
                .Interior.Color = RGB(255, 250, 205)
                .Font.Bold = True
            End With
        End If
    Next i
End Sub

Private Sub FitColumns(ByVal ws As Worksheet, ByVal firstCol As Long, ByVal m As Long, _
                       ByVal headRow As Long, ByVal n As Long)
    Dim j As Long
    Dim oldWidth As Double
    Dim lastRow As Long
    lastRow = headRow + n
    If n < 1 Then lastRow = headRow + 1
    For j = firstCol To firstCol + m - 1
        oldWidth = ws.Columns(j).ColumnWidth
        ws.Range(ws.Cells(headRow, j), ws.Cells(lastRow, j)).Columns.AutoFit
        If ws.Columns(j).ColumnWidth < oldWidth Then ws.Columns(j).ColumnWidth = oldWidth
        If ws.Columns(j).ColumnWidth > MAX_COLUMN_WIDTH Then ws.Columns(j).ColumnWidth = MAX_COLUMN_WIDTH
    Next j
End Sub

Private Sub FreezeBelow(ByVal ws As Worksheet, ByVal rowsAbove As Long, ByVal colsLeft As Long)
    On Error Resume Next
    ws.Activate
    With ActiveWindow
        .FreezePanes = False
        .SplitColumn = 0
        .SplitRow = 0
        .ScrollRow = 1
        .ScrollColumn = 1
        .SplitRow = rowsAbove
        .SplitColumn = colsLeft
        .FreezePanes = True
    End With
End Sub

' Graficul bancii din jurnalul simularii (optional: fara grafic, jurnalul ramane).
Private Sub AddEquityChart(ByVal ws As Worksheet, ByVal firstRow As Long, ByVal n As Long, ByVal valueCol As Long, _
                           ByVal dateCol As Long, ByVal posX As Double, ByVal posY As Double, ByVal title As String)
    Dim chartShape As Object
    On Error Resume Next
    Set chartShape = ws.Shapes.AddChart(xlLine, posX, posY, 520, 260)
    If chartShape Is Nothing Then Exit Sub
    With chartShape.Chart
        .SetSourceData ws.Range(ws.Cells(firstRow, valueCol), ws.Cells(firstRow + n - 1, valueCol))
        .SeriesCollection(1).XValues = ws.Range(ws.Cells(firstRow, dateCol), ws.Cells(firstRow + n - 1, dateCol))
        .SeriesCollection(1).Name = title
        .HasLegend = False
        .HasTitle = True
        .ChartTitle.Text = title
    End With
End Sub

Private Sub DeleteCharts(ByVal ws As Worksheet)
    Dim i As Long
    On Error Resume Next
    For i = ws.ChartObjects.Count To 1 Step -1
        ws.ChartObjects(i).Delete
    Next i
End Sub

' Sterge rezultatele de la firstRow in jos; setarile de deasupra raman neatinse.
Private Sub ClearFrom(ByVal ws As Worksheet, ByVal firstRow As Long)
    Dim target As Range
    If ws.AutoFilterMode Then ws.AutoFilterMode = False
    DeleteCharts ws
    Set target = ws.Range(ws.Rows(firstRow), ws.Rows(ws.Rows.Count))
    target.FormatConditions.Delete
    target.Clear
End Sub

'==============================================================================
' Foi, panou si butoane
'==============================================================================

Private Function SheetNames() As Variant
    SheetNames = Array(SH_PANEL, SH_PRED, SH_MATCH, SH_FORM, SH_SCORE, SH_VALUE, SH_RECORD, _
                       SH_RECO, SH_LIVE, SH_SIM, SH_WALLET, SH_HELP, SH_LISTS)
End Function

Private Function SheetExists(ByVal sheetName As String) As Boolean
    Dim ws As Worksheet
    On Error Resume Next
    Set ws = ThisWorkbook.Worksheets(sheetName)
    On Error GoTo 0
    SheetExists = Not ws Is Nothing
End Function

Private Function EnsureSheet(ByVal sheetName As String) As Worksheet
    Dim ws As Worksheet
    On Error Resume Next
    Set ws = ThisWorkbook.Worksheets(sheetName)
    On Error GoTo 0
    If ws Is Nothing Then
        Set ws = ThisWorkbook.Worksheets.Add(After:=ThisWorkbook.Sheets(ThisWorkbook.Sheets.Count))
        ws.Name = sheetName
    End If
    Set EnsureSheet = ws
End Function

Private Function PrepareSheet(ByVal sheetName As String) As Worksheet
    Dim ws As Worksheet
    Set ws = EnsureSheet(sheetName)
    If ws.AutoFilterMode Then ws.AutoFilterMode = False
    ws.Cells.FormatConditions.Delete
    ws.Cells.Clear
    ws.Columns.ColumnWidth = ws.StandardWidth
    Set PrepareSheet = ws
End Function

Private Sub EnsureReady()
    Dim list As Variant
    Dim i As Long
    list = SheetNames()
    For i = LBound(list) To UBound(list)
        If Not SheetExists(CStr(list(i))) Then
            BuildWorkbook
            Exit Sub
        End If
    Next i
End Sub

Private Sub BuildWorkbook()
    Dim list As Variant
    Dim i As Long
    list = SheetNames()
    For i = LBound(list) To UBound(list)
        EnsureSheet CStr(list(i))
    Next i
    RemoveEmptyForeignSheets
    BuildLists
    BuildPanel
    BuildPredictionsSheet
    BuildRecoSheet
    BuildLiveSheet
    BuildSimSheet
    BuildWalletSheet
    BuildHelp
    ThisWorkbook.Worksheets(SH_LISTS).Visible = xlSheetHidden
End Sub

Private Function IsOwnSheet(ByVal sheetName As String) As Boolean
    Dim list As Variant
    Dim i As Long
    list = SheetNames()
    For i = LBound(list) To UBound(list)
        If CStr(list(i)) = sheetName Then
            IsOwnSheet = True
            Exit Function
        End If
    Next i
End Function

' Un registru nou are o foaie goala (Foaie1 / Sheet1): o eliminam ca Panou sa fie prima.
Private Sub RemoveEmptyForeignSheets()
    Dim ws As Worksheet
    Dim i As Long
    Application.DisplayAlerts = False
    For i = ThisWorkbook.Worksheets.Count To 1 Step -1
        Set ws = ThisWorkbook.Worksheets(i)
        If Not IsOwnSheet(ws.Name) Then
            If Application.WorksheetFunction.CountA(ws.Cells) = 0 And ws.Shapes.Count = 0 Then
                If ThisWorkbook.Worksheets.Count > 1 Then ws.Delete
            End If
        End If
    Next i
    Application.DisplayAlerts = True
    If ThisWorkbook.Sheets(1).Name <> SH_PANEL Then
        ThisWorkbook.Worksheets(SH_PANEL).Move Before:=ThisWorkbook.Sheets(1)
    End If
End Sub

Private Sub BuildLists()
    Dim ws As Worksheet
    Set ws = ThisWorkbook.Worksheets(SH_LISTS)
    ws.Range("A1:B1").NumberFormat = "@"
    If Len(SafeText(ws.Range("A1").Value)) = 0 Then ws.Range("A1").Value = Ro("(toate)")
    ws.Range("D1:J7").NumberFormat = "@"
    ws.Range("D1").Value = "A"
    ws.Range("D2").Value = "B"
    ws.Range("D3").Value = "C"
    ws.Range("D4").Value = "D"
    ws.Range("E1").Value = "DA"
    ws.Range("E2").Value = "NU"
    ws.Range("F1").Value = "Fotbal"
    ws.Range("F2").Value = "Baschet"
    ws.Range("F3").Value = "Tenis"
    ws.Range("H1").Value = "Toate"
    ws.Range("H2").Value = "Fotbal"
    ws.Range("H3").Value = "Baschet"
    ws.Range("H4").Value = "Tenis"
    ws.Range("I1").Value = "scara"
    ws.Range("I2").Value = "bilet"
    ws.Range("I3").Value = "simple"
    ws.Range("J1").Value = "recent"
    ws.Range("J2").Value = "football"
    ws.Range("J3").Value = "football-plus"
    ws.Range("J4").Value = "tennis"
    ws.Range("J5").Value = "local-football"
    ws.Range("J6").Value = "local-basketball"
    ws.Range("J7").Value = "local-tennis"
End Sub

Private Sub BuildPanel()
    Dim ws As Worksheet
    Dim posX As Double
    Dim posY As Double
    Set ws = ThisWorkbook.Worksheets(SH_PANEL)
    ws.Columns(1).ColumnWidth = 30
    ws.Columns(2).ColumnWidth = 34
    ws.Columns(3).ColumnWidth = 58
    ws.Columns(4).ColumnWidth = 3
    ws.Range("A1").Value = "FootyPreds"
    ws.Range("A1").Font.Size = 22
    ws.Range("A1").Font.Bold = True
    ws.Range("A2").Value = Ro("Predic{t}ii pentru fotbal, baschet {s}i tenis din API-ul local FootyPreds. Porne{s}te {i^}nt{a^}i serverul cu start.ps1.")
    ws.Range("A2").Font.Italic = True

    PanelRow ws, 3, "Sport", "Fotbal", Ro("Fotbal, Baschet sau Tenis: predic{t}ii, valoare, analiz{a} {s}i live.")
    PanelRow ws, 4, Ro("Adres{a} API"), DEFAULT_URL, Ro("Serverul local pornit cu start.ps1. Nu expune API-ul pe internet.")
    PanelRow ws, 5, Ro("Data meciurilor (UTC)"), Empty, Ro("Gol = azi {i^}n UTC (data serverului, ca aplica{t}ia web). Sau o dat{a} fix{a}, ex. 2026-09-25.")
    ' Registrele vechi aveau =TODAY() (data locala): trec pe data UTC a serverului.
    If UsesServerDay() Then ws.Range(CELL_DATE).ClearContents
    ws.Range(CELL_DATE).NumberFormat = "yyyy-mm-dd"
    ws.Range(CELL_DATE).HorizontalAlignment = xlLeft
    PanelRow ws, 6, Ro("Competi{t}ie"), Ro("(toate)"), Ro("List{a} completat{a} la {i^}nc{a}rcarea predic{t}iilor.")
    PanelRow ws, 7, Ro("Calitate minim{a}"), "D", Ro("A = cele mai multe date recente, D = pu{t}ine. D arat{a} tot.")
    PanelRow ws, 8, "Doar meciuri viitoare", "NU", Ro("DA ascunde meciurile {i^}ncepute sau terminate.")
    PanelRow ws, 9, Ro("Limit{a} meciuri"), 200, Ro("Maxim 400 de meciuri pe zi (competi{t}iile populare primele).")
    PanelRow ws, 10, Ro("Analiz{a} complet{a}: top N"), 10, Ro("C{a^}te meciuri C/D analizeaz{a} butonul Analiz{a} complet{a} top N.")
    PanelRow ws, 11, Ro("Prag selec{t}ie (afi{s}are)"), 0.85, Ro("Coloana Selec{t}ie din foi (0.5 - 0.99). Registrul (track record) folose{s}te mereu 85%.")
    PanelRow ws, 12, "Mod demo", "NU", Ro("DA = meciuri sintetice de fotbal, f{a}r{a} cheie RapidAPI; analiza lor nu cere FlashScore {s}i nu intr{a} {i^}n registru.")
    PanelRow ws, 13, Ro("ID meci (op{t}ional)"), Empty, Ro("Butonul din Panou analizeaz{a} acest ID (sportul din B3); gol = r{a^}ndul selectat {i^}n foaia Predictii.")
    ws.Range(CELL_THRESHOLD).NumberFormat = "0.00"
    ws.Range(CELL_MATCH).NumberFormat = "@"
    ws.Range("A15").Value = "Stare"
    ws.Range("A15").Font.Bold = True
    ws.Range(CELL_STATUS).NumberFormat = "@"

    SetListValidation ws.Range(CELL_SPORT), "=" & SH_LISTS & "!$F$1:$F$3", True
    SetListValidation ws.Range(CELL_GRADE), "=" & SH_LISTS & "!$D$1:$D$4", True
    SetListValidation ws.Range(CELL_UPCOMING), "=" & SH_LISTS & "!$E$1:$E$2", True
    SetListValidation ws.Range(CELL_DEMO), "=" & SH_LISTS & "!$E$1:$E$2", True
    SetListValidation ws.Range(CELL_COMP), "=" & SH_LISTS & "!$A$1:$A$1", False

    DeleteButtons ws
    posX = ws.Range("E3").Left
    posY = ws.Range("E3").Top
    AddButton ws, "VerificaServer", Ro("Verific{a} serverul"), posX, posY, 220
    AddButton ws, "IncarcaPredictii", Ro("{I^}ncarc{a} predic{t}iile"), posX, posY + 34, 220
    AddButton ws, "AnalizaMeci", Ro("Analizeaz{a} meciul (B13 / Predictii)"), posX, posY + 68, 220
    AddButton ws, "AnalizaCompletaTop", Ro("Analiz{a} complet{a}: top N (C/D)"), posX, posY + 102, 220
    AddButton ws, "IncarcaValoare", Ro("Valoare (EV pozitiv)"), posX, posY + 136, 220
    AddButton ws, "IncarcaTrackRecord", "Track record", posX, posY + 170, 220
    AddButton ws, "IncarcaRecomandari", Ro("Recomand{a}ri AI (bilete)"), posX, posY + 204, 220
    AddButton ws, "ActualizeazaLive", Ro("Live (sportul din B3)"), posX, posY + 238, 220
    AddButton ws, "IncarcaPortofel", "Portofel virtual", posX, posY + 272, 220
    AddButton ws, "Setup", Ro("Reconstruie{s}te foile"), posX, posY + 306, 220
End Sub

Private Sub PanelRow(ByVal ws As Worksheet, ByVal r As Long, ByVal label As String, _
                     ByVal defaultValue As Variant, ByVal hint As String)
    ws.Cells(r, 1).Value = label
    ws.Cells(r, 1).Font.Bold = True
    If Not IsEmpty(defaultValue) Then
        If Len(SafeText(ws.Cells(r, 2).Formula)) = 0 Then ws.Cells(r, 2).Value = defaultValue
    End If
    ws.Cells(r, 2).Interior.Color = RGB(255, 255, 230)
    ws.Cells(r, 2).HorizontalAlignment = xlLeft
    ws.Cells(r, 3).Value = hint
    ws.Cells(r, 3).Font.Italic = True
    ws.Cells(r, 3).Font.Color = RGB(110, 110, 110)
End Sub

Private Sub BuildPredictionsSheet()
    Dim ws As Worksheet
    Set ws = ThisWorkbook.Worksheets(SH_PRED)
    DeleteButtons ws
    ws.Rows(1).RowHeight = 30
    If Len(SafeText(ws.Range("A1").Value)) = 0 Then
        ws.Range("A1").Value = Ro("Predic{t}ii: ap{a}s{a} {I^}ncarc{a} predic{t}iile")
        ws.Range("A1").Font.Size = 14
    End If
    AddButton ws, "IncarcaPredictii", Ro("Re{i^}ncarc{a}"), 330, 3, 110
    AddButton ws, "AnalizaMeci", Ro("Analizeaz{a} r{a^}ndul selectat"), 446, 3, 190
    AddButton ws, "AnalizaCompletaTop", Ro("Analiz{a} complet{a} top N"), 642, 3, 170
    InstallDoubleClick ws
End Sub

' Titlu pe randul 1 si latimi pentru o foaie cu setari (Recomandari, Live, Simulare, Portofel).
Private Sub SheetTitle(ByVal ws As Worksheet, ByVal title As String, ByVal subtitle As String)
    ws.Columns(1).ColumnWidth = 28
    ws.Columns(2).ColumnWidth = 18
    ws.Range("A1").Value = title
    ws.Range("A1").Font.Size = 16
    ws.Range("A1").Font.Bold = True
    ws.Range("C1").Value = subtitle
    ws.Range("C1").Font.Italic = True
    ws.Range("C1").Font.Color = RGB(110, 110, 110)
End Sub

Private Sub BuildRecoSheet()
    Dim ws As Worksheet
    Set ws = ThisWorkbook.Worksheets(SH_RECO)
    SheetTitle ws, Ro("Recomand{a}ri AI"), Ro("Bilete x2, x5, x10, x100 {s}i selec{t}ii simple. Ziua vine din Panou (B5). 18+.")
    ' Text, ca "2,5,10,100" sa nu devina numar (virgula e separator zecimal in Romania).
    ws.Range(RECO_TARGETS).NumberFormat = "@"
    PanelRow ws, 3, "Sporturi", "Toate", Ro("Toate sau un singur sport (Fotbal, Baschet, Tenis).")
    PanelRow ws, 4, Ro("Cote {t}int{a}"), "2,5,10,100", Ro("Separate prin virgul{a}, fiecare {i^}ntre 1.2 {s}i 1000.")
    PanelRow ws, 5, Ro("Regenereaz{a}"), "NU", Ro("DA = recalculeaz{a} biletele ne{i^}ncepute; biletele cu meciuri {i^}ncepute r{a^}m{a^}n neschimbate.")
    SetListValidation ws.Range(RECO_SPORTS), "=" & SH_LISTS & "!$H$1:$H$4", True
    SetListValidation ws.Range(RECO_REFRESH), "=" & SH_LISTS & "!$E$1:$E$2", True
    DeleteButtons ws
    AddButton ws, "IncarcaRecomandari", Ro("{I^}ncarc{a} recomand{a}rile"), ws.Range("A6").Left, ws.Range("A6").Top + 2, 200
    ws.Rows(6).RowHeight = 30
End Sub

Private Sub BuildLiveSheet()
    Dim ws As Worksheet
    Set ws = ThisWorkbook.Worksheets(SH_LIVE)
    SheetTitle ws, "Live", Ro("Sportul din Panou (B3). Probabilit{a}{t}i pe rezultatul final; cotele afi{s}ate sunt corecte, nu pre{t}uri live. 18+.")
    DeleteButtons ws
    AddButton ws, "ActualizeazaLive", Ro("Actualizeaz{a} live"), ws.Range("A2").Left, ws.Range("A2").Top + 2, 200
    ws.Rows(2).RowHeight = 30
End Sub

Private Sub BuildSimSheet()
    Dim ws As Worksheet
    Set ws = ThisWorkbook.Worksheets(SH_SIM)
    SheetTitle ws, "Simulare", Ro("Bani virtuali, meciuri din trecut, predic{t}ii f{a}cute orb (doar cu rezultatele de dinainte de fiecare zi). 18+.")
    ws.Range(SIM_START & ":" & SIM_END).NumberFormat = "@"
    PanelRow ws, 3, "Suma (lei)", 5, Ro("Banca de pornire; la scar{a} este miza primei zile.")
    PanelRow ws, 4, "Set de date", "recent", Ro("recent = ultimele zile; football, football-plus, tennis = arhive; local-* = meciurile salvate.")
    PanelRow ws, 5, "Sporturi (recent)", "Fotbal", Ro("Doar pentru setul recent: Toate sau un singur sport (Toate = de 3 ori mai multe cereri).")
    PanelRow ws, 6, "Zile recente", 14, Ro("1 - 60. Zilele lips{a} se descarc{a} automat (o cerere FlashScore pe zi {s}i sport).")
    PanelRow ws, 7, Ro("Cot{a} {t}int{a}"), 2, Ro("Cota biletului zilnic (scar{a} {s}i bilet), 1.2 - 100.")
    PanelRow ws, 8, "Strategie", "scara", Ro("scara = tot c{a^}{s}tigul se joac{a} a doua zi; bilet = un bilet pe zi; simple = selec{t}ii simple (miz{a} 10% din sum{a}).")
    PanelRow ws, 9, "Reinvestire (scara)", 1, Ro("Partea din banca sc{a}rii jucat{a} zilnic: 1 = tot, 0.5 = jum{a}tate.")
    PanelRow ws, 10, Ro("Reia dup{a} pierdere"), "DA", Ro("DA = a doua zi porne{s}te o scar{a} nou{a} cu suma ini{t}ial{a} (banii investi{t}i se adun{a}).")
    PanelRow ws, 11, Ro("De la (op{t}ional)"), Empty, Ro("AAAA-LL-ZZ; gol = ultimul an al setului. Nu se folose{s}te la recent.")
    PanelRow ws, 12, Ro("P{a^}n{a} la (op{t}ional)"), Empty, Ro("AAAA-LL-ZZ; gol = ultima zi a setului.")
    PanelRow ws, 13, Ro("{I^}ncaseaz{a} dup{a} N zile (scara)"), Empty, Ro("Op{t}ional: dup{a} N bilete reu{s}ite scara se {i^}ncaseaz{a} {s}i porne{s}te alta cu suma ini{t}ial{a}.")
    ws.Range(SIM_AMOUNT).NumberFormat = "0.00"
    ws.Range(SIM_ODDS).NumberFormat = "0.00"
    ws.Range(SIM_REINVEST).NumberFormat = "0.00"
    SetListValidation ws.Range(SIM_DATASET), "=" & SH_LISTS & "!$J$1:$J$7", False
    SetListValidation ws.Range(SIM_SPORTS), "=" & SH_LISTS & "!$H$1:$H$4", True
    SetListValidation ws.Range(SIM_STRATEGY), "=" & SH_LISTS & "!$I$1:$I$3", True
    SetListValidation ws.Range(SIM_RESTART), "=" & SH_LISTS & "!$E$1:$E$2", True
    DeleteButtons ws
    ws.Range(SIM_MAXDAYS).NumberFormat = "0"
    AddButton ws, "RuleazaSimularea", Ro("Ruleaz{a} simularea"), ws.Range("A14").Left, ws.Range("A14").Top + 2, 180
    AddButton ws, "IncarcaSeturiDate", "Seturi de date", ws.Range("A14").Left + 190, ws.Range("A14").Top + 2, 140
    ws.Rows(14).RowHeight = 30
End Sub

Private Sub BuildWalletSheet()
    Dim ws As Worksheet
    Set ws = ThisWorkbook.Worksheets(SH_WALLET)
    SheetTitle ws, "Portofel virtual", Ro("Bani fictivi: pariurile se plaseaz{a} din aplica{t}ia web, aici doar se afi{s}eaz{a}. 18+.")
    DeleteButtons ws
    AddButton ws, "IncarcaPortofel", Ro("Actualizeaz{a} portofelul"), ws.Range("A2").Left, ws.Range("A2").Top + 2, 200
    ws.Rows(2).RowHeight = 30
End Sub

Private Sub BuildHelp()
    Dim ws As Worksheet
    Dim s As String
    Dim lines() As String
    Dim out() As Variant
    Dim i As Long
    Set ws = PrepareSheet(SH_HELP)
    ' Randurile sunt separate prin "~".
    s = "FootyPreds pentru Excel - ajutor~~"
    s = s & "1. Porne{s}te serverul: dublu-click pe start.ps1 (sau PowerShell: .\start.ps1) {s}i las{a} fereastra deschis{a}.~"
    s = s & "2. {I^}n Panou verific{a} adresa API (implicit http://127.0.0.1:8000) {s}i ap{a}s{a} Verific{a} serverul.~"
    s = s & "3. Alege sportul (B3: Fotbal, Baschet, Tenis) {s}i data, apoi {I^}ncarc{a} predic{t}iile. Fiecare meci prime{s}te o not{a} de calitate A-D.~"
    s = s & "4. Selecteaz{a} un r{a^}nd din Predictii {s}i ap{a}s{a} Analizeaz{a} meciul selectat (sau dublu-click pe r{a^}nd).~"
    s = s & "   Analiza complet{a} aduce din FlashScore forma din toate competi{t}iile, meciurile directe, clasamentul {s}i cotele.~"
    s = s & "   Rezultatul apare {i^}n foile Meci, Forma {s}i ScorCorect (scorul corect doar la fotbal).~"
    s = s & "5. Analiz{a} complet{a} top N: urm{a}toarele N meciuri viitoare C/D neanalizate {i^}nc{a} (Esc opre{s}te).~"
    s = s & "6. Valoare: pie{t}ele cu probabilitate x cot{a} > 1 (EV pozitiv). Track record: selec{t}iile salvate {i^}nainte de meci.~"
    s = s & "7. Recomandari: biletele AI la cota {t}int{a} (x2, x5, x10, x100) {s}i cele mai sigure selec{t}ii simple, din toate sporturile.~"
    s = s & "8. Live: meciurile {i^}n desf{a}{s}urare ale sportului din B3, cu probabilit{a}{t}i pe rezultatul final {s}i sugestii.~"
    s = s & "9. Simulare: scara (tot c{a^}{s}tigul se joac{a} a doua zi) sau un bilet / simple pe zi, pe ultimele zile sau pe arhive.~"
    s = s & "   Biletul fiec{a}rei zile se alege orb, doar cu rezultatele de dinainte; apoi se afl{a} rezultatul.~"
    s = s & "10. Portofel: pariurile virtuale plasate din aplica{t}ia web, cu soldul {s}i istoricul.~~"
    s = s & "Calitate A/B/C/D: c{a^}t de multe {s}i de recente sunt datele pentru ambele echipe. Doar A-C intr{a} {i^}n registru.~"
    s = s & "Prag selec{t}ie (Panou B11) schimb{a} doar coloana Selec{t}ie din foi; registrul folose{s}te mereu pragul de 85%.~"
    s = s & "Data (Panou B5) goal{a} = ziua de azi {i^}n UTC, ca aplica{t}ia web {s}i Power Query.~"
    s = s & "{I^}ncredere 0-100: istoric recent pe echip{a} (toate competi{t}iile) {s}i cote disponibile.~"
    s = s & "Cot{a} corect{a} = 1 / probabilitate. EV = probabilitate x cot{a} - 1. La live, cota minim{a} nu este un pre{t} de la cas{a}.~"
    s = s & "Form{a}: W = victorie, D = egal, L = {i^}nfr{a^}ngere; cel mai recent meci primul.~"
    s = s & "Coloanele Sigl{a} / Steag (URL) sunt adrese de imagini prin serverul local; Excel le arat{a} ca text.~~"
    s = s & "F{a}r{a} macro-uri: vezi PowerQuery.md (Date > Din web, cu adresele CSV /api/excel/...).~"
    s = s & "Probabilit{a}{t}ile sunt estim{a}ri statistice, nu garan{t}ii. 18+. Joac{a} responsabil."
    lines = Split(s, "~")
    ReDim out(1 To UBound(lines) + 1, 1 To 1)
    For i = 0 To UBound(lines)
        out(i + 1, 1) = Ro(lines(i))
    Next i
    ws.Columns(1).ColumnWidth = 120
    ws.Range(ws.Cells(1, 1), ws.Cells(UBound(lines) + 1, 1)).NumberFormat = "@"
    ws.Range(ws.Cells(1, 1), ws.Cells(UBound(lines) + 1, 1)).Value = out
    ws.Range("A1").Font.Size = 16
    ws.Range("A1").Font.Bold = True
End Sub

Private Sub SetListValidation(ByVal target As Range, ByVal source As String, ByVal strict As Boolean)
    On Error Resume Next
    target.Validation.Delete
    target.Validation.Add Type:=xlValidateList, AlertStyle:=xlValidAlertStop, Operator:=xlBetween, Formula1:=source
    target.Validation.IgnoreBlank = True
    target.Validation.InCellDropdown = True
    target.Validation.ShowError = strict
End Sub

Private Sub DeleteButtons(ByVal ws As Worksheet)
    Dim i As Long
    On Error Resume Next
    For i = ws.Buttons.Count To 1 Step -1
        If Left$(ws.Buttons(i).Name, 3) = "fp_" Then ws.Buttons(i).Delete
    Next i
End Sub

Private Sub AddButton(ByVal ws As Worksheet, ByVal macroName As String, ByVal buttonText As String, _
                      ByVal posX As Double, ByVal posY As Double, ByVal buttonWidth As Double)
    Dim btn As Object
    Set btn = ws.Buttons.Add(posX, posY, buttonWidth, 26)
    btn.OnAction = macroName
    btn.Caption = buttonText
    btn.Name = "fp_" & macroName
    btn.Placement = xlFreeFloating
    btn.Font.Bold = True
End Sub

' Dublu-click pe un rand din Predictii porneste analiza. Necesita "Trust access to the
' VBA project object model"; fara el, butoanele functioneaza la fel (fara mesaj de eroare).
Private Sub InstallDoubleClick(ByVal ws As Worksheet)
    Dim project As Object
    Dim sheetCode As Object
    Dim code As String
    On Error GoTo Skip
    Set project = ThisWorkbook.VBProject
    ' ws.CodeName poate fi "" pentru o foaie adaugata chiar acum: cautam modulul dupa nume.
    Set sheetCode = SheetCodeModule(project, ws)
    If sheetCode Is Nothing Then Exit Sub
    If sheetCode.CountOfLines > 0 Then
        If InStr(1, sheetCode.Lines(1, sheetCode.CountOfLines), "Worksheet_BeforeDoubleClick", vbTextCompare) > 0 Then Exit Sub
    End If
    code = "Private Sub Worksheet_BeforeDoubleClick(ByVal Target As Range, Cancel As Boolean)" & vbCrLf
    code = code & "    If Target.Row > " & PRED_HEADER & " Then" & vbCrLf
    code = code & "        Cancel = True" & vbCrLf
    code = code & "        Application.Run `'` & Me.Parent.Name & `'!AnalizaMeciDinRand`, Target.Row" & vbCrLf
    code = code & "    End If" & vbCrLf
    code = code & "End Sub" & vbCrLf
    sheetCode.AddFromString Replace(code, "`", Chr$(34))
Skip:
End Sub

' Modulul de cod al unei foi (componenta de tip document, 100) gasit dupa numele foii.
Private Function SheetCodeModule(ByVal project As Object, ByVal ws As Worksheet) As Object
    Dim component As Object
    On Error GoTo Done
    For Each component In project.VBComponents
        If component.Type = 100 Then
            If ComponentSheetName(component) = ws.Name Then
                Set SheetCodeModule = component.CodeModule
                Exit Function
            End If
        End If
    Next component
Done:
End Function

Private Function ComponentSheetName(ByVal component As Object) As String
    On Error Resume Next
    ComponentSheetName = CStr(component.Properties("Name").Value)
End Function

'==============================================================================
' Citirea panoului si a foilor cu setari
'==============================================================================

Private Function SheetValue(ByVal sheetName As String, ByVal address As String) As Variant
    SheetValue = ThisWorkbook.Worksheets(sheetName).Range(address).Value
End Function

Private Function PanelValue(ByVal address As String) As Variant
    PanelValue = SheetValue(SH_PANEL, address)
End Function

Private Function SheetText(ByVal sheetName As String, ByVal address As String) As String
    SheetText = Trim$(SafeText(SheetValue(sheetName, address)))
End Function

Private Function PanelDay() As String
    If UsesServerDay() Then
        PanelDay = ServerDay()
        Exit Function
    End If
    PanelDay = DayText(PanelValue(CELL_DATE), "Panou (B5)")
End Function

' O celula cu o data optionala: "" daca e goala, altfel AAAA-LL-ZZ.
Private Function SheetDay(ByVal sheetName As String, ByVal address As String) As String
    Dim v As Variant
    v = SheetValue(sheetName, address)
    If Len(Trim$(SafeText(v))) = 0 Then Exit Function
    SheetDay = DayText(v, sheetName & " (" & address & ")")
End Function

' Data dintr-o celula (data Excel, AAAA-LL-ZZ sau ZZ.LL.AAAA) -> AAAA-LL-ZZ.
Private Function DayText(ByVal v As Variant, ByVal where As String) As String
    Dim s As String
    If VarType(v) = vbDate Then
        DayText = Format$(v, "yyyy\-mm\-dd")
        Exit Function
    End If
    If VarType(v) = vbDouble Then
        If v > 20000 And v < 80000 Then
            DayText = Format$(CDate(v), "yyyy\-mm\-dd")
            Exit Function
        End If
    End If
    s = Trim$(SafeText(v))
    If s Like "####-##-##" Then
        DayText = s
    ElseIf s Like "##.##.####" Then
        DayText = Mid$(s, 7, 4) & "-" & Mid$(s, 4, 2) & "-" & Left$(s, 2)
    Else
        Err.Raise ERR_INPUT, APP_TITLE, Ro("Data din ") & where & Ro(" nu este valid{a}. Folose{s}te formatul AAAA-LL-ZZ, de ex. 2026-09-25.")
    End If
End Function

' B5 gol (sau vechiul =TODAY(), data locala) inseamna ziua de azi a serverului, in UTC.
Private Function UsesServerDay() As Boolean
    Dim formulaText As String
    formulaText = UCase$(Replace(Trim$(SafeText(ThisWorkbook.Worksheets(SH_PANEL).Range(CELL_DATE).Formula)), " ", ""))
    UsesServerDay = (Len(formulaText) = 0 Or formulaText = "=TODAY()")
End Function

' Ziua UTC a serverului, ca in aplicatia web si in Power Query (nu ceasul local al PC-ului).
Private Function ServerDay() As String
    Dim stamp As String
    stamp = Left$(FieldOf(ApiTable("GET", "/api/excel/health", ""), "server_time_utc"), 10)
    If Not (stamp Like "####-##-##") Then
        Err.Raise ERR_API, APP_TITLE, Ro("Serverul nu a trimis data curent{a} (server_time_utc).")
    End If
    ServerDay = stamp
End Function

Private Function PanelMatchId() As String
    PanelMatchId = Trim$(SafeText(PanelValue(CELL_MATCH)))
End Function

Private Function PanelGrade() As String
    Dim s As String
    s = UCase$(Trim$(SafeText(PanelValue(CELL_GRADE))))
    If Len(s) = 1 And InStr(1, "ABCD", s) > 0 Then
        PanelGrade = s
    Else
        PanelGrade = "D"
    End If
End Function

Private Function SheetNumber(ByVal sheetName As String, ByVal address As String, ByVal defaultValue As Double) As Double
    Dim v As Variant
    Dim s As String
    v = SheetValue(sheetName, address)
    Select Case VarType(v)
        Case vbDouble, vbInteger, vbLong, vbSingle, vbCurrency, vbDecimal
            SheetNumber = CDbl(v)
        Case Else
            s = Replace(Trim$(SafeText(v)), ",", ".")
            If Len(s) = 0 Then
                SheetNumber = defaultValue
            Else
                SheetNumber = Val(s)
            End If
    End Select
End Function

Private Function SheetLong(ByVal sheetName As String, ByVal address As String, ByVal defaultValue As Long, _
                           ByVal minimum As Long, ByVal maximum As Long) As Long
    Dim amount As Double
    amount = SheetNumber(sheetName, address, defaultValue)
    If amount < minimum Then amount = minimum
    If amount > maximum Then amount = maximum
    SheetLong = CLng(amount)
End Function

Private Function PanelLong(ByVal address As String, ByVal defaultValue As Long, ByVal minimum As Long, _
                           ByVal maximum As Long) As Long
    PanelLong = SheetLong(SH_PANEL, address, defaultValue, minimum, maximum)
End Function

Private Function PanelThreshold() As Double
    Dim amount As Double
    amount = SheetNumber(SH_PANEL, CELL_THRESHOLD, 0.85)
    If amount > 1 Then amount = amount / 100
    If amount <= 0 Then amount = 0.85
    If amount < 0.5 Then amount = 0.5
    If amount > 0.99 Then amount = 0.99
    PanelThreshold = amount
End Function

Private Function SheetYes(ByVal sheetName As String, ByVal address As String) As Boolean
    Dim v As Variant
    Dim s As String
    v = SheetValue(sheetName, address)
    If VarType(v) = vbBoolean Then
        SheetYes = v
        Exit Function
    End If
    s = UCase$(Trim$(SafeText(v)))
    SheetYes = (s = "DA" Or s = "YES" Or s = "1" Or s = "TRUE" Or s = "ADEVARAT")
End Function

Private Function PanelYes(ByVal address As String) As Boolean
    PanelYes = SheetYes(SH_PANEL, address)
End Function

' Eticheta aleasa din lista -> ID-ul competitiei (foaia ascunsa Liste, coloana B).
Private Function PanelCompetition() As String
    Dim ws As Worksheet
    Dim label As String
    Dim lastRow As Long
    Dim r As Long
    label = Trim$(SafeText(PanelValue(CELL_COMP)))
    If Len(label) = 0 Or label = Ro("(toate)") Then Exit Function
    Set ws = ThisWorkbook.Worksheets(SH_LISTS)
    ' Lista din Liste!A:B este a altui sport (B3 s-a schimbat): filtrul vechi nu se trimite.
    If SafeText(ws.Range(LIST_COMP_SPORT).Value) <> PanelSport() Then
        ResetCompetition
        Exit Function
    End If
    lastRow = ws.Cells(ws.Rows.Count, 1).End(xlUp).Row
    For r = 2 To lastRow
        If SafeText(ws.Cells(r, 1).Value) = label Then
            PanelCompetition = SafeText(ws.Cells(r, 2).Value)
            Exit Function
        End If
    Next r
    ' Un ID scris de mana (ex. england|premier league) se trimite ca atare; o eticheta
    ' necunoscuta ar goli tabelul, deci filtrul revine la (toate).
    If InStr(label, "|") > 0 Or InStr(label, ":") > 0 Then
        PanelCompetition = label
    Else
        ResetCompetition
    End If
End Function

Private Sub ResetCompetition()
    On Error Resume Next
    ThisWorkbook.Worksheets(SH_PANEL).Range(CELL_COMP).Value = Ro("(toate)")
    SetStatus Ro("Competi{t}ia din Panou B6 nu este {i^}n lista sportului ales; filtrul a revenit la (toate).")
End Sub

'==============================================================================
' Utilitare
'==============================================================================

' Numar -> text cu punct zecimal, indiferent de setarile regionale (Str foloseste mereu ".").
Private Function NumToStr(ByVal amount As Double) As String
    Dim s As String
    s = Trim$(Str$(amount))
    If Left$(s, 1) = "." Then s = "0" & s
    If Left$(s, 2) = "-." Then s = "-0" & Mid$(s, 2)
    NumToStr = s
End Function

Private Function SafeText(ByVal v As Variant) As String
    If IsError(v) Or IsNull(v) Or IsEmpty(v) Then Exit Function
    SafeText = CStr(v)
End Function

' Marcaje ASCII -> diacritice romanesti (virgula dedesubt, forma corecta).
Private Function Ro(ByVal text As String) As String
    text = Replace(text, "{a^}", ChrW(226))
    text = Replace(text, "{A^}", ChrW(194))
    text = Replace(text, "{i^}", ChrW(238))
    text = Replace(text, "{I^}", ChrW(206))
    text = Replace(text, "{a}", ChrW(259))
    text = Replace(text, "{A}", ChrW(258))
    text = Replace(text, "{s}", ChrW(537))
    text = Replace(text, "{S}", ChrW(536))
    text = Replace(text, "{t}", ChrW(539))
    text = Replace(text, "{T}", ChrW(538))
    Ro = text
End Function

' MsgBox foloseste codepage-ul ANSI: fara diacritice, ca sa nu apara semne de intrebare.
Private Function Plain(ByVal text As String) As String
    text = Replace(text, ChrW(226), "a")
    text = Replace(text, ChrW(194), "A")
    text = Replace(text, ChrW(238), "i")
    text = Replace(text, ChrW(206), "I")
    text = Replace(text, ChrW(259), "a")
    text = Replace(text, ChrW(258), "A")
    text = Replace(text, ChrW(537), "s")
    text = Replace(text, ChrW(536), "S")
    text = Replace(text, ChrW(539), "t")
    text = Replace(text, ChrW(538), "T")
    text = Replace(text, ChrW(351), "s")
    text = Replace(text, ChrW(350), "S")
    text = Replace(text, ChrW(355), "t")
    text = Replace(text, ChrW(354), "T")
    Plain = text
End Function

' True (cu un mesaj in Panou) cand alt macro ruleaza inca: al doilea clic nu porneste nimic.
Private Function IsBusy() As Boolean
    If Not m_busy Then Exit Function
    IsBusy = True
    Application.StatusBar = APP_TITLE & ": " & Ro("o alt{a} opera{t}ie ruleaz{a} {i^}nc{a}; a{s}teapt{a} s{a} se termine.")
End Function

Private Sub BeginWork(ByVal message As String)
    m_busy = True
    Application.ScreenUpdating = False
    Application.Cursor = xlWait
    Application.EnableCancelKey = xlErrorHandler
    Application.StatusBar = APP_TITLE & ": " & message
    SetStatus message
End Sub

Private Sub EndWork(Optional ByVal message As String = "")
    m_busy = False
    Application.ScreenUpdating = True
    Application.Cursor = xlDefault
    Application.StatusBar = False
    Application.EnableCancelKey = xlInterrupt
    If Len(message) > 0 Then SetStatus message
End Sub

Private Sub SetStatus(ByVal message As String)
    On Error Resume Next
    ThisWorkbook.Worksheets(SH_PANEL).Range(CELL_STATUS).Value = Format$(Now, "hh:nn:ss") & "  " & Replace(message, vbLf, " ")
End Sub

Private Sub ShowError(ByVal message As String)
    SetStatus message
    ' Excel ascuns (build_xlsm.py): un MsgBox nu poate fi inchis de nimeni si ar bloca scriptul.
    If Not Application.Visible Then Exit Sub
    MsgBox Plain(message), vbExclamation, APP_TITLE
End Sub

Private Function ErrorText(ByVal errNumber As Long, ByVal errText As String) As String
    Select Case errNumber
        Case 18
            ErrorText = Ro("Opera{t}ie oprit{a} (Esc). Rezultatele primite p{a^}n{a} acum au r{a}mas {i^}n foi.")
        Case ERR_SERVER, ERR_API, ERR_INPUT
            ErrorText = errText
        Case Else
            ErrorText = Ro("Eroare nea{s}teptat{a} (") & errNumber & "): " & errText
    End Select
End Function
