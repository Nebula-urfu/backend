from datetime import datetime
from typing import Generic, Literal, TypeVar

from pydantic import BaseModel, model_validator

from .schemas import Money, QuoteData, TriageInput

T = TypeVar("T")
Step = Literal["TRIAGE", "TZ", "QUOTES_NMC", "TENDER", "PACKAGE"]
State = Literal["NOT_STARTED", "PROCESSING", "DRAFT_READY", "IN_REVIEW", "NEEDS_REWORK", "APPROVED", "AI_FAILED"]
Subject = Literal["TZ", "NMC", "PACKAGE"]


class Page(BaseModel, Generic[T]):
    items: list[T]
    nextCursor: str | None


class Actor(BaseModel):
    userId: str
    displayName: str


class FileRef(BaseModel):
    fileId: str
    name: str
    mediaType: str
    sizeBytes: int


class StepInfo(BaseModel):
    step: Step
    state: State
    updatedAt: datetime


class RequestSummary(BaseModel):
    id: str
    status: Literal["ACCEPTED", "IN_PROGRESS", "COMPLETED"]
    address: str
    entrance: str
    description: str
    photoId: str
    createdAt: datetime
    updatedAt: datetime
    urgency: Literal["EMERGENCY", "PLANNED"] | None = None
    currentStep: Step | None = None
    currentStepState: State | None = None


class RequestDetail(RequestSummary):
    photos: list[FileRef]
    author: Actor | None = None
    steps: list[StepInfo] | None = None


class Triage(BaseModel):
    state: State
    proposal: TriageInput | None
    confirmed: TriageInput | None
    confirmedBy: Actor | None = None
    confirmedAt: datetime | None = None
    updatedAt: datetime | None = None


class Versioned(BaseModel):
    state: State
    version: int
    updatedAt: datetime | None = None
    lastReviewComment: str | None = None


class TzSection(BaseModel):
    id: str
    title: str
    text: str
    editedByStaff: bool = False


class TzQuestion(BaseModel):
    id: str
    text: str
    answer: str | None


class Tz(Versioned):
    sections: list[TzSection] = []
    openQuestions: list[TzQuestion] = []
    origin: Literal["AI", "STAFF"] | None = None


class Nmc(Versioned):
    method: str | None = None
    amount: Money | None = None
    justification: str | None = None
    quoteIds: list[str] | None = None
    quoteVersions: dict[str, int] | None = None
    calculatedAt: datetime | None = None


class Document(BaseModel):
    kind: Literal["TZ", "QUOTE_FORM", "CONTRACT", "EVALUATION_PROTOCOL"]
    fileId: str
    name: str
    version: int


class Package(Versioned):
    documents: list[Document]
    approvedAt: datetime | None = None


class Winner(BaseModel):
    quoteId: str
    selectedBy: Actor
    selectedAt: datetime
    comment: str


class Contract(BaseModel):
    type: Literal["STANDARD", "NON_STANDARD"]


class Tender(BaseModel):
    state: State
    documents: list[Document]
    winnerProposal: None
    selectedWinner: Winner | None
    contract: Contract | None


class Quote(BaseModel):
    id: str
    contractorId: str | None
    extraction: Literal["EXTRACTING", "EXTRACTED", "AI_FAILED"]
    version: int
    files: list[FileRef]
    data: QuoteData | None
    editedByStaff: bool = False
    createdAt: datetime


class Contractor(BaseModel):
    id: str
    name: str
    email: str
    nonstandardContract: bool


class Recipient(BaseModel):
    contractorId: str
    name: str
    email: str


class DraftRequest(BaseModel):
    id: str
    recipients: list[Recipient]
    subject: str
    body: str
    attachments: list[FileRef]


class ComparisonQuote(BaseModel):
    quoteId: str
    contractorName: str | None
    totalPrice: Money | None
    termsDays: int | None
    warranty: str | None


class ComparisonCell(BaseModel):
    quoteId: str
    flag: Literal["NONE", "MISSING", "MISMATCH", "OUTLIER"]
    unitPrice: Money | None = None
    price: Money | None = None


class ComparisonRow(BaseModel):
    name: str
    unit: str | None
    quantity: float | None
    cells: list[ComparisonCell]


class Comparison(BaseModel):
    quotes: list[ComparisonQuote]
    rows: list[ComparisonRow]
    discrepancies: list[str]


class JournalEntry(BaseModel):
    id: str
    requestId: str
    subjectType: Subject
    subjectVersion: int
    decision: Literal["APPROVED", "REJECTED"]
    decidedBy: Actor
    decidedAt: datetime
    comment: str | None = None


class Approval(BaseModel):
    id: str
    requestId: str
    subjectType: Subject
    subjectVersion: int
    state: Literal["PENDING", "APPROVED", "REJECTED"]
    urgency: Literal["EMERGENCY", "PLANNED"]
    address: str
    submittedBy: Actor
    submittedAt: datetime
    decidedBy: Actor | None = None
    decidedAt: datetime | None = None
    comment: str | None = None


class ApprovalDetail(Approval):
    subject: Tz | Nmc | Package
    previousDecisions: list[JournalEntry]

    @model_validator(mode="before")
    @classmethod
    def validate_subject(cls, value):
        kinds = {"TZ": Tz, "NMC": Nmc, "PACKAGE": Package}
        return {**value, "subject": kinds[value["subjectType"]].model_validate(value["subject"])}


class FieldProblem(BaseModel):
    field: str
    message: str


class ProblemView(BaseModel):
    status: int
    code: str
    title: str
    errors: list[FieldProblem] | None = None
