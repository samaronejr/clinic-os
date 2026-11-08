"""Closed neutral outreach content; no clinical inference or template variables."""

from typing import Final

MAX_NAME: Final = 160
MAX_EMAIL: Final = 254
MAX_DESCRIPTION: Final = 400
MAX_CODE: Final = 40
MAX_SCORE: Final = 10
PROMOTER_MIN: Final = 9
DETRACTOR_MAX: Final = 6

# Exact reviewed synthetic copy, not a claim of external lawful-basis review.
# Placeholders (including diagnosis, condition, names and scores) are forbidden.
MARKETING_BODIES: Final = (
    "A clinica tem horarios disponiveis. Consulte a agenda no portal. "
    "Voce pode desativar mensagens de divulgacao nas preferencias do portal.",
    "Conte com o portal para consultar horarios da clinica. "
    "Voce pode desativar mensagens de divulgacao nas preferencias do portal.",
)
