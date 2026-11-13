"""Console: review members' draw eligibility forms (photo, national ID, monthly income)."""
from __future__ import annotations

from django.contrib import messages
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.equb import kyc as kyc_mod
from apps.equb.models import EqubMembership, MemberKYC

from .views import page, staff_required

IMAGES = {"photo": ("photo", "photo_type"), "id_front": ("id_front", "id_front_type"),
          "id_back": ("id_back", "id_back_type")}


@staff_required
def kyc_list(request):
    tab = request.GET.get("tab", "SUBMITTED")
    if tab not in MemberKYC.Status.values:
        tab = "SUBMITTED"
    qs = (MemberKYC.objects.filter(status=tab).select_related("user", "reviewed_by")
          .defer("photo", "id_front", "id_back").order_by("updated_at" if tab == "SUBMITTED" else "-updated_at"))
    counts = {s: MemberKYC.objects.filter(status=s).count() for s in MemberKYC.Status.values}
    return render(request, "console/kyc_list.html", {"nav": "kyc", "page": page(request, qs), "tab": tab,
                                                     "counts": counts, "tabs": MemberKYC.Status.choices})


@staff_required
def kyc_detail(request, pk):
    k = get_object_or_404(MemberKYC.objects.select_related("user", "reviewed_by").defer("photo", "id_front", "id_back"),
                          pk=pk)
    if request.method == "POST":
        approve = request.POST.get("action") == "approve"
        note = request.POST.get("note", "")
        if not approve and not note.strip():
            messages.error(request, "Tell the member what to fix (the note is sent to them).")
        else:
            kyc_mod.review(k, request.user, approve=approve, note=note)
            messages.success(request, f"Form {'approved' if approve else 'rejected – the member was asked to send it again'}.")
            return redirect("console:kyc_list")
    places = (EqubMembership.objects.filter(user=k.user).exclude(status=EqubMembership.Status.LEFT)
              .select_related("group").order_by("-created_at"))
    has_back = MemberKYC.objects.filter(pk=pk, id_back__isnull=False).exists()
    return render(request, "console/kyc_detail.html", {"nav": "kyc", "k": k, "places": places, "has_back": has_back})


@staff_required
def kyc_image(request, pk, which):
    if which not in IMAGES:
        raise Http404
    field, type_field = IMAGES[which]
    row = MemberKYC.objects.filter(pk=pk).values(field, type_field).first()
    if not row or not row[field]:
        raise Http404
    resp = HttpResponse(bytes(row[field]), content_type=row[type_field] or "image/jpeg")
    resp["Cache-Control"] = "private, no-store"
    return resp
