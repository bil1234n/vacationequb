"""
Group Equb (NiyaEqubBank style): a family, friends, a company team or a community joining an Equb together.

* The owner (created by) is the first member.
* Members are customers – staff add them directly, or they join in the app with the group's invite code.
* Responsibility people are relatives without the app (a child, a parent…). Each takes a place in the Equb and
  counts as a member, but the owner pays every contribution for them. Nobody has to accept anything.
"""
from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from apps.core.exceptions import BusinessRuleError
from apps.notifications.models import Notification
from apps.notifications.services import notify

from . import services
from .models import EqubGroup, EqubMembership, GroupEqub, GroupEqubInvite

MAX_OPEN_PER_OWNER = 10


def _check_open(team: GroupEqub):
    if team.equb.status != EqubGroup.Status.OPEN:
        raise BusinessRuleError("The Equb has already started – the group can no longer change.")


def _check_owner(user, team: GroupEqub):
    if team.owner_id != user.id and not user.is_staff:
        raise BusinessRuleError("Only the owner of this group can do that.")


@transaction.atomic
def create_group_equb(owner, equb: EqubGroup, *, name: str, kind: str = GroupEqub.Kind.FAMILY, description: str = "",
                      members=(), dependents=(), created_by=None, invite_phones=()) -> GroupEqub:
    """Create the group and give the owner, each member and each responsibility person a place in ``equb``."""
    equb = EqubGroup.objects.select_for_update().get(pk=equb.pk)
    name = (name or "").strip()
    if len(name) < 2:
        raise BusinessRuleError("Give the group a name.")
    if equb.status != EqubGroup.Status.OPEN:
        raise BusinessRuleError(f"{equb.name} is not accepting new members.")
    if not created_by or not created_by.is_staff:
        if GroupEqub.objects.filter(owner=owner, is_active=True, equb__status=EqubGroup.Status.OPEN).count() >= MAX_OPEN_PER_OWNER:
            raise BusinessRuleError("You already have many groups waiting to start.")
    members = [m for m in dict.fromkeys(members) if m.pk != owner.pk]
    dependents = [d for d in dependents if (d.get("name") or "").strip()]
    in_equb = set(equb.memberships.exclude(status=EqubMembership.Status.LEFT).filter(dependent_name="")
                  .values_list("user_id", flat=True))
    for m in members:
        if m.pk in in_equb:
            raise BusinessRuleError(f"{m.full_name} is already in {equb.name}.")
    needed = (0 if owner.pk in in_equb else 1) + len(members) + len(dependents)
    free = equb.max_members - equb.members_count
    if needed > free:
        raise BusinessRuleError(f"{equb.name} has {free} free place{'s' if free != 1 else ''} – this group needs {needed}.")

    team = GroupEqub.objects.create(name=name[:120], kind=kind, equb=equb, owner=owner, description=description[:2000],
                                    created_by=created_by or owner)
    if owner.pk in in_equb:
        equb.memberships.filter(user=owner, dependent_name="").update(team=team)
    else:
        services.join_group(owner, equb, team=team, auto_start=False)
    for m in members:
        services.join_group(m, equb, team=team, auto_start=False)
        notify(m, Notification.Kind.EQUB_REMINDER, f"You were added to {team.name}",
               f"{owner.full_name}'s group is saving in {equb.name}.", {"equb": equb.slug, "team": team.id})
    for d in dependents:
        services.join_group(owner, equb, team=team, dependent=d, auto_start=False, notify_member=False)
    for phone in dict.fromkeys(p for p in invite_phones if p):
        invite(owner, team, phone)
    _maybe_start(equb)
    return team


def _maybe_start(equb: EqubGroup):
    equb.refresh_from_db()
    if equb.status == EqubGroup.Status.OPEN and equb.auto_start_when_full and equb.members_count >= equb.max_members:
        services.start_group(equb)


@transaction.atomic
def add_dependent(user, team: GroupEqub, *, name: str, phone: str = "", **_ignored) -> EqubMembership:
    _check_owner(user, team)
    _check_open(team)
    m = services.join_group(team.owner, team.equb, team=team, auto_start=False, notify_member=False,
                            dependent={"name": name, "phone": phone})
    _maybe_start(team.equb)
    return m


@transaction.atomic
def add_member(user, team: GroupEqub, member) -> EqubMembership:
    _check_owner(user, team)
    _check_open(team)
    m = services.join_group(member, team.equb, team=team)
    return m


@transaction.atomic
def remove_member(user, team: GroupEqub, membership_id: int) -> EqubMembership:
    _check_owner(user, team)
    _check_open(team)
    m = team.memberships.select_related("user").filter(pk=membership_id).exclude(status=EqubMembership.Status.LEFT).first()
    if not m:
        raise BusinessRuleError("That person is not in this group.")
    if m.user_id == team.owner_id and not m.is_dependent:
        raise BusinessRuleError("The owner can't be removed. Close the group instead.")
    m.status = EqubMembership.Status.LEFT
    m.ended_at = timezone.now()
    m.save(update_fields=["status", "ended_at", "updated_at"])
    if not m.is_dependent:
        notify(m.user, Notification.Kind.EQUB_REMINDER, f"You were removed from {team.name}",
               "You were removed from the group before the Equb started. Nothing was charged.", {"equb": team.equb.slug})
    return m


@transaction.atomic
def close_group(user, team: GroupEqub) -> GroupEqub:
    """Before the Equb starts: everyone in the group leaves the Equb and the group is closed."""
    _check_owner(user, team)
    _check_open(team)
    for m in team.memberships.exclude(status=EqubMembership.Status.LEFT).select_related("user"):
        m.status = EqubMembership.Status.LEFT
        m.ended_at = timezone.now()
        m.save(update_fields=["status", "ended_at", "updated_at"])
    team.is_active = False
    team.save(update_fields=["is_active", "updated_at"])
    return team


# --- invitations: the owner asks a customer (by phone) to join; they accept and pay for themselves ---------
def _mask_name(name: str) -> str:
    parts = (name or "").split()
    return " ".join([parts[0]] + [p[0] + "." for p in parts[1:2]]) if parts else "Vacation Equb customer"


def lookup_phone(user, phone: str, team: GroupEqub | None = None) -> dict:
    """Is there a Vacation Equb customer with this full phone number? (+251912345678 or 0912345678)"""
    from apps.accounts.models import User, normalize_phone

    try:
        phone = normalize_phone(phone or "")
    except Exception:  # noqa: BLE001 – any malformed number is simply "not found"
        phone = ""
    if len(phone) < 13:
        return {"found": False, "phone": phone, "reason": "Enter the full number, e.g. +251912345678."}
    other = User.objects.filter(phone=phone, is_active=True, is_staff=False).first()
    if not other:
        return {"found": False, "phone": phone, "reason": "No Vacation Equb customer uses this number yet."}
    out = {"found": True, "phone": phone, "user_id": other.id, "name": _mask_name(other.full_name),
           "is_me": other.id == user.id, "already_in": False, "pending": False}
    if team:
        out["already_in"] = team.equb.memberships.filter(user=other, dependent_name="").exclude(
            status=EqubMembership.Status.LEFT).exists()
        out["pending"] = team.invites.filter(user=other, status=GroupEqubInvite.Status.PENDING).exists()
    return out


@transaction.atomic
def invite(user, team: GroupEqub, phone: str) -> GroupEqubInvite:
    _check_owner(user, team)
    if not team.is_active or team.equb.status not in (EqubGroup.Status.OPEN, EqubGroup.Status.ACTIVE):
        raise BusinessRuleError("This group can no longer take new members.")
    info = lookup_phone(user, phone, team)
    if not info["found"]:
        raise BusinessRuleError(info["reason"])
    if info["is_me"]:
        raise BusinessRuleError("You are already the owner of this group.")
    if info["already_in"]:
        raise BusinessRuleError(f"{info['name']} is already in {team.equb.name}.")
    if info["pending"]:
        raise BusinessRuleError(f"{info['name']} already has an invitation waiting.")
    from apps.accounts.models import User

    other = User.objects.get(pk=info["user_id"])
    inv = GroupEqubInvite.objects.create(team=team, user=other, invited_by=user)
    notify(other, Notification.Kind.GROUP_INVITE, f"{team.owner.first_name} invited you to {team.name}",
           f"Join their {team.get_kind_display().lower()} group in {team.equb.name}: "
           f"{services.money(team.equb.contribution_amount)} ETB {team.equb.every}. You pay your own place – "
           f"when the group wins, you win together.", {"invite": inv.id, "team": team.id, "equb": team.equb.slug})
    return inv


@transaction.atomic
def respond_invite(user, inv: GroupEqubInvite, accept: bool) -> GroupEqubInvite:
    inv = GroupEqubInvite.objects.select_for_update().select_related("team__equb", "team__owner").get(pk=inv.pk)
    if inv.user_id != user.id:
        raise BusinessRuleError("This invitation is not for you.")
    if inv.status != GroupEqubInvite.Status.PENDING:
        raise BusinessRuleError("This invitation was already answered or cancelled.")
    team = inv.team
    if accept:
        if not team.is_active:
            raise BusinessRuleError("This group was closed.")
        inv.membership = services.join_group(user, team.equb, team=team)
    inv.status = GroupEqubInvite.Status.ACCEPTED if accept else GroupEqubInvite.Status.DECLINED
    inv.responded_at = timezone.now()
    inv.save(update_fields=["status", "responded_at", "membership", "updated_at"])
    notify(team.owner, Notification.Kind.GROUP_INVITE,
           f"{user.first_name} {'joined' if accept else 'declined'} {team.name}",
           (f"{user.full_name} accepted and holds place #{inv.membership.position} in {team.equb.name}. They pay their own contributions."
            if accept else f"{user.full_name} won't join the group this time."), {"team": team.id})
    return inv


def cancel_invite(user, team: GroupEqub, invite_id: int) -> GroupEqubInvite:
    _check_owner(user, team)
    inv = team.invites.filter(pk=invite_id, status=GroupEqubInvite.Status.PENDING).first()
    if not inv:
        raise BusinessRuleError("That invitation is no longer waiting.")
    inv.status = GroupEqubInvite.Status.CANCELLED
    inv.responded_at = timezone.now()
    inv.save(update_fields=["status", "responded_at", "updated_at"])
    return inv


def invite_info(inv: GroupEqubInvite) -> dict:
    t, e = inv.team, inv.team.equb
    return {"id": inv.id, "status": inv.status, "created_at": inv.created_at, "team_id": t.id, "team_name": t.name,
            "kind": t.kind, "kind_display": t.get_kind_display(), "owner_name": t.owner.full_name,
            "members_count": t.live_memberships.count(), "equb_name": e.name, "equb_slug": e.slug,
            "contribution_amount": str(e.contribution_amount), "every": e.every, "frequency_days": e.frequency_days,
            "start_date": e.start_date, "equb_status": e.status}


def new_invite_code(user, team: GroupEqub) -> GroupEqub:
    _check_owner(user, team)
    team.invite_code = GroupEqub.new_code()
    team.save(update_fields=["invite_code", "updated_at"])
    return team


def group_info(team: GroupEqub, user) -> dict:
    members = list(team.memberships.exclude(status=EqubMembership.Status.LEFT).select_related("user").order_by("position"))
    is_owner = team.owner_id == getattr(user, "id", None)
    open_ = team.equb.status == EqubGroup.Status.OPEN and team.is_active

    def phone(p: str) -> str:
        return p if is_owner or getattr(user, "is_staff", False) else (p[:4] + "•••" + p[-2:] if p and len(p) > 6 else p)

    return {
        "id": team.id, "name": team.name, "kind": team.kind, "kind_display": team.get_kind_display(),
        "description": team.description, "invite_code": team.invite_code if (is_owner or any(m.user_id == getattr(user, "id", None) for m in members)) else None,
        "owner_name": team.owner.full_name, "is_owner": is_owner, "is_active": team.is_active, "can_manage": is_owner and open_,
        "members_count": len(members), "dependents_count": sum(1 for m in members if m.is_dependent),
        "equb_free_places": max(team.equb.max_members - team.equb.members_count, 0),
        "invites": [{"id": i.id, "name": _mask_name(i.user.full_name), "phone": phone(i.user.phone), "status": i.status,
                     "status_display": i.get_status_display(), "created_at": i.created_at}
                    for i in team.invites.select_related("user").exclude(status=GroupEqubInvite.Status.CANCELLED)[:50]]
        if is_owner else [],
        "members": [{"membership_id": m.id, "position": m.position, "name": m.display_name,
                     "phone": m.dependent_phone if m.is_dependent else phone(m.user.phone),
                     "is_owner": m.user_id == team.owner_id and not m.is_dependent, "is_dependent": m.is_dependent,
                     "paid_by": m.user.full_name if m.is_dependent else None,
                     "is_me": m.user_id == getattr(user, "id", None) and not m.is_dependent,
                     "status": m.status, "has_won": m.has_won, "total_paid": str(m.total_paid)} for m in members],
    }
