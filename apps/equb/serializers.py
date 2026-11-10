import re
from decimal import Decimal

from rest_framework import serializers


from . import terms as terms_mod
from . import standing as standing_mod
from .models import (Contribution, EqubGroup, EqubMembership, EqubPackage, EqubPayment, EqubPayout, EqubRound,
                     EqubSettings, GroupEqub)
from .services import money


class EqubRoundSerializer(serializers.ModelSerializer):
    seed_commitment = serializers.CharField(source="draw.seed_commitment", read_only=True, default=None)
    winners = serializers.SerializerMethodField()
    paid_count = serializers.SerializerMethodField()
    members_count = serializers.SerializerMethodField()
    eligible_count = serializers.SerializerMethodField()
    has_draw = serializers.SerializerMethodField()

    class Meta:
        model = EqubRound
        fields = ["id", "number", "due_date", "draw_date", "status", "expected_amount", "collected_amount",
                  "paid_count", "members_count", "eligible_count", "seed_commitment", "draw_note", "winners", "has_draw",
                  "extra_draw"]

    def get_has_draw(self, obj) -> bool:
        """False for payment-only rounds (when "Rounds drawn" is lower than the number of payments)."""
        from .services import is_draw_round

        return is_draw_round(obj)

    def get_winners(self, obj) -> list[dict]:
        from .services import public_name

        request = self.context.get("request")
        viewer = getattr(request, "user", None) if request else None
        return [{"name": public_name(p.membership, viewer) if viewer is not None else f"Member {p.membership.position}",
                 "position": p.membership.position, "amount": str(p.amount),
                 "is_me": bool(viewer is not None and viewer.is_authenticated and p.membership.user_id == viewer.id)}
                for p in obj.payouts.select_related("membership__user")]

    def get_paid_count(self, obj) -> int:
        return obj.contributions.filter(status__in=Contribution.SETTLED).count()

    def get_members_count(self, obj) -> int:
        return obj.contributions.count()

    def get_eligible_count(self, obj) -> int | None:
        draw = getattr(obj, "draw", None)
        return len(draw.eligible_memberships) if draw and draw.is_executed else None


class EqubPackageRefSerializer(serializers.ModelSerializer):
    class Meta:
        model = EqubPackage
        fields = ["id", "name", "slug"]


class EqubPackageSerializer(serializers.ModelSerializer):
    """An Equb package with its groups that are open for members."""

    open_groups = serializers.IntegerField(read_only=True, default=0)
    terms = serializers.SerializerMethodField()
    summary = serializers.SerializerMethodField()

    class Meta:
        model = EqubPackage
        fields = ["id", "name", "slug", "open_groups", "terms", "summary"]

    def get_terms(self, obj) -> list[str]:
        return [line.strip() for line in (obj.terms_content or "").splitlines() if line.strip()]

    def get_summary(self, obj) -> dict:
        """What the package's groups look like (for the package card): durations, frequencies, amount range."""
        groups = [g for g in obj.groups.all() if g.status in ("OPEN", "ACTIVE")]
        amounts = [g.contribution_amount for g in groups]
        freqs, durations = [], []
        for g in sorted(groups, key=lambda g: g.frequency_days):
            if g.get_frequency_display() not in freqs:
                freqs.append(g.get_frequency_display())
            if g.duration_label and g.duration_label not in durations:
                durations.append(g.duration_label)
        return {"groups": len(groups), "frequencies": freqs, "durations": durations,
                "min_amount": str(min(amounts)) if amounts else None, "max_amount": str(max(amounts)) if amounts else None}


class EqubGroupListSerializer(serializers.ModelSerializer):
    equb_package = EqubPackageRefSerializer(read_only=True)
    members_count = serializers.IntegerField(read_only=True)
    pot_per_round = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)
    payout_per_winner = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)
    total_rounds = serializers.IntegerField(read_only=True)
    frequency_display = serializers.CharField(source="get_frequency_display", read_only=True)
    spots_left = serializers.SerializerMethodField()
    duration_label = serializers.CharField(read_only=True)

    class Meta:
        model = EqubGroup
        fields = ["id", "equb_package", "name", "slug", "contribution_amount", "frequency", "frequency_days",
                  "frequency_display", "duration_value", "duration_unit", "duration_label", "max_members",
                  "members_count", "spots_left", "pot_per_round", "payout_per_winner", "total_rounds", "current_round",
                  "status", "start_date", "end_date", "draw_type", "rounds_drawn", "winners_per_round", "price_per_person"]

    def get_spots_left(self, obj) -> int:
        return max(obj.max_members - obj.members_count, 0)


class MyMembershipSerializer(serializers.ModelSerializer):
    class Meta:
        model = EqubMembership
        fields = ["id", "position", "status", "has_won", "total_paid", "missed_count", "prepaid_credit"]


def rules_summary(g: EqubGroup, rules: EqubSettings) -> list[str]:
    total = g.total_rounds
    if g.rounds_drawn and g.rounds_drawn < total:
        out = [f"Pay {money(g.contribution_amount)} ETB {g.every} for {total} rounds. There are {g.rounds_drawn} draw"
               f"{'s' if g.rounds_drawn > 1 else ''}, spread over the rounds and ending in the last one – the other "
               "rounds only collect payments."]
    else:
        out = [f"Pay {money(g.contribution_amount)} ETB {g.every} for {total} rounds – one draw each round."]
    if Decimal(g.late_fee):
        out.append(f"You have {g.grace_days} days' grace after each due date; after that a late fee of "
                   f"{money(g.late_fee)} ETB is added.")
    else:
        out.append(f"You have {g.grace_days} days' grace after each due date.")
    if g.require_paid_to_draw:
        out.append("Only members who have paid the round (and nothing older) enter its draw.")
    out.append(f"{rules.block_at} unpaid rounds keep you out of draws until you catch up; "
               f"{rules.suspend_at} unpaid rounds can lead to your place being cancelled.")
    if g.draw_type == EqubGroup.DrawType.MANUAL:
        out.append("vacation Travel runs each round's draw on the draw date and announces the winner in the app.")
    else:
        out.append("A provably-fair random draw runs automatically on each draw date – you can verify it in the app.")
    basis = ("guaranteed by vacation Travel" if g.payout_basis == EqubGroup.PayoutBasis.FULL_POT
             else "from what was collected by draw day")
    if g.payment_rounds:
        out.append(f"Each winner receives the price per person – {money(g.price_per_person)} ETB – "
                   f"less the {g.service_fee_percent.normalize()}% service fee.")
    else:
        out.append(f"Every member wins once. Each winner receives about {money(g.payout_per_winner)} ETB ({basis}), "
                   f"after a {g.service_fee_percent.normalize()}% service fee.")
    out.append(f"You can pay up to {rules.max_advance_rounds} rounds ahead; early payments are used automatically.")
    out.append("Before the Equb starts you can leave freely. Once it is running, leaving needs approval from vacation Travel.")
    out += [line.strip() for line in (g.rules_text or "").splitlines() if line.strip()]
    return out


def terms_text(g: EqubGroup) -> str:
    """The group's own Terms Content (falls back to its package's terms)."""
    return g.rules_text or (g.equb_package.terms_content if g.equb_package_id else "") or ""


def terms_lines(g: EqubGroup) -> list[str]:
    """One plain line per term (older app versions)."""
    return terms_mod.lines(terms_text(g))


def terms_document(g: EqubGroup) -> dict:
    """The agreement as formatted blocks for the app's reader."""
    return terms_mod.document(terms_text(g))


class EqubGroupDetailSerializer(EqubGroupListSerializer):
    current_round_detail = serializers.SerializerMethodField()
    my_membership = serializers.SerializerMethodField()
    my_current_contribution = serializers.SerializerMethodField()
    my_standing = serializers.SerializerMethodField()
    organizer_name = serializers.CharField(source="organizer.full_name", read_only=True, default=None)
    rules_summary = serializers.SerializerMethodField()
    terms_content = serializers.SerializerMethodField()
    terms_document = serializers.SerializerMethodField()
    rejoin = serializers.SerializerMethodField()
    join_code = serializers.SerializerMethodField()
    payout_basis_display = serializers.CharField(source="get_payout_basis_display", read_only=True)

    class Meta(EqubGroupListSerializer.Meta):
        fields = EqubGroupListSerializer.Meta.fields + [
            "service_fee_percent", "late_fee", "grace_days",
            "require_paid_to_draw", "payout_basis", "payout_basis_display", "organizer_name", "rules_summary", "terms_content", "terms_document", "rejoin",
            "join_code", "current_round_detail", "my_membership", "my_current_contribution", "my_standing",
        ]

    def _current(self, obj):
        if not hasattr(self, "_cur"):
            self._cur = obj.rounds.select_related("draw").filter(number=obj.current_round).first() if obj.current_round else None
        return self._cur

    def get_current_round_detail(self, obj) -> dict | None:
        r = self._current(obj)
        return EqubRoundSerializer(r, context=self.context).data if r else None

    def _me(self, obj):
        if not hasattr(self, "_mine"):
            request = self.context.get("request")
            user = getattr(request, "user", None)
            self._mine = (obj.memberships.filter(user=user, dependent_name="").exclude(status=EqubMembership.Status.LEFT).first()
                          if user and user.is_authenticated else None)
        return self._mine

    def get_my_membership(self, obj) -> dict | None:
        m = self._me(obj)
        return MyMembershipSerializer(m).data if m else None

    def get_my_current_contribution(self, obj) -> dict | None:
        m, r = self._me(obj), self._current(obj)
        if not (m and r):
            return None
        c = r.contributions.filter(membership=m).first()
        return ContributionSerializer(c).data if c else None

    def get_my_standing(self, obj) -> dict | None:
        m = self._me(obj)
        return standing_mod.standing_for(m).as_dict() if m else None

    def get_rules_summary(self, obj) -> list[str]:
        return rules_summary(obj, self.context.get("rules") or EqubSettings.load())

    def get_terms_content(self, obj) -> list[str]:
        return terms_lines(obj)

    def get_terms_document(self, obj) -> dict:
        return terms_document(obj)

    def get_rejoin(self, obj) -> dict | None:
        """Set when I was removed from (or left) this Equb after it started: I must ask to join again."""
        from .operations import rejoin_info

        request = self.context.get("request")
        return rejoin_info(request.user, obj) if request else None

    def get_join_code(self, obj) -> str | None:
        request = self.context.get("request")
        user = getattr(request, "user", None)
        if user and user.is_authenticated and (user.is_staff or self._me(obj)):
            return obj.join_code
        return None


class EqubGroupWriteSerializer(serializers.ModelSerializer):
    class Meta:
        model = EqubGroup
        fields = ["equb_package", "name", "contribution_amount", "frequency_days", "max_members", "rounds_drawn", "duration_value",
                  "duration_unit", "start_date", "end_date", "rules_text", "status", "draw_type"]
        extra_kwargs = {"frequency_days": {"required": False}}  # set from the Duration Unit


class ContributionSerializer(serializers.ModelSerializer):
    round_number = serializers.IntegerField(source="round.number", read_only=True)
    group = serializers.CharField(source="round.group.slug", read_only=True)
    group_name = serializers.CharField(source="round.group.name", read_only=True)
    due_date = serializers.DateField(source="round.due_date", read_only=True)
    amount_due = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)
    receipt = serializers.CharField(source="payment_record.reference", read_only=True, default=None)

    class Meta:
        model = Contribution
        fields = ["id", "group", "group_name", "round", "round_number", "due_date", "amount", "late_fee", "amount_due",
                  "status", "paid_at", "receipt", "note"]


class MembershipSerializer(serializers.ModelSerializer):
    """List view: the pool plus this member's standing (standings are computed in bulk by the view)."""

    group = EqubGroupListSerializer(read_only=True)
    standing = serializers.SerializerMethodField()
    team_name = serializers.CharField(source="team.name", read_only=True, default=None)

    class Meta:
        model = EqubMembership
        fields = ["id", "group", "position", "status", "has_won", "total_paid", "missed_count", "prepaid_credit",
                  "exit_requested_at", "created_at", "standing", "dependent_name", "team",
                  "team_name"]

    def get_standing(self, obj) -> dict | None:
        standings = self.context.get("standings") or {}
        st = standings.get(obj.id) or standing_mod.standing_for(obj)
        return st.as_dict()


class EqubPaymentSerializer(serializers.ModelSerializer):
    method_display = serializers.CharField(source="get_method_display", read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    rounds = serializers.SerializerMethodField()
    credit_after = serializers.SerializerMethodField()
    group = serializers.CharField(source="membership.group.name", read_only=True)
    is_office = serializers.BooleanField(read_only=True)
    checkout = serializers.SerializerMethodField()

    class Meta:
        model = EqubPayment
        fields = ["id", "reference", "group", "amount", "rounds_requested", "method", "method_display", "status",
                  "status_display", "external_reference", "is_office", "paid_at", "created_at", "rounds", "credit_after",
                  "note", "checkout"]

    def get_checkout(self, obj) -> dict | None:
        """For a payment still waiting: the Chapa page to finish it (it can also be cancelled)."""
        txn = obj.transaction
        if obj.status != EqubPayment.Status.PENDING or txn is None or txn.is_final or not txn.checkout_url:
            return None
        return {"reference": str(txn.reference), "checkout_url": txn.checkout_url, "provider": txn.provider}

    def get_rounds(self, obj) -> list[int]:
        return [r["round"] for r in (obj.allocation or {}).get("rounds", [])]

    def get_credit_after(self, obj) -> str | None:
        return (obj.allocation or {}).get("credit_after")


class EqubPayoutSerializer(serializers.ModelSerializer):
    group = serializers.CharField(source="round.group.name", read_only=True)
    group_slug = serializers.CharField(source="round.group.slug", read_only=True)
    round_number = serializers.IntegerField(source="round.number", read_only=True)
    booking_reference = serializers.CharField(source="booking.reference", read_only=True, default=None)
    status_display = serializers.CharField(source="get_status_display", read_only=True)

    class Meta:
        model = EqubPayout
        fields = ["id", "membership", "group", "group_slug", "round_number", "amount", "gross_amount", "fee_amount",
                  "voucher_code", "status", "status_display", "booking_reference", "redeemed_at", "paid_out_at",
                  "paid_out_method", "created_at"]


class RedeemSerializer(serializers.Serializer):
    booking_reference = serializers.CharField(max_length=6)


class JoinByCodeSerializer(serializers.Serializer):
    code = serializers.CharField(max_length=12)


class MemberPaySerializer(serializers.Serializer):
    rounds = serializers.IntegerField(min_value=1, max_value=120)
    provider = serializers.ChoiceField(choices=[("telebirr", "telebirr"), ("chapa", "Chapa"), ("mock", "Sandbox")],
                                       required=False)
    payer_phone = serializers.CharField(required=False, allow_blank=True, max_length=16)


class ExitRequestSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=500)


class DependentSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=120)
    phone = serializers.CharField(max_length=20, required=False, allow_blank=True, default="")


class GroupEqubCreateSerializer(serializers.Serializer):
    """A customer's Group Equb: which Equb it joins, its name and type, and responsibility people."""

    equb = serializers.SlugRelatedField(slug_field="slug", queryset=EqubGroup.objects.all())
    name = serializers.CharField(max_length=120)
    kind = serializers.ChoiceField(choices=GroupEqub.Kind.choices, default=GroupEqub.Kind.FAMILY)
    description = serializers.CharField(max_length=2000, required=False, allow_blank=True, default="")
    dependents = DependentSerializer(many=True, required=False, default=list)
    invite_phones = serializers.ListField(child=serializers.CharField(max_length=20), required=False, default=list,
                                          max_length=50, help_text="Full phone numbers of customers to invite")


class InvitePhoneSerializer(serializers.Serializer):
    phone = serializers.CharField(max_length=20)


class InviteIdSerializer(serializers.Serializer):
    invite_id = serializers.IntegerField()


class RemoveMemberSerializer(serializers.Serializer):
    membership_id = serializers.IntegerField()


class ScheduledPaymentCreateSerializer(serializers.Serializer):
    membership = serializers.IntegerField()
    scheduled_for = serializers.DateTimeField()
    rounds = serializers.IntegerField(min_value=1, max_value=60, default=1)
    repeat = serializers.ChoiceField(choices=["ONCE", "EVERY_DUE"], default="ONCE")
