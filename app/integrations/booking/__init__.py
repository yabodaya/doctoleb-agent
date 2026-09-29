"""The Booking Service integration.

This package re-exports the INTERFACE only, never FakeBookingClient and never
the HTTP client VS-011 will add - exactly like app/integrations/openai/. The
agent's tools import from here, so importing a tool must not drag in demo data
or an HTTP stack. The worker imports the concrete client from
app.integrations.booking.fake explicitly, where the choice is visible.
"""

from app.integrations.booking.interface import (
    BookingClient,
    BookingError,
    ClinicInfo,
    Doctor,
    Location,
    OpeningHours,
    Service,
    Slot,
)

__all__ = [
    "BookingClient",
    "BookingError",
    "ClinicInfo",
    "Doctor",
    "Location",
    "OpeningHours",
    "Service",
    "Slot",
]
