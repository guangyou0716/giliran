from django.urls import include, path

from .views import api, manage, public, staff

staff_patterns = (
    [
        path("", staff.dashboard, name="dashboard"),
        path("counters/<int:pk>/", staff.counter_console, name="counter"),
        path("counters/<int:pk>/action/", staff.counter_action, name="counter_action"),
        path("b/<slug:slug>/walk-in/", staff.walk_in, name="walk_in"),
        path("b/<slug:slug>/tickets/", staff.ticket_history, name="history"),
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
    path("staff/", include(staff_patterns)),
    path("manage/", include(manage_patterns)),
    path("api/", include(api_patterns)),
]
