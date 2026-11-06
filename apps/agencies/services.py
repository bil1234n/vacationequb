"""Agency business rules: who a sale belongs to, the 5 % commission, customers, Equb places, payouts."""
from __future__ import annotations

import logging
from decimal import Decimal

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q, Sum
from django.utils import timezone

from apps.accounts.models import User, normalize_phone, validate_phone
from apps.core.exceptions import BusinessRuleError

from .models import Agency, AgencyCustomer, AgencyLedgerEntry, AgencyStaff, FamilyMember

log = logging.getLogger(__name__)
ZERO = Decimal("0")
Payout = AgencyLedgerEntry.Payout


# --------------------------------------------------------------------------- who is who
def staff_for(user) -> AgencyStaff | None:
    """The active agency membership of a signed-in user, if their agency is active."""
    if not getattr(user, "is_authenticated", False):
        return None
    st = AgencyStaff.objects.select_related("agency").filter(user=user, is_active=True).first()
    return st if st and st.agency.status != Agency.Status.SUSPENDED else None


def agency_of_customer(user) -> Agency | None:
    link = AgencyCustomer.objects.select_related("agency").filter(user=user).first()
    return link.agency if link and link.agency.is_active else None


def agency_for_payment(target, user) -> Agency | None:
    """
    Ticket and package sales to an agency's customers are agency sales (Chapa splits them).
    Equb contributions and crowdfunding gifts are not: that money is the members' pot / the cause.
    """
    from apps.travel.models import Booking

    if not isinstance(target, Booking):
        return None
    return agency_of_customer(target.user)


# --------------------------------------------------------------------------- commission ledger
def _split(agency: Agency, gross: Decimal) -> tuple[Decimal, Decimal]:
    commission = agency.commission_on(gross)
    return commission, gross - commission


def record_commission(txn) -> AgencyLedgerEntry | None:
    """Called once a gateway payment for an agency sale succeeds (idempotent)."""
    if not txn.agency_id or AgencyLedgerEntry.objects.filter(payment=txn).exists():
        return None
    agency = txn.agency
    commission, share = _split(agency, txn.amount)
    booking = txn.target if txn.purpose == "BOOKING" else None
    try:
        with transaction.atomic():
            return AgencyLedgerEntry.objects.create(
                agency=agency, kind=AgencyLedgerEntry.Kind.GATEWAY, booking=booking, payment=txn, customer=txn.user,
                gross_amount=txn.amount, commission_percent=agency.commission_percent, commission=commission,
                agency_amount=share, payout_status=Payout.SETTLED_BY_CHAPA if txn.split_settled else Payout.OWED,
                description=f"{txn.get_provider_display()} · {getattr(booking, 'reference', '')} "
                            f"{getattr(booking, 'destination_label', '')}".strip()[:200])
    except IntegrityError:  # raced with another webhook
        return None


def on_booking_saved(sender, instance, created, **kwargs):
    """Bookings paid (fully or partly) with an Equb payout / prize credit: the agency is owed its share."""
    from apps.travel.models import Booking

    if instance.status == Booking.Status.REFUNDED:
        reverse_for_refund(instance)
        return
    if instance.status != Booking.Status.CONFIRMED or instance.funding_source == Booking.Funding.DIRECT:
        return
    credit = Decimal(str((instance.price_breakdown or {}).get("credit_applied") or 0))
    if credit <= 0:
        return
    agency = agency_of_customer(instance.user)
    if not agency or AgencyLedgerEntry.objects.filter(booking=instance, kind=AgencyLedgerEntry.Kind.CREDIT).exists():
        return
    commission, share = _split(agency, credit)
    try:
        with transaction.atomic():
            AgencyLedgerEntry.objects.create(
                agency=agency, kind=AgencyLedgerEntry.Kind.CREDIT, booking=instance, customer=instance.user,
                gross_amount=credit, commission_percent=agency.commission_percent, commission=commission,
                agency_amount=share, payout_status=Payout.OWED,
                description=f"{instance.get_funding_source_display()} · {instance.reference} {instance.destination_label}"[:200])
    except IntegrityError:
        pass


def reverse_for_refund(booking) -> int:
    """A refunded booking: owed entries are cancelled so they are not paid out."""
    return AgencyLedgerEntry.objects.filter(booking=booking, payout_status=Payout.OWED).update(
        payout_status=Payout.REVERSED)


def summary(agency: Agency, since=None) -> dict:
    qs = agency.ledger.exclude(payout_status=Payout.REVERSED)
    if since:
        qs = qs.filter(created_at__gte=since)
    agg = qs.aggregate(gross=Sum("gross_amount"), commission=Sum("commission"), share=Sum("agency_amount"))
    owed = agency.ledger.filter(payout_status=Payout.OWED).aggregate(s=Sum("agency_amount"))["s"] or ZERO
    return {"gross": agg["gross"] or ZERO, "commission": agg["commission"] or ZERO, "share": agg["share"] or ZERO,
            "owed": owed, "sales": qs.count()}


@transaction.atomic
def mark_paid(entries, *, by, reference: str = "") -> int:
    ids = [e.pk for e in entries if e.payout_status == Payout.OWED]
    return AgencyLedgerEntry.objects.filter(pk__in=ids, payout_status=Payout.OWED).update(
        payout_status=Payout.PAID, paid_at=timezone.now(), paid_by=by, paid_reference=(reference or "")[:64])


# --------------------------------------------------------------------------- agencies & team
@transaction.atomic
def create_agency(*, name: str, phone: str, owner_name: str, owner_phone: str, owner_password: str = "",
                  created_by=None, **fields) -> Agency:
    agency = Agency.objects.create(name=name.strip(), phone=phone.strip(), created_by=created_by, **fields)
    add_staff(agency, full_name=owner_name, phone=owner_phone, password=owner_password, role=AgencyStaff.Role.OWNER)
    return agency


def _user_for(phone: str, full_name: str, email: str = "") -> tuple[User, bool]:
    phone = normalize_phone(phone)
    try:
        validate_phone(phone)
    except Exception as exc:  # noqa: BLE001 – Django ValidationError
        raise BusinessRuleError("Enter a valid Ethiopian mobile number, e.g. 0911 234 567.") from exc
    user = User.objects.filter(phone=phone).first()
    if user:
        return user, False
    name = (full_name or "").strip()
    if len(name) < 2:
        raise BusinessRuleError("Enter the person's full name.")
    return User.objects.create_user(phone, None, full_name=name, email=(email or "").strip()), True


@transaction.atomic
def add_staff(agency: Agency, *, full_name: str, phone: str, password: str = "", role=AgencyStaff.Role.AGENT,
              email: str = "") -> AgencyStaff:
    user, created = _user_for(phone, full_name, email)
    if user.is_staff:
        raise BusinessRuleError("This phone belongs to a Vacation Equb staff account.")
    existing = AgencyStaff.objects.filter(user=user).select_related("agency").first()
    if existing and existing.agency_id != agency.id:
        raise BusinessRuleError(f"{user.full_name} already works for {existing.agency.name}.")
    if AgencyCustomer.objects.filter(user=user).exclude(agency=agency).exists():
        raise BusinessRuleError(f"{user.full_name} is a customer of another agency.")
    if password:
        if len(password) < 6:
            raise BusinessRuleError("The password needs at least 6 characters.")
        user.set_password(password)
        user.save(update_fields=["password"])
    if existing:
        existing.role, existing.is_active = role, True
        existing.save(update_fields=["role", "is_active", "updated_at"])
        return existing
    return AgencyStaff.objects.create(agency=agency, user=user, role=role)


# --------------------------------------------------------------------------- customers & family
@transaction.atomic
def add_customer(agency: Agency, *, full_name: str, phone: str, email: str = "", password: str = "",
                 notes: str = "", added_by=None) -> tuple[AgencyCustomer, bool]:
    """Register a new customer (or link an existing app user who isn't with another agency)."""
    if not agency.is_active:
        raise BusinessRuleError("Your agency is not active yet. Contact Vacation Equb.")
    user, created = _user_for(phone, full_name, email)
    if user.is_staff or AgencyStaff.objects.filter(user=user).exists():
        raise BusinessRuleError("This phone number belongs to a staff account.")
    link = AgencyCustomer.objects.filter(user=user).select_related("agency").first()
    if link:
        if link.agency_id == agency.id:
            raise BusinessRuleError(f"{user.full_name} is already one of your customers.")
        raise BusinessRuleError(f"{user.full_name} is already registered with another agency.")
    if password:
        if len(password) < 6:
            raise BusinessRuleError("The password needs at least 6 characters.")
        if not created and user.has_usable_password():
            raise BusinessRuleError("This customer already has an app password; they can sign in with it.")
        user.set_password(password)
        user.save(update_fields=["password"])
    return AgencyCustomer.objects.create(agency=agency, user=user, added_by=added_by, notes=notes), created


def set_customer_password(customer: AgencyCustomer, password: str):
    if customer.user.has_usable_password():
        raise BusinessRuleError("This customer already set their own password.")
    if len(password or "") < 6:
        raise BusinessRuleError("The password needs at least 6 characters.")
    customer.user.set_password(password)
    customer.user.save(update_fields=["password"])


def search_customers(agency: Agency, q: str = ""):
    qs = agency.customers.select_related("user")
    if q:
        qs = qs.filter(Q(user__full_name__icontains=q) | Q(user__phone__icontains=q)
                       | Q(family__first_name__icontains=q) | Q(family__last_name__icontains=q)).distinct()
    return qs


# --------------------------------------------------------------------------- Equb places
def enrol(customer: AgencyCustomer, group, *, family_member: FamilyMember | None = None):
    """Give the customer – or one of their family (paid for by the customer) – a place in an Equb."""
    from apps.equb.services import join_group

    if not customer.agency.is_active:
        raise BusinessRuleError("Your agency is not active.")
    if family_member:
        if family_member.customer_id != customer.id:
            raise BusinessRuleError("That person is not in this customer's family.")
        return join_group(customer.user, group, dependent={"name": family_member.full_name,
                                                           "phone": family_member.phone})
    return join_group(customer.user, group)


# --------------------------------------------------------------------------- bookings
def passenger_from(*, first_name, last_name, gender, date_of_birth, passport_number="", passport_expiry=None,
                   phone="", nationality="ET") -> dict:
    """Shape a traveller for ``create_flight_booking`` / ``create_package_booking``."""
    from datetime import date

    dob = date_of_birth
    today = timezone.localdate()
    age = today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
    ptype = "INF" if age < 2 else "CHD" if age < 12 else "ADT"
    out = {"passenger_type": ptype, "first_name": first_name, "last_name": last_name, "gender": gender,
           "date_of_birth": dob, "nationality": nationality or "ET", "phone": phone or ""}
    if passport_number:
        out["passport_number"] = passport_number
    if passport_expiry:
        out["passport_expiry"] = passport_expiry
    return out


def start_checkout(booking, *, provider: str | None = None):
    """Create (or reuse) the customer's payment for a booking – Chapa by default for agency sales."""
    from apps.payments.models import PaymentTransaction
    from apps.payments.services import initiate_payment

    if not booking.is_payable:
        raise BusinessRuleError("This booking can't be paid any more (it was paid, expired or cancelled).")
    if booking.total_amount <= 0:
        raise BusinessRuleError("Nothing to pay on this booking.")
    provider = provider or ("chapa" if settings.PAYMENT_GATEWAYS.get("chapa", {}).get("SECRET_KEY")
                            else settings.VACATION["DEFAULT_PAYMENT_PROVIDER"])
    return initiate_payment(user=booking.user, target=booking, amount=booking.total_amount,
                            purpose=PaymentTransaction.Purpose.BOOKING, provider=provider,
                            payer_phone=booking.contact_phone)


# --------------------------------------------------------------------------- Chapa subaccount
def create_chapa_subaccount(agency: Agency) -> str:
    """Register the agency's bank account with Chapa so its share is paid automatically on every sale."""
    from apps.payments.gateways import GatewayError, get_gateway

    if agency.chapa_subaccount_id:
        raise BusinessRuleError("This agency already has a Chapa subaccount.")
    missing = [label for label, v in (("bank", agency.bank_code), ("account name", agency.account_name),
                                      ("account number", agency.account_number)) if not v]
    if missing:
        raise BusinessRuleError("Add the agency's " + ", ".join(missing) + " first.")
    try:
        sub_id = get_gateway("chapa").create_subaccount(
            business_name=agency.name, account_name=agency.account_name, bank_code=agency.bank_code,
            account_number=agency.account_number, split_value=agency.commission_percent / 100)
    except GatewayError as exc:
        raise BusinessRuleError(str(exc)) from exc
    agency.chapa_subaccount_id = sub_id
    agency.save(update_fields=["chapa_subaccount_id", "updated_at"])
    return sub_id


def chapa_banks() -> list[dict]:
    from apps.payments.gateways import GatewayError, get_gateway

    try:
        return get_gateway("chapa").banks()
    except GatewayError as exc:
        log.warning("Chapa bank list unavailable: %s", exc)
        return []
