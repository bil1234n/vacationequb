from rest_framework.routers import DefaultRouter

from .views import EqubGroupViewSet, GroupEqubViewSet, GroupInviteViewSet, EqubPackageViewSet, EqubRoundViewSet, MembershipViewSet, PayoutViewSet, ScheduledPaymentViewSet

router = DefaultRouter()
router.register("packages", EqubPackageViewSet, basename="equb-package")
router.register("groups", EqubGroupViewSet, basename="equb-group")
router.register("rounds", EqubRoundViewSet, basename="equb-round")
router.register("memberships", MembershipViewSet, basename="equb-membership")
router.register("group-equbs", GroupEqubViewSet, basename="group-equb")
router.register("group-invites", GroupInviteViewSet, basename="group-invite")
router.register("payouts", PayoutViewSet, basename="equb-payout")
router.register("scheduled-payments", ScheduledPaymentViewSet, basename="equb-scheduled-payment")
urlpatterns = router.urls
