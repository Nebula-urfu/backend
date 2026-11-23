from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, StringConstraints
from typing_extensions import Annotated

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=6000)]
Short = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Positive = Annotated[Decimal, Field(gt=0, max_digits=14, decimal_places=2)]


class RequestInput(BaseModel):
    description: Text
    entrance: Short
    photoFileIds: list[Short] = Field(min_length=1, max_length=5)


class TriageInput(BaseModel):
    urgency: Literal["EMERGENCY", "PLANNED"]
    workType: Short
    route: Literal["EMERGENCY_CONTRACTOR_CALL", "PLANNED_PROCUREMENT"]


class Section(BaseModel):
    id: Short
    text: Text


class Answer(BaseModel):
    questionId: Short
    answer: str = Field(max_length=2000)


class TzInput(BaseModel):
    baseVersion: int = Field(ge=0)
    sections: list[Section] = Field(default_factory=list, max_length=20)
    answers: list[Answer] = Field(default_factory=list, max_length=20)


class VersionInput(BaseModel):
    version: int = Field(ge=1)


class DecisionInput(BaseModel):
    subjectVersion: int = Field(ge=1)
    comment: str = Field(default="", max_length=2000)


class QuoteInput(BaseModel):
    fileIds: list[Short] = Field(min_length=1, max_length=5)
    contractorId: Short | None = None


class Recipients(BaseModel):
    contractorIds: list[Short] = Field(min_length=1, max_length=30)


class Money(BaseModel):
    amount: Positive
    currency: Literal["RUB"] = "RUB"


class QuoteItem(BaseModel):
    name: Short
    quantity: Positive | None = None
    unit: Short | None = None
    unitPrice: Money | None = None
    price: Money | None = None


class QuoteData(BaseModel):
    contractorName: Short | None = None
    totalPrice: Money | None = None
    termsDays: int | None = Field(default=None, ge=0, le=10000)
    warranty: str | None = Field(default=None, max_length=1000)
    items: list[QuoteItem] = Field(default_factory=list, max_length=100)
    discrepancies: list[Short] = Field(default_factory=list, max_length=30)


class QuoteEdit(BaseModel):
    baseVersion: int = Field(ge=1)
    data: QuoteData


class CalculationInput(BaseModel):
    quoteIds: list[Short] | None = Field(default=None, max_length=50)


class WinnerInput(BaseModel):
    quoteId: Short
    comment: str = Field(default="", max_length=2000)
