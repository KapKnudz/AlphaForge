"""Trusted analyst admission, never an automated ROIC backfill."""

from datetime import date

from alphaforge.core.valuation.reinvestment import qualify_calibration


def append_reinvestment_calibration(conn, company_id: int, record: dict, *, as_of: date) -> str:
    if record.get("company_id") != company_id:
        raise ValueError("calibration company identity mismatch")
    calibration = qualify_calibration(record, as_of=as_of, currency="SEK", tax_rate=0.21)
    existing = conn.execute(
        "SELECT record_json FROM reinvestment_calibrations WHERE company_id=? AND identity=?",
        (company_id, calibration.identity),
    ).fetchone()
    if existing:
        if existing[0] != calibration.record_json:
            raise ValueError("calibration identity content mismatch")
        return calibration.identity
    conn.execute(
        "INSERT INTO reinvestment_calibrations(company_id,identity,record_json) VALUES (?,?,?)",
        (company_id, calibration.identity, calibration.record_json),
    )
    return calibration.identity
