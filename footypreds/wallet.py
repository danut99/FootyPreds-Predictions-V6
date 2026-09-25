"""Local virtual wallet (paper trading) on upcoming picks: fictitious money only.

Tables (created lazily, docs/CONTRACTS.md §8): ``wallet_ledger`` keeps every money movement
(deposit, bet, payout, reset) with the balance after it; ``wallet_bets`` keeps each bet with the
legs and the prices locked when it was placed. Open bets settle from stored results
(``sports.legs.settle_leg``) every time the wallet is read.
"""

import json
import math
import threading
import uuid
from datetime import date, datetime, timezone
from typing import Annotated

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from footypreds.media import with_leg_media
from footypreds.sports.legs import candidate_legs, settle_leg, settled_odds, ticket_status

CURRENCY = "RON"
MAX_DEPOSIT = 1_000_000
MAX_LEGS = 20
THRESHOLD = 0.85
NOTICE = (
    "Portofel virtual pentru simulare: bani fictivi, nu se pariază bani reali. "
    "Estimări statistice, nu garanții. 18+."
)
_LOCK = threading.Lock()

router = APIRouter(prefix="/api/wallet", tags=["wallet"])


def ro_amount(value):
    """1234.5 -> "1.234,50" (the UI's ro-RO money format, also in server messages)."""
    text = f"{abs(value):,.2f}".replace(",", " ").replace(".", ",").replace(" ", ".")
    return f"-{text}" if value < 0 else text


def utcnow():
    """Current time; tests replace it to freeze the clock."""
    return datetime.now(timezone.utc)


def money(value):
    return round(value + 1e-9, 2)


def ensure_tables(store):
    with store.connect() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS wallet_ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, type TEXT NOT NULL,
                amount REAL NOT NULL, balance REAL NOT NULL, bet_id TEXT
            );
            CREATE TABLE IF NOT EXISTS wallet_bets (
                id TEXT PRIMARY KEY, created TEXT NOT NULL, label TEXT NOT NULL,
                stake REAL NOT NULL, total_odds REAL NOT NULL, legs TEXT NOT NULL,
                status TEXT NOT NULL, payout REAL, settled TEXT, source TEXT NOT NULL
            );
        """)


def _balance(db):
    row = db.execute("SELECT balance FROM wallet_ledger ORDER BY id DESC LIMIT 1").fetchone()
    return row[0] if row else 0.0


def _record(db, now, kind, amount, balance, bet_id=None):
    db.execute(
        "INSERT INTO wallet_ledger (at, type, amount, balance, bet_id) VALUES (?, ?, ?, ?, ?)",
        (now.isoformat(), kind, money(amount), money(balance), bet_id),
    )


def deposit(store, amount, now=None):
    now = now or utcnow()
    ensure_tables(store)
    with _LOCK, store.connect() as db:
        _record(db, now, "deposit", amount, _balance(db) + amount)


def reset(store, now=None):
    now = now or utcnow()
    ensure_tables(store)
    with _LOCK, store.connect() as db:
        db.execute("DELETE FROM wallet_bets")
        db.execute("DELETE FROM wallet_ledger")
        _record(db, now, "reset", 0.0, 0.0)


def place_bet(store, stake, legs, label, source="custom", now=None):
    """Store a bet on `legs` (prices already locked); ValueError when the balance is too low."""
    now = now or utcnow()
    ensure_tables(store)
    total = math.prod(item["odds"] for item in legs)
    bet_id = uuid.uuid4().hex[:12]
    with _LOCK, store.connect() as db:
        balance = _balance(db)
        if stake > balance + 1e-9:
            raise ValueError(
                f"Sold insuficient în portofelul virtual: ai {ro_amount(balance)} {CURRENCY}, "
                f"miza este {ro_amount(stake)} {CURRENCY}."
            )
        db.execute(
            "INSERT INTO wallet_bets VALUES (?, ?, ?, ?, ?, ?, 'pending', NULL, NULL, ?)",
            (bet_id, now.isoformat(), label, money(stake), total, json.dumps(legs), source),
        )
        _record(db, now, "bet", -stake, balance - stake, bet_id)
    return bet_id


def settle_open(store, now=None):
    """Settle every open bet whose results are stored; returns how many were closed."""
    now = now or utcnow()
    ensure_tables(store)
    closed = 0
    with _LOCK, store.connect() as db:
        rows = db.execute(
            "SELECT id, stake, legs FROM wallet_bets WHERE status='pending' ORDER BY created"
        ).fetchall()
        for row in rows:
            legs = [settle_leg(item, store.match(item["match_id"])) for item in json.loads(row[2])]
            status = ticket_status([item["status"] for item in legs])
            if status == "pending":
                db.execute("UPDATE wallet_bets SET legs=? WHERE id=?", (json.dumps(legs), row[0]))
                continue
            stake = row[1]
            payout = 0.0
            if status == "won":
                payout = money(stake * settled_odds(legs))
            elif status in ("void", "unavailable"):
                status, payout = "void", stake
            db.execute(
                "UPDATE wallet_bets SET legs=?, status=?, payout=?, settled=? WHERE id=?",
                (json.dumps(legs), status, payout, now.isoformat(), row[0]),
            )
            if payout > 0:
                _record(db, now, "payout", payout, _balance(db) + payout, row[0])
            closed += 1
    return closed


def wallet(store, now=None):
    """The wallet after settling open bets (docs/CONTRACTS.md §10.2)."""
    settle_open(store, now)
    with store.connect() as db:
        ledger = [
            {
                "at": r["at"],
                "type": r["type"],
                "amount": r["amount"],
                "balance": r["balance"],
                "bet_id": r["bet_id"],
            }
            for r in db.execute("SELECT * FROM wallet_ledger ORDER BY id DESC LIMIT 500")
        ]
        deposited = db.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM wallet_ledger WHERE type='deposit'"
        ).fetchone()[0]
        balance = _balance(db)
        bets = [
            {
                "id": r["id"],
                "created": r["created"],
                "label": r["label"],
                "stake": r["stake"],
                "total_odds": r["total_odds"],
                "legs": json.loads(r["legs"]),
                "status": r["status"],
                "payout": r["payout"],
                "settled": r["settled"],
                "source": r["source"],
            }
            for r in db.execute("SELECT * FROM wallet_bets ORDER BY created DESC, id")
        ]
    # Display logos (media.py) on legs stored before logos existed; newer legs carry them.
    bets = with_leg_media(bets, store)
    closed = [b for b in bets if b["status"] != "pending"]
    return {
        "currency": CURRENCY,
        "balance": money(balance),
        "deposited": money(deposited),
        "staked_open": money(sum(b["stake"] for b in bets if b["status"] == "pending")),
        "profit": money(sum((b["payout"] or 0) - b["stake"] for b in closed)),
        "open": sum(b["status"] == "pending" for b in bets),
        "won": sum(b["status"] == "won" for b in bets),
        "lost": sum(b["status"] == "lost" for b in bets),
        "void": sum(b["status"] == "void" for b in bets),
        "bets": bets,
        "history": ledger,
        "notice": NOTICE,
        "disclaimer": NOTICE,
    }


# --- API ------------------------------------------------------------------------------------


class DepositRequest(BaseModel):
    amount: float


class LegRequest(BaseModel):
    match_id: Annotated[str, Field(min_length=1, max_length=120)]
    key: Annotated[str, Field(min_length=1, max_length=40)]


class BetRequest(BaseModel):
    stake: float
    legs: list[LegRequest] | None = None
    label: Annotated[str, Field(max_length=120)] | None = None
    # AI ticket of the day: {"day", "target"} (+ optional sports) instead of legs.
    day: date | None = None
    target: float | None = None
    sports: list[str] | None = None


def _state(request):
    store = request.app.state.store
    ensure_tables(store)
    return store


async def _current_leg(request, match_id, key, now):
    """The leg as it can be bet right now, with the price locked from Match.odds."""
    store = request.app.state.store
    match = store.match(match_id)
    if match is None:
        raise HTTPException(404, f"Meciul {match_id} nu există în baza locală.")
    analysis = await run_in_threadpool(request.app.state.cache.get, match, THRESHOLD)
    for item in candidate_legs(match, analysis, now):
        if item["key"] == key:
            return item
    raise HTTPException(
        400,
        f"Selecția {key} pentru {match.home} – {match.away} nu mai poate fi pariată "
        "(meci început, fără cotă reală sau date insuficiente).",
    )


async def _ai_ticket(request, day, target, sports):
    """The day's AI recommendation ticket for `target` (recommend.py), or HTTP 400."""
    from footypreds import recommend

    sports = sports or ["football", "basketball", "tennis"]
    data = await recommend.recommendations(request.app.state, day, sports, [target])
    item = data["tickets"][0] if data.get("tickets") else None
    if not item or item.get("status") == "unavailable" or not item.get("legs"):
        reason = (item or {}).get("reason") or "Nu există un bilet AI pentru această cotă."
        raise HTTPException(400, reason)
    return item


@router.get("")
def get_wallet(request: Request):
    return wallet(_state(request), utcnow())


@router.post("/deposit")
def post_deposit(request: Request, body: DepositRequest):
    if not (math.isfinite(body.amount) and 0 < body.amount <= MAX_DEPOSIT):
        raise HTTPException(422, "Suma depusă trebuie să fie între 0 și 1.000.000 (bani virtuali).")
    store = _state(request)
    deposit(store, money(body.amount), utcnow())
    return wallet(store, utcnow())


@router.post("/reset")
def post_reset(request: Request):
    store = _state(request)
    reset(store, utcnow())
    return wallet(store, utcnow())


@router.post("/bet")
async def post_bet(request: Request, body: BetRequest):
    store = _state(request)
    if not (math.isfinite(body.stake) and 0 < body.stake <= MAX_DEPOSIT):
        raise HTTPException(422, "Miza trebuie să fie un număr pozitiv (bani virtuali).")
    if money(body.stake) <= 0:
        raise HTTPException(422, "Miza minimă este 0.01.")
    now = utcnow()
    source, label = "custom", body.label
    if body.legs:
        specs = [(item.match_id, item.key) for item in body.legs]
    elif body.day is not None and body.target is not None:
        if not 1.2 <= body.target <= 1000:
            raise HTTPException(422, "Cota țintă trebuie să fie între 1.2 și 1000.")
        known = {"football", "basketball", "tennis"}
        if body.sports and not set(body.sports) <= known:
            raise HTTPException(422, "Sport necunoscut.")
        item = await _ai_ticket(request, body.day, body.target, body.sports)
        specs = [(leg["match_id"], leg["key"]) for leg in item["legs"]]
        source = "ai"
        label = label or f"Bilet AI cota {body.target:g} ({body.day.isoformat()})"
    else:
        raise HTTPException(422, "Alege selecțiile (legs) sau ziua și cota biletului AI.")
    if not 1 <= len(specs) <= MAX_LEGS:
        raise HTTPException(422, f"Un bilet are între 1 și {MAX_LEGS} selecții.")
    if len({match_id for match_id, _ in specs}) != len(specs):
        raise HTTPException(400, "Cel mult o selecție pe meci într-un bilet.")
    legs = [await _current_leg(request, match_id, key, now) for match_id, key in specs]
    if label is None:
        label = legs[0]["label"] if len(legs) == 1 else f"Bilet {len(legs)} selecții"
    try:
        place_bet(store, money(body.stake), legs, label, source, now)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    return wallet(store, utcnow())
