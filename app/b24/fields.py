"""Пользовательские поля сделок для олимпиады.

Поля сделок в Битрикс24 общие для всех воронок, поэтому все названия
начинаются с «Олимпиада:», а коды — с UF_CRM_OL_ (код поля в Битриксе не
длиннее 20 символов). Создаются командой
`python -m app.cli.b24 setup --apply`.

СНИЛС в Битрикс намеренно не передаём: он хранится только в базе кабинета.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class FieldSpec:
    name: str  # без префикса UF_CRM_
    label: str
    type: str = "string"
    rows: int = 1
    items: tuple[str, ...] = field(default_factory=tuple)

    @property
    def code(self) -> str:
        return f"UF_CRM_{self.name}"


DEAL_FIELDS = (
    FieldSpec("OL_PID", "Олимпиада: ID в кабинете"),
    FieldSpec("OL_FIO", "Олимпиада: ФИО участника"),
    FieldSpec("OL_CODE", "Олимпиада: код участника"),
    FieldSpec("OL_CITY", "Олимпиада: город"),
    FieldSpec("OL_SCHOOL", "Олимпиада: школа"),
    FieldSpec("OL_QSCORE", "Олимпиада: баллы отборочного", "double"),
    FieldSpec("OL_QTASKS", "Олимпиада: отборочный по задачам"),
    FieldSpec("OL_CIPHER", "Олимпиада: шифр"),
    FieldSpec("OL_TEACHER", "Олимпиада: учитель"),
    FieldSpec("OL_VENUE", "Олимпиада: точка финала"),
    FieldSpec("OL_FSCORE", "Олимпиада: баллы финала", "double"),
    FieldSpec("OL_FTASKS", "Олимпиада: финал по задачам"),
    FieldSpec("OL_SCAN", "Олимпиада: скан работы", "url"),
    FieldSpec("OL_APPEAL", "Олимпиада: текст апелляции", rows=5),
    FieldSpec("OL_DECISION", "Олимпиада: решение по апелляции", rows=5),
    FieldSpec("OL_TOTAL", "Олимпиада: итоговый балл", "double"),
    FieldSpec(
        "OL_RESULT", "Олимпиада: итог", "enumeration", items=("Победитель", "Призёр", "Участник")
    ),
    FieldSpec("OL_DIPLOMA", "Олимпиада: диплом", "url"),
)

F = {spec.name: spec.code for spec in DEAL_FIELDS}


def userfield_payload(spec: FieldSpec) -> dict:
    label = {"ru": spec.label}
    payload = {
        "FIELD_NAME": spec.name,
        "USER_TYPE_ID": spec.type,
        "XML_ID": spec.name,
        "EDIT_FORM_LABEL": label,
        "LIST_COLUMN_LABEL": label,
        "LIST_FILTER_LABEL": label,
        "SHOW_FILTER": "Y",
    }
    if spec.rows > 1:
        payload["SETTINGS"] = {"ROWS": spec.rows}
    if spec.items:
        payload["LIST"] = [{"VALUE": value, "SORT": (i + 1) * 10} for i, value in enumerate(spec.items)]
    return payload
