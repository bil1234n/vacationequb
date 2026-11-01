from django.db import connection
from django.db.models import Sum
from django.http import HttpResponse
from django.utils import timezone
from django.views import View
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView


class HealthView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request):
        with connection.cursor() as cur:
            cur.execute("SELECT 1")
        return Response({"status": "ok", "time": timezone.now()})


class DashboardView(APIView):
    """Aggregated payload for the mobile Home Dashboard (one round-trip)."""

    permission_classes = [AllowAny]

    def get(self, request):
        from apps.equb.models import EqubGroup
        from apps.equb.serializers import EqubGroupListSerializer
        from apps.luckydraw.models import CrowdfundingCampaign, Giveaway
        from apps.luckydraw.serializers import CrowdfundingCampaignSerializer, GiveawaySerializer
        from apps.travel.models import Booking, Flight, TravelPackage
        from apps.travel.serializers import BookingListSerializer, FlightListSerializer, TravelPackageSerializer

        now = timezone.now()
        ctx = {"request": request}
        data = {
            "featured_packages": TravelPackageSerializer(
                TravelPackage.objects.filter(is_active=True, is_featured=True)[:6], many=True, context=ctx
            ).data,
            "upcoming_flights": FlightListSerializer(
                Flight.objects.filter(departure_time__gt=now, status=Flight.Status.SCHEDULED)
                .select_related("origin", "destination")
                .order_by("departure_time")[:5],
                many=True,
                context=ctx,
            ).data,
            "open_equbs": EqubGroupListSerializer(
                EqubGroup.objects.filter(status=EqubGroup.Status.OPEN)[:5], many=True, context=ctx
            ).data,
            "live_campaigns": CrowdfundingCampaignSerializer(
                CrowdfundingCampaign.objects.live().order_by("-is_featured", "-starts_at")[:3], many=True, context=ctx
            ).data,
            "live_giveaways": GiveawaySerializer(
                Giveaway.objects.live().order_by("-is_featured", "ends_at")[:3], many=True, context=ctx
            ).data,
        }
        user = request.user
        if user.is_authenticated:
            from django.db.models import DateTimeField, F
            from django.db.models.functions import Cast, Coalesce

            from apps.equb import standing as standing_mod
            from apps.equb.models import EqubMembership
            from apps.equb.serializers import MembershipSerializer
            from apps.travel.views import trip_tabs

            data["user"] = {
                "full_name": user.full_name,
                "referral_code": user.referral_code,
                "live_entries": user.giveaway_progress.filter(giveaway__status="LIVE").aggregate(s=Sum("entries"))["s"] or 0,
                "friends_invited": user.referrals.filter(is_active=True).count(),
            }
            mine = Booking.objects.filter(user=user).select_related("flight__origin", "flight__destination", "package")
            tabs = trip_tabs()
            data["my_upcoming_bookings"] = BookingListSerializer(
                mine.filter(tabs["upcoming"]).annotate(
                    dep=Coalesce(F("flight__departure_time"), Cast("package__departure_date", DateTimeField()))
                ).order_by("dep")[:3],
                many=True,
                context=ctx,
            ).data
            data["my_pending_bookings"] = BookingListSerializer(
                mine.filter(tabs["pending"]).order_by("expires_at")[:3], many=True, context=ctx).data
            # Live Equb places with standing, most urgent first (owing money, then the soonest due date).
            places = list(EqubMembership.objects.filter(user=user, status__in=EqubMembership.LIVE_STATUSES,
                                                        group__status__in=[EqubGroup.Status.OPEN, EqubGroup.Status.ACTIVE])
                          .select_related("group", "won_round").order_by("-created_at")[:10])
            standings = standing_mod.standings_for(places)
            places.sort(key=lambda m: (-float(standings[m.id].owes),
                                       standings[m.id].next_due_date or timezone.localdate().replace(year=9999)))
            data["my_equbs"] = MembershipSerializer(places[:5], many=True, context={**ctx, "standings": standings}).data
            data["unread_notifications"] = user.notifications.filter(is_read=False).count()
        return Response(data)


LANDING_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Rihla Travel API</title>
<style>
:root{--g:#0F382B;--g2:#1B4D3E;--soft:#E7F0EC;--line:#E1EAE5;--muted:#5B6F66}
*{box-sizing:border-box}body{margin:0;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#fff;color:#0D1F18}
header{background:var(--g);color:#fff;padding:48px 24px 56px;border-radius:0 0 28px 28px}
.wrap{max-width:880px;margin:0 auto}header small{letter-spacing:.15em;opacity:.7;text-transform:uppercase}
h1{font-size:34px;margin:8px 0 6px}header p{opacity:.85;margin:0}
.status{display:inline-flex;align-items:center;gap:8px;margin-top:18px;background:rgba(255,255,255,.1);padding:6px 12px;border-radius:999px;font-size:14px}
.dot{width:8px;height:8px;border-radius:50%;background:#7CD992}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:14px;margin:-28px auto 32px;padding:0 24px}
a.card{display:block;text-decoration:none;color:inherit;background:#fff;border:1px solid var(--line);border-radius:16px;padding:18px;box-shadow:0 4px 14px rgba(15,56,43,.06)}
a.card:hover{border-color:var(--g)}a.card.primary{background:#C9A227;border-color:#C9A227}a.card.primary span{color:#0F382B}a.card b{color:var(--g);font-size:17px}a.card span{display:block;color:var(--muted);font-size:14px;margin-top:4px}
code{background:var(--soft);color:var(--g);padding:2px 6px;border-radius:6px;font-size:13px}
section{padding:0 24px 40px}section h2{font-size:17px}td{padding:6px 12px 6px 0;font-size:14px}
</style></head><body>
<header><div class="wrap"><small>ሪሕላ · Rihla Travel</small><h1>Backend is running</h1>
<p>Flights &amp; packages · Travel Equb · Sadaqah crowdfunding · free giveaways</p>
<div class="status"><span class="dot"></span>Database connected · {flights} flights · {equbs} Equbs · {draws} live campaigns</div></div></header>
<div class="wrap"><div class="grid">
<a class="card primary" href="/console/"><b>Staff console →</b><span>Dashboard, charts, bookings, Equbs, draws</span></a>
<a class="card" href="/api/docs/"><b>API documentation →</b><span>Try every endpoint in Swagger UI</span></a>
<a class="card" href="/admin/"><b>Data admin →</b><span>Raw records: flights, fares, users</span></a>
<a class="card" href="/api/travel/flights/?origin=ADD&amp;destination=DXB"><b>Flight search →</b><span>Addis Ababa → Dubai, live fares</span></a>
<a class="card" href="/api/equb/groups/"><b>Equb pools →</b><span>Destination savings circles</span></a>
<a class="card" href="/api/community/crowdfunding/"><b>Crowdfunding →</b><span>Sadaqah / Hibah campaigns for travellers</span></a>
<a class="card" href="/api/community/giveaways/"><b>Giveaways →</b><span>Free sponsored trips – 10 referrals = 1 entry</span></a>
</div></div>
<section class="wrap"><h2>Demo logins</h2><table>
<tr><td>Admin</td><td><code>+251900000000</code> / <code>admin12345</code></td></tr>
<tr><td>Customer (app)</td><td><code>0911223344</code> / <code>demo12345</code></td></tr></table></section>
</body></html>"""


class LandingView(View):
    """Friendly home page at / with links to the docs, admin and key endpoints."""

    def get(self, request):
        from apps.equb.models import EqubGroup
        from apps.luckydraw.models import CrowdfundingCampaign, Giveaway
        from apps.travel.models import Flight

        html = (LANDING_HTML.replace("{flights}", str(Flight.objects.filter(departure_time__gt=timezone.now()).count()))
                .replace("{equbs}", str(EqubGroup.objects.count()))
                .replace("{draws}", str(CrowdfundingCampaign.objects.live().count() + Giveaway.objects.live().count())))
        return HttpResponse(html)
