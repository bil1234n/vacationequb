"""
Wipe demo / test data to start clean.

    python manage.py reset_data              # asks for confirmation
    python manage.py reset_data --yes        # no prompt
    python manage.py reset_data --keep-catalog   # remove customers & transactions only,
                                                 # keep flights, packages, Equb pools, campaigns and giveaways

Always kept: staff accounts (so you can still sign in), airports, aircraft seat
layouts and baggage options (reference data needed to create flights).
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction


class Command(BaseCommand):
    help = "Delete all customers, bookings, payments, Equb/crowdfunding/giveaway activity (and the catalogue) for a clean start."

    def add_arguments(self, parser):
        parser.add_argument("--yes", action="store_true", help="Do not ask for confirmation")
        parser.add_argument("--keep-catalog", action="store_true",
                            help="Keep flights, packages, Equb pools, campaigns and giveaways (reset their counters)")

    def handle(self, *args, **o):
        from apps.accounts.models import User
        from apps.equb.models import (CallLog, Contribution, EqubDraw, EqubGroup, EqubMembership, EqubPackage, EqubPayment, EqubPayout,
                                      EqubRound, Termination)
        from apps.luckydraw.models import (CrowdfundingCampaign, Disbursement, Donation, Giveaway, GiveawayEntry,
                                           GiveawayParticipant, GiveawayWinner)
        from apps.notifications.models import Notification
        from apps.payments.models import PaymentTransaction, WebhookEvent
        from apps.travel.models import Booking, FareClass, Flight, Passenger, PassengerBaggage, Seat, Ticket, TravelPackage

        keep = o["keep_catalog"]
        customers = User.objects.filter(is_staff=False)
        summary = {
            "customers": customers.count(), "bookings": Booking.objects.count(), "payments": PaymentTransaction.objects.count(),
            "equb memberships": EqubMembership.objects.count(), "crowdfunding donations": Donation.objects.count(),
        }
        if not keep:
            summary |= {"flights": Flight.objects.count(), "packages": TravelPackage.objects.count(),
                        "Equb groups": EqubGroup.objects.count(), "crowdfunding campaigns": CrowdfundingCampaign.objects.count(),
                        "giveaways": Giveaway.objects.count()}
        self.stdout.write("This will permanently delete:")
        for k, v in summary.items():
            self.stdout.write(f"  • {v} {k}")
        self.stdout.write(f"Kept: {User.objects.filter(is_staff=True).count()} staff account(s), airports, aircraft layouts, baggage options.")
        if not o["yes"]:
            if input("Type 'yes' to continue: ").strip().lower() != "yes":
                raise CommandError("Cancelled — nothing was deleted.")

        with transaction.atomic():
            Notification.objects.all().delete()
            WebhookEvent.objects.all().delete()
            PaymentTransaction.objects.all().delete()
            # community finance
            GiveawayWinner.objects.all().delete()
            GiveawayEntry.objects.all().delete()
            GiveawayParticipant.objects.all().delete()
            Disbursement.objects.all().delete()
            Donation.objects.all().delete()
            # equb
            CallLog.objects.all().delete()
            Termination.objects.all().delete()
            EqubPayout.objects.all().delete()
            Contribution.objects.all().delete()
            EqubPayment.objects.all().delete()
            EqubDraw.objects.all().delete()
            EqubRound.objects.all().delete()
            EqubMembership.objects.all().delete()
            # travel
            Ticket.objects.all().delete()
            PassengerBaggage.objects.all().delete()
            Passenger.objects.all().delete()
            Booking.objects.all().delete()
            if keep:
                Seat.objects.exclude(status="BLOCKED").update(status="AVAILABLE", locked_by=None, locked_until=None)
                FareClass.objects.update(seats_sold=0)
                TravelPackage.objects.update(seats_sold=0)
                EqubGroup.objects.exclude(status="CANCELLED").update(status="OPEN", current_round=0)
                CrowdfundingCampaign.objects.update(raised_amount=0, fees_collected=0, disbursed_amount=0, donations_count=0, donors_count=0)
                CrowdfundingCampaign.objects.filter(status="FUNDED").update(status="LIVE")
                Giveaway.objects.update(entries_issued=0, participants_count=0, referrals_count=0, drawn_at=None, public_input="")
                Giveaway.objects.filter(status__in=["CLOSED", "DRAWN"]).update(status="SCHEDULED")
            else:
                CrowdfundingCampaign.objects.all().delete()
                Giveaway.objects.all().delete()
                EqubGroup.objects.all().delete()
                EqubPackage.objects.all().delete()
                TravelPackage.objects.all().delete()
                Seat.objects.all().delete()
                FareClass.objects.all().delete()
                Flight.objects.all().delete()
            User.objects.filter(is_staff=True).update(referred_by=None)
            customers.delete()

        self.stdout.write(self.style.SUCCESS("Done — the system is clean. Sign in at /console/ to add your own data."))
