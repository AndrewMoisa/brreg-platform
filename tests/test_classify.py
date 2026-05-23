from datetime import date

from brreg_leads.classify import EnhetSnapshot, classify


def _snap(**kw):
    base = dict(
        orgnr="999999999",
        organisasjonsform="AS",
        hjemmeside=None,
        naeringskode1_kode=None,
        konkurs=False,
        under_avvikling=False,
        slettedato=None,
        last_oppdatering_dato=None,
    )
    base.update(kw)
    return EnhetSnapshot(**base)


def test_no_website_when_blank():
    cohorts, score = classify(_snap(hjemmeside=""), enk_conversion=False)
    assert "no_website" in cohorts
    assert score >= 3


def test_website_excludes_no_website():
    cohorts, _ = classify(_snap(hjemmeside="https://example.no"), enk_conversion=False)
    assert "no_website" not in cohorts


def test_target_industry_prefix_match():
    cohorts, _ = classify(_snap(naeringskode1_kode="47.110"), enk_conversion=False)
    assert "target_industry" in cohorts


def test_target_industry_no_match():
    cohorts, _ = classify(_snap(naeringskode1_kode="64.200"), enk_conversion=False)
    assert "target_industry" not in cohorts


def test_recently_moved_within_window():
    cohorts, _ = classify(
        _snap(naeringskode1_kode="47.110", last_oppdatering_dato="2026-05-01"),
        enk_conversion=False,
        today=date(2026, 5, 19),
    )
    assert "recently_moved" in cohorts


def test_recently_moved_outside_window():
    cohorts, _ = classify(
        _snap(naeringskode1_kode="47.110", last_oppdatering_dato="2026-01-01"),
        enk_conversion=False,
        today=date(2026, 5, 19),
    )
    assert "recently_moved" not in cohorts


def test_enk_conversion_flag():
    cohorts, score = classify(_snap(naeringskode1_kode="47.110"), enk_conversion=True)
    assert "enk_conversion" in cohorts
    assert score >= 6  # target_industry(2) + enk_conversion(4)


def test_konkurs_excluded():
    cohorts, score = classify(_snap(konkurs=True, hjemmeside=""), enk_conversion=False)
    assert cohorts == []
    assert score == 0


def test_enk_without_recent_regdato_excluded():
    cohorts, _ = classify(
        _snap(organisasjonsform="ENK", hjemmeside=""), enk_conversion=False
    )
    assert cohorts == []


def test_reachable_when_email_present():
    cohorts, score = classify(
        _snap(epost="post@firma.no"), enk_conversion=False
    )
    assert "reachable" in cohorts
    assert score >= 5


def test_not_reachable_when_email_blank():
    cohorts, _ = classify(_snap(epost=""), enk_conversion=False)
    assert "reachable" not in cohorts


def test_enk_recent_with_email_qualifies():
    cohorts, _ = classify(
        _snap(
            organisasjonsform="ENK",
            epost="post@enk.no",
            registreringsdato="2026-05-01",
            hjemmeside="",
        ),
        enk_conversion=False,
        today=date(2026, 5, 23),
    )
    assert "reachable" in cohorts
    assert "no_website" in cohorts


def test_enk_old_is_not_a_lead():
    cohorts, score = classify(
        _snap(
            organisasjonsform="ENK",
            epost="post@enk.no",
            registreringsdato="2025-01-01",
            hjemmeside="",
        ),
        enk_conversion=False,
        today=date(2026, 5, 23),
    )
    assert cohorts == []
    assert score == 0


def test_enk_missing_regdato_is_not_a_lead():
    cohorts, _ = classify(
        _snap(organisasjonsform="ENK", epost="post@enk.no", registreringsdato=None),
        enk_conversion=False,
        today=date(2026, 5, 23),
    )
    assert cohorts == []
