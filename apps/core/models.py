import uuid

from django.db import models


class TimeStampedModel(models.Model):
    """Abstract base with created/updated timestamps."""

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class UUIDTimeStampedModel(TimeStampedModel):
    uid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)

    class Meta:
        abstract = True


class Banner(TimeStampedModel):
    """A slide in the banner carousel at the top of the app's Home page (managed in the console)."""

    class Link(models.TextChoices):
        NONE = "NONE", "Nothing – just a picture"
        FLIGHTS = "FLIGHTS", "Flight search"
        EQUB = "EQUB", "Equb tab"
        EQUB_PACKAGE = "EQUB_PACKAGE", "An Equb package (slug)"
        EQUB_GROUP = "EQUB_GROUP", "An Equb group (slug)"
        TRAVEL_PACKAGE = "TRAVEL_PACKAGE", "A Hajj / Umrah / tour package (slug)"
        COMMUNITY = "COMMUNITY", "Community tab"
        CAMPAIGN = "CAMPAIGN", "A crowdfunding campaign (slug)"
        GIVEAWAY = "GIVEAWAY", "A giveaway (slug)"
        URL = "URL", "A web page (https://…)"

    title = models.CharField(max_length=80, blank=True, help_text="Big text on the slide (optional – leave empty for picture-only).")
    subtitle = models.CharField(max_length=140, blank=True)
    image = models.ImageField(upload_to="banners/", help_text="Wide picture, about 1200 × 520 px (JPG or PNG).")
    button_label = models.CharField(max_length=30, blank=True, help_text="e.g. “Join now” – shown when the slide links somewhere.")
    link_type = models.CharField(max_length=16, choices=Link.choices, default=Link.NONE)
    link_value = models.CharField(max_length=300, blank=True, help_text="The slug or web address the slide opens.")
    sort_order = models.PositiveSmallIntegerField(default=0, help_text="Lower numbers show first.")
    is_active = models.BooleanField(default=True)
    starts_at = models.DateTimeField(null=True, blank=True, help_text="Optional: show from this time.")
    ends_at = models.DateTimeField(null=True, blank=True, help_text="Optional: hide after this time.")

    class Meta:
        ordering = ["sort_order", "-created_at"]

    def __str__(self):
        return self.title or f"Banner {self.pk}"


class AppRelease(TimeStampedModel):
    """A new app version in the App Store / Play Store. The app shows an update popup when it is older."""

    class Platform(models.TextChoices):
        ANDROID = "android", "Android (Play Store)"
        IOS = "ios", "iPhone (App Store)"

    class Kind(models.TextChoices):
        NORMAL = "NORMAL", "Normal – users can choose “Later”"
        FORCE = "FORCE", "Force – the app can't be used until they update"

    platform = models.CharField(max_length=8, choices=Platform.choices)
    version = models.CharField(max_length=20, help_text="The new version number, e.g. 1.4.0 (same as in the store).")
    kind = models.CharField("Update type", max_length=6, choices=Kind.choices, default=Kind.NORMAL)
    min_supported_version = models.CharField(
        max_length=20, blank=True,
        help_text="Optional: versions older than this must update even when the type is Normal.")
    store_url = models.URLField(blank=True, help_text="Link to the app in the store. Empty = the default store link.")
    title = models.CharField(max_length=80, default="A new version is available")
    message = models.TextField(blank=True, default="Update Vacation Equb to get the latest features and fixes.")
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.get_platform_display()} {self.version}"


def version_tuple(v: str) -> tuple:
    out = []
    for part in str(v or "0").strip().lstrip("vV").split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        out.append(int(digits) if digits else 0)
    while len(out) < 3:
        out.append(0)
    return tuple(out)
