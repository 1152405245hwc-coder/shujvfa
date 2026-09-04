from datetime import date
from decimal import Decimal

from legal_funds_agent.domain.models import Claim, MatchLevel, SourceLocator, Transaction
from legal_funds_agent.persistence.database import connect
from legal_funds_agent.persistence.repository import Repository
from legal_funds_agent.services.candidate_matcher import (
    CandidateMatch,
    candidate_review_priority,
    sort_candidates_for_review,
)


def _tx(tx_id: str, amount: str, *, payee: str = "何某", case_id: str = "CASE-UI") -> Transaction:
    return Transaction(
        id=f"TX-{tx_id}", case_id=case_id, transaction_id=tx_id,
        date=date(2026, 1, 1), payer_name="李某", payee_name=payee,
        amount=Decimal(amount), source_evidence_id="EVI-XLSX", source_row=8,
        dedup_fingerprint=f"FP-{tx_id}",
    )


def test_candidates_are_sorted_by_review_priority_then_amount():
    transactions = {
        tx.id: tx for tx in [_tx("LOW", "90000"), _tx("HIGH", "10000", payee="林某")]
    }
    candidates = [
        CandidateMatch("CLM-1", "TX-LOW", MatchLevel.EXACT, MatchLevel.EXACT,
                       "EXACT", "EXACT", ("M01",), False, ()),
        CandidateMatch("CLM-1", "TX-HIGH", MatchLevel.EXACT, MatchLevel.MISMATCH,
                       "EXACT", "EXACT", ("M01",), True, ("THIRD_PARTY_RECIPIENT",)),
    ]
    ordered = sort_candidates_for_review(candidates, transactions)
    assert ordered[0].transaction_id == "TX-HIGH"
    assert candidate_review_priority(ordered[0]) > candidate_review_priority(ordered[1])


def test_xlsx_source_row_and_claim_locators_survive_snapshot():
    locator = SourceLocator(
        evidence_id="EVI-INDICTMENT", locator_type="text_span",
        start_offset=2, end_offset=8, label="LOC-1", source_text="李某支付",
    )
    claim = Claim(
        id="CLM-LOC", case_id="CASE-LOC", victim_name="李某",
        claimed_amount=Decimal("100.00"), time_start="2026-01-01", time_end="2026-01-01",
        source_locator_ids=["LOC-1"], source_locators=[locator], extraction_status="human_confirmed",
    )
    tx = _tx("LOC", "100", case_id="CASE-LOC")
    with connect(":memory:") as connection:
        repository = Repository(connection)
        repository.save_claim(claim)
        repository.save_transactions([tx])
        restored_claim = repository.load_case_claims("CASE-LOC")[0]
        restored_tx = repository.load_case_transactions("CASE-LOC")[tx.id]
    assert restored_claim.source_locators[0].label == "LOC-1"
    assert restored_tx.source_row == 8


def test_investigation_status_is_persisted_per_case():
    with connect(":memory:") as connection:
        repository = Repository(connection)
        repository.save_investigation_items("CASE-UI", [{
            "item_id": "INV-1", "status": "待核查", "category": "资金缺口补证",
        }])
        repository.save_investigation_items("CASE-UI", [{
            "item_id": "INV-1", "status": "已核查", "category": "资金缺口补证",
        }])
        repository.save_investigation_items("CASE-OTHER", [{
            "item_id": "INV-1", "status": "待核查", "category": "其他",
        }])
        first = repository.load_investigation_items("CASE-UI")
        second = repository.load_investigation_items("CASE-OTHER")
    assert first[0]["status"] == "已核查"
    assert second[0]["status"] == "待核查"
