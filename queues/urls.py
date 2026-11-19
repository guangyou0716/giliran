from django.urls import include, path

from .views import api, appointments, manage, public, reports, staff

staff_patterns = (
    [
        path("", staff.dashboard, name="dashboard"),
        path("counters/<int:pk>/", staff.counter_console, name="counter"),
        path("counters/<int:pk>/action/", staff.counter_action, name="counter_action"),
        path("b/<slug:slug>/walk-in/", staff.walk_in, name="walk_in"),
        path("b/<slug:slug>/tickets/", staff.ticket_history, name="history"),
        path("b/<slug:slug>/appointments/", appointments.appointments, name="appointments"),
        path("bookings/<int:pk>/check-in/", appointments.staff_check_in, name="booking_check_in"),
    ],
    "staff",
)

manage_patterns = (
    [
        path("b/<slug:slug>/", manage.settings_page, name="settings"),
        path("b/<slug:slug>/services/new/", manage.service_create, name="service_create"),
        path("b/<slug:slug>/services/<int:pk>/", manage.service_edit, name="service_edit"),
        path("b/<slug:slug>/counters/new/", manage.counter_create, name="counter_create"),
        path("b/<slug:slug>/counters/<int:pk>/", manage.counter_edit, name="counter_edit"),
        path("b/<slug:slug>/slots/", appointments.slots, name="slots"),
        path("b/<slug:slug>/slots/new/", appointments.slot_generate, name="slot_generate"),
        path("b/<slug:slug>/slots/<int:pk>/", appointments.slot_edit, name="slot_edit"),
        path("b/<slug:slug>/slots/<int:pk>/delete/", appointments.slot_delete, name="slot_delete"),
        path("b/<slug:slug>/reports/", reports.reports, name="reports"),
    ],
    "manage",
)

api_patterns = (
    [
        path("tickets/<uuid:token>/", api.ticket_status, name="ticket"),
        path("branches/<slug:slug>/board/", api.board, name="board"),
    ],
    "api",
)

urlpatterns = [
    path("", public.home, name="home"),
    path("b/<slug:slug>/join/", public.join, name="join"),
    path("b/<slug:slug>/board/", public.board, name="board"),
    path("t/<uuid:token>/", public.ticket, name="ticket"),
    path("t/<uuid:token>/cancel/", public.ticket_cancel, name="ticket_cancel"),
    path("b/<slug:slug>/book/", appointments.book, name="book"),
    path("a/<uuid:token>/", appointments.booking, name="booking"),
    path("a/<uuid:token>/cancel/", appointments.booking_cancel, name="booking_cancel"),
    path("a/<uuid:token>/check-in/", appointments.booking_check_in, name="booking_check_in"),
    path("staff/", include(staff_patterns)),
    path("manage/", include(manage_patterns)),
    path("api/", include(api_patterns)),
]
