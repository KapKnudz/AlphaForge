import json
from dataclasses import asdict, make_dataclass
from types import SimpleNamespace

import pytest
from dcf_calibration_fixtures import synthetic_calibration_fixture
from test_method_date_growth_selection import (
    CUTOFF,
    annual,
    packet,
    rank_exports,
    setup,
)

from alphaforge.cli.main import cmd_rank
from alphaforge.cli.ranking_loader import load_results_for_company
from alphaforge.core.gate.readiness import AgentReadinessGate
from alphaforge.core.ranking.engine import RankingEngine
from alphaforge.core.valuation.dcf_router import decide_dcf_route
from alphaforge.db.numerical_runs import (
    ReplayRefusal,
    capture_inputs,
    replay_run,
    retain_inputs,
    rules_bundle,
)


def route(archetype="operating_company", profile="mature", source_id="document:1"):
    return {
        "archetype": archetype,
        "forecast_profile": profile,
        "evidence_references": [{"source_id": source_id, "anchor": "document:1#page:1"}],
    }


def qualified_company(branch=None):
    conn, company_id = setup(
        branch=branch,
        periods=[annual(2025, 100), annual(2026, 110)],
    )
    packet(conn, company_id)
    synthetic_calibration_fixture(conn, company_id)
    return conn, company_id


def test_explicit_mature_operating_route_values_with_canonical_identities():
    conn, company_id = qualified_company()
    result = load_results_for_company(conn, company_id, CUTOFF, dcf_routing=route())["reverse_dcf"]

    dcf = result["dcf"]
    decision = dcf["routing"]
    assert result["status"] == "available"
    assert dcf["available"] is True
    assert dcf["value_per_share"] > 0
    assert decision["policy_version"] == "dcf-routing-v1-explicit-mature-operating-company"
    assert decision["method"] == "fcff"
    assert decision["method_identity"] == "fcff-forward-reinvestment-v1"
    assert decision["archetype"] == "operating_company"
    assert decision["forecast_profile"] == "mature"
    assert decision["profile_identity"] == "operating_company+mature-v1"
    assert decision["evidence_references"] == [
        {
            "source_id": "document:1",
            "source_url": "https://mfn.test/fix",
            "published_on": "2026-05-01T00:00:00Z",
            "observed_on": "2026-06-01T00:00:00Z",
            "anchor": "document:1#page:1",
            "sha256": "a" * 64,
        }
    ]
    assert all(
        decision[name] for name in ("input_identity", "evidence_identity", "decision_identity")
    )
    assert dcf["version"] == "reverse-dcf-v16-explicit-input-quality"
    assert dcf["contract_version"] == "dcf-result-contract-v1"


@pytest.mark.parametrize(
    ("archetype", "profile", "reason", "status"),
    [
        (
            "operating_company",
            "high_growth",
            "high_growth_transition_and_funding_evidence_unavailable",
            "insufficient_evidence",
        ),
        (
            "operating_company",
            "cyclical",
            "cyclical_normalized_base_unavailable",
            "insufficient_evidence",
        ),
        ("bank", "mature", "financial_company_requires_non_fcff_method", "unsupported"),
        ("financial", "mature", "financial_company_requires_non_fcff_method", "unsupported"),
        ("property", "mature", "property_company_requires_nav_or_ffo_method", "unsupported"),
        ("resource", "mature", "resource_company_requires_separate_economic_policy", "unsupported"),
        ("holding", "mature", "holding_or_unusual_structure_unsupported", "unsupported"),
        ("unusual", "mature", "holding_or_unusual_structure_unsupported", "unsupported"),
        ("unknown", "mature", "archetype_unknown_or_mixed", "insufficient_evidence"),
        ("mixed", "mature", "archetype_unknown_or_mixed", "insufficient_evidence"),
        (
            "operating_company",
            "unknown",
            "forecast_profile_unknown_or_mixed",
            "insufficient_evidence",
        ),
        ("operating_company", "unsupported", "unsupported_forecast_profile", "unsupported"),
    ],
)
def test_unsupported_and_unavailable_routes_refuse_without_values_or_roots(
    archetype, profile, reason, status
):
    conn, company_id = qualified_company()
    result = load_results_for_company(
        conn, company_id, CUTOFF, dcf_routing=route(archetype, profile)
    )["reverse_dcf"]
    dcf = result["dcf"]

    assert result["status"] == "unavailable"
    assert dcf["status"] == status
    assert dcf["reason"] == reason
    assert dcf["routing"]["reason"] == reason
    assert not dcf["available"]
    assert not result.get("implied")
    assert (
        not {
            "value_per_share",
            "enterprise_value",
            "equity_value",
            "terminal_value",
            "projected_cash_flows",
        }
        & dcf.keys()
    )


def test_missing_or_unknown_routing_never_auto_admits_general_company():
    conn, company_id = qualified_company()
    loaded = load_results_for_company(conn, company_id, CUTOFF)
    dcf = loaded["reverse_dcf"]["dcf"]
    assert loaded["candidate"].ranking_model == "general"
    assert dcf["routing"]["archetype"] == "unknown"
    assert dcf["routing"]["forecast_profile"] == "unknown"
    assert dcf["reason"] == "archetype_unknown_or_mixed"
    assert dcf["available"] is False

    no_evidence = load_results_for_company(
        conn,
        company_id,
        CUTOFF,
        dcf_routing={"archetype": "operating_company", "forecast_profile": "mature"},
    )["reverse_dcf"]["dcf"]
    assert no_evidence["reason"] == "mature_operating_route_evidence_unavailable"
    assert no_evidence["available"] is False
    un_catalogued = load_results_for_company(
        conn,
        company_id,
        CUTOFF,
        dcf_routing=route(source_id="not-in-the-frozen-packet"),
    )["reverse_dcf"]["dcf"]
    assert un_catalogued["reason"] == "mature_operating_route_evidence_not_catalogued"


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        (
            "source_url",
            "https://fabricated.test/report",
            "mature_operating_route_evidence_mismatch",
        ),
        (
            "published_on",
            "1900-01-01T00:00:00Z",
            "mature_operating_route_evidence_mismatch",
        ),
        (
            "observed_on",
            "1900-01-02T00:00:00Z",
            "mature_operating_route_evidence_mismatch",
        ),
        ("anchor", "document:1#page:999", "mature_operating_route_evidence_not_catalogued"),
        ("sha256", "f" * 64, "mature_operating_route_evidence_mismatch"),
    ],
)
def test_mature_route_rejects_provenance_not_owned_by_frozen_source(field, value, reason):
    conn, company_id = qualified_company()
    supplied = route()
    supplied["evidence_references"][0][field] = value

    dcf = load_results_for_company(conn, company_id, CUTOFF, dcf_routing=supplied)["reverse_dcf"][
        "dcf"
    ]

    assert dcf["available"] is False
    assert dcf["status"] == "insufficient_evidence"
    assert dcf["reason"] == reason
    assert dcf["routing"]["evidence_references"] == []


def test_routing_cannot_override_capital_quality_or_economic_refusals():
    route_high_growth = route(profile="high_growth")
    conn, company_id = setup(periods=[annual(2025, 100), annual(2026, 110)])
    packet(conn, company_id)
    missing_capital = load_results_for_company(
        conn, company_id, CUTOFF, dcf_routing=route_high_growth
    )["reverse_dcf"]["dcf"]
    assert missing_capital["reason"] == "dated_positive_roic"
    assert missing_capital["routing"]["reason"] == (
        "high_growth_transition_and_funding_evidence_unavailable"
    )
    assert "value_per_share" not in missing_capital

    conn, company_id = setup(periods=[annual(2026, 110)])
    packet(conn, company_id)
    synthetic_calibration_fixture(conn, company_id)
    bad_quality = load_results_for_company(conn, company_id, CUTOFF, dcf_routing=route_high_growth)[
        "reverse_dcf"
    ]["dcf"]
    assert bad_quality["reason"] == "qualified_consecutive_annual_history_unavailable"
    assert bad_quality["routing"]["reason"] == (
        "high_growth_transition_and_funding_evidence_unavailable"
    )
    assert "value_per_share" not in bad_quality

    conn, company_id = setup(periods=[annual(2025, 100), annual(2026, 110, operating_Income=-22)])
    packet(conn, company_id)
    synthetic_calibration_fixture(conn, company_id)
    bad_economics = load_results_for_company(
        conn,
        company_id,
        CUTOFF,
        dcf_routing=route(),
    )["reverse_dcf"]["dcf"]
    assert bad_economics["reason"] == "negative_nopat_unsupported_reinvestment"
    assert "value_per_share" not in bad_economics

    conn, company_id = setup(periods=[annual(2025, 100), annual(2026, 110, net_Debt=None)])
    packet(conn, company_id)
    synthetic_calibration_fixture(conn, company_id)
    missing_bridge_loaded = load_results_for_company(
        conn, company_id, CUTOFF, dcf_routing=route(profile="high_growth")
    )
    missing_bridge = missing_bridge_loaded["reverse_dcf"]["dcf"]
    assert missing_bridge["reason"] == "net_debt"
    assert missing_bridge["routing"]["reason"] == (
        "high_growth_transition_and_funding_evidence_unavailable"
    )
    assert "value_per_share" not in missing_bridge
    bridge_policy = missing_bridge_loaded["dcf"]["policy"]
    assert bridge_policy.available is False
    assert bridge_policy.assumptions is None
    assert bridge_policy.missing_information == (
        "high_growth_transition_and_funding_evidence_unavailable",
    )

    conn, company_id = setup(periods=[annual(2025, 100), annual(2026, 110)], price_date=None)
    packet(conn, company_id)
    synthetic_calibration_fixture(conn, company_id)
    missing_price = load_results_for_company(
        conn, company_id, CUTOFF, dcf_routing=route(profile="high_growth")
    )["reverse_dcf"]["dcf"]
    assert missing_price["reason"] == "latest stock price unavailable"
    assert "value_per_share" not in missing_price


def test_rank_cli_accepts_explicit_routing_json(monkeypatch, tmp_path):
    conn, company_id = qualified_company()
    route_file = tmp_path / "dcf-routing.json"
    route_file.write_text(json.dumps({str(company_id): route()}), encoding="utf-8")
    monkeypatch.setattr("alphaforge.db.connection.get_connection", lambda settings: conn)
    monkeypatch.chdir(tmp_path)

    assert (
        cmd_rank(
            SimpleNamespace(
                dsn="sqlite:///:memory:",
                as_of=CUTOFF,
                watchlist=None,
                dcf_routing_json=str(route_file),
            )
        )
        == 0
    )
    output = json.loads((tmp_path / "exports" / CUTOFF / "dcf.json").read_text())
    assert output[str(company_id)]["dcf"]["available"] is True
    assert output[str(company_id)]["dcf"]["routing"]["archetype"] == "operating_company"


def test_route_input_is_frozen_replayed_and_changes_numerical_identity(monkeypatch, tmp_path):
    conn, company_id = qualified_company()
    routes = {company_id: route()}
    score, _, _ = rank_exports(conn, monkeypatch, tmp_path, dcf_routing=routes)
    retained = conn.execute(
        "SELECT run_id,numerical_identity,outputs FROM executed_numerical_runs ORDER BY run_id DESC LIMIT 1"
    ).fetchone()
    run_id = retained["run_id"]
    original = json.loads(retained["outputs"])
    body_row = conn.execute(
        "SELECT body,rules FROM numerical_input_bodies WHERE numerical_identity=?",
        (retained["numerical_identity"],),
    ).fetchone()
    frozen_body = json.loads(body_row["body"])
    assert (
        frozen_body["dcf_routing"][str(company_id)]["evidence_references"][0]["source_id"]
        == "document:1"
    )
    assert rules_bundle()["dcf_routing"] == "dcf-routing-v1-explicit-mature-operating-company"
    assert original["dcf"][str(company_id)]["dcf"]["routing"]["decision_identity"]

    routes[company_id] = route("property", source_id="fixture:mutated-route")
    assert replay_run(conn, run_id)["outputs"] == original
    with monkeypatch.context() as version_change:
        version_change.setattr(
            "alphaforge.db.numerical_runs.DCF_ROUTING_POLICY_VERSION",
            "dcf-routing-v2-incompatible",
        )
        with pytest.raises(ReplayRefusal, match="unsupported_rules_or_code"):
            replay_run(conn, run_id)
    Company = make_dataclass("Company", ["id", "name", "ticker", "branch_id"])
    companies = [Company(company_id, "Synthetic AB", "FIX", None)]
    changed_body, _ = capture_inputs(conn, companies, CUTOFF, dcf_routing=routes)
    changed_identity, _ = retain_inputs(conn, changed_body, rules_bundle())
    assert changed_identity != retained["numerical_identity"]
    assert score["readiness_status"] == original["scores"][0]["readiness_status"]


@pytest.mark.parametrize("branch,archetype", [(68, "financial"), (75, "property")])
def test_sector_scores_and_readiness_do_not_depend_on_dcf_route(branch, archetype):
    conn, company_id = qualified_company(branch=branch)
    company = SimpleNamespace(id=company_id, name="Synthetic AB", ticker="FIX", branch_id=branch)
    results = [
        load_results_for_company(conn, company_id, CUTOFF, dcf_routing=route(archetype)),
        load_results_for_company(conn, company_id, CUTOFF),
    ]
    scores = []
    assessments = []
    for loaded in results:
        ranked = RankingEngine().rank([company], {company_id: loaded})
        score = ranked.scores[0]
        candidate = loaded["candidate"]
        candidate.ticker = score.ticker
        candidate.ranking_model = score.ranking_model
        scores.append(asdict(score))
        assessments.append(AgentReadinessGate().assess(candidate))

    assert scores[0] == scores[1]
    assert assessments[0].status == assessments[1].status == "method_unsupported"
    assert assessments[0].blockers == assessments[1].blockers


@pytest.mark.parametrize("invalid_key", [True, 1.5, "01"])
def test_capture_rejects_noncanonical_programmatic_route_keys(invalid_key):
    conn, company_id = qualified_company()
    Company = make_dataclass("Company", ["id", "name", "ticker", "branch_id"])
    companies = [Company(company_id, "Synthetic AB", "FIX", None)]

    with pytest.raises(ValueError, match="canonical company ids"):
        capture_inputs(conn, companies, CUTOFF, dcf_routing={invalid_key: route()})


def test_capture_rejects_route_key_normalization_collisions():
    conn, company_id = qualified_company()
    Company = make_dataclass("Company", ["id", "name", "ticker", "branch_id"])
    companies = [Company(company_id, "Synthetic AB", "FIX", None)]

    with pytest.raises(ValueError, match="at most once"):
        capture_inputs(
            conn,
            companies,
            CUTOFF,
            dcf_routing={company_id: route(), str(company_id): route()},
        )


@pytest.mark.parametrize(
    "payload",
    [
        '{"1": {}, "1": {}}',
        '{"1": {}, "01": {}}',
    ],
)
def test_rank_cli_rejects_duplicate_or_noncanonical_route_json_keys(payload, monkeypatch, tmp_path):
    conn, _ = qualified_company()
    route_file = tmp_path / "dcf-routing.json"
    route_file.write_text(payload, encoding="utf-8")
    monkeypatch.setattr("alphaforge.db.connection.get_connection", lambda settings: conn)

    with pytest.raises(ValueError, match="duplicate key|canonical company ids"):
        cmd_rank(
            SimpleNamespace(
                dsn="sqlite:///:memory:",
                as_of=CUTOFF,
                watchlist=None,
                dcf_routing_json=str(route_file),
            )
        )


def test_route_identities_are_stable_and_evidence_bound():
    conn, company_id = setup()
    evidence_packet = packet(conn, company_id)
    first = decide_dcf_route(route(), evidence_packet=evidence_packet)
    same = decide_dcf_route(route(), evidence_packet=evidence_packet)
    other_value = route()
    other_value["evidence_references"][0]["source_url"] = "https://fabricated.test"
    other = decide_dcf_route(other_value, evidence_packet=evidence_packet)

    assert first.decision_identity == same.decision_identity
    assert first.input_identity == same.input_identity
    assert first.decision_identity != other.decision_identity
    assert first.evidence_identity != other.evidence_identity


def test_decision_identity_tracks_resolved_evidence_identity():
    conn, company_id = setup()
    evidence_packet = packet(conn, company_id)
    later_packet = packet(conn, company_id, cutoff="2026-06-02")

    first = decide_dcf_route(route(), evidence_packet=evidence_packet)
    later = decide_dcf_route(route(), evidence_packet=later_packet)

    assert first.input_identity == later.input_identity
    assert first.evidence_identity != later.evidence_identity
    assert first.decision_identity != later.decision_identity
