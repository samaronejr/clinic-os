# ruff: noqa: S608 - DDL text built from migration literals, never caller input
"""Frozen export-v1 relation set and its static-branch organization reader.

Every ``clinic_app`` domain relation is either exported here or excluded with a
closed reason (``EXCLUDED_V1``); ``tests/interop/test_org_export.py`` proves the
union equals the recovery manifest's domain relations plus the interop tables.
The reader is static SQL: one fixed branch per relation, no dynamic SQL.
A later export version adds a new migration; the v1 datasets never change. A
later relation that exports nothing may join ``EXCLUDED_V1`` with a closed reason
(``interop_rndssubmission``, todo 68); the reader SQL is untouched by it.
"""

from typing import Final

# (relation, key column, key type, omitted columns, scope). Scope ``self`` is
# the organization row, ``staff`` the accounts holding a role in it.
EXPORT_V1: Final = (
    ("ai_actionexecution", "id", "uuid", (), "organization"),
    ("ai_actionproposal", "id", "uuid", (), "organization"),
    ("ai_actionreceipt", "id", "uuid", (), "organization"),
    ("ai_agentpolicyversion", "id", "uuid", (), "organization"),
    ("ai_agentrun", "id", "uuid", (), "organization"),
    ("ai_aiartifact", "id", "uuid", (), "organization"),
    ("ai_aiinvocation", "id", "uuid", (), "organization"),
    ("ai_approval", "id", "uuid", (), "organization"),
    ("ai_careproposal", "id", "uuid", (), "organization"),
    ("ai_compensation", "id", "uuid", (), "organization"),
    ("ai_evaluationprotocol", "id", "uuid", (), "organization"),
    ("ai_evaluationrating", "id", "uuid", (), "organization"),
    ("ai_evaluationrun", "id", "uuid", (), "organization"),
    ("ai_extractioncandidate", "id", "uuid", (), "organization"),
    ("ai_extractionjob", "id", "uuid", (), "organization"),
    ("ai_frontdeskthread", "id", "uuid", (), "organization"),
    ("ai_frontdeskturn", "id", "uuid", (), "organization"),
    ("ai_knowledgearticle", "id", "uuid", (), "organization"),
    ("ai_modelchangerequest", "id", "uuid", (), "organization"),
    ("analytics_analystreview", "id", "uuid", (), "organization"),
    ("analytics_metricdefinitionversion", "id", "uuid", (), "organization"),
    ("analytics_savedreport", "id", "uuid", (), "organization"),
    ("billing_account", "id", "uuid", (), "organization"),
    ("billing_cashsession", "id", "uuid", (), "organization"),
    ("billing_cashsessionevent", "id", "uuid", (), "organization"),
    ("billing_fiscalconfiguration", "id", "uuid", (), "organization"),
    ("billing_fiscaldocument", "id", "uuid", (), "organization"),
    ("billing_fiscaltransition", "id", "uuid", (), "organization"),
    ("billing_invoice", "id", "uuid", (), "organization"),
    ("billing_invoicerevision", "id", "uuid", (), "organization"),
    ("billing_invoiceterms", "id", "uuid", (), "organization"),
    ("billing_journalbook", "id", "uuid", (), "organization"),
    ("billing_journalentry", "id", "uuid", (), "organization"),
    ("billing_journalline", "id", "uuid", (), "organization"),
    ("billing_journalsettlement", "id", "uuid", (), "organization"),
    ("billing_paymentevent", "id", "uuid", (), "organization"),
    ("billing_payout", "id", "uuid", (), "organization"),
    ("billing_pixcharge", "operation_id", "uuid", (), "organization"),
    ("billing_pixoperation", "id", "uuid", (), "organization"),
    ("billing_practitionerpayoutrule", "id", "uuid", (), "organization"),
    ("billing_pspbinding", "id", "uuid", (), "organization"),
    ("billing_pspevidence", "id", "uuid", (), "organization"),
    ("billing_pspintent", "id", "uuid", (), "organization"),
    ("billing_psppolicy", "id", "uuid", (), "organization"),
    ("billing_pspreceipt", "reference", "uuid", (), "organization"),
    ("billing_receipt", "id", "uuid", (), "organization"),
    ("billing_refund", "id", "uuid", (), "organization"),
    ("billing_settlement", "id", "uuid", (), "organization"),
    ("billing_settlementallocation", "id", "uuid", (), "organization"),
    ("comms_appointmentreminder", "operation_id", "uuid", (), "organization"),
    ("comms_conversation", "id", "uuid", (), "organization"),
    ("comms_internalnote", "id", "uuid", (), "organization"),
    ("comms_message", "id", "uuid", (), "organization"),
    ("comms_participant", "id", "uuid", (), "organization"),
    ("comms_quickreply", "id", "uuid", (), "organization"),
    ("comms_suggestedreply", "id", "uuid", (), "organization"),
    ("comms_templateversion", "id", "uuid", (), "organization"),
    ("consent_aiusedisclosure", "id", "uuid", (), "organization"),
    ("consent_consentacceptance", "id", "uuid", (), "organization"),
    ("consent_consentrevocation", "id", "uuid", (), "organization"),
    ("consent_consenttext", "id", "uuid", (), "organization"),
    ("consent_noticeversion", "id", "uuid", (), "organization"),
    ("consent_participantacknowledgment", "id", "uuid", (), "organization"),
    ("consent_refusalrecord", "id", "uuid", (), "organization"),
    ("crm_campaign", "id", "uuid", (), "organization"),
    ("crm_campaignsend", "id", "uuid", (), "organization"),
    ("crm_lead", "id", "uuid", (), "organization"),
    ("crm_marketingpreference", "id", "uuid", (), "organization"),
    ("crm_publicbookingprofile", "id", "uuid", (), "organization"),
    ("crm_referralsource", "id", "uuid", (), "organization"),
    ("crm_satisfactionsurvey", "id", "uuid", (), "organization"),
    ("ehr_allergy", "id", "uuid", (), "organization"),
    ("ehr_clinicalattachment", "id", "uuid", (), "organization"),
    ("ehr_clinicaldocument", "id", "uuid", (), "organization"),
    ("ehr_clinicaldocumentversion", "id", "uuid", (), "organization"),
    ("ehr_copiednoteblock", "id", "uuid", (), "organization"),
    ("ehr_encounter", "id", "uuid", (), "organization"),
    ("ehr_encounteraddendum", "id", "uuid", (), "organization"),
    ("ehr_encounterintakereference", "id", "uuid", (), "organization"),
    ("ehr_episode", "id", "uuid", (), "organization"),
    ("ehr_episodeencounter", "id", "uuid", (), "organization"),
    ("ehr_historyassessment", "id", "uuid", (), "organization"),
    ("ehr_medicationstatement", "id", "uuid", (), "organization"),
    ("ehr_noteaccessgrant", "id", "uuid", (), "organization"),
    ("ehr_observation", "id", "uuid", (), "organization"),
    ("ehr_problem", "id", "uuid", (), "organization"),
    ("ehr_restrictednote", "id", "uuid", (), "organization"),
    ("ehr_specialtyentry", "id", "uuid", (), "organization"),
    ("ehr_specialtypack", "id", "uuid", (), "organization"),
    ("ehr_specialtytemplate", "id", "uuid", (), "organization"),
    ("identity_careteammembership", "id", "uuid", (), "organization"),
    ("identity_clinic", "id", "uuid", (), "organization"),
    ("identity_clinicconfiguration", "id", "uuid", (), "organization"),
    ("identity_onboardingprogress", "id", "uuid", (), "organization"),
    ("identity_organization", "id", "uuid", (), "self"),
    ("identity_patientdelegategrant", "id", "uuid", (), "organization"),
    ("identity_physicianprofile", "id", "uuid", (), "organization"),
    ("identity_professionalregistration", "id", "uuid", (), "organization"),
    ("identity_rolegrant", "id", "uuid", (), "organization"),
    ("identity_serviceprincipal", "id", "uuid", (), "organization"),
    ("identity_serviceprincipalgrant", "id", "uuid", (), "organization"),
    (
        "identity_staffinvitation",
        "id",
        "uuid",
        ("token_hash", "token_nonce", "email_hash"),
        "organization",
    ),
    ("identity_user", "id", "uuid", (), "staff"),
    ("identity_userclinicrole", "id", "uuid", (), "organization"),
    ("insurance_appeal", "id", "uuid", (), "organization"),
    ("insurance_batch", "id", "uuid", (), "organization"),
    ("insurance_batchguide", "id", "uuid", (), "organization"),
    ("insurance_clinicalapproval", "id", "uuid", (), "organization"),
    ("insurance_glosa", "id", "uuid", (), "organization"),
    ("insurance_guide", "id", "uuid", (), "organization"),
    ("insurance_insurancemembership", "id", "uuid", (), "organization"),
    ("insurance_payer", "id", "uuid", (), "organization"),
    ("insurance_payercontract", "id", "uuid", (), "organization"),
    ("insurance_payerrequest", "id", "uuid", (), "organization"),
    ("insurance_paymentmatch", "id", "uuid", (), "organization"),
    ("insurance_pricetable", "id", "uuid", (), "organization"),
    ("insurance_return", "id", "uuid", (), "organization"),
    ("insurance_submission", "id", "uuid", (), "organization"),
    ("insurance_tussterm", "id", "uuid", (), "organization"),
    ("intake_clinicintakepolicy", "id", "uuid", (), "organization"),
    ("intake_datasubjectrequest", "id", "uuid", (), "organization"),
    ("intake_demographicscorrection", "id", "uuid", (), "organization"),
    ("intake_duplicatecandidate", "id", "uuid", (), "organization"),
    ("intake_emergencycontact", "id", "uuid", (), "organization"),
    ("intake_insurancemembership", "id", "uuid", (), "organization"),
    ("intake_mergedecision", "id", "uuid", (), "organization"),
    ("intake_patient", "id", "uuid", ("full_name_index",), "organization"),
    ("intake_patientaddress", "id", "uuid", (), "organization"),
    ("intake_patientalias", "id", "uuid", (), "organization"),
    ("intake_patientchannelpreference", "id", "uuid", (), "organization"),
    ("intake_patientclinicenrollment", "id", "uuid", (), "organization"),
    ("intake_patientcontact", "id", "uuid", (), "organization"),
    ("intake_patientcontactevent", "id", "uuid", (), "organization"),
    (
        "intake_patientdemographics",
        "id",
        "uuid",
        ("registry_full_name", "registry_full_name_index", "registry_birth_date"),
        "organization",
    ),
    ("intake_patientduplicateindex", "id", "uuid", (), "organization"),
    ("intake_patientidentifier", "id", "uuid", ("blind_index",), "organization"),
    ("intake_patientmergetransfer", "id", "uuid", (), "organization"),
    ("intake_questionnaireevent", "id", "uuid", (), "organization"),
    ("intake_questionnaireresponse", "id", "uuid", (), "organization"),
    ("intake_questionnairetemplate", "id", "uuid", (), "organization"),
    ("interop_apiclient", "id", "uuid", (), "organization"),
    ("interop_apiwritereceipt", "id", "uuid", (), "organization"),
    ("interop_organizationexport", "id", "uuid", (), "organization"),
    ("interop_webhookattempt", "id", "uuid", (), "organization"),
    ("interop_webhookdelivery", "id", "uuid", (), "organization"),
    ("interop_webhooksubscription", "id", "uuid", (), "organization"),
    ("inventory_batch", "id", "uuid", (), "organization"),
    ("inventory_calibration", "id", "uuid", (), "organization"),
    ("inventory_equipment", "id", "uuid", (), "organization"),
    ("inventory_item", "id", "uuid", (), "organization"),
    ("inventory_location", "id", "uuid", (), "organization"),
    ("inventory_maintenance", "id", "uuid", (), "organization"),
    ("inventory_minimumstock", "id", "uuid", (), "organization"),
    ("inventory_purchasebudget", "id", "uuid", (), "organization"),
    ("inventory_purchaserequest", "id", "uuid", (), "organization"),
    ("inventory_stockmovement", "id", "uuid", (), "organization"),
    ("inventory_supplier", "id", "uuid", (), "organization"),
    ("orders_labaccount", "id", "uuid", ("signing_key_digest",), "organization"),
    ("orders_order", "id", "uuid", (), "organization"),
    ("orders_orderevent", "id", "uuid", (), "organization"),
    ("orders_orderitem", "id", "uuid", (), "organization"),
    ("orders_result", "id", "uuid", (), "organization"),
    ("orders_resultacknowledgment", "id", "uuid", (), "organization"),
    ("orders_resultmatch", "id", "uuid", (), "organization"),
    ("orders_resultobservation", "id", "uuid", (), "organization"),
    ("orders_resultquarantine", "id", "uuid", (), "organization"),
    ("orders_resultrejection", "id", "uuid", (), "organization"),
    ("orders_resultrelease", "id", "uuid", (), "organization"),
    ("orders_resultversion", "id", "uuid", (), "organization"),
    ("orders_resultworkitem", "id", "uuid", (), "organization"),
    ("prescription_documentdraftcontent", "id", "uuid", (), "organization"),
    ("prescription_medicationfavorite", "id", "uuid", (), "organization"),
    ("prescription_prescriptiondocument", "id", "uuid", (), "organization"),
    ("prescription_prescriptiondocumentrelease", "id", "uuid", (), "organization"),
    ("prescription_prescriptiondocumentrevocation", "id", "uuid", (), "organization"),
    ("prescription_prescriptiondraft", "id", "uuid", (), "organization"),
    ("prescription_prescriptionissuance", "id", "uuid", (), "organization"),
    ("prescription_prescriptionitem", "id", "uuid", (), "organization"),
    ("prescription_signaturecallback", "id", "uuid", (), "organization"),
    ("prescription_signatureoperation", "id", "uuid", (), "organization"),
    ("prescription_sncrnumberallocation", "id", "uuid", (), "organization"),
    ("retention_artifactpurge", "id", "uuid", (), "organization"),
    ("retention_legalhold", "id", "uuid", (), "organization"),
    ("retention_recordexport", "id", "uuid", (), "organization"),
    ("retention_recordrelease", "id", "uuid", (), "organization"),
    ("retention_retentionpolicy", "id", "uuid", (), "organization"),
    ("scheduling_absence", "id", "uuid", (), "organization"),
    ("scheduling_appointment", "id", "uuid", (), "organization"),
    ("scheduling_appointmentresource", "id", "uuid", (), "organization"),
    ("scheduling_appointmentseries", "id", "uuid", (), "organization"),
    ("scheduling_appointmenttransition", "id", "uuid", (), "organization"),
    ("scheduling_availabilityblock", "id", "uuid", (), "organization"),
    ("scheduling_availabilitytemplate", "id", "uuid", (), "organization"),
    ("scheduling_cliniciandelay", "id", "uuid", (), "organization"),
    ("scheduling_holiday", "id", "uuid", (), "organization"),
    ("scheduling_patientbookingevent", "id", "uuid", (), "organization"),
    ("scheduling_queueentry", "id", "uuid", (), "organization"),
    ("scheduling_resource", "id", "uuid", (), "organization"),
    ("scheduling_roomstatus", "id", "uuid", (), "organization"),
    ("scheduling_seriesexception", "id", "uuid", (), "organization"),
    ("scheduling_servicetype", "id", "uuid", (), "organization"),
    ("scheduling_waitlistentry", "id", "bigint", (), "organization"),
    ("scheduling_waitlistoffer", "id", "uuid", (), "organization"),
    ("scribe_aidraft", "id", "uuid", (), "organization"),
    ("scribe_captureepoch", "id", "uuid", (), "organization"),
    ("scribe_chunktranscription", "id", "uuid", (), "organization"),
    ("scribe_draftactionproposal", "id", "uuid", (), "organization"),
    ("scribe_draftsectionsuggestion", "id", "uuid", (), "organization"),
    ("scribe_recordingcommand", "id", "uuid", (), "organization"),
    ("scribe_recordingsession", "id", "uuid", ("token_digest",), "organization"),
    ("scribe_reviewdecision", "id", "uuid", (), "organization"),
    ("scribe_transcriptsegment", "id", "uuid", (), "organization"),
    ("scribe_transcriptversion", "id", "uuid", (), "organization"),
    ("subscriptions_seat", "id", "uuid", (), "organization"),
    ("subscriptions_subscription", "id", "uuid", (), "organization"),
    ("subscriptions_subscriptionevent", "id", "uuid", (), "organization"),
    ("subscriptions_usageevent", "id", "uuid", (), "organization"),
    ("subscriptions_usagerollup", "id", "uuid", (), "organization"),
    ("teleconsult_teleconsultdevicecheck", "id", "uuid", (), "organization"),
    ("teleconsult_teleconsultevent", "id", "bigint", (), "organization"),
    ("teleconsult_teleconsultroom", "operation_id", "uuid", (), "organization"),
    ("teleconsult_teleconsultsession", "id", "uuid", (), "organization"),
    ("workflows_careaction", "id", "uuid", (), "organization"),
    ("workflows_careplan", "id", "uuid", (), "organization"),
    ("workflows_carerefusal", "id", "uuid", (), "organization"),
    ("workflows_proassignment", "id", "uuid", (), "organization"),
    ("workflows_prooccurrence", "id", "uuid", (), "organization"),
    ("workflows_recall", "id", "uuid", (), "organization"),
    ("workflows_referral", "id", "uuid", (), "organization"),
    ("workflows_releasedelivery", "id", "uuid", (), "organization"),
    ("workflows_releasenoticetemplate", "id", "uuid", (), "organization"),
    ("workflows_task", "id", "uuid", (), "organization"),
    ("workflows_taskcomment", "id", "uuid", (), "organization"),
    ("workflows_urgentconcern", "id", "uuid", (), "organization"),
    ("workflows_urgentcoveragepolicy", "id", "uuid", (), "organization"),
    ("workflows_workflowdefinitionversion", "id", "uuid", (), "organization"),
    ("workflows_workflowrun", "id", "uuid", (), "organization"),
    ("workflows_workflowstep", "id", "uuid", (), "organization"),
)
EXCLUDED_V1: Final = {
    "ai_aicircuit": "machinery",
    "ai_gatewayswitch": "platform",
    "ai_modelconfiguration": "platform",
    "analytics_readmodelrefresh": "machinery",
    "audit_event": "integrity_ledger",
    "comms_inboundevent": "machinery",
    "comms_integrationoperation": "machinery",
    "comms_messagemeter": "machinery",
    "comms_provideraccount": "credential",
    "ehr_addendumsavereceipt": "machinery",
    "ehr_discarded_content": "discarded",
    "ehr_drafteditstate": "machinery",
    "ehr_draftsavereceipt": "machinery",
    "ehr_timelineprojection": "machinery",
    "ehr_timelinerefresh": "machinery",
    "identity_authenticatorreset": "credential",
    "identity_authenticatorretirement": "credential",
    "identity_passkey": "credential",
    "identity_physicianevidence": "platform",
    "identity_recoverybatch": "credential",
    "identity_recoverycode": "credential",
    "identity_savedview": "presentation",
    "identity_sessionrecord": "credential",
    "identity_signupregistration": "credential",
    "identity_totpsecret": "credential",
    "identity_userpreference": "presentation",
    "insurance_commandreceipt": "machinery",
    "intake_patientaccessgrant": "credential",
    "intake_patientsession": "credential",
    "interop_apiaccesstoken": "credential",
    "interop_apiclientsecret": "credential",
    "interop_organizationexportpart": "derived_archive",
    "interop_rndssubmission": "machinery",
    "inventory_commandreceipt": "machinery",
    "otp_static_staticdevice": "credential",
    "otp_totp_totpdevice": "credential",
    "prescription_medicationcatalogentry": "platform",
    "prescription_medicationcatalogimport": "platform",
    "prescription_verificationprobe": "platform",
    "scheduling_displaydevice": "credential",
    "scribe_audiochunk": "transient_media",
    "subscriptions_entitlement": "platform",
    "subscriptions_plan": "platform",
    "teleconsult_teleconsultcredential": "credential",
    "tenancy_tenantdatakey": "credential",
}
# Exported relations clinic_resolver could not read before this migration.
RESOLVER_READ_GRANTS: Final = (
    "comms_internalnote",
    "comms_participant",
    "comms_quickreply",
    "comms_suggestedreply",
    "consent_aiusedisclosure",
    "consent_participantacknowledgment",
    "intake_clinicintakepolicy",
    "intake_demographicscorrection",
    "intake_emergencycontact",
    "intake_patientaddress",
    "intake_patientcontactevent",
    "prescription_documentdraftcontent",
    "prescription_medicationfavorite",
    "prescription_prescriptionissuance",
    "prescription_prescriptionitem",
    "prescription_sncrnumberallocation",
    "scheduling_patientbookingevent",
    "teleconsult_teleconsultdevicecheck",
    "workflows_releasenoticetemplate",
    "workflows_taskcomment",
)

_STAFF_COLUMNS: Final = (
    "id",
    "username",
    "first_name",
    "last_name",
    "email",
    "is_active",
    "date_joined",
    "last_login",
)


def _projection(relation: str, omitted: tuple[str, ...]) -> str:
    if relation == "identity_user":
        pairs = ",".join(f"'{column}',t.{column}" for column in _STAFF_COLUMNS)
        return f"jsonb_build_object({pairs})"
    if not omitted:
        return "to_jsonb(t)"
    names = ",".join(f"'{column}'" for column in omitted)
    return f"to_jsonb(t)-ARRAY[{names}]::text[]"


def _scope(key: str, scope: str) -> str:
    if scope == "self":
        return f"t.{key}=owning_org"
    if scope == "staff":
        return (
            "EXISTS (SELECT 1 FROM clinic_app.identity_userclinicrole r "
            "WHERE r.user_id=t.id AND r.organization_id=owning_org)"
        )
    return "t.organization_id=owning_org"


def _branch(  # noqa: PLR0913 - one EXPORT_V1 row and its branch keyword
    relation: str,
    key: str,
    kind: str,
    omitted: tuple[str, ...],
    scope: str,
    keyword: str,
) -> str:
    return (
        f" {keyword} requested_relation='{relation}' THEN\n"
        f"  RETURN QUERY SELECT t.{key}::text,{_projection(relation, omitted)}\n"
        f"  FROM clinic_app.{relation} t WHERE {_scope(key, scope)}\n"
        f"  AND (after_key IS NULL OR t.{key}>after_key::{kind})\n"
        f"  ORDER BY t.{key} LIMIT page_size;"
    )


# Plain calls and comprehensions only, so the clock census decodes this SQL.
_BRANCHES: Final = "\n".join(
    _branch(
        relation,
        key,
        kind,
        omitted,
        scope,
        "IF" if relation == EXPORT_V1[0][0] else "ELSIF",
    )
    for relation, key, kind, omitted, scope in EXPORT_V1
)

SQL: Final = (
    "GRANT SELECT ON "
    + ",".join(f"clinic_app.{relation}" for relation in RESOLVER_READ_GRANTS)
    + " TO clinic_resolver;\n"
    """SET LOCAL ROLE clinic_resolver;
CREATE FUNCTION clinic_app.interop_export_rows(requested_export uuid,
 requested_relation text, after_key text, page_size integer)
RETURNS TABLE(row_key text, row_data jsonb)
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE job clinic_app.interop_organizationexport; owning_org uuid;
BEGIN
 SELECT * INTO job FROM clinic_app.interop_organizationexport x
 WHERE x.id=requested_export AND x.state='building'
  AND x.organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid;
 IF job.id IS NULL OR NOT clinic_app.interop_manager(job.organization_id)
  OR job.requested_by_id IS DISTINCT FROM
   (SELECT u.id FROM clinic_app.load_current_user() u) THEN
  RAISE EXCEPTION 'interop export denied' USING ERRCODE='42501';
 END IF;
 IF page_size IS NULL OR page_size NOT BETWEEN 1 AND 1000 THEN
  RAISE EXCEPTION 'interop export page invalid' USING ERRCODE='22023';
 END IF;
 owning_org:=job.organization_id;
"""
    + _BRANCHES
    + """
 ELSE
  RAISE EXCEPTION 'interop export relation denied' USING ERRCODE='42501';
 END IF;
END $f$;
REVOKE ALL ON FUNCTION clinic_app.interop_export_rows(uuid,text,text,integer)
 FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.interop_export_rows(uuid,text,text,integer)
 TO clinic_app;
RESET ROLE;
"""
)

# Column privileges earlier migrations gave clinic_resolver on exported tables.
# A table-level REVOKE removes a role's column privileges too, so the reverse
# grants them back exactly.
PRIOR_COLUMN_GRANTS: Final = (
    (
        "consent_aiusedisclosure",
        (
            "clinic_id",
            "encounter_id",
            "informed",
            "organization_id",
            "patient_id",
            "refused",
        ),
    ),
    ("intake_demographicscorrection", ("actor_id", "demographics_id")),
)

REVERSE: Final = (
    "SET LOCAL ROLE clinic_resolver;\n"
    "DROP FUNCTION clinic_app.interop_export_rows(uuid,text,text,integer);\n"
    "RESET ROLE;\n"
    "REVOKE SELECT ON "
    + ",".join(f"clinic_app.{relation}" for relation in RESOLVER_READ_GRANTS)
    + " FROM clinic_resolver;\n"
    + "".join(
        f"GRANT SELECT ({','.join(columns)}) ON clinic_app.{table}"
        " TO clinic_resolver;\n"
        for table, columns in PRIOR_COLUMN_GRANTS
    )
)
