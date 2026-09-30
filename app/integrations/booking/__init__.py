"""The Booking Service integration.

This package re-exports the INTERFACE only, never FakeBookingClient, never the
stateful in-memory service VS-007 adds, and never the HTTP client VS-011 will
add - exactly like app/integrations/openai/. The agent's tools import from here,
so importing a tool must not drag in demo data or an HTTP stack. The worker
imports the concrete client from app.integrations.booking.fake or
app.integrations.booking.memory explicitly, where the choice is visible.
"""

from app.integrations.booking.interface import (
    Appointment,
    AppointmentStatus,
    BookingClient,
    BookingError,
    ClinicInfo,
    Doctor,
    Hold,
    Location,
    OpeningHours,
    PatientBookingClient,
    PatientRef,
    Service,
    Slot,
)

__all__ = [
    "Appointment",
    "AppointmentStatus",
    "BookingClient",
    "BookingError",
    "ClinicInfo",
    "Doctor",
    "Hold",
    "Location",
    "OpeningHours",
    "PatientBookingClient",
    "PatientRef",
    "Service",
    "Slot",
]
