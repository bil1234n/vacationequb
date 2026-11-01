import django_filters
from django.db.models import Q

from .models import Flight, TravelPackage


class FlightFilter(django_filters.FilterSet):
    origin = django_filters.CharFilter(field_name="origin__iata", lookup_expr="iexact")
    destination = django_filters.CharFilter(field_name="destination__iata", lookup_expr="iexact")
    date = django_filters.DateFilter(field_name="departure_time", lookup_expr="date")
    date_from = django_filters.DateFilter(field_name="departure_time", lookup_expr="date__gte")
    date_to = django_filters.DateFilter(field_name="departure_time", lookup_expr="date__lte")
    cabin = django_filters.CharFilter(method="filter_cabin")
    passengers = django_filters.NumberFilter(method="filter_capacity")
    q = django_filters.CharFilter(method="filter_q")

    class Meta:
        model = Flight
        fields = ["origin", "destination", "date", "status", "is_international"]

    def filter_cabin(self, qs, name, value):
        return qs.filter(fares__cabin=value.upper()).distinct()

    def filter_capacity(self, qs, name, value):
        # at least one fare bucket with enough seats left
        from django.db.models import F

        return qs.filter(fares__seats_allocated__gte=F("fares__seats_sold") + int(value)).distinct()

    def filter_q(self, qs, name, value):
        return qs.filter(Q(destination__city__icontains=value) | Q(destination__iata__iexact=value) |
                         Q(flight_number__iexact=value))


class PackageFilter(django_filters.FilterSet):
    min_price = django_filters.NumberFilter(field_name="price", lookup_expr="gte")
    max_price = django_filters.NumberFilter(field_name="price", lookup_expr="lte")
    departs_after = django_filters.DateFilter(field_name="departure_date", lookup_expr="gte")

    class Meta:
        model = TravelPackage
        fields = ["category", "is_featured", "destination_country"]
