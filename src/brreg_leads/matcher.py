import sqlite3
import unicodedata
from datetime import date, timedelta

from .config import ENK_CONVERSION_AS_LOOKBACK_DAYS, ENK_CONVERSION_ENK_LOOKBACK_DAYS

AS_OWNER_ROLE_TYPES = ("DAGL", "LEDE", "INNH", "STYR")
ENK_OWNER_ROLE_TYPES = ("INNH",)


NORWEGIAN_FOLD = str.maketrans({
    "ø": "o", "Ø": "O",
    "æ": "ae", "Æ": "Ae",
    "å": "a", "Å": "A",
})


def normalize_name(name: str | None) -> str:
    if not name:
        return ""
    s = name.translate(NORWEGIAN_FOLD)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().strip()
    # Brreg returns "Etternavn, Fornavn" — fold to "fornavn etternavn"
    if "," in s:
        last, _, first = s.partition(",")
        s = f"{first.strip()} {last.strip()}"
    return " ".join(s.split())


def find_enk_conversions(
    conn: sqlite3.Connection,
    today: date | None = None,
) -> dict[str, dict]:
    """Returns {as_orgnr: {"enk_orgnr": ..., "person_navn": ..., "match_strength": "name+kommune"}}"""
    today = today or date.today()
    as_since = (today - timedelta(days=ENK_CONVERSION_AS_LOOKBACK_DAYS)).isoformat()
    enk_since = (today - timedelta(days=ENK_CONVERSION_ENK_LOOKBACK_DAYS)).isoformat()

    as_role_placeholders = ",".join(["?"] * len(AS_OWNER_ROLE_TYPES))
    enk_role_placeholders = ",".join(["?"] * len(ENK_OWNER_ROLE_TYPES))

    as_rows = conn.execute(
        f"""
        SELECT e.orgnr, e.kommunenummer, r.person_navn
        FROM enheter e
        JOIN roller r ON r.orgnr = e.orgnr
        WHERE e.organisasjonsform = 'AS'
          AND e.registreringsdato >= ?
          AND e.slettedato IS NULL
          AND r.rolle_type IN ({as_role_placeholders})
        """,
        (as_since, *AS_OWNER_ROLE_TYPES),
    ).fetchall()

    enk_rows = conn.execute(
        f"""
        SELECT e.orgnr, e.kommunenummer, r.person_navn
        FROM enheter e
        JOIN roller r ON r.orgnr = e.orgnr
        WHERE e.organisasjonsform = 'ENK'
          AND e.slettedato IS NOT NULL
          AND e.slettedato >= ?
          AND r.rolle_type IN ({enk_role_placeholders})
        """,
        (enk_since, *ENK_OWNER_ROLE_TYPES),
    ).fetchall()

    enk_index: dict[tuple[str, str], str] = {}  # (norm_name, kommune) -> enk_orgnr
    enk_index_name_only: dict[str, str] = {}
    for row in enk_rows:
        norm = normalize_name(row["person_navn"])
        if not norm:
            continue
        if row["kommunenummer"]:
            enk_index[(norm, row["kommunenummer"])] = row["orgnr"]
        enk_index_name_only.setdefault(norm, row["orgnr"])

    matches: dict[str, dict] = {}
    for row in as_rows:
        if row["orgnr"] in matches:
            continue
        norm = normalize_name(row["person_navn"])
        if not norm:
            continue
        kommune = row["kommunenummer"]
        if kommune and (norm, kommune) in enk_index:
            matches[row["orgnr"]] = {
                "enk_orgnr": enk_index[(norm, kommune)],
                "person_navn": row["person_navn"],
                "match_strength": "name+kommune",
            }
        elif norm in enk_index_name_only:
            matches[row["orgnr"]] = {
                "enk_orgnr": enk_index_name_only[norm],
                "person_navn": row["person_navn"],
                "match_strength": "name_only",
            }
    return matches
