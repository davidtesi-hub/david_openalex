"""OpenAlex PDF review partitions by publication_date."""

import datetime as dt

DAY_GROUPS = {
    "monday": (0,), "tuesday": (1,), "wednesday": (2,),
    "thursday": (3,), "friday": (4,), "weekend": (5, 6),
}


def group_for_timestamp(timestamp: str) -> str:
    parsed = dt.datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    weekday = parsed.astimezone(dt.timezone.utc).weekday()
    return next(group for group, weekdays in DAY_GROUPS.items() if weekday in weekdays)


def qwen_pdf_db(root, group: str):
    if group not in DAY_GROUPS:
        raise ValueError(f"Unknown PDF weekday group: {group}")
    suffix = "" if group == "monday" else f"_{group}"
    return root / "abstract_evaluation" / "pdf_answers_v1" / f"pdf_answers_qwen3_rag_8b{suffix}.sqlite"
