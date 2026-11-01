from django.contrib import admin, messages

from .models import (AircraftType, Airport, BaggageOption, Booking, BookingEvent, FareClass, Flight, Passenger,
                     PassengerBaggage, Seat, Ticket, TravelPackage)


@admin.register(Airport)
class AirportAdmin(admin.ModelAdmin):
    list_display = ["iata", "name", "city", "country"]
    search_fields = ["iata", "name", "city"]


@admin.register(AircraftType)
class AircraftTypeAdmin(admin.ModelAdmin):
    list_display = ["code", "name"]


class FareClassInline(admin.TabularInline):
    model = FareClass
    extra = 1


@admin.register(Flight)
class FlightAdmin(admin.ModelAdmin):
    list_display = ["flight_number", "origin", "destination", "departure_time", "status", "aircraft", "seat_count"]
    list_filter = ["status", "is_international", "origin", "destination"]
    search_fields = ["flight_number"]
    date_hierarchy = "departure_time"
    inlines = [FareClassInline]
    actions = ["generate_seats"]

    @admin.display(description="Seats")
    def seat_count(self, obj):
        return obj.seats.count()

    @admin.action(description="Generate seat map from aircraft layout")
    def generate_seats(self, request, queryset):
        total = sum(f.generate_seats() for f in queryset)
        self.message_user(request, f"Created {total} seats.", messages.SUCCESS)

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        if not change:
            obj.generate_seats()


@admin.register(Seat)
class SeatAdmin(admin.ModelAdmin):
    list_display = ["flight", "number", "cabin", "position", "status", "surcharge", "locked_by", "locked_until"]
    list_filter = ["cabin", "position", "status"]
    search_fields = ["flight__flight_number", "number"]
    raw_id_fields = ["flight", "locked_by"]


@admin.register(BaggageOption)
class BaggageOptionAdmin(admin.ModelAdmin):
    list_display = ["code", "name", "weight_kg", "price", "international_only", "is_active"]


@admin.register(TravelPackage)
class TravelPackageAdmin(admin.ModelAdmin):
    list_display = ["name", "category", "departure_date", "price", "capacity", "seats_sold", "is_featured", "is_active"]
    list_filter = ["category", "is_featured", "is_active"]
    prepopulated_fields = {"slug": ("name",)}
    search_fields = ["name", "destination_city"]


class PassengerBaggageInline(admin.TabularInline):
    model = PassengerBaggage
    extra = 0


class PassengerInline(admin.StackedInline):
    model = Passenger
    extra = 0
    raw_id_fields = ["seat"]


class BookingEventInline(admin.TabularInline):
    model = BookingEvent
    extra = 0
    readonly_fields = ["kind", "message", "by", "created_at"]
    can_delete = False


@admin.register(Booking)
class BookingAdmin(admin.ModelAdmin):
    list_display = ["reference", "user", "booking_type", "status", "total_amount", "funding_source", "created_at"]
    list_filter = ["status", "booking_type", "funding_source"]
    search_fields = ["reference", "user__phone", "passengers__last_name", "passengers__passport_number"]
    raw_id_fields = ["user", "flight", "fare_class", "package", "cancelled_by"]
    inlines = [PassengerInline, BookingEventInline]
    readonly_fields = ["reference", "price_breakdown", "confirmed_at", "cancelled_at", "refunded_at"]


@admin.register(Ticket)
class TicketAdmin(admin.ModelAdmin):
    list_display = ["ticket_number", "booking", "passenger", "status", "checked_in_at", "boarded_at", "created_at"]
    list_filter = ["status"]
    search_fields = ["ticket_number", "booking__reference"]
    raw_id_fields = ["checked_in_by", "boarded_by"]
