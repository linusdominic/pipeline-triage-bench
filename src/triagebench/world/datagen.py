"""Deterministic generation of Harbor Lending's source data.

Everything here is a pure function of the seed. The generator walks the timeline one
business day at a time, keeps the loan book's state in memory, and emits the files each
source would deliver to the landing zone, with the time each file arrives.
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from triagebench.config import BACKBOOK_DATE, BRANCH_OPENINGS, WORLD_END, WORLD_START
from triagebench.world import calendar as cal

REGIONS = ["NORTH", "SOUTH", "EAST", "WEST", "CENTRAL"]
TOWNS = [
    "Alder Point",
    "Bayview",
    "Cedar Falls",
    "Driftwood",
    "Elm Harbor",
    "Fairhaven",
    "Glenmoor",
    "Hollis Bay",
    "Ironbridge",
    "Juniper Cove",
    "Kestrel Ridge",
    "Larkspur",
    "Millbrook",
    "Northgate",
    "Oakhurst",
    "Pinecrest",
    "Quarry Hill",
    "Redwater",
    "Saltmarsh",
    "Tidewater",
    "Upper Ferry",
    "Valewood",
    "Westport",
    "Yarrow Mills",
    "Ashcombe",
    "Brightwater",
    "Coldspring",
    "Dunmore",
    "Eastleigh",
    "Foxhollow",
    "Greystone",
    "Havenford",
    "Inglewood",
    "Lowmoor",
    "Marlow Sound",
    "Newcastle Reach",
]

PRODUCTS: dict[str, dict[str, float]] = {
    # share of originations, principal range, annual rate range (percent), term in months
    "MORT30": {"share": 0.20, "pmin": 150_000, "pmax": 650_000, "rmin": 6.0, "rmax": 7.5, "term": 360},
    "MORT15": {"share": 0.08, "pmin": 100_000, "pmax": 450_000, "rmin": 5.4, "rmax": 6.6, "term": 180},
    "HELOC": {"share": 0.38, "pmin": 20_000, "pmax": 150_000, "rmin": 7.5, "rmax": 9.5, "term": 120},
    "AUTO60": {"share": 0.22, "pmin": 8_000, "pmax": 55_000, "rmin": 5.9, "rmax": 11.9, "term": 60},
    "PERS36": {"share": 0.12, "pmin": 2_000, "pmax": 25_000, "rmin": 9.9, "rmax": 19.9, "term": 36},
}

LOANS_HEADER = [
    "loan_id",
    "borrower_id",
    "branch_code",
    "product_code",
    "origination_date",
    "principal",
    "annual_rate",
    "term_months",
]
PAYMENTS_HEADER = [
    "payment_id",
    "loan_id",
    "payment_date",
    "amount",
    "principal_amount",
    "interest_amount",
    "cdc_op",
    "cdc_seq",
]
BUREAU_HEADER = ["borrower_id", "score", "score_date"]
BRANCHES_HEADER = ["branch_code", "branch_name", "region", "opened_date"]

DAILY_ORIGINATIONS = 20
BACKBOOK_LOANS = 4000


def rng(seed: int, *keys: object) -> random.Random:
    h = hashlib.sha256("|".join([str(seed), *map(str, keys)]).encode()).digest()
    return random.Random(int.from_bytes(h[:8], "big"))


def cents(x: float) -> int:
    return round(x * 100)


def dollars(c: int) -> str:
    sign = "-" if c < 0 else ""
    c = abs(c)
    return f"{sign}{c // 100}.{c % 100:02d}"


@dataclass
class Branch:
    code: str
    name: str
    region: str
    opened: date
    weight: float


@dataclass
class Loan:
    loan_id: str
    borrower_id: str
    branch_code: str
    product: str
    origination: date
    principal_c: int
    rate: float
    term: int
    balance_c: int
    monthly_c: int
    last_activity: date
    delinquent_from: date | None


@dataclass
class LandingFile:
    source: str
    ds: date
    name: str
    header: list[str]
    rows: list[list[str]]
    trailer: tuple[int, str] | None  # (record_count, control_total) as written by the sender
    arrived_at: datetime

    def render(self) -> str:
        lines = [",".join(self.header)] + [",".join(r) for r in self.rows]
        if self.trailer is not None:
            lines.append(f"TRL|{self.trailer[0]}|{self.trailer[1]}")
        return "\n".join(lines) + "\n"


@dataclass
class Timeline:
    seed: int
    files: dict[tuple[str, date], LandingFile] = field(default_factory=dict)
    branches: list[Branch] = field(default_factory=list)

    def get(self, source: str, ds: date) -> LandingFile | None:
        return self.files.get((source, ds))


def _monthly_payment(principal_c: int, rate: float, term: int) -> int:
    r = rate / 100 / 12
    p = principal_c / 100
    pmt = p * r / (1 - (1 + r) ** (-term))
    return cents(pmt)


def _arrival(seed: int, source: str, ds: date, base: time, jitter_min: int) -> datetime:
    j = rng(seed, "arrival", source, ds.isoformat()).randint(0, jitter_min)
    return datetime.combine(ds + timedelta(days=1), base) + timedelta(minutes=j)


def make_branches(seed: int) -> list[Branch]:
    r = rng(seed, "branches")
    out: list[Branch] = []
    for i in range(1, 31):
        opened = date(r.randint(2004, 2024), r.randint(1, 12), r.randint(1, 28))
        weight = r.choice([0.6, 0.8, 1.0, 1.0, 1.2, 1.5])
        out.append(Branch(f"BR-{i:03d}", f"Harbor Lending {TOWNS[i - 1]}", REGIONS[(i - 1) % 5], opened, weight))
    for j, (code, opened) in enumerate(sorted(BRANCH_OPENINGS.items())):
        out.append(Branch(code, f"Harbor Lending {TOWNS[30 + j]}", REGIONS[j % 5], opened, 4.0))
    return out


def _pick_product(r: random.Random) -> str:
    x = r.random()
    acc = 0.0
    for code, p in PRODUCTS.items():
        acc += p["share"]
        if x < acc:
            return code
    return "PERS36"


def _new_loan(seed: int, idx: int, branch: Branch, product: str, orig: date, r: random.Random) -> Loan:
    p = PRODUCTS[product]
    principal_c = cents(round(r.uniform(p["pmin"], p["pmax"]) / 100) * 100)
    rate = round(r.uniform(p["rmin"], p["rmax"]), 3)
    term = int(p["term"])
    delinquent_from: date | None = None
    if r.random() < 0.06:
        delinquent_from = orig + timedelta(days=r.randint(20, 120))
    return Loan(
        loan_id=f"L{idx:07d}",
        borrower_id=f"B{idx:07d}",
        branch_code=branch.code,
        product=product,
        origination=orig,
        principal_c=principal_c,
        rate=rate,
        term=term,
        balance_c=principal_c,
        monthly_c=_monthly_payment(principal_c, rate, term),
        last_activity=orig,
        delinquent_from=delinquent_from,
    )


def _loan_row(loan: Loan) -> list[str]:
    return [
        loan.loan_id,
        loan.borrower_id,
        loan.branch_code,
        loan.product,
        loan.origination.isoformat(),
        dollars(loan.principal_c),
        f"{loan.rate:.3f}",
        str(loan.term),
    ]


def trailer_for(rows: list[list[str]], amount_col: int) -> tuple[int, str]:
    total = sum(cents(float(r[amount_col])) for r in rows)
    return (len(rows), dollars(total))


def generate(seed: int) -> Timeline:
    tl = Timeline(seed=seed)
    tl.branches = make_branches(seed)
    loans: list[Loan] = []
    next_idx = 1

    # Back book: existing loans migrated into the platform on BACKBOOK_DATE.
    r = rng(seed, "backbook")
    legacy = [b for b in tl.branches if b.opened < date(2026, 1, 1)]
    for _ in range(BACKBOOK_LOANS):
        branch = r.choices(legacy, weights=[b.weight for b in legacy])[0]
        product = _pick_product(r)
        orig = date(2023, 8, 1) + timedelta(days=r.randint(0, 1088))
        loan = _new_loan(seed, next_idx, branch, product, orig, r)
        next_idx += 1
        paid_fraction = min(0.9, (BACKBOOK_DATE - orig).days / (loan.term * 30.4) * 0.8)
        loan.balance_c = int(loan.principal_c * (1 - paid_fraction))
        loan.last_activity = BACKBOOK_DATE - timedelta(days=r.randint(1, 30))
        if r.random() < 0.03:
            loan.last_activity = BACKBOOK_DATE - timedelta(days=r.randint(40, 90))
            loan.delinquent_from = loan.last_activity
        loans.append(loan)
    backbook_rows = [_loan_row(x) for x in loans]
    tl.files[("loans", BACKBOOK_DATE)] = LandingFile(
        "loans",
        BACKBOOK_DATE,
        f"loans_{BACKBOOK_DATE:%Y%m%d}.csv",
        list(LOANS_HEADER),
        backbook_rows,
        trailer_for(backbook_rows, 5),
        datetime.combine(BACKBOOK_DATE + timedelta(days=1), time(1, 0)),
    )

    # Business-day index, so the payment process has no weekly rhythm.
    bidx: dict[date, int] = {}
    count = 0
    for d in cal.date_range(date(2023, 1, 1), WORLD_END):
        if cal.is_business_day(d):
            count += 1
        bidx[d] = count

    pay_idx = 1
    for ds in cal.business_days(WORLD_START, WORLD_END):
        # --- payments for ds (only loans that existed before today) ---
        rp = rng(seed, "payments", ds.isoformat())
        events: list[list[str]] = []
        seq = 1
        corrections: list[list[str]] = []
        for loan in loans:
            if loan.balance_c <= 0 or loan.origination >= ds:
                continue
            days = bidx[ds] - bidx[loan.last_activity]
            delinquent = loan.delinquent_from is not None and ds >= loan.delinquent_from
            if delinquent:
                hazard = 0.03 if days >= 21 else 0.0
            elif days < 16:
                hazard = 0.0
            else:
                hazard = 0.3
            if rp.random() >= hazard:
                continue
            interest_c = round(loan.balance_c * loan.rate / 100 / 12)
            amount_c = min(loan.monthly_c, loan.balance_c + interest_c)
            principal_c = amount_c - interest_c
            loan.balance_c -= principal_c
            loan.last_activity = ds
            pid = f"P{pay_idx:09d}"
            pay_idx += 1
            row = [
                pid,
                loan.loan_id,
                ds.isoformat(),
                dollars(amount_c),
                dollars(principal_c),
                dollars(interest_c),
                "I",
                str(seq),
            ]
            seq += 1
            events.append(row)
            if rp.random() < 0.03:
                # servicer correction later in the day: a late-fee reversal moves interest
                adj = rp.randint(500, 2500)
                corrections.append(
                    [
                        pid,
                        loan.loan_id,
                        ds.isoformat(),
                        dollars(amount_c - adj),
                        dollars(principal_c),
                        dollars(interest_c - adj),
                        "U",
                        "",
                    ]
                )
        for c in corrections:
            c[7] = str(seq)
            seq += 1
            events.append(c)
        tl.files[("payments", ds)] = LandingFile(
            "payments",
            ds,
            f"keel_payments_{ds:%Y%m%d}.csv",
            list(PAYMENTS_HEADER),
            events,
            trailer_for(events, 3),
            _arrival(seed, "payments", ds, time(3, 10), 50),
        )

        # --- originations on ds ---
        ro = rng(seed, "originations", ds.isoformat())
        n = round(DAILY_ORIGINATIONS * (1 + ro.uniform(-0.08, 0.08)))
        open_branches = [b for b in tl.branches if b.opened <= ds]
        new_rows: list[list[str]] = []
        for _ in range(n):
            branch = ro.choices(open_branches, weights=[b.weight for b in open_branches])[0]
            loan = _new_loan(seed, next_idx, branch, _pick_product(ro), ds, ro)
            next_idx += 1
            loans.append(loan)
            new_rows.append(_loan_row(loan))
        tl.files[("loans", ds)] = LandingFile(
            "loans",
            ds,
            f"loans_{ds:%Y%m%d}.csv",
            list(LOANS_HEADER),
            new_rows,
            trailer_for(new_rows, 5),
            _arrival(seed, "loans", ds, time(1, 30), 20),
        )

        # --- weekly bureau refresh ---
        if cal.is_bureau_day(ds):
            rows = []
            for loan in loans:
                if loan.origination > ds:
                    continue
                base = rng(seed, "score", loan.borrower_id).randint(590, 815)
                week = (ds - WORLD_START).days // 7
                drift = rng(seed, "drift", loan.borrower_id, week).randint(-6, 6)
                if loan.delinquent_from is not None and ds >= loan.delinquent_from:
                    drift -= 40
                score = max(300, min(850, base + drift))
                rows.append([loan.borrower_id, str(score), ds.isoformat()])
            tl.files[("bureau", ds)] = LandingFile(
                "bureau",
                ds,
                f"meridian_refresh_{ds:%Y%m%d}.csv",
                list(BUREAU_HEADER),
                rows,
                None,
                _arrival(seed, "bureau", ds, time(2, 0), 30),
            )

        # --- weekly branch reference snapshot (first business day of the week) ---
        if cal.is_first_business_day_of_week(ds):
            horizon = ds + timedelta(days=6)
            rows = [[b.code, b.name, b.region, b.opened.isoformat()] for b in tl.branches if b.opened <= horizon]
            tl.files[("branches", ds)] = LandingFile(
                "branches",
                ds,
                f"branches_{ds:%Y%m%d}.csv",
                list(BRANCHES_HEADER),
                rows,
                None,
                datetime.combine(ds, time(5, 0))
                + timedelta(minutes=rng(seed, "arrival", "branches", ds).randint(0, 25)),
            )
    return tl
