"""Help-center task guides generated from the real staff and patient screens.

Every step names the URL of the screen it happens on, the template that renders
the control, and the control's exact source text: an English msgid rendered with
``{% trans %}`` (shown through the same pt-BR catalog the product uses) or a
literal label the template prints as is. ``tests/website/test_guides.py`` fails
when a route, template, screen title or control label drifts, so a guide can
never describe a button that no longer exists.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from django.utils.html import escape, format_html
from django.utils.translation import gettext

# Guide copy is stored as English msgids and translated when a page renders,
# like the navigation registry; `_` only marks the strings for the catalog.
from django.utils.translation import gettext_noop as _

if TYPE_CHECKING:
    from django.utils.safestring import SafeString


@dataclass(frozen=True, slots=True)
class Label:
    """Exact source text of a screen title or control in its template."""

    source: str
    translated: bool = True

    @property
    def text(self) -> str:
        """Return the label as the screen shows it in pt-BR."""
        return gettext(self.source) if self.translated else self.source


@dataclass(frozen=True, slots=True)
class Screen:
    """One real screen: its URL name, its template and its visible title."""

    url_name: str
    template: str
    title: Label


@dataclass(frozen=True, slots=True)
class Step:
    """One action on a screen; the ``text`` msgid holds ``{control}`` once."""

    screen: Screen
    control: Label
    text: str
    result: str | None = None
    control_template: str | None = None

    @property
    def template(self) -> str:
        """Return the template that renders the control."""
        return self.control_template or self.screen.template

    def sentence(self) -> SafeString:
        """Render the instruction with the control's real label emphasized."""
        control = format_html(
            '<strong class="help-control">{}</strong>', self.control.text
        )
        return format_html(escape(gettext(self.text)), control=control)


@dataclass(frozen=True, slots=True)
class Guide:
    """A task guide for one audience, in the order the screens are used."""

    slug: str
    audience: str
    title: str
    summary: str
    steps: tuple[Step, ...]
    before: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()


AUDIENCES: Final[dict[str, str]] = {
    "everyone": _("Every team"),
    "managers": _("Managers"),
    "reception": _("Reception"),
    "clinicians": _("Clinicians"),
    "patients": _("Patients"),
}

LOGIN = Screen("identity:login", "identity/login.html", Label("Sign in to Clinic OS"))
ENROLL = Screen(
    "identity:enroll", "identity/enroll.html", Label("Protect your clinic account")
)
VERIFY = Screen(
    "identity:verify", "identity/verify.html", Label("Verify your identity")
)
STEP_UP = Screen(
    "identity:step-up", "identity/step_up.html", Label("Confirm this sensitive action")
)
STAFF = Screen(
    "identity:staff", "identity/staff.html", Label("Staff and account security")
)
ONBOARDING = Screen(
    "identity:onboarding", "identity/onboarding.html", Label("First steps")
)
PREFERENCES = Screen(
    "identity:preferences", "identity/preferences.html", Label("Display preferences")
)
PATIENT_SEARCH = Screen(
    "intake:patient-list", "intake/patient_list.html", Label("Patient search")
)
PATIENT_CREATE = Screen(
    "intake:patient-create", "intake/patient_create.html", Label("Register a patient")
)
BOOKING = Screen(
    "scheduling:appointment-create",
    "scheduling/appointment_book.html",
    Label("Book an appointment"),
)
AGENDA = Screen("scheduling:agenda", "scheduling/agenda.html", Label("Clinic agenda"))
RESCHEDULE = Screen(
    "scheduling:appointment-reschedule",
    "scheduling/appointment_transition.html",
    Label("Reschedule an appointment"),
)
CANCEL = Screen(
    "scheduling:appointment-cancel",
    "scheduling/appointment_transition.html",
    Label("Cancel an appointment"),
)
RECEPTION = Screen(
    "scheduling:reception", "scheduling/reception.html", Label("Reception Today")
)
TODAY = Screen("workflows:today", "workflows/today.html", Label("Today"))
ENCOUNTER = Screen(
    "ehr:encounter", "ehr/encounter.html", Label("Atendimento", translated=False)
)
TRANSCRIPT = Screen(
    "scribe:transcript", "scribe/transcript.html", Label("Consultation transcript")
)
AI_REVIEW = Screen("scribe:review", "scribe/review.html", Label("Clinician AI review"))
PUBLIC_BOOKING = Screen(
    "crm:public-booking", "crm/public.html", Label("Your next visit starts here.")
)

GUIDES: Final[tuple[Guide, ...]] = (
    Guide(
        slug="invite-your-team",
        audience="managers",
        title=_("Invite your team"),
        summary=_(
            "Send an invitation for each role. Access starts only after the "
            "person sets a password and an authenticator."
        ),
        steps=(
            Step(
                STAFF,
                Label("Send invitation"),
                _("Enter the person's email and role, then press {control}."),
                _(
                    "The invitation waits until the person confirms a password and an "
                    "authenticator."
                ),
            ),
            Step(
                ONBOARDING,
                Label("Refresh progress"),
                _(
                    "Work through the clinic checklist and press {control} after each "
                    "task."
                ),
                _(
                    "Finished items are marked complete. Progress is kept on the "
                    "server."
                ),
            ),
        ),
        notes=(
            _(
                "In this version, invitation emails are simulations. No real "
                "message leaves until an email provider is approved."
            ),
        ),
    ),
    Guide(
        slug="sign-in-securely",
        audience="everyone",
        title=_("Sign in and protect your account"),
        summary=_(
            "Sign in with your password. Privileged roles then confirm a code "
            "from an authenticator app, and sensitive actions ask for a fresh code."
        ),
        before=(_("Install an authenticator app on your phone."),),
        steps=(
            Step(
                LOGIN,
                Label("Sign in"),
                _("Enter your clinic username and password and press {control}."),
            ),
            Step(
                ENROLL,
                Label("Show my QR code"),
                _(
                    "If you have a privileged role, open the authenticator app "
                    "the first time and press {control}."
                ),
                _("A private QR code appears. Scan it once with the app."),
            ),
            Step(
                ENROLL,
                Label("Confirm authenticator"),
                _("Type the current six-digit code and press {control}."),
            ),
            Step(
                VERIFY,
                Label("Verify and continue"),
                _(
                    "If you have a privileged role, type a fresh code on later "
                    "sign-ins and press {control}."
                ),
            ),
            Step(
                STEP_UP,
                Label("Confirm and continue"),
                _("Before a sensitive action, type a new code and press {control}."),
                _("The code confirms exactly that action and changes nothing else."),
            ),
        ),
        notes=(
            _("A code works only once. If it is refused, wait for the next one."),
            _(
                "Never share codes or the QR code with anyone, including the clinic "
                "team."
            ),
        ),
    ),
    Guide(
        slug="display-preferences",
        audience="everyone",
        title=_("Choose theme and density"),
        summary=_(
            "Use the dark theme in low-light rooms or the compact density at the "
            "desk. The choice follows your account."
        ),
        steps=(
            Step(
                PREFERENCES,
                Label("Theme"),
                _("Under {control}, choose light or dark."),
            ),
            Step(
                PREFERENCES,
                Label("Density"),
                _("Under {control}, choose comfortable or compact."),
            ),
            Step(
                PREFERENCES,
                Label("Save preferences"),
                _("Press {control}."),
                _("Your choices apply on every device you sign in to."),
            ),
        ),
    ),
    Guide(
        slug="find-or-register-patient",
        audience="reception",
        title=_("Find or register a patient"),
        summary=_(
            "Search the clinic registry by name or exact document, and register "
            "the patient when there is no match."
        ),
        steps=(
            Step(
                PATIENT_SEARCH,
                Label("Search registry"),
                _(
                    "Type at least two letters of the name, or an exact document "
                    "number, and press {control}."
                ),
            ),
            Step(
                PATIENT_SEARCH,
                Label("Register a new patient"),
                _("If the patient is not in the list, press {control}."),
                control_template="intake/partials/patient_results.html",
            ),
            Step(
                PATIENT_CREATE,
                Label("Register patient"),
                _("Check the legal name and the birth date, then press {control}."),
                _("You return to the search, ready to book."),
            ),
        ),
        notes=(
            _(
                "The search never goes into the address bar, so it does not stay "
                "in the browser history."
            ),
        ),
    ),
    Guide(
        slug="book-appointment",
        audience="reception",
        title=_("Book an appointment"),
        summary=_(
            "Start from the patient, choose the physician and the exact time "
            "inside an offered window."
        ),
        before=(_("The physician has published availability for the day."),),
        steps=(
            Step(
                PATIENT_SEARCH,
                Label("Book appointment"),
                _("Find the patient and, in their row, press {control}."),
                control_template="intake/partials/patient_results.html",
            ),
            Step(
                BOOKING,
                Label("Book this appointment"),
                _(
                    "Choose the physician, type the start and end inside an "
                    "offered window and press {control}."
                ),
                control_template="scheduling/partials/booking_panel.html",
            ),
            Step(
                AGENDA,
                Label("Today"),
                _("To check the day, open the agenda and press {control}."),
            ),
        ),
        notes=(
            _(
                "Times always follow the clinic's time zone, even when your "
                "computer is set to another one."
            ),
        ),
    ),
    Guide(
        slug="register-arrival",
        audience="reception",
        title=_("Register an arrival"),
        summary=_(
            "Mark the patient as arrived and call them to a room from Reception Today."
        ),
        steps=(
            Step(
                RECEPTION,
                Label("Register arrival"),
                _(
                    "When the patient arrives, choose the arrival location and press "
                    "{control}."
                ),
                control_template="scheduling/partials/reception_board.html",
            ),
            Step(
                RECEPTION,
                Label("Call to room"),
                _("When the room is ready, choose it and press {control}."),
                _("The waiting display shows only the ticket and room codes."),
                control_template="scheduling/partials/reception_board.html",
            ),
        ),
        notes=(
            _(
                "Optional consent, documents and payment never prevent an "
                "arrival; they appear only as information."
            ),
        ),
    ),
    Guide(
        slug="document-encounter",
        audience="clinicians",
        title=_("Document an encounter"),
        summary=_(
            "Start the encounter from Today, write the SOAP note and finalize it "
            "with a fresh authenticator code."
        ),
        before=(_("Reception has registered the patient's arrival."),),
        steps=(
            Step(
                TODAY,
                Label("Start encounter"),
                _("Find your next patient and press {control}."),
                control_template="workflows/partials/today_board.html",
            ),
            Step(
                ENCOUNTER,
                Label("Salvar rascunho", translated=False),
                _(
                    "Write the note in the SOAP sections and press {control} to "
                    "save it on request."
                ),
                _("With JavaScript on, the draft is saved on the server as you type."),
            ),
            Step(
                ENCOUNTER,
                Label("Finalizar documento", translated=False),
                _("When the note is complete, press {control}."),
            ),
            Step(
                STEP_UP,
                Label("Confirm and continue"),
                _("Type a fresh authenticator code and press {control}."),
                _(
                    "The finalized version can no longer change. Corrections are made "
                    "by amendment."
                ),
            ),
        ),
        notes=(_("Finalizing in Clinic Ops is not a digital signature."),),
    ),
    Guide(
        slug="reschedule-or-cancel",
        audience="reception",
        title=_("Reschedule or cancel an appointment"),
        summary=_(
            "Move an appointment to a new time or cancel it with a reason. The "
            "history is kept either way."
        ),
        steps=(
            Step(
                AGENDA,
                Label("Reschedule"),
                _("In the agenda, find the appointment and press {control}."),
            ),
            Step(
                RESCHEDULE,
                Label("Save the new time"),
                _("Enter the new start and press {control}."),
                _(
                    "The same patient and physician keep the appointment at the new "
                    "time."
                ),
                control_template="scheduling/partials/transition_panel.html",
            ),
            Step(
                AGENDA,
                Label("Cancel"),
                _("To cancel instead, press {control} in the appointment's row."),
            ),
            Step(
                CANCEL,
                Label("Cancel the appointment"),
                _("Choose one of the listed reasons and press {control}."),
                _("The time becomes free again and the record keeps its history."),
                control_template="scheduling/partials/transition_panel.html",
            ),
        ),
    ),
    Guide(
        slug="review-ai-draft",
        audience="clinicians",
        title=_("Record a consultation and review the AI draft"),
        summary=_(
            "With the patient's consent, record the consultation and review each "
            "source-linked suggestion before it reaches your note."
        ),
        before=(
            _("The patient agreed to the recording and, separately, to AI processing."),
            _("Your organization has enabled the AI policy."),
        ),
        steps=(
            Step(
                ENCOUNTER,
                Label("Start recording"),
                _("When everyone is ready, press {control}."),
                control_template="scribe/capture.html",
            ),
            Step(
                ENCOUNTER,
                Label("Stop and keep received audio"),
                _("At the end of the consultation, press {control}."),
                control_template="scribe/capture.html",
            ),
            Step(
                TRANSCRIPT,
                Label("Finalize transcript"),
                _("Review the transcript and press {control}."),
            ),
            Step(
                ENCOUNTER,
                Label("Review source-linked AI suggestions"),
                _("Back in the encounter, press {control}."),
            ),
            Step(
                AI_REVIEW,
                Label("Generate source-linked suggestions"),
                _("Press {control}."),
            ),
            Step(
                AI_REVIEW,
                Label("Accept suggestion"),
                _(
                    "Compare each suggestion with its source, then press {control} or "
                    "reject it."
                ),
                _(
                    "Nothing is applied or finalized automatically. Finalizing the "
                    "note is a separate step."
                ),
            ),
        ),
        notes=(
            _(
                "In this version, transcription and suggestions come from a "
                "simulated model. Real models are switched on only after approval."
            ),
            _("You can always continue the note manually, without AI."),
        ),
    ),
    Guide(
        slug="patient-online-booking",
        audience="patients",
        title=_("Book online as a patient"),
        summary=_(
            "Use your clinic's booking code and private invitation to choose a "
            "time, without sharing the reason for your visit."
        ),
        steps=(
            Step(
                PUBLIC_BOOKING,
                Label("See clinic page"),
                _("Enter the booking code your clinic gave you and press {control}."),
            ),
            Step(
                PUBLIC_BOOKING,
                Label("Verify patient access"),
                _("Press {control} and use the private invitation from your clinic."),
            ),
            Step(
                PUBLIC_BOOKING,
                Label("Show available times"),
                _("Choose a date and press {control}."),
            ),
            Step(
                PUBLIC_BOOKING,
                Label("Book this time"),
                _("Pick a time and press {control}."),
                _("Your appointment stays available in the patient portal."),
            ),
        ),
        notes=(
            _(
                "This page is not monitored for emergencies. In an emergency, "
                "seek urgent care."
            ),
        ),
    ),
)

# The onboarding path: what a new clinic team does first, in order.
FIRST_STEPS: Final[tuple[str, ...]] = (
    "invite-your-team",
    "sign-in-securely",
    "display-preferences",
    "find-or-register-patient",
    "book-appointment",
    "register-arrival",
    "document-encounter",
)


def guide_by_slug(slug: str) -> Guide | None:
    """Return the guide published under ``slug``, or ``None``."""
    return next((guide for guide in GUIDES if guide.slug == slug), None)


def first_steps() -> list[Guide]:
    """Return the onboarding guides in their published order."""
    return [guide for slug in FIRST_STEPS if (guide := guide_by_slug(slug))]


def guides_for(audience: str) -> list[Guide]:
    """Return every guide written for one audience, in registry order."""
    return [guide for guide in GUIDES if guide.audience == audience]
