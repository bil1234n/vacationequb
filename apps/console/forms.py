"""Console create/edit forms. Each form declares ``sections`` = [(title, help, [fields])] for layout."""
from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal

from django import forms
from django.conf import settings
from django.db.models import Q
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.accounts.models import User, normalize_phone, validate_phone
from apps.equb.models import EqubGroup, EqubPackage, EqubSettings, GroupEqub, duration_end
from apps.luckydraw.models import CrowdfundingCampaign, Giveaway
from apps.travel.models import AircraftType, Airport, BaggageOption, FareClass, Flight, TravelPackage


class StyledMixin:
    """Gives every widget the console look and HTML5 input types."""

    sections: list = []

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            w = field.widget
            if isinstance(field, forms.DateTimeField):
                field.widget = forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M")
                field.input_formats = ["%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"]
            elif isinstance(field, forms.DateField):
                field.widget = forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")
            elif isinstance(w, forms.Textarea):
                w.attrs.setdefault("rows", 4)
            elif isinstance(w, (forms.NumberInput,)):
                w.attrs.setdefault("step", "any")
            if isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs["class"] = "toggle"

    def disable(self, *names, reason=""):
        for n in names:
            if n in self.fields:
                self.fields[n].disabled = True
                if reason:
                    self.fields[n].help_text = reason


# ----------------------------------------------------------------- packages
class PackageForm(StyledMixin, forms.ModelForm):
    includes = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 5, "placeholder": "Umrah visa\nReturn flight ADD–JED\n4★ hotel near Haram"}),
                               help_text="One item per line.")

    sections = [
        ("Package", "What customers see first in the app.", ["name", "category", "summary", "description", "image_url"]),
        ("Destination & dates", "", ["destination_city", "destination_country", "departure_date", "return_date", "duration_days"]),
        ("Price & capacity", "Prices are in Birr.", ["price", "deposit_amount", "capacity"]),
        ("What's included", "", ["includes", "hotel_name", "hotel_stars", "outbound_flight", "return_flight"]),
        ("Visibility", "", ["is_featured", "is_active"]),
    ]

    class Meta:
        model = TravelPackage
        fields = ["name", "category", "summary", "description", "image_url", "destination_city", "destination_country",
                  "departure_date", "return_date", "duration_days", "price", "deposit_amount", "capacity", "includes",
                  "hotel_name", "hotel_stars", "outbound_flight", "return_flight", "is_featured", "is_active"]
        labels = {"image_url": "Image URL", "is_featured": "Feature on the app home screen", "is_active": "Available for booking",
                  "deposit_amount": "Deposit", "capacity": "Total places"}
        help_texts = {"summary": "One line, shown on package cards."}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            self.initial["includes"] = "\n".join(self.instance.includes or [])
        upcoming = Flight.objects.filter(departure_time__gt=timezone.now()).select_related("origin", "destination")
        for f in ("outbound_flight", "return_flight"):
            self.fields[f].queryset = upcoming
            self.fields[f].required = False

    def clean_includes(self):
        return [line.strip(" •-") for line in self.cleaned_data["includes"].splitlines() if line.strip()]

    def clean(self):
        d = super().clean()
        if d.get("departure_date") and d.get("return_date") and d["return_date"] <= d["departure_date"]:
            self.add_error("return_date", "Return must be after departure.")
        if self.instance.pk and d.get("capacity") is not None and d["capacity"] < self.instance.seats_sold:
            self.add_error("capacity", f"{self.instance.seats_sold} places are already sold.")
        if d.get("hotel_stars") and not 1 <= d["hotel_stars"] <= 5:
            self.add_error("hotel_stars", "Between 1 and 5.")
        return d


# ----------------------------------------------------------------- flights
class FlightForm(StyledMixin, forms.ModelForm):
    sections = [
        ("Flight", "", ["airline_code", "airline_name", "flight_number", "aircraft", "status"]),
        ("Route & schedule", "Times are Addis Ababa local time.", ["origin", "destination", "departure_time", "arrival_time", "is_international", "terminal", "gate"]),
    ]

    class Meta:
        model = Flight
        fields = ["airline_code", "airline_name", "flight_number", "aircraft", "status", "origin", "destination",
                  "departure_time", "arrival_time", "is_international", "terminal", "gate"]
        labels = {"aircraft": "Aircraft (seat layout)", "is_international": "International flight"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk and self.instance.seats.exists():
            self.disable("aircraft", reason="Seat map already generated; create a new flight to change aircraft.")

    def clean(self):
        d = super().clean()
        if d.get("origin") and d.get("origin") == d.get("destination"):
            self.add_error("destination", "Destination must differ from origin.")
        if d.get("departure_time") and d.get("arrival_time") and d["arrival_time"] <= d["departure_time"]:
            self.add_error("arrival_time", "Arrival must be after departure.")
        return d


class FareForm(StyledMixin, forms.ModelForm):
    class Meta:
        model = FareClass
        fields = ["cabin", "code", "name", "base_fare", "taxes", "seats_allocated", "checked_baggage_kg",
                  "cabin_baggage_kg", "change_fee", "refundable", "free_seat_selection"]
        labels = {"code": "Booking code", "name": "Fare name", "base_fare": "Base fare (ETB)", "taxes": "Taxes (ETB)",
                  "seats_allocated": "Seats", "checked_baggage_kg": "Checked bag (kg)", "cabin_baggage_kg": "Cabin bag (kg)",
                  "change_fee": "Change fee (ETB)", "refundable": "Refundable", "free_seat_selection": "Free seat selection"}


def cabin_capacity(aircraft: AircraftType) -> dict:
    cap = {}
    for block in aircraft.layout:
        n = (block["rows"][1] - block["rows"][0] + 1) * len(block["letters"].replace("|", ""))
        cap[block["cabin"]] = cap.get(block["cabin"], 0) + n
    return cap


class BaseFareFormSet(forms.BaseInlineFormSet):
    aircraft = None

    def clean(self):
        super().clean()
        if any(self.errors):
            return
        alive = [f for f in self.forms if f.cleaned_data and not f.cleaned_data.get("DELETE")]
        if not alive:
            raise ValidationError("Add at least one fare class.")
        codes = [f.cleaned_data["code"].upper() for f in alive]
        if len(codes) != len(set(codes)):
            raise ValidationError("Each fare needs a unique booking code (e.g. K, Y, J).")
        if self.aircraft:
            cap = cabin_capacity(self.aircraft)
            used = {}
            for f in alive:
                used[f.cleaned_data["cabin"]] = used.get(f.cleaned_data["cabin"], 0) + f.cleaned_data["seats_allocated"]
                if f.instance.pk and f.cleaned_data["seats_allocated"] < f.instance.seats_sold:
                    raise ValidationError(f"{f.cleaned_data['name']}: {f.instance.seats_sold} seats already sold.")
            for cabin, n in used.items():
                if cabin not in cap:
                    raise ValidationError(f"{self.aircraft.name} has no {cabin.title()} cabin.")
                if n > cap[cabin]:
                    raise ValidationError(f"{cabin.title()} fares allocate {n} seats but the aircraft has {cap[cabin]}.")


FareFormSet = forms.inlineformset_factory(Flight, FareClass, form=FareForm, formset=BaseFareFormSet, extra=0, can_delete=True)


# ----------------------------------------------------------------- equb
class EqubPackageForm(StyledMixin, forms.ModelForm):
    """Create Equb Package – exactly NiyaEqubBank's form: Name, Contribution Frequency (Days), Terms, Active."""

    sections = [("", "", ["name", "terms_content", "is_active"])]
    create_another = True

    class Meta:
        model = EqubPackage
        fields = ["name", "terms_content", "is_active"]
        labels = {"name": "Name", "terms_content": "Terms and Conditions", "is_active": "Active"}
        help_texts = {"terms_content": "", "is_active": ""}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["name"].widget.attrs.update(placeholder="e.g. Umrah, Hajj, Dubia")
        self.fields["terms_content"].widget.attrs.update(rows=16, **{"data-terms-editor": "1"})


class EqubForm(StyledMixin, forms.ModelForm):
    """Create Equb Group – NiyaEqubBank's fields plus how many members the Equb takes."""

    price_per_person = forms.CharField(
        label="Price per person", required=False, disabled=True,
        help_text="Contribution Amount × Duration Value – what each member (winners too) pays in total.")
    sections = [("", "", ["equb_package", "name", "contribution_amount", "duration_value",
                          "duration_unit", "price_per_person", "max_members", "rounds_drawn", "kyc_after_payments",
                          "service_fee_override", "start_date", "end_date", "status", "draw_type", "rules_text"])]
    create_another = True

    class Meta:
        model = EqubGroup
        fields = ["equb_package", "name", "contribution_amount", "max_members", "rounds_drawn", "kyc_after_payments",
                  "service_fee_override", "duration_value", "duration_unit", "start_date", "end_date", "rules_text",
                  "status", "draw_type"]
        labels = {"equb_package": "Equb Package", "name": "Name", "contribution_amount": "Contribution Amount",
                  "rounds_drawn": "Rounds Drawn", "kyc_after_payments": "Eligibility form after (payments)",
                  "frequency_days": "Contribution Frequency (Days)", "max_members": "Members",
                  "duration_value": "Duration Value", "duration_unit": "Duration Unit", "start_date": "Equb Start Date",
                  "end_date": "Equb End Date", "rules_text": "Terms Content", "status": "Status", "draw_type": "Draw Type"}
        help_texts = {"rules_text": "", "frequency_days": "", "draw_type": "", "max_members": "", "equb_package": "",
                      "rounds_drawn": "How many draws in total. Members ÷ draws = winners per draw (12 members, 3 draws → 4 winners each). "
                                      "If members pay more times than this, the draws are spread out and the other rounds only collect money.",
                      "duration_value": "How many rounds – members pay once per round (e.g. 7).",
                      "duration_unit": "Length of one round: Days = pay every day, Weeks = every week, Months = every month.",
                      "end_date": "Filled in from the start date and duration. You can change it."}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        pkgs = EqubPackage.objects.filter(is_active=True)
        if self.instance.pk and self.instance.equb_package_id:
            pkgs = EqubPackage.objects.filter(Q(is_active=True) | Q(pk=self.instance.equb_package_id))
        f = self.fields
        f["equb_package"].queryset = pkgs
        f["equb_package"].required = True
        f["equb_package"].empty_label = "Select an option"
        f["equb_package"].label_from_instance = lambda p: p.name
        # Choosing a package fills in its frequency and terms (console.js, data-prefill).
        f["equb_package"].widget.attrs["data-prefill"] = json.dumps(
            {str(p.pk): {k: str(v) for k, v in p.group_defaults().items()} for p in pkgs})
        for name in ("contribution_amount", "duration_value"):
            f[name].required = True
        default_fee = EqubSettings.load().service_fee_percent
        f["service_fee_override"].widget.attrs.update(min=0, max=20, step="0.01", placeholder=f"Default {default_fee:g}%")
        f["service_fee_override"].help_text = (f"Kept from each winner's payout. Leave empty to use the default "
                                               f"{default_fee:g}% from Equb rules.")
        f["max_members"].widget.attrs.update(min=2, max=10000, step=1)
        f["rounds_drawn"].widget.attrs.update(min=1, max=10000, step=1, placeholder="One draw every round")
        f["kyc_after_payments"].widget.attrs.update(min=0, max=1000, step=1, placeholder="0 = off")
        f["kyc_after_payments"].required = False
        if not self.instance.pk and not self.is_bound:
            f["kyc_after_payments"].initial = 3
        f["kyc_after_payments"].help_text = ("Members enter draws only after paying this many rounds and sending a "
                                             "photo, their national ID and monthly income (the app asks for it). "
                                             "Draws before that have no eligible members. 0 = off.")
        f["contribution_amount"].widget.attrs.update(placeholder="e.g. 70000")
        f["price_per_person"].widget.attrs.update({"data-price": "1", "readonly": True})
        inst = self.instance
        amount = self.initial.get("contribution_amount") or (inst.contribution_amount if inst.pk else None)
        value = self.initial.get("duration_value") or (inst.duration_value if inst.pk else None)
        if inst.pk and inst.payment_rounds:
            f["price_per_person"].initial = f"{inst.price_per_person:,.2f} ETB".replace(".00 ETB", " ETB")
        elif amount and value:
            f["price_per_person"].initial = f"{Decimal(amount) * int(value):,.2f} ETB".replace(".00 ETB", " ETB")
        f["duration_value"].widget.attrs.update(min=1, step=1)
        f["contribution_amount"].widget.attrs.update(min=1)
        f["draw_type"].choices = [("MANUAL", "Manual"), ("AUTOMATIC", "Automatic")]
        # Two simple states for members: not started (anyone can join, nobody pays yet) and started (join and pay).
        running = bool(self.instance.pk and self.instance.status not in ("DRAFT", "OPEN"))
        f["status"].choices = ([("ACTIVE", "Started – members join and pay"), ("COMPLETED", "Completed"),
                                ("CANCELLED", "Cancelled")] if running else
                               [("OPEN", "Not started – members can join, payments wait for the start"),
                                ("ACTIVE", "Started – members join and pay"), ("DRAFT", "Draft – hidden from the app")])
        f["rules_text"].widget.attrs.update(rows=16, **{"data-terms-editor": "1"})

    def clean(self):
        d = super().clean()
        mx, rd = d.get("max_members"), d.get("rounds_drawn")
        if mx and rd and rd > mx:
            self.add_error("rounds_drawn", "There can't be more draws than members.")
        payments = d.get("duration_value") or 0  # each member pays Duration Value times (price per person)
        if rd and payments and rd > payments:
            self.add_error("rounds_drawn", f"Members pay {payments} time{'s' if payments > 1 else ''} in this duration, "
                                           f"so there can be at most {payments} draw{'s' if payments > 1 else ''}.")
        kyc = d.get("kyc_after_payments") or 0
        d["kyc_after_payments"] = kyc
        if kyc and payments and kyc >= payments:
            self.add_error("kyc_after_payments", f"Members only pay {payments} time{'s' if payments > 1 else ''} – "
                                                 "use a smaller number, or 0 to switch the form off.")
        if self.instance.pk and mx and mx < self.instance.members_count:
            self.add_error("max_members", f"{self.instance.members_count} members have already joined.")
        start, value, unit = d.get("start_date"), d.get("duration_value"), d.get("duration_unit")
        if start and value and unit and not d.get("end_date"):
            d["end_date"] = duration_end(start, value, unit)
        elif start and d.get("end_date") and d["end_date"] <= start:
            self.add_error("end_date", "The end date must be after the start date.")
        return d


class GroupEqubForm(StyledMixin, forms.Form):
    """Create Group Equb (NiyaEqubBank style): a family, friends or a team joining an Equb together."""

    equb = forms.ModelChoiceField(queryset=EqubGroup.objects.none(), label="Which Equb is this group joining?",
                                  empty_label="Select an option",
                                  help_text="The contribution per person, the frequency and the schedule all come from this Equb.")
    name = forms.CharField(label="Group name", max_length=120,
                           help_text="Any name you like — this is what members will see.")
    kind = forms.ChoiceField(label="Type", choices=GroupEqub.Kind.choices)
    owner = forms.ModelChoiceField(queryset=User.objects.none(), label="Created by", empty_label="Select an option",
                                   help_text="This member becomes the owner and the first participant.")
    members = forms.ModelMultipleChoiceField(queryset=User.objects.none(), label="Add members", required=False,
                                             help_text="Start typing a name or phone number. Each person gets a place in "
                                                       "the Equb straight away and is notified.")
    description = forms.CharField(label="Description", required=False, widget=forms.Textarea(attrs={"rows": 3}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["equb"].queryset = (EqubGroup.objects.filter(status=EqubGroup.Status.OPEN)
                                        .select_related("equb_package").order_by("name"))
        self.fields["equb"].label_from_instance = (
            lambda g: f"{g.name} – {g.contribution_amount:,.0f} ETB {g.every} ({g.members_count}/{g.max_members} joined)")
        customers = User.objects.filter(is_staff=False, is_active=True).order_by("full_name")
        self.fields["owner"].queryset = customers
        self.fields["members"].queryset = customers
        label = lambda u: f"{u.full_name} · {u.phone}"
        self.fields["owner"].label_from_instance = label
        self.fields["members"].label_from_instance = label
        self.fields["owner"].widget.attrs.update({"data-combo": "single", "data-placeholder": "Search a customer by name or phone…"})
        self.fields["members"].widget.attrs.update({"data-combo": "multi", "data-placeholder": "Type a name or phone, then click to add…"})
        self.fields["equb"].widget.attrs.update({"data-equb-select": "1"})
        self.fields["kind"].widget = forms.RadioSelect(choices=GroupEqub.Kind.choices)
        self.fields["description"].widget.attrs.update(placeholder="What is this group saving for?")
        self.fields["name"].widget.attrs.update(placeholder="e.g. Bilal Family")

    def equb_data(self) -> str:
        return _equb_info(self.fields["equb"].queryset)

    def clean(self):
        d = super().clean()
        owner, members = d.get("owner"), d.get("members")
        if owner and members is not None:
            d["members"] = [m for m in members if m.pk != owner.pk]
        return d


class DependentForm(forms.Form):
    name = forms.CharField(max_length=120, required=False, widget=forms.TextInput(attrs={"placeholder": "Full name"}))
    phone = forms.CharField(max_length=20, required=False, widget=forms.TextInput(attrs={"placeholder": "Optional"}))


DependentFormSet = forms.formset_factory(DependentForm, extra=1, can_delete=True)


def _equb_info(groups) -> str:
    """Details of each Equb for the live summaries on the Group Equb and membership pages."""
    return json.dumps({str(g.pk): {"name": g.name, "amount": f"{g.contribution_amount:,.0f}", "amount_raw": float(g.contribution_amount),
                                   "every": g.every, "free": max(g.max_members - g.members_count, 0),
                                   "members": g.members_count, "max": g.max_members, "status": g.get_status_display(),
                                   "start": g.start_date.strftime("%d %b %Y"),
                                   "price": f"{g.price_per_person:,.0f}" if g.duration_value else ""}
                       for g in groups})


class AddMemberForm(StyledMixin, forms.Form):
    """Create Equb membership: pick one or more customers (search by name or phone), or register a new one."""

    group = forms.ModelChoiceField(queryset=EqubGroup.objects.none(), label="Equb group", empty_label="Select an option")
    customers = forms.ModelMultipleChoiceField(queryset=User.objects.none(), label="Customers", required=False)
    new_name = forms.CharField(label="Full name", max_length=150, required=False)
    new_phone = forms.CharField(label="Phone number", max_length=20, required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        f = self.fields
        f["group"].queryset = (EqubGroup.objects.filter(status__in=(EqubGroup.Status.OPEN, EqubGroup.Status.ACTIVE))
                               .select_related("equb_package").order_by("equb_package__name", "name"))
        f["group"].label_from_instance = (
            lambda g: f"{g.name} – {g.equb_package.name if g.equb_package_id else 'no package'} "
                      f"({g.members_count}/{g.max_members}{', running' if g.status == 'ACTIVE' else ''})")
        f["customers"].queryset = User.objects.filter(is_staff=False, is_active=True).order_by("full_name")
        f["customers"].label_from_instance = lambda u: f"{u.full_name} · {u.phone}"
        f["customers"].widget.attrs.update({"data-combo": "multi", "data-placeholder": "Search by name or phone, then click to add…"})
        f["new_name"].widget.attrs.update(placeholder="e.g. Ahmed Mohammed")
        f["new_phone"].widget.attrs.update(placeholder="e.g. 0911223344", inputmode="tel")

    def equb_data(self) -> str:
        return _equb_info(self.fields["group"].queryset)

    def clean(self):
        d = super().clean()
        name, phone = (d.get("new_name") or "").strip(), (d.get("new_phone") or "").strip()
        new_user = None
        if phone or name:
            if not phone:
                self.add_error("new_phone", "Enter the new customer's phone number.")
            else:
                try:
                    phone = normalize_phone(phone)
                    validate_phone(phone)
                except (ValidationError, ValueError):
                    self.add_error("new_phone", "Enter a valid phone number, e.g. 0911223344.")
                    phone = ""
                if phone:
                    new_user = User.objects.filter(phone=phone).first()
                    if not new_user and not name:
                        self.add_error("new_name", "Enter the new customer's full name.")
        people = list(d.get("customers") or [])
        if new_user and new_user not in people:
            people.append(new_user)
        d["register"] = {"phone": phone, "full_name": name} if phone and name and not new_user else None
        d["people"] = people
        if not people and not d["register"] and not self.errors:
            raise forms.ValidationError("Choose at least one customer, or register a new one.")
        g = d.get("group")
        if g and not self.errors:
            need = len(people) + (1 if d["register"] else 0)
            free = max(g.max_members - g.members_count, 0)
            if need > free:
                raise forms.ValidationError(f"{g.name} has {free} free place{'s' if free != 1 else ''} – you chose {need}.")
        return d


class CustomerForm(StyledMixin, forms.ModelForm):
    """Staff edit a customer's profile and, for customers registered at the desk, set their password."""

    new_password = forms.CharField(label="New password", required=False, min_length=6,
                                   widget=forms.PasswordInput(render_value=False, attrs={"autocomplete": "new-password"}),
                                   help_text="Leave empty to keep the current password.")
    confirm_password = forms.CharField(label="Confirm password", required=False,
                                       widget=forms.PasswordInput(render_value=False, attrs={"autocomplete": "new-password"}))
    sections = [
        ("Profile", "", ["full_name", "phone", "email", "preferred_language", "date_of_birth", "is_active"]),
        ("Travel documents", "Used to fill in passenger details when booking.",
         ["national_id", "passport_number", "passport_expiry"]),
        ("Password", "Give the customer a password so they can sign in to the app with their phone number.",
         ["new_password", "confirm_password"]),
    ]

    class Meta:
        model = User
        fields = ["full_name", "phone", "email", "preferred_language", "date_of_birth", "national_id",
                  "passport_number", "passport_expiry", "is_active"]
        labels = {"full_name": "Full name", "phone": "Phone number", "preferred_language": "App language",
                  "is_active": "Account active"}
        widgets = {"date_of_birth": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
                   "passport_expiry": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")}

    def clean_phone(self):
        phone = normalize_phone(self.cleaned_data["phone"])
        if User.objects.filter(phone=phone).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError("Another account already uses this phone number.")
        return phone

    def clean(self):
        d = super().clean()
        p1, p2 = d.get("new_password") or "", d.get("confirm_password") or ""
        if p1 or p2:
            if p1 != p2:
                self.add_error("confirm_password", "The two passwords don't match.")
            else:
                from django.contrib.auth import password_validation
                try:
                    password_validation.validate_password(p1, self.instance)
                except ValidationError as e:
                    self.add_error("new_password", e)
        return d

    def save(self, commit=True):
        user = super().save(commit=False)
        if self.cleaned_data.get("new_password"):
            user.set_password(self.cleaned_data["new_password"])
        if commit:
            user.save()
        return user


class DrawSettingsForm(StyledMixin, forms.ModelForm):
    """Rules for who can win an Equb draw (Equb draws → Draw settings)."""

    apply_draw_type = forms.ChoiceField(
        required=False, label="Change the draw type of groups",
        choices=[("", "Keep each group's own setting"), ("MANUAL", "Manual – for every group that hasn't finished"),
                 ("AUTOMATIC", "Automatic – for every group that hasn't finished")],
        help_text="Manual: staff run the draw or pick the winner. Automatic: a fair random draw runs on each draw date.")
    sections = [
        ("Who can win", "Members who don't meet these rules stay in the group but are left out of that round's draw.",
         ["require_paid_to_draw", "min_paid_rounds_to_win", "block_at"]),
        ("Draw type", "", ["apply_draw_type"]),
    ]

    class Meta:
        model = EqubSettings
        fields = ["require_paid_to_draw", "min_paid_rounds_to_win", "block_at"]
        labels = {"require_paid_to_draw": "Only members paid up for the round enter its draw",
                  "min_paid_rounds_to_win": "Paid rounds needed to win",
                  "block_at": "Leave out of draws after (unpaid rounds)"}

    def update_groups_draw_type(self) -> int:
        value = self.cleaned_data.get("apply_draw_type")
        if not value:
            return 0
        return (EqubGroup.objects.filter(status__in=["DRAFT", "OPEN", "ACTIVE"]).exclude(draw_type=value)
                .update(draw_type=value))


class EqubRulesForm(StyledMixin, forms.ModelForm):
    sections = [
        ("Money rules", "Apply to every Equb. There is no late fee – late payments only affect draw eligibility.", ["service_fee_percent", "grace_days", "payout_basis",
                                                 "require_paid_to_draw", "auto_start_when_full"]),
        ("When is a member behind?", "Counted in unpaid rounds (a round is unpaid once its due date and the pool's grace days have passed).",
         ["warn_at", "block_at", "suspend_at", "auto_flag_for_termination"]),
        ("Draws & payments", "", ["min_paid_rounds_to_win", "max_advance_rounds", "pending_payment_minutes"]),
        ("Reminders & notices", "Sent as in-app notifications and push messages.", ["reminder_days_before", "send_overdue_notices"]),
        ("Call centre & terminations", "Days are counted from the due date of the oldest unpaid round.",
         ["call_after_days", "terminate_after_days"]),
    ]

    class Meta:
        model = EqubSettings
        fields = ["service_fee_percent", "grace_days", "payout_basis", "require_paid_to_draw",
                  "auto_start_when_full", "warn_at", "block_at", "suspend_at", "auto_flag_for_termination",
                  "min_paid_rounds_to_win", "max_advance_rounds", "pending_payment_minutes", "reminder_days_before",
                  "send_overdue_notices", "call_after_days", "terminate_after_days"]
        labels = {"service_fee_percent": "Service fee (%)", "grace_days": "Grace days",
                  "payout_basis": "Winner receives", "require_paid_to_draw": "Only paid-up members enter the draw",
                  "auto_start_when_full": "Start an Equb automatically when it is full",
                  "warn_at": "Warn after (unpaid rounds)", "block_at": "Leave out of draws after (unpaid rounds)",
                  "suspend_at": "Suspend after (unpaid rounds)",
                  "auto_flag_for_termination": "Put suspended members on the termination list automatically",
                  "min_paid_rounds_to_win": "Paid rounds needed to win", "max_advance_rounds": "Rounds a member may pay ahead",
                  "pending_payment_minutes": "Cancel unpaid app payments after (minutes)",
                  "reminder_days_before": "Remind this many days before the due date",
                  "send_overdue_notices": "Notify members when they fall behind",
                  "call_after_days": "Show in the call centre after (days overdue)",
                  "terminate_after_days": "Suggest termination after (days overdue)"}

    def clean(self):
        d = super().clean()
        w, b, s_ = d.get("warn_at"), d.get("block_at"), d.get("suspend_at")
        if w and b and b < w:
            self.add_error("block_at", "Must be the same as or more than the warning level.")
        if b and s_ and s_ < b:
            self.add_error("suspend_at", "Must be the same as or more than the draw level.")
        return d


# ----------------------------------------------------------------- community finance & impact
class CrowdfundingForm(StyledMixin, forms.ModelForm):
    """Crowdfunding Hub (donations): the gift goes 100% to the cause, the platform fee is added on top."""

    sections = [
        ("Campaign", "", ["title", "category", "story", "image_url", "is_featured"]),
        ("Who is helped", "Donors see the beneficiary's name. Link their EthioVacation account to notify them of each gift.",
         ["beneficiary_name", "beneficiary", "goal_amount"]),
        ("Giving", "Every gift goes 100% to the campaign. The platform fee is a separate amount the donor pays on top "
                   "(100 ETB gift + 5% = 105 ETB at checkout).", ["platform_fee_percent", "min_donation"]),
        ("Schedule", "", ["starts_at", "ends_at", "status"]),
    ]

    class Meta:
        model = CrowdfundingCampaign
        fields = ["title", "category", "story", "image_url", "is_featured", "beneficiary_name", "beneficiary",
                  "goal_amount", "platform_fee_percent", "min_donation", "starts_at", "ends_at", "status"]
        labels = {"goal_amount": "Goal (ETB)", "platform_fee_percent": "Platform fee added on top (%)",
                  "min_donation": "Smallest gift (ETB)", "is_featured": "Feature on the app home screen",
                  "image_url": "Image URL", "beneficiary": "Beneficiary's EthioVacation account", "story": "Story"}
        widgets = {"starts_at": forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
                   "ends_at": forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M")}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        f = self.fields
        f["beneficiary"].required = False
        f["beneficiary"].queryset = User.objects.filter(is_staff=False, is_active=True).order_by("full_name")
        f["beneficiary"].label_from_instance = lambda u: f"{u.full_name} · {u.phone}"
        f["beneficiary"].widget.attrs.update({"data-combo": "single", "data-placeholder": "Search a customer (optional)…"})
        f["story"].widget.attrs.update(rows=5, placeholder="Who is being helped, and why this journey matters.")
        f["title"].widget.attrs.update(placeholder="e.g. Help Ummi Fatuma reach Umrah")
        f["status"].choices = [c for c in CrowdfundingCampaign.Status.choices if c[0] != "FUNDED" or
                               (self.instance.pk and self.instance.status == "FUNDED")]
        if not self.instance.pk:
            self.initial.update(starts_at=timezone.localtime().replace(second=0, microsecond=0), status="LIVE",
                                platform_fee_percent=Decimal(str(settings.ETHIOVACATION["CROWDFUND_FEE_PERCENT"])), min_donation=10)
        elif self.instance.donations_count:
            self.disable("platform_fee_percent", reason="Fixed after the first gift, so every donor saw the same fee.")

    def clean(self):
        d = super().clean()
        s, e = d.get("starts_at"), d.get("ends_at")
        if s and e and e <= s:
            self.add_error("ends_at", "The end must be after the start.")
        g = d.get("goal_amount")
        if g and self.instance.pk and g < self.instance.disbursed_amount:
            self.add_error("goal_amount", "The goal can't be below what was already paid out.")
        return d


class DisbursementForm(StyledMixin, forms.Form):
    amount = forms.DecimalField(label="Amount paid out (ETB)", min_value=1, max_digits=12, decimal_places=2)
    paid_to = forms.CharField(label="Paid to", max_length=150)
    method = forms.ChoiceField(label="Method", choices=[(x, x) for x in ("Bank transfer", "telebirr", "Cash",
                                                                         "Paid to travel booking")])
    reference = forms.CharField(label="Reference", max_length=80, required=False)
    paid_on = forms.DateField(label="Date", widget=forms.DateInput(attrs={"type": "date"}))
    note = forms.CharField(label="Note", max_length=255, required=False)


class GiveawayForm(StyledMixin, forms.ModelForm):
    """Community Giveaway: 100% paid by EthioVacation or a sponsor. Entry is free – earned only by inviting friends."""

    sections = [
        ("Giveaway", "", ["title", "description", "image_url", "is_featured"]),
        ("Sponsor", "Who pays for the prize. Donations are never used for giveaways.",
         ["sponsor_type", "sponsor_name", "sponsor_note", "sponsor_logo_url"]),
        ("Prize", "Winners receive this as credit on a booking or as paid Equb contributions.",
         ["prize_type", "prize_title", "prize_value", "number_of_winners", "prize_package", "prize_equb_group"]),
        ("Free entries", "Nobody pays to enter. Each person earns an entry for every N friends who register on EthioVacation "
                         "with their referral code while the giveaway is running.", ["referrals_per_entry", "max_entries_per_user"]),
        ("Schedule", "", ["starts_at", "ends_at", "draw_at", "status"]),
    ]

    class Meta:
        model = Giveaway
        fields = ["title", "description", "image_url", "is_featured", "sponsor_type", "sponsor_name", "sponsor_note",
                  "sponsor_logo_url", "prize_type", "prize_title", "prize_value", "number_of_winners", "prize_package",
                  "prize_equb_group", "referrals_per_entry", "max_entries_per_user", "starts_at", "ends_at", "draw_at", "status"]
        labels = {"prize_value": "Prize value per winner (ETB)", "referrals_per_entry": "Friends who register = 1 free entry",
                  "max_entries_per_user": "Most entries per person", "is_featured": "Feature on the app home screen",
                  "image_url": "Image URL", "sponsor_logo_url": "Sponsor logo URL", "prize_package": "Prize package",
                  "prize_equb_group": "Prize Equb group", "sponsor_type": "Paid by"}
        help_texts = {"max_entries_per_user": "Leave empty for no limit."}
        widgets = {k: forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M")
                   for k in ("starts_at", "ends_at", "draw_at")}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["prize_package"].required = False
        self.fields["prize_equb_group"].required = False
        self.fields["prize_package"].queryset = TravelPackage.objects.filter(is_active=True)
        self.fields["status"].choices = [c for c in Giveaway.Status.choices if c[0] in ("SCHEDULED", "LIVE", "CANCELLED")]
        if not self.instance.pk:
            now = timezone.localtime().replace(second=0, microsecond=0)
            self.initial.update(starts_at=now, ends_at=now + timedelta(days=21), draw_at=now + timedelta(days=21, hours=1),
                                referrals_per_entry=settings.ETHIOVACATION["REFERRALS_PER_GIVEAWAY_ENTRY"], status="LIVE",
                                sponsor_name="EthioVacation")
        elif self.instance.entries_issued:
            self.disable("prize_type", "prize_value", "number_of_winners", "referrals_per_entry", "starts_at",
                         reason="Fixed after the first entry was earned.")
        if self.instance.pk and self.instance.status in ("CLOSED", "DRAWN"):
            for fld in self.fields.values():
                fld.disabled = True

    def clean(self):
        d = super().clean()
        s, e, dr = d.get("starts_at"), d.get("ends_at"), d.get("draw_at")
        if s and e and e <= s:
            self.add_error("ends_at", "The end must be after the start.")
        if e and dr and dr < e:
            self.add_error("draw_at", "The draw must be after referrals close.")
        if d.get("prize_type") == "EQUB_CONTRIBUTION" and not d.get("prize_equb_group"):
            self.add_error("prize_equb_group", "Choose the Equb whose contributions are paid.")
        if d.get("prize_type") == "TRAVEL_PACKAGE" and not d.get("prize_package"):
            self.add_error("prize_package", "Choose the package the winner travels on.")
        return d


class AirportForm(StyledMixin, forms.ModelForm):
    sections = [("Airport", "", ["iata", "name", "city", "country", "timezone"])]

    class Meta:
        model = Airport
        fields = ["iata", "name", "city", "country", "timezone"]
        labels = {"iata": "IATA code"}
        help_texts = {"timezone": "e.g. Africa/Addis_Ababa, Asia/Dubai"}


class BaggageForm(StyledMixin, forms.ModelForm):
    sections = [("Baggage option", "", ["code", "name", "weight_kg", "price", "international_only", "is_active"])]

    class Meta:
        model = BaggageOption
        fields = ["code", "name", "weight_kg", "price", "international_only", "is_active"]
        labels = {"price": "Price (ETB)", "is_active": "Offered to customers"}


class StaffForm(StyledMixin, forms.Form):
    full_name = forms.CharField(max_length=150)
    phone = forms.CharField(max_length=20, help_text="Used to sign in, e.g. 0911223344")
    email = forms.EmailField(required=False)
    password = forms.CharField(widget=forms.PasswordInput, min_length=8)
    is_superuser = forms.BooleanField(required=False, label="Full administrator (can manage staff & raw data)")
    sections = [("Staff member", "Staff can sign in to this console.", ["full_name", "phone", "email", "password", "is_superuser"])]

    def clean_phone(self):
        phone = normalize_phone(self.cleaned_data["phone"])
        validate_phone(phone)
        if User.objects.filter(phone=phone).exists():
            raise ValidationError("An account with this phone already exists.")
        return phone

    def save(self):
        d = self.cleaned_data
        return User.objects.create_user(phone=d["phone"], password=d["password"], full_name=d["full_name"],
                                        email=d["email"], is_staff=True, is_superuser=d["is_superuser"])


# ----------------------------------------------------------------- mobile app: home banners & update popup
class BannerForm(StyledMixin, forms.ModelForm):
    sections = [
        ("Slide", "Shown in the banner carousel at the top of the app's Home page.",
         ["image", "title", "subtitle", "button_label"]),
        ("When tapped", "", ["link_type", "link_value"]),
        ("Showing", "", ["sort_order", "is_active", "starts_at", "ends_at"]),
    ]

    class Meta:
        from apps.core.models import Banner

        model = Banner
        fields = ["image", "title", "subtitle", "button_label", "link_type", "link_value", "sort_order", "is_active",
                  "starts_at", "ends_at"]
        labels = {"is_active": "Show in the app", "starts_at": "Show from", "ends_at": "Hide after",
                  "link_type": "Opens", "link_value": "Slug or web address"}

    def clean(self):
        d = super().clean()
        link, value = d.get("link_type"), (d.get("link_value") or "").strip()
        if link in ("EQUB_PACKAGE", "EQUB_GROUP", "TRAVEL_PACKAGE", "CAMPAIGN", "GIVEAWAY", "URL") and not value:
            self.add_error("link_value", "Say which one the slide should open.")
        if link == "URL" and value and not value.startswith(("https://", "http://")):
            self.add_error("link_value", "Web addresses start with https://")
        if d.get("starts_at") and d.get("ends_at") and d["ends_at"] <= d["starts_at"]:
            self.add_error("ends_at", "Must be after “Show from”.")
        return d


class AppReleaseForm(StyledMixin, forms.ModelForm):
    sections = [
        ("New version", "Add one when you publish an update to the Play Store or App Store. Phones with an older "
                        "version see the update popup the next time they open the app.",
         ["platform", "version", "kind", "min_supported_version", "store_url"]),
        ("Popup", "", ["title", "message", "is_active"]),
    ]

    class Meta:
        from apps.core.models import AppRelease

        model = AppRelease
        fields = ["platform", "version", "kind", "min_supported_version", "store_url", "title", "message", "is_active"]
        labels = {"is_active": "Show the popup", "min_supported_version": "Oldest version still allowed"}

    def clean_version(self):
        v = (self.cleaned_data.get("version") or "").strip().lstrip("vV")
        if not v or not all(p.isdigit() for p in v.split(".")):
            raise ValidationError("Use numbers like 1.4.0")
        return v

    def clean_min_supported_version(self):
        v = (self.cleaned_data.get("min_supported_version") or "").strip().lstrip("vV")
        if v and not all(p.isdigit() for p in v.split(".")):
            raise ValidationError("Use numbers like 1.2.0")
        return v


class ChanceSettingsForm(StyledMixin, forms.ModelForm):
    """Winning-chance rules (weighted automatic draw)."""

    class Meta:
        model = EqubSettings
        fields = ["weighted_draw", "chance_on_time_percent", "chance_prepaid_percent", "chance_streak_percent",
                  "chance_streak_cap_percent", "chance_late_percent", "chance_missed_percent", "chance_min_percent"]
        labels = {"weighted_draw": "Use winning chance rules",
                  "chance_on_time_percent": "Paid this round on time (+%)",
                  "chance_prepaid_percent": "Paid in advance (+%)",
                  "chance_streak_percent": "Each round on time in a row (+%)",
                  "chance_streak_cap_percent": "Streak bonus at most (+%)",
                  "chance_late_percent": "Paid this round late (−%)",
                  "chance_missed_percent": "Each earlier round still unpaid (−%)",
                  "chance_min_percent": "Lowest chance anyone can have (% of normal)"}
