"""Pydantic request/response models."""
from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class SampleIn(BaseModel):
    code: str = Field(min_length=1)
    true_label: str = Field(min_length=1)


class BatchIn(BaseModel):
    categories: list[str] = Field(min_length=2)
    samples: list[SampleIn] = Field(min_length=1)

    @field_validator("categories")
    @classmethod
    def _unique_categories(cls, v: list[str]) -> list[str]:
        if len(set(v)) != len(v):
            raise ValueError("categories must be unique")
        if any(not c.strip() for c in v):
            raise ValueError("categories must be non-empty")
        return v

    @field_validator("samples")
    @classmethod
    def _unique_sample_codes(cls, v: list[SampleIn]) -> list[SampleIn]:
        codes = [s.code for s in v]
        if len(set(codes)) != len(codes):
            raise ValueError("sample codes must be unique")
        return v


class BatchCreated(BaseModel):
    batch_id: str
    stage: str
    reviewer1_token: str
    reviewer2_token: str
    arbiter_token: str
    sample_count: int


class ClassificationIn(BaseModel):
    label: str = Field(min_length=1)


class SubmitIn(BaseModel):
    classifications: dict[str, str] = Field(min_length=1)


class SubmitResult(BaseModel):
    batch_id: str
    reviewer: str
    submitted: bool
    stage: str
    sealed_now: bool


class Dispute(BaseModel):
    code: str
    reviewer1_label: str
    reviewer2_label: str
    chosen_label: str | None


class DisputesOut(BaseModel):
    batch_id: str
    stage: str
    remaining: int
    disputes: list[Dispute]


class AdjudicateIn(BaseModel):
    code: str = Field(min_length=1)
    label: str = Field(min_length=1)


class AdjudicateResult(BaseModel):
    batch_id: str
    code: str
    chosen_label: str
    remaining: int
    stage: str
    sealed_now: bool


class SampleResult(BaseModel):
    code: str
    final_label: str
    true_label: str
    reviewer1_label: str
    reviewer2_label: str
    correct: bool
    source: str  # "agreement" | "adjudication"


class BatchView(BaseModel):
    batch_id: str
    stage: str
    # Before sealing: only codes + stage, nothing else.
    sample_codes: list[str] | None = None
    samples: list[SampleResult] | None = None
    reviewer1_correct: int | None = None
    reviewer2_correct: int | None = None
    sample_count: int


class SubmissionStatus(BaseModel):
    batch_id: str
    reviewer: str
    submitted: bool
    submitted_at: str | None = None
    sample_count: int
    # Only the caller's own labels are ever returned here.  Batch stage is
    # deliberately omitted so a reviewer cannot infer whether the other side
    # has already handed in.
    classifications: dict[str, str] | None = None
