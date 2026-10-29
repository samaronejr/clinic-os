"""Follow clinician-owned appointment progress in the reception queue."""

from apps.audit.services import record_phase1_event
from apps.realtime.transport import publish_on_commit
from apps.scheduling.models import Appointment, QueueEntry


def synchronize_queue(*, appointment: Appointment) -> None:
    """Keep queue progress in the appointment transaction and under its locks."""
    entry = QueueEntry.objects.filter(appointment_id=appointment.pk).first()
    if entry is None:
        return
    target = {
        "in_progress": QueueEntry.State.IN_ROOM,
        "completed": QueueEntry.State.DONE,
        "cancelled": QueueEntry.State.DONE,
        "no_show": QueueEntry.State.DONE,
    }.get(appointment.status)
    if target is None or entry.state == target:
        return
    entry.state = target
    entry.save(update_fields=("state",))
    record_phase1_event(
        "scheduling.queue.progressed",
        clinic_id=appointment.clinic_id,
        affected_record_id=entry.pk,
    )
    publish_on_commit(
        topic=f"clinic:{appointment.clinic_id}:queue", kind="queue", version=1
    )
