from __future__ import annotations

from datetime import date, datetime, time as ClockTime
from decimal import Decimal
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class MatchLevel(str, Enum):
    EXACT = "EXACT"
    FUZZY = "FUZZY"
    MISMATCH = "MISMATCH"
    UNAVAILABLE = "UNAVAILABLE"


class ReviewStatus(str, Enum):
    CONFLICTING = "CONFLICTING"
    PENDING_REVIEW = "PENDING_REVIEW"
    FULLY_CORROBORATED = "FULLY_CORROBORATED"
    PARTIALLY_CORROBORATED = "PARTIALLY_CORROBORATED"
    UNSUPPORTED = "UNSUPPORTED"


class DecisionType(str, Enum):
    SYSTEM_PROPOSED = "SYSTEM_PROPOSED"
    HUMAN_CONFIRMED = "HUMAN_CONFIRMED"
    HUMAN_REJECTED = "HUMAN_REJECTED"


class SourceLocator(BaseModel):
    model_config = ConfigDict(extra="forbid")
    evidence_id: str
    locator_type: Literal["text_span", "csv_row"]
    start_offset: int | None = Field(default=None, ge=0)
    end_offset: int | None = Field(default=None, ge=0)
    line_number: int | None = Field(default=None, ge=1)
    label: str | None = None
    source_text: str | None = None

    @model_validator(mode="after")
    def valid_location(self):
        if self.locator_type == "text_span" and (self.start_offset is None or self.end_offset is None):
            raise ValueError("text_span requires start_offset and end_offset")
        if self.locator_type == "csv_row" and self.line_number is None:
            raise ValueError("csv_row requires line_number")
        return self


class ObjectRef(BaseModel):
    object_type: Literal["case", "claim", "person", "account", "transaction", "evidence", "decision"]
    object_id: str


class EvidenceItem(BaseModel):
    id: str
    evidence_type: Literal["indictment", "victim_statement", "bank_csv", "manual_note"]
    filename: str
    sha256: str
    mime_type: str
    text: str | None = None
    row_count: int | None = Field(default=None, ge=0)
    created_at: datetime


class Person(BaseModel):
    id: str
    display_name: str
    role: Literal["defendant", "victim", "recipient", "other"]
    aliases: list[str] = Field(default_factory=list)
    source_locator_ids: list[str] = Field(default_factory=list)


class Account(BaseModel):
    id: str
    masked_number: str
    institution: str | None = None
    holder_person_id: str | None = None
    source_locator_ids: list[str] = Field(default_factory=list)


class Case(BaseModel):
    id: str
    title: str
    offense_category: Literal["fraud"] = "fraud"
    status: Literal["draft", "processing", "review_required", "review_complete"] = "draft"
    created_at: datetime
    updated_at: datetime
    claim_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)


class Claim(BaseModel):
    id: str
    case_id: str
    victim_person_id: str | None = None
    victim_name: str
    victim_account: str | None = None
    alleged_recipient_person_id: str | None = None
    alleged_recipient_account_id: str | None = None
    alleged_recipient_name: str | None = None
    alleged_recipient_account: str | None = None
    claimed_amount: Decimal = Field(gt=Decimal("0"))
    currency: Literal["CNY"] = "CNY"
    time_start: date
    time_end: date
    payment_method: Literal["bank_transfer"] = "bank_transfer"
    source_locator_ids: list[str] = Field(min_length=1)
    source_locators: list[SourceLocator] = Field(default_factory=list)
    extraction_status: Literal["model_extracted", "human_confirmed", "human_corrected", "extraction_review_required"]

    @model_validator(mode="after")
    def valid_dates(self):
        if self.time_start > self.time_end:
            raise ValueError("time_start must not be after time_end")
        return self


class Transaction(BaseModel):
    id: str
    case_id: str
    transaction_id: str
    date: date
    time: ClockTime | None = None
    payer_name: str | None = None
    payer_account: str | None = None
    payee_name: str | None = None
    payee_account: str | None = None
    payer_person_id: str | None = None
    payer_account_id: str | None = None
    payee_person_id: str | None = None
    payee_account_id: str | None = None
    amount: Decimal = Field(gt=Decimal("0"))
    currency: Literal["CNY"] = "CNY"
    remark: str | None = None
    source_evidence_id: str
    source_account_id: str | None = None
    source_row: int = Field(ge=1)
    dedup_fingerprint: str

    @model_validator(mode="after")
    def has_counterparty(self):
        if not any((self.payer_name, self.payer_account, self.payer_person_id, self.payer_account_id)):
            raise ValueError("payer person or account is required")
        if not any((self.payee_name, self.payee_account, self.payee_person_id, self.payee_account_id)):
            raise ValueError("payee person or account is required")
        return self


class EvidenceLink(BaseModel):
    id: str
    from_ref: ObjectRef
    to_evidence_id: str
    locator: SourceLocator
    relation: Literal["supports", "contradicts", "mentions", "derived_from"]
    note: str | None = None

    @model_validator(mode="after")
    def locator_matches_target(self):
        if self.to_evidence_id != self.locator.evidence_id:
            raise ValueError("locator evidence_id must match to_evidence_id")
        return self


class TransactionReviewAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    transaction_id: str
    disposition: Literal["INCLUDED", "EXCLUDED", "DISPUTED"]
    reason_code: Literal[
        "MATCHED_CLAIM",
        "DUPLICATE_TRANSACTION",
        "UNRELATED_TRANSACTION",
        "THIRD_PARTY_RECIPIENT",
        "ACCOUNT_MISMATCH",
        "AMOUNT_MISMATCH",
        "DATE_MISMATCH",
        "OTHER",
    ]
    note: str | None = None

    @model_validator(mode="after")
    def reason_matches_disposition(self):
        allowed_reasons = {
            "INCLUDED": {"MATCHED_CLAIM", "OTHER"},
            "EXCLUDED": {
                "DUPLICATE_TRANSACTION",
                "UNRELATED_TRANSACTION",
                "ACCOUNT_MISMATCH",
                "DATE_MISMATCH",
                "OTHER",
            },
            "DISPUTED": {
                "THIRD_PARTY_RECIPIENT",
                "ACCOUNT_MISMATCH",
                "AMOUNT_MISMATCH",
                "DATE_MISMATCH",
                "OTHER",
            },
        }
        if self.reason_code not in allowed_reasons[self.disposition]:
            raise ValueError(
                f"reason_code {self.reason_code} is incompatible with disposition {self.disposition}"
            )
        return self


class ReviewDecision(BaseModel):
    id: str
    case_id: str
    claim_id: str
    version: int = Field(ge=1)
    decision_type: DecisionType
    supersedes_decision_id: str | None = None
    status: ReviewStatus
    included_transaction_ids: list[str] = Field(default_factory=list)
    excluded_transaction_ids: list[str] = Field(default_factory=list)
    disputed_transaction_ids: list[str] = Field(default_factory=list)
    covered_amount: Decimal = Field(ge=Decimal("0"))
    uncovered_amount: Decimal = Field(ge=Decimal("0"))
    disputed_amount: Decimal = Field(ge=Decimal("0"))
    reason_codes: list[str] = Field(default_factory=list)
    reviewer: str | None = None
    reviewed_at: datetime | None = None
    note: str | None = None
    verification_error_codes: list[str] = Field(default_factory=list)
    transaction_review_actions: list[TransactionReviewAction] = Field(default_factory=list)
