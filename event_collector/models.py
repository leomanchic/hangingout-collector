from datetime import datetime, timezone, date
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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


class PeriodInput(Input):
    date_from: str = Field(pattern=r'^\d{4}-\d{2}-\d{2}$')
    date_to: str = Field(pattern=r'^\d{4}-\d{2}-\d{2}$')
    timezone: Literal['Europe/Moscow'] = 'Europe/Moscow'
    precision: Literal['estimated'] = 'estimated'

    @model_validator(mode='after')
    def ordered_dates(self):
        first, last = date.fromisoformat(self.date_from), date.fromisoformat(self.date_to)
        if not 1 <= (last - first).days <= 1096:
            raise ValueError('Expected a bounded multi-day period')
        return self


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
    source_movie_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,48}$")
    source_updated_at: str | None = None
    occurrences: list[OccurrenceInput] = Field(default_factory=list, max_length=100)
    period: PeriodInput | None = None

    @model_validator(mode='after')
    def schedule_kind(self):
        if self.source_movie_id and self.category != 'cinema':
            raise ValueError('Movie identity is only valid for cinema')
        if bool(self.occurrences) == bool(self.period):
            raise ValueError('Supply precise sessions or an estimated period')
        if self.period and self.category not in {'art', 'walk', 'learning'}:
            raise ValueError('Periods are supported for exhibitions and visiting activities')
        return self

    @field_validator("source_updated_at")
    @classmethod
    def source_date(cls, value):
        return aware(value) if value else None
