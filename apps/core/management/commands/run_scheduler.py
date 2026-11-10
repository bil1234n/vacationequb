"""
Periodic housekeeping. Run every minute from cron / systemd timer / Kubernetes CronJob:

    * * * * *  cd /srv/vacation/backend && python manage.py run_scheduler

(or with ``--loop`` as a long-running worker container).
"""
import time

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.core.exceptions import BusinessRuleError


class Command(BaseCommand):
    help = ("Send calendar (scheduled) Equb payments, expire seat holds & unpaid bookings, send departure reminders, flag late Equb contributions, send Equb "
            "reminders and standing notices, run due draws.")

    def add_arguments(self, parser):
        parser.add_argument("--loop", action="store_true", help="Run forever, every --interval seconds")
        parser.add_argument("--interval", type=int, default=60)
        parser.add_argument("--no-draws", action="store_true", help="Skip automatic draws")

    def handle(self, *args, **opts):
        while True:
            self.tick(run_draws=not opts["no_draws"])
            if not opts["loop"]:
                break
            time.sleep(opts["interval"])

    def tick(self, run_draws=True):
        from apps.equb.models import EqubRound, EqubSettings
        from apps.equb.operations import escalate_standings, send_due_reminders
        from apps.equb.services import run_draw, sweep_late_contributions
        from apps.luckydraw.services import run_schedule as community_schedule
        from apps.travel.services.bookings import expire_unpaid_bookings
        from apps.travel.services.operations import send_trip_reminders
        from apps.travel.services.seats import expire_stale_holds

        rules = EqubSettings.load()
        out = {"bookings_expired": expire_unpaid_bookings(), "holds_released": expire_stale_holds(),
               "trip_reminders": send_trip_reminders(), "late_contributions": sweep_late_contributions()}
        from apps.equb.scheduled import dispatch_due as send_scheduled_payments

        from apps.equb.services import expire_pending_payments

        from apps.equb.models import EqubGroup
        from apps.equb.services import reopen_if_short

        for g in EqubGroup.objects.filter(status=EqubGroup.Status.COMPLETED):
            reopen_if_short(g)  # finished early, or while members were still waiting to win
        out["scheduled_payments_sent"] = send_scheduled_payments()
        out["pending_payments_cancelled"] = expire_pending_payments(rules)
        escalation = escalate_standings(rules)
        out.update(equb_reminders=send_due_reminders(rules), standing_notices=escalation["notified"],
                   flagged_for_termination=escalation["flagged"], equb_draws=0, giveaways_drawn=0)
        now = timezone.now()
        # Payment-only rounds (everyone has won) close on their draw day whatever the draw type.
        from apps.equb.models import EqubMembership
        from apps.equb.services import close_payment_round, is_draw_round

        out["payment_rounds_closed"] = 0
        for rnd in EqubRound.objects.filter(status="COLLECTING", draw_date__lte=timezone.localdate()).select_related("group"):
            if rnd.group.current_round != rnd.number:
                continue
            if not rnd.group.memberships.filter(status=EqubMembership.Status.ACTIVE, has_won=False).exists():
                close_payment_round(rnd)
                out["payment_rounds_closed"] += 1
            elif not is_draw_round(rnd):  # not one of the "Rounds drawn": it only collects money
                close_payment_round(rnd, "Payment-only round – no draw this round")
                out["payment_rounds_closed"] += 1
        if run_draws:
            for rnd in EqubRound.objects.filter(status="COLLECTING", draw_date__lte=timezone.localdate(),
                                                  group__draw_type="AUTOMATIC"):
                try:
                    run_draw(rnd)
                    out["equb_draws"] += 1
                except BusinessRuleError as exc:
                    self.stderr.write(f"Equb {rnd}: {exc.detail}")
            out.update(community_schedule(now))
        self.stdout.write(f"[{now:%H:%M:%S}] " + ", ".join(f"{k}={v}" for k, v in out.items()))
