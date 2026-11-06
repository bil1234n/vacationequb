"""Forms for the agency portal and for managing agencies in the Vacation Equb console."""
from __future__ import annotations

from django import forms
from django.utils import timezone

from apps.console.forms import StyledMixin

from .models import Agency, AgencyStaff, FamilyMember


class AgencyForm(StyledMixin, forms.ModelForm):
    """Console: create / edit an agency. On create, the owner's account is set up too."""

    owner_name = forms.CharField(label="Owner full name", max_length=150, required=False)
    owner_phone = forms.CharField(label="Owner phone", max_length=20, required=False,
                                  help_text="The owner signs in to the agency portal with this number.")
    owner_password = forms.CharField(label="Owner password", required=False, min_length=6,
                                     widget=forms.PasswordInput(render_value=True),
                                     help_text="At least 6 characters. Leave empty for an existing account.")
    bank_code = forms.TypedChoiceField(label="Bank", coerce=int, required=False, empty_value=None)

    sections = [
        ("Agency", "", ["name", "license_number", "tin", "phone", "email", "city", "address"]),
        ("Owner account", "The person who runs the agency. They can add their own sales agents later.",
         ["owner_name", "owner_phone", "owner_password"]),
        ("Commission & status", "Vacation Equb's cut is taken from every ticket and package the agency sells.",
         ["commission_percent", "status"]),
        ("Payout bank account", "Chapa sends the agency's share of every sale here automatically, once the Chapa "
                                "subaccount is created on the agency's page.",
         ["bank_code", "account_name", "account_number"]),
        ("Internal", "", ["notes"]),
    ]

    class Meta:
        model = Agency
        fields = ["name", "license_number", "tin", "phone", "email", "city", "address", "commission_percent",
                  "status", "bank_code", "account_name", "account_number", "notes"]
        labels = {"commission_percent": "Vacation Equb commission (%)", "account_name": "Account holder name"}

    def __init__(self, *args, banks=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.banks = {b["id"]: b["name"] for b in (banks or [])}
        choices = [("", "— choose the bank —")] + [(b["id"], b["name"]) for b in (banks or [])]
        if self.instance.bank_code and self.instance.bank_code not in self.banks:
            choices.append((self.instance.bank_code, self.instance.bank_name or f"Bank #{self.instance.bank_code}"))
        self.fields["bank_code"].choices = choices
        if not banks:
            self.fields["bank_code"].help_text = "Chapa's bank list couldn't be loaded – check the Chapa keys."
        if self.instance.pk:
            for n in ("owner_name", "owner_phone", "owner_password"):
                self.fields.pop(n)
            self.sections = [s for s in self.sections if s[0] != "Owner account"]
            if self.instance.chapa_subaccount_id:
                self.disable("bank_code", "account_name", "account_number",
                             reason="Linked to Chapa. To change it, remove the subaccount in Chapa first.")
        else:
            self.fields["owner_name"].required = self.fields["owner_phone"].required = True

    def save(self, commit=True):
        obj = super().save(commit=False)
        if obj.bank_code:
            obj.bank_name = self.banks.get(obj.bank_code, obj.bank_name)
        if commit:
            obj.save()
        return obj


class StaffForm(StyledMixin, forms.Form):
    full_name = forms.CharField(max_length=150)
    phone = forms.CharField(max_length=20, help_text="They sign in with this phone number.")
    password = forms.CharField(min_length=6, widget=forms.PasswordInput(render_value=True), required=False,
                               help_text="At least 6 characters. Leave empty if they already have an account.")
    role = forms.ChoiceField(choices=[(AgencyStaff.Role.AGENT, "Sales agent"), (AgencyStaff.Role.MANAGER, "Manager")])
    sections = [("Team member", "", ["full_name", "phone", "password", "role"])]


class CustomerForm(StyledMixin, forms.Form):
    full_name = forms.CharField(max_length=150, help_text="As written on their passport or ID.")
    phone = forms.CharField(max_length=20, help_text="Their own mobile number, e.g. 0911 234 567.")
    email = forms.EmailField(required=False)
    password = forms.CharField(required=False, min_length=6, widget=forms.PasswordInput(render_value=True),
                               help_text="Optional – lets them sign in to the Vacation Equb app too.")
    notes = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 3}))
    sections = [("Customer", "", ["full_name", "phone", "email", "password", "notes"])]


class FamilyForm(StyledMixin, forms.ModelForm):
    sections = [("Family member", "Used to fill in passenger details and to give them an Equb place that the "
                                  "customer pays for.",
                 ["first_name", "last_name", "relation", "gender", "date_of_birth", "phone", "passport_number",
                  "passport_expiry"])]

    class Meta:
        model = FamilyMember
        fields = ["first_name", "last_name", "relation", "gender", "date_of_birth", "phone", "passport_number",
                  "passport_expiry"]

    def clean_date_of_birth(self):
        d = self.cleaned_data["date_of_birth"]
        if d > timezone.localdate():
            raise forms.ValidationError("The date of birth can't be in the future.")
        return d


class TravellerForm(forms.Form):
    """One traveller on a portal booking."""

    first_name = forms.CharField(max_length=60)
    last_name = forms.CharField(max_length=60)
    gender = forms.ChoiceField(choices=[("M", "Male"), ("F", "Female")])
    date_of_birth = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"))
    passport_number = forms.CharField(max_length=32, required=False)
    passport_expiry = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"))

    def clean_date_of_birth(self):
        d = self.cleaned_data["date_of_birth"]
        if d > timezone.localdate():
            raise forms.ValidationError("Date of birth can't be in the future.")
        return d


TravellerFormSet = forms.formset_factory(TravellerForm, extra=0, min_num=1, validate_min=True, max_num=9)
