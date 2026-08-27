from __future__ import annotations

from decimal import Decimal

from legal_funds_agent.domain.models import Claim, SourceLocator
from legal_funds_agent.llm.base import LLMProvider


def extract_claims(text: str, *, case_id: str, evidence_id: str, provider: LLMProvider) -> tuple[list[Claim], list[SourceLocator]]:
    rows = provider.generate_structured(text=text, schema_name="payment_claim_v0.1")
    claims: list[Claim] = []
    locators: list[SourceLocator] = []
    for index, row in enumerate(rows, start=1):
        source_text = str(row.get("source_text") or "")
        start_offset = text.find(source_text) if source_text else -1
        if start_offset < 0:
            raise ValueError("claim source_text is not present in the evidence text")
        end_offset = start_offset + len(source_text)
        locator_id = f"LOC-{evidence_id}-{index}"
        locator = SourceLocator(
            evidence_id=evidence_id,
            locator_type="text_span",
            start_offset=start_offset,
            end_offset=end_offset,
            label=locator_id,
        )
        claim = Claim(
            id=f"CLM-{index:03d}", case_id=case_id, victim_name=row["victim_name"],
            alleged_recipient_name=row.get("alleged_recipient_name"),
            claimed_amount=Decimal(str(row["claimed_amount"])).quantize(Decimal("0.01")),
            time_start=row["time_start"], time_end=row["time_end"],
            source_locator_ids=[locator_id], extraction_status="model_extracted",
        )
        claims.append(claim)
        locators.append(locator)
    return claims, locators
