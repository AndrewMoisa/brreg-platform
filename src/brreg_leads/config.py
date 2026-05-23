from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = PROJECT_ROOT / "data"
DB_PATH = DATA_DIR / "leads.db"

BRREG_API_BASE = "https://data.brreg.no/enhetsregisteret/api"
USER_AGENT = "brreg-leads/0.1 (private lead-gen tool; contact opendata@brreg.no)"
REQUEST_TIMEOUT_SECONDS = 30.0
THROTTLE_SECONDS = 0.5

KOMMUNER: list[str] = [
    "0301",  # Oslo
    "3201",  # Bærum
    "3203",  # Asker
    "3205",  # Lillestrøm
    "3207",  # Nordre Follo
    "3209",  # Ullensaker
    "3220",  # Nesodden
    "3216",  # Nittedal
    "3214",  # Lørenskog
    "3212",  # Rælingen
]

NAERINGSKODE_WHITELIST_PREFIXES: list[str] = [
    "43.",   # construction / trades
    "45.",   # vehicle trade/repair
    "46.",   # wholesale
    "47.",   # retail
    "55.",   # accommodation
    "56.",   # food service
    "70.2",  # management consulting
    "73.",   # advertising / market research
    "74.",   # other professional/scientific
    "93.",   # sports / amusement / recreation
    "96.",   # other personal services (hair, beauty, wellness)
]

COHORT_WEIGHTS: dict[str, int] = {
    "reachable": 5,
    "no_website": 3,
    "target_industry": 2,
    "enk_conversion": 4,
    "recently_moved": 1,
}

LEAD_STATUSES: list[str] = ["new", "contacted", "interested", "won", "lost", "ignored"]

ENK_CONVERSION_AS_LOOKBACK_DAYS = 30
ENK_CONVERSION_ENK_LOOKBACK_DAYS = 60
RECENTLY_MOVED_LOOKBACK_DAYS = 30
NEW_BUSINESS_LOOKBACK_DAYS = 90
