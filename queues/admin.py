from django.contrib import admin

from .forms import SlotForm
from .models import Booking, Branch, Counter, Membership, Service, Slot, Ticket, TicketEvent


@admin.register(Branch)
class BranchAdmin(admin.ModelAdmin):
    list_display = ["name", "slug", "is_open"]
    prepopulated_fields = {"slug": ["name"]}


@admin.register(Membership)
class MembershipAdmin(admin.ModelAdmin):
    list_display = ["user", "branch", "role"]
    list_filter = ["branch", "role"]


@admin.register(Service)
class ServiceAdmin(admin.ModelAdmin):
    list_display = ["prefix", "name", "branch", "avg_minutes", "is_active"]
    list_filter = ["branch", "is_active"]


@admin.register(Counter)
class CounterAdmin(admin.ModelAdmin):
    list_display = ["name", "branch", "is_active", "is_paused"]
    list_filter = ["branch"]


class TicketEventInline(admin.TabularInline):
    model = TicketEvent
    extra = 0
    readonly_fields = ["action", "from_status", "to_status", "counter", "actor", "note", "at"]


@admin.register(Ticket)
class TicketAdmin(admin.ModelAdmin):
    list_display = ["code", "branch", "day", "status", "priority", "counter", "created_at"]
    list_filter = ["branch", "status", "day"]
    search_fields = ["code", "nickname"]
    readonly_fields = ["token"]
    inlines = [TicketEventInline]


@admin.register(Slot)
class SlotAdmin(admin.ModelAdmin):
    form = SlotForm  # refuses a capacity below the seats already booked
    list_display = ["starts_at", "service", "capacity", "booked_count", "is_active"]
    list_filter = ["service__branch", "is_active"]
    readonly_fields = ["booked_count"]


@admin.register(Booking)
class BookingAdmin(admin.ModelAdmin):
    list_display = ["ref", "slot", "nickname", "status", "created_at"]
    list_filter = ["status", "slot__service__branch"]
    search_fields = ["ref", "nickname"]
    readonly_fields = ["token", "ref", "slot", "status", "ticket"]
