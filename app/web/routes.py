from datetime import datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import config
from ..auth.sessions import (
    end_session,
    optional_account,
    require_account,
    session_token,
)
from ..auth.telegram import LINK_INTENT_COOKIE
from ..db import get_db
from ..models import Account, B24Outbox, Participant, utcnow
from ..security import csrf_ok, csrf_token, participant_code
from ..templating import templates
from .validation import clean, normalize_phone, valid_email

router = APIRouter()


def _render(request: Request, name: str, **context):
    token = request.cookies.get("sid")
    context.setdefault("csrf", csrf_token(token) if token else "")
    return templates.TemplateResponse(request, name, context)


def _check_csrf(request: Request, value: str) -> None:
    if not csrf_ok(session_token(request), value):
        raise HTTPException(403)


def _now() -> datetime:
    return utcnow()


def qual_state(now: datetime | None = None) -> str:
    """before | open | closed — состояние окна отборочного тура."""
    now = now or _now()
    if config.QUAL_OPENS_AT is None or now < config.QUAL_OPENS_AT:
        return "before"
    if config.QUAL_CLOSES_AT is not None and now >= config.QUAL_CLOSES_AT:
        return "closed"
    return "open"


def children_editable() -> bool:
    # Данные ребёнка (особенно класс) меняются только до старта тура:
    # после старта ответы в Яндекс.Форме уже привязаны к классу.
    return qual_state() == "before"


def _enqueue(db: AsyncSession, entity: str, entity_id: int, action: str) -> None:
    db.add(B24Outbox(entity=entity, entity_id=entity_id, action=action))


# --- вход ----------------------------------------------------------------


@router.get("/")
async def index(request: Request, account: Account | None = Depends(optional_account)):
    if account is not None:
        return RedirectResponse("/cabinet", status_code=303)
    return _render(request, "index.html", error=request.query_params.get("error"))


@router.post("/logout")
async def logout(request: Request, csrf: str = Form(""), db: AsyncSession = Depends(get_db)):
    _check_csrf(request, csrf)
    response = RedirectResponse("/", status_code=303)
    await end_session(db, request, response)
    return response


@router.get("/consent")
async def consent(request: Request):
    return _render(request, "consent.html")


# --- анкета родителя -------------------------------------------------------


@router.get("/profile")
async def profile_form(request: Request, account: Account = Depends(require_account)):
    return _render(request, "profile.html", account=account, errors={}, form={})


@router.post("/profile")
async def profile_save(
    request: Request,
    csrf: str = Form(""),
    full_name: str = Form(""),
    phone: str = Form(""),
    email: str = Form(""),
    consent: str = Form(""),
    account: Account = Depends(require_account),
    db: AsyncSession = Depends(get_db),
):
    _check_csrf(request, csrf)
    form = {"full_name": clean(full_name, 200), "phone": phone.strip(), "email": email.strip()}
    errors = {}
    if len(form["full_name"].split()) < 2:
        errors["full_name"] = "Укажите фамилию и имя"
    norm_phone = normalize_phone(phone)
    if norm_phone is None:
        errors["phone"] = "Номер мобильного телефона, например +7 900 123-45-67"
    if not valid_email(form["email"]):
        errors["email"] = "Проверьте адрес почты"
    if account.consent_at is None and consent != "yes":
        errors["consent"] = "Без согласия мы не можем зарегистрировать участника"
    if errors:
        return _render(request, "profile.html", account=account, errors=errors, form=form)

    account = await db.merge(account)
    account.full_name = form["full_name"]
    account.phone = norm_phone
    account.email = form["email"].lower()
    if account.consent_at is None:
        account.consent_at = utcnow()
        account.consent_version = config.CONSENT_VERSION
        account.consent_ip = request.headers.get("x-real-ip") or (
            request.client.host if request.client else None
        )
    _enqueue(db, "account", account.id, "upsert_contact")
    await db.commit()
    return RedirectResponse("/cabinet", status_code=303)


# --- кабинет ---------------------------------------------------------------


@router.get("/cabinet")
async def cabinet(request: Request, account: Account = Depends(require_account)):
    if account.consent_at is None:
        return RedirectResponse("/profile", status_code=303)
    return _render(
        request,
        "cabinet.html",
        account=account,
        providers={link.provider: link for link in account.links},
        qual=qual_state(),
        editable=children_editable(),
        opens_at=config.QUAL_OPENS_AT,
        closes_at=config.QUAL_CLOSES_AT,
        linked=request.query_params.get("linked"),
        error=request.query_params.get("error"),
    )


@router.post("/cabinet/link/telegram")
async def link_telegram(
    request: Request, csrf: str = Form(""), account: Account = Depends(require_account)
):
    _check_csrf(request, csrf)
    response = _render(request, "link_telegram.html", account=account)
    response.set_cookie(
        LINK_INTENT_COOKIE,
        csrf,
        max_age=600,
        httponly=True,
        secure=config.COOKIE_SECURE,
        samesite="lax",
    )
    return response


# --- дети ------------------------------------------------------------------


def _own_participant(account: Account, participant_id: int) -> Participant:
    for p in account.participants:
        if p.id == participant_id:
            return p
    raise HTTPException(404)


def _child_form(
    last_name: str, first_name: str, middle_name: str, grade: str, city: str, school: str
) -> tuple[dict, dict]:
    form = {
        "last_name": clean(last_name, 100),
        "first_name": clean(first_name, 100),
        "middle_name": clean(middle_name, 100),
        "grade": grade,
        "city": clean(city, 100),
        "school": clean(school, 200),
    }
    errors = {}
    for field, label in (("last_name", "фамилию"), ("first_name", "имя"), ("city", "город")):
        if not form[field]:
            errors[field] = f"Укажите {label}"
    if not form["school"]:
        errors["school"] = "Укажите школу, например «Лицей № 131»"
    if not grade.isdigit() or int(grade) not in config.GRADES:
        errors["grade"] = "Выберите класс"
    return form, errors


@router.get("/children/new")
async def child_new_form(request: Request, account: Account = Depends(require_account)):
    if account.consent_at is None:
        return RedirectResponse("/profile", status_code=303)
    if not children_editable():
        return RedirectResponse("/cabinet", status_code=303)
    return _render(request, "child_form.html", child=None, form={}, errors={}, grades=config.GRADES)


@router.post("/children/new")
async def child_create(
    request: Request,
    csrf: str = Form(""),
    last_name: str = Form(""),
    first_name: str = Form(""),
    middle_name: str = Form(""),
    grade: str = Form(""),
    city: str = Form(""),
    school: str = Form(""),
    account: Account = Depends(require_account),
    db: AsyncSession = Depends(get_db),
):
    _check_csrf(request, csrf)
    if account.consent_at is None or not children_editable():
        raise HTTPException(403)
    form, errors = _child_form(last_name, first_name, middle_name, grade, city, school)
    if errors:
        return _render(
            request, "child_form.html", child=None, form=form, errors=errors, grades=config.GRADES
        )
    code = participant_code()
    while (await db.execute(select(Participant.id).where(Participant.code == code))).first():
        code = participant_code()
    child = Participant(
        account_id=account.id,
        code=code,
        last_name=form["last_name"],
        first_name=form["first_name"],
        middle_name=form["middle_name"] or None,
        grade=int(form["grade"]),
        city=form["city"],
        school=form["school"],
    )
    db.add(child)
    await db.flush()
    _enqueue(db, "participant", child.id, "upsert_deal")
    await db.commit()
    return RedirectResponse("/cabinet", status_code=303)


@router.get("/children/{participant_id}/edit")
async def child_edit_form(
    request: Request, participant_id: int, account: Account = Depends(require_account)
):
    child = _own_participant(account, participant_id)
    if not children_editable():
        return RedirectResponse("/cabinet", status_code=303)
    form = {
        "last_name": child.last_name,
        "first_name": child.first_name,
        "middle_name": child.middle_name or "",
        "grade": str(child.grade),
        "city": child.city,
        "school": child.school,
    }
    return _render(
        request, "child_form.html", child=child, form=form, errors={}, grades=config.GRADES
    )


@router.post("/children/{participant_id}/edit")
async def child_update(
    request: Request,
    participant_id: int,
    csrf: str = Form(""),
    last_name: str = Form(""),
    first_name: str = Form(""),
    middle_name: str = Form(""),
    grade: str = Form(""),
    city: str = Form(""),
    school: str = Form(""),
    account: Account = Depends(require_account),
    db: AsyncSession = Depends(get_db),
):
    _check_csrf(request, csrf)
    child = _own_participant(account, participant_id)
    if not children_editable():
        raise HTTPException(403)
    form, errors = _child_form(last_name, first_name, middle_name, grade, city, school)
    if errors:
        return _render(
            request, "child_form.html", child=child, form=form, errors=errors, grades=config.GRADES
        )
    child = await db.merge(child)
    child.last_name = form["last_name"]
    child.first_name = form["first_name"]
    child.middle_name = form["middle_name"] or None
    child.grade = int(form["grade"])
    child.city = form["city"]
    child.school = form["school"]
    _enqueue(db, "participant", child.id, "upsert_deal")
    await db.commit()
    return RedirectResponse("/cabinet", status_code=303)


# --- тест ------------------------------------------------------------------


@router.get("/test/{participant_id}")
async def start_test(participant_id: int, account: Account = Depends(require_account)):
    child = _own_participant(account, participant_id)
    url_template = config.FORM_URLS.get(child.grade)
    if qual_state() != "open" or not url_template:
        return RedirectResponse("/cabinet", status_code=303)
    return RedirectResponse(url_template.replace("{code}", child.code), status_code=303)
