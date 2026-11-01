"""
Periodic housekeeping. Run every minute from cron / systemd timer / Kubernetes CronJob:

    * * * * *  cd /srv/rihla/backend && python manage.py run_scheduler

(or with ``--loop`` as a long-running worker container).
"""
import time

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.core.exceptions import BusinessRuleError


class Command(BaseCommand):
    help = ("Expire seat holds & unpaid bookings, send departure reminders, flag late Equb contributions, send Equb "
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
        escalation = escalate_standings(rules)
        out.update(equb_reminders=send_due_reminders(rules), standing_notices=escalation["notified"],
                   flagged_for_termination=escalation["flagged"], equb_draws=0, giveaways_drawn=0)
        now = timezone.now()
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
