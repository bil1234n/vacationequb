"""
Public web pages Google Play asks for:

* ``/privacy/``         – the privacy policy (link it in Play Console → App content → Privacy policy)
* ``/account/delete/``  – lets anyone ask to delete their account without the app
                          (Play Console → App content → Data safety → "Delete account URL")
"""
from django import forms
from django.conf import settings
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from . import services
from .models import User, normalize_phone


def _context(**extra):
    return {"support_email": settings.VACATION["SUPPORT_EMAIL"], "company": "Vacation Equb",
            "updated": settings.VACATION["PRIVACY_UPDATED"], "year": timezone.localdate().year, **extra}


def privacy_policy(request):
    return render(request, "accounts/privacy.html", _context())


class DeleteAccountForm(forms.Form):
    phone = forms.CharField(max_length=20, label="Phone number", widget=forms.TextInput(attrs={
        "placeholder": "09XX XXX XXX", "autocomplete": "tel", "inputmode": "tel"}))
    password = forms.CharField(label="Password", widget=forms.PasswordInput(attrs={"autocomplete": "current-password"}))
    reason = forms.CharField(min_length=5, max_length=1000, label="Why do you want to delete your account?",
                             widget=forms.Textarea(attrs={"rows": 3, "placeholder": "Tell us the reason"}))
    confirm = forms.BooleanField(label="I understand my account will be suspended and then deleted after review.")

    def clean(self):
        data = super().clean()
        phone, password = normalize_phone(data.get("phone", "")), data.get("password")
        if phone and password:
            # Checked directly (not with authenticate()) so an already-suspended account can update its reason.
            user = User.objects.filter(phone=phone).first()
            if (user is None or user.deletion_status == User.DeletionStatus.DELETED
                    or not user.check_password(password)):
                raise forms.ValidationError("The phone number or password is incorrect.")
            if user.is_staff:
                raise forms.ValidationError("Staff accounts are closed by an administrator, not from this page.")
            data["user"] = user
        return data


@require_http_methods(["GET", "POST"])
def delete_account(request):
    form = DeleteAccountForm(request.POST or None)
    done = None
    if request.method == "POST" and form.is_valid():
        user = form.cleaned_data["user"]
        services.request_deletion(user, form.cleaned_data["reason"])
        done = user
    return render(request, "accounts/delete_account.html", _context(form=form, done=done))
