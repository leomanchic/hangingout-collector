from datetime import datetime, timezone
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from pydantic import BaseModel, ConfigDict, Field, field_validator

class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


def aware(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError()
        return parsed.astimezone(timezone.utc).isoformat()
    except ValueError:
        raise ValueError("Укажите дату и время с часовым поясом") from None


class OccurrenceInput(Input):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,48}$")
    starts_at: str = Field(max_length=50)
    timezone: str = Field(max_length=80)
    status: Literal["scheduled", "cancelled"] = "scheduled"
    _aware = field_validator("starts_at")(aware)

    @field_validator("timezone")
    @classmethod
    def timezone_valid(cls, value):
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError:
            raise ValueError("Unknown IANA timezone") from None
        return value


class EventImport(Input):
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=5000)
    city: str = Field(min_length=1, max_length=80)
    category: Literal["music", "art", "theatre", "walk", "learning", "cinema"]
    venue: str = Field(min_length=1, max_length=200)
    address: str = Field(min_length=1, max_length=300)
    price: int | None = Field(default=None, ge=0)
    price_text: str = Field(default="", max_length=1000)
    source_name: str = Field(min_length=1, max_length=200)
    source_url: str = Field(pattern=r"^https?://[^\s]+$", max_length=2000)
    source_event_id: str = Field(min_length=1, max_length=100)
    source_updated_at: str | None = None
    occurrences: list[OccurrenceInput] = Field(min_length=1, max_length=100)

    @field_validator("source_updated_at")
    @classmethod
    def source_date(cls, value):
        return aware(value) if value else None
