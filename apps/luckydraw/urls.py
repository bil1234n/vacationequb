from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import CommunitySummaryView, CrowdfundingViewSet, GiveawayViewSet, MyDonationsViewSet, MyWinsViewSet

router = DefaultRouter()
router.register("crowdfunding", CrowdfundingViewSet, basename="crowdfunding")
router.register("my-donations", MyDonationsViewSet, basename="my-donation")
router.register("giveaways", GiveawayViewSet, basename="giveaway")
router.register("my-wins", MyWinsViewSet, basename="my-win")
urlpatterns = [path("summary/", CommunitySummaryView.as_view(), name="community-summary"), *router.urls]
