"""Every policy identity 0051 fences, with the expressions it fences.

Rows are (table, policy, command, USING, WITH CHECK) as deparsed under the
migration search path with every other app at its leaf and identity at 0050,
with the text casts PostgreSQL prints on setting names left out (it stores the
same constant either way).
USING is None for INSERT; WITH CHECK is None where the policy checks new rows
with its USING expression. An absent expression already holds the fallback the
command means (false when permissive, true when restrictive).

These are data, not SQL assembled from the live catalog at run time, so the
clock census decodes every statement the fence runs
(tests/scheduling/test_clock_boundary.py). The fence and its reverse are built
from them in _enterprise_context_fence_sql.py; a policy created after 0051 must
carry the fence itself (tests/identity/test_enterprise_migration.py).
"""

from typing import Final

POLICIES: Final = (
    (
        "ai_actionexecution",
        "agent_finance",
        "*",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND ai_revenue_row(clinic_id, 'propos"
            "al'::text, proposal_id))"
        ),
        None,
    ),
    (
        "ai_actionexecution",
        "agent_finance_bound",
        "*",
        (
            "((NOT ai_revenue_ancestry(clinic_id, 'proposal'::text, proposal_"
            "id)) OR ai_revenue_row(clinic_id, 'proposal'::text, proposal_id)"
            ")"
        ),
        None,
    ),
    (
        "ai_actionexecution",
        "agent_staff",
        "*",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND agent_staff(clinic_id))"
        ),
        None,
    ),
    (
        "ai_actionproposal",
        "agent_finance",
        "*",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND ai_revenue_row(clinic_id, 'run'::"
            "text, run_id))"
        ),
        None,
    ),
    (
        "ai_actionproposal",
        "agent_finance_bound",
        "*",
        (
            "((NOT ai_revenue_ancestry(clinic_id, 'run'::text, run_id)) OR ai"
            "_revenue_row(clinic_id, 'run'::text, run_id))"
        ),
        None,
    ),
    (
        "ai_actionproposal",
        "agent_staff",
        "*",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND agent_staff(clinic_id))"
        ),
        None,
    ),
    (
        "ai_actionreceipt",
        "agent_finance",
        "*",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND ai_revenue_row(clinic_id, 'execut"
            "ion'::text, execution_id))"
        ),
        None,
    ),
    (
        "ai_actionreceipt",
        "agent_finance_bound",
        "*",
        (
            "((NOT ai_revenue_ancestry(clinic_id, 'execution'::text, executio"
            "n_id)) OR ai_revenue_row(clinic_id, 'execution'::text, execution"
            "_id))"
        ),
        None,
    ),
    (
        "ai_actionreceipt",
        "agent_staff",
        "*",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND agent_staff(clinic_id))"
        ),
        None,
    ),
    (
        "ai_agentpolicyversion",
        "agent_finance",
        "*",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND ai_revenue_row(clinic_id, 'policy"
            "'::text, id))"
        ),
        None,
    ),
    (
        "ai_agentpolicyversion",
        "agent_finance_bound",
        "*",
        (
            "((NOT ai_revenue_ancestry(clinic_id, 'policy'::text, id)) OR ai_"
            "revenue_row(clinic_id, 'policy'::text, id))"
        ),
        (
            "(((NOT ai_revenue_ancestry(clinic_id, 'policy'::text, id)) OR ai"
            "_revenue_row(clinic_id, 'policy'::text, id)) AND (((capability):"
            ":text <> 'revenue_cycle'::text) OR has_permission('automation.fi"
            "nance'::text, clinic_id, NULL::uuid)))"
        ),
    ),
    (
        "ai_agentpolicyversion",
        "agent_staff",
        "*",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND agent_staff(clinic_id))"
        ),
        None,
    ),
    (
        "ai_agentrun",
        "agent_finance",
        "*",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND ai_revenue_row(clinic_id, 'policy"
            "'::text, policy_id))"
        ),
        None,
    ),
    (
        "ai_agentrun",
        "agent_finance_bound",
        "*",
        (
            "((NOT ai_revenue_ancestry(clinic_id, 'policy'::text, policy_id))"
            " OR ai_revenue_row(clinic_id, 'policy'::text, policy_id))"
        ),
        None,
    ),
    (
        "ai_agentrun",
        "agent_staff",
        "*",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND agent_staff(clinic_id))"
        ),
        None,
    ),
    (
        "ai_aiartifact",
        "ai_artifact_user",
        "*",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (user_id = (NULLIF(current_settin"
            "g('app.current_user_id', true), ''::text))::uuid) AND ai_staff(c"
            "linic_id, (purpose)::text, patient_enrollment_id))"
        ),
        None,
    ),
    (
        "ai_aicircuit",
        "ai_circuit_tenant",
        "*",
        (
            "(organization_id = (NULLIF(current_setting('app.current_tenant',"
            " true), ''::text))::uuid)"
        ),
        None,
    ),
    (
        "ai_aiinvocation",
        "ai_invocation_user",
        "*",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (user_id = (NULLIF(current_settin"
            "g('app.current_user_id', true), ''::text))::uuid))"
        ),
        None,
    ),
    (
        "ai_approval",
        "agent_finance",
        "*",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND ai_revenue_row(clinic_id, 'propos"
            "al'::text, proposal_id))"
        ),
        None,
    ),
    (
        "ai_approval",
        "agent_finance_bound",
        "*",
        (
            "((NOT ai_revenue_ancestry(clinic_id, 'proposal'::text, proposal_"
            "id)) OR ai_revenue_row(clinic_id, 'proposal'::text, proposal_id)"
            ")"
        ),
        None,
    ),
    (
        "ai_approval",
        "agent_staff",
        "*",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND agent_staff(clinic_id))"
        ),
        None,
    ),
    (
        "ai_careproposal",
        "care_staff",
        "*",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND ai_care_visible(clinic_id, enroll"
            "ment_id, (audience)::text, (state)::text))"
        ),
        None,
    ),
    (
        "ai_compensation",
        "agent_finance",
        "*",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND ai_revenue_row(clinic_id, 'execut"
            "ion'::text, execution_id))"
        ),
        None,
    ),
    (
        "ai_compensation",
        "agent_finance_bound",
        "*",
        (
            "((NOT ai_revenue_ancestry(clinic_id, 'execution'::text, executio"
            "n_id)) OR ai_revenue_row(clinic_id, 'execution'::text, execution"
            "_id))"
        ),
        None,
    ),
    (
        "ai_compensation",
        "agent_staff",
        "*",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND agent_staff(clinic_id))"
        ),
        None,
    ),
    (
        "ai_evaluationprotocol",
        "evaluation_staff",
        "*",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (ai_eval_staff(clinic_id, 'clinic"
            "ian'::text) OR ai_eval_staff(clinic_id, 'owner'::text)))"
        ),
        None,
    ),
    (
        "ai_evaluationrating",
        "evaluation_staff",
        "*",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND ai_eval_rating_visible(run_id, (c"
            "ase_id)::text, user_id))"
        ),
        None,
    ),
    (
        "ai_evaluationrun",
        "evaluation_staff",
        "*",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (ai_eval_staff(clinic_id, 'clinic"
            "al_owner'::text) OR ai_eval_run_visible(id)))"
        ),
        None,
    ),
    (
        "ai_extractioncandidate",
        "extraction_staff",
        "*",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND ai_extraction_job_allowed(job_id)"
            " AND (((status)::text = ANY (ARRAY[('confirmed'::character varyi"
            "ng)::text, ('rejected'::character varying)::text])) OR (expires_"
            "at > statement_timestamp())))"
        ),
        None,
    ),
    (
        "ai_extractionjob",
        "extraction_staff",
        "*",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND ai_extraction_source_allowed(clin"
            "ic_id, patient_enrollment_id, result_version_id, attachment_id, "
            "(sha256)::text))"
        ),
        None,
    ),
    (
        "ai_frontdeskthread",
        "front_desk_read",
        "r",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND has_permission('demographics.read"
            "'::text, clinic_id, NULL::uuid))"
        ),
        None,
    ),
    (
        "ai_frontdeskthread",
        "front_desk_update",
        "w",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND has_permission('demographics.read"
            "'::text, clinic_id, NULL::uuid))"
        ),
        (
            "(organization_id = (NULLIF(current_setting('app.current_tenant',"
            " true), ''::text))::uuid)"
        ),
    ),
    (
        "ai_frontdeskturn",
        "front_desk_read",
        "r",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND has_permission('demographics.read"
            "'::text, clinic_id, NULL::uuid))"
        ),
        None,
    ),
    (
        "ai_knowledgearticle",
        "front_desk_insert",
        "a",
        None,
        (
            "(organization_id = (NULLIF(current_setting('app.current_tenant',"
            " true), ''::text))::uuid)"
        ),
    ),
    (
        "ai_knowledgearticle",
        "front_desk_read",
        "r",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND has_permission('configuration.cli"
            "nic'::text, clinic_id, NULL::uuid))"
        ),
        None,
    ),
    (
        "ai_knowledgearticle",
        "front_desk_update",
        "w",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND has_permission('configuration.cli"
            "nic'::text, clinic_id, NULL::uuid))"
        ),
        (
            "(organization_id = (NULLIF(current_setting('app.current_tenant',"
            " true), ''::text))::uuid)"
        ),
    ),
    (
        "ai_modelchangerequest",
        "evaluation_staff",
        "*",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND ai_eval_staff(clinic_id, 'owner':"
            ":text))"
        ),
        None,
    ),
    (
        "analytics_analystreview",
        "analytics_scope",
        "*",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND analytics_allowed(clinic_id, (des"
            "criptor ->> 'metric_id'::text)) AND (user_id = (NULLIF(current_s"
            "etting('app.current_user_id', true), ''::text))::uuid))"
        ),
        None,
    ),
    (
        "analytics_metricdefinitionversion",
        "analytics_scope",
        "*",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND analytics_allowed(clinic_id, (met"
            "ric_id)::text))"
        ),
        None,
    ),
    (
        "analytics_readmodelrefresh",
        "analytics_scope",
        "*",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND analytics_allowed(clinic_id, (des"
            "criptor ->> 'metric_id'::text)) AND (user_id = (NULLIF(current_s"
            "etting('app.current_user_id', true), ''::text))::uuid) AND ((aut"
            "horization_version)::text = analytics_authorization(clinic_id)))"
        ),
        None,
    ),
    (
        "analytics_savedreport",
        "analytics_scope",
        "*",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND analytics_allowed(clinic_id, (des"
            "criptor ->> 'metric_id'::text)) AND (user_id = (NULLIF(current_s"
            "etting('app.current_user_id', true), ''::text))::uuid))"
        ),
        None,
    ),
    (
        "billing_account",
        "billing_journal_scope",
        "*",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND billing_book_access(book_id, 'jou"
            "rnal.read'::text))"
        ),
        None,
    ),
    (
        "billing_cashsession",
        "billing_journal_scope",
        "*",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND billing_book_access(book_id, 'jou"
            "rnal.read'::text))"
        ),
        None,
    ),
    (
        "billing_cashsessionevent",
        "billing_journal_scope",
        "*",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND billing_book_access(book_id, 'jou"
            "rnal.read'::text))"
        ),
        None,
    ),
    (
        "billing_fiscalconfiguration",
        "fiscal_read",
        "r",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND billing_fiscal_access(clinic_id))"
        ),
        None,
    ),
    (
        "billing_fiscaldocument",
        "fiscal_insert",
        "a",
        None,
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND billing_fiscal_access(clinic_id))"
        ),
    ),
    (
        "billing_fiscaldocument",
        "fiscal_read",
        "r",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND billing_fiscal_access(clinic_id))"
        ),
        None,
    ),
    (
        "billing_fiscaldocument",
        "fiscal_update",
        "w",
        "billing_fiscal_access(clinic_id)",
        None,
    ),
    (
        "billing_fiscaltransition",
        "fiscal_read",
        "r",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (EXISTS ( SELECT 1\n   FROM billin"
            "g_fiscaldocument d\n  WHERE ((d.id = billing_fiscaltransition.doc"
            "ument_id) AND billing_fiscal_access(d.clinic_id)))))"
        ),
        None,
    ),
    (
        "billing_invoice",
        "billing_staff",
        "*",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND billing_charge_authorized(clinic_"
            "id))"
        ),
        None,
    ),
    (
        "billing_invoicerevision",
        "billing_staff",
        "*",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND billing_staff_invoice(invoice_id)"
            ")"
        ),
        None,
    ),
    (
        "billing_invoiceterms",
        "billing_journal_scope",
        "*",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND billing_book_access(book_id, 'jou"
            "rnal.read'::text))"
        ),
        None,
    ),
    (
        "billing_journalbook",
        "billing_journal_scope",
        "*",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND billing_finance_read(clinic_id))"
        ),
        None,
    ),
    (
        "billing_journalentry",
        "billing_journal_scope",
        "*",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND billing_book_access(book_id, 'jou"
            "rnal.read'::text))"
        ),
        None,
    ),
    (
        "billing_journalline",
        "billing_journal_scope",
        "*",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND billing_entry_readable(entry_id))"
        ),
        None,
    ),
    (
        "billing_journalsettlement",
        "billing_journal_scope",
        "*",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND billing_book_access(book_id, 'jou"
            "rnal.read'::text))"
        ),
        None,
    ),
    (
        "billing_paymentevent",
        "billing_event_recorder",
        "a",
        None,
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND billing_payment_event_recorder(op"
            "eration_id))"
        ),
    ),
    (
        "billing_paymentevent",
        "billing_staff",
        "r",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND billing_staff_invoice(invoice_id)"
            ")"
        ),
        None,
    ),
    (
        "billing_payout",
        "billing_journal_scope",
        "*",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND billing_book_access(book_id, 'jou"
            "rnal.read'::text))"
        ),
        None,
    ),
    (
        "billing_pixcharge",
        "billing_staff",
        "*",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND billing_staff_invoice(invoice_id)"
            ")"
        ),
        None,
    ),
    (
        "billing_pixoperation",
        "billing_staff",
        "*",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND billing_staff_invoice(invoice_id)"
            ")"
        ),
        None,
    ),
    (
        "billing_practitionerpayoutrule",
        "billing_journal_scope",
        "*",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND billing_book_access(book_id, 'jou"
            "rnal.read'::text))"
        ),
        None,
    ),
    (
        "billing_pspbinding",
        "billing_psp_insert",
        "a",
        None,
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND billing_psp_access(intent_id))"
        ),
    ),
    (
        "billing_pspbinding",
        "billing_psp_read",
        "r",
        "billing_psp_access(intent_id)",
        None,
    ),
    (
        "billing_pspevidence",
        "billing_psp_insert",
        "a",
        None,
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND billing_psp_access(intent_id))"
        ),
    ),
    (
        "billing_pspevidence",
        "billing_psp_read",
        "r",
        "billing_psp_access(intent_id)",
        None,
    ),
    (
        "billing_pspintent",
        "billing_psp_insert",
        "a",
        None,
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND true)"
        ),
    ),
    (
        "billing_pspintent",
        "billing_psp_read",
        "r",
        "billing_psp_access(id)",
        None,
    ),
    (
        "billing_psppolicy",
        "billing_psp_insert",
        "a",
        None,
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND billing_book_access(book_id, 'fin"
            "ance.policy'::text))"
        ),
    ),
    (
        "billing_psppolicy",
        "billing_psp_read",
        "r",
        (
            "(billing_book_access(book_id, 'journal.read'::text) OR billing_b"
            "ook_access(book_id, 'charge.collect'::text))"
        ),
        None,
    ),
    (
        "billing_pspreceipt",
        "billing_psp_insert",
        "a",
        None,
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND billing_psp_access(intent_id))"
        ),
    ),
    (
        "billing_pspreceipt",
        "billing_psp_read",
        "r",
        "billing_psp_access(intent_id)",
        None,
    ),
    (
        "billing_receipt",
        "billing_staff",
        "*",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND billing_staff_invoice(invoice_id)"
            ")"
        ),
        None,
    ),
    (
        "billing_refund",
        "billing_journal_scope",
        "*",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND billing_book_access(book_id, 'jou"
            "rnal.read'::text))"
        ),
        None,
    ),
    (
        "billing_settlement",
        "billing_staff",
        "*",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND billing_staff_invoice(invoice_id)"
            ")"
        ),
        None,
    ),
    (
        "billing_settlementallocation",
        "billing_journal_scope",
        "*",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND billing_book_access(book_id, 'jou"
            "rnal.read'::text))"
        ),
        None,
    ),
    (
        "comms_appointmentreminder",
        "tenant_isolation",
        "*",
        (
            "(organization_id = (NULLIF(current_setting('app.current_tenant',"
            " true), ''::text))::uuid)"
        ),
        None,
    ),
    (
        "comms_conversation",
        "tenant_isolation",
        "*",
        (
            "(organization_id = (NULLIF(current_setting('app.current_tenant',"
            " true), ''::text))::uuid)"
        ),
        None,
    ),
    (
        "comms_inboundevent",
        "tenant_isolation",
        "*",
        (
            "(organization_id = (NULLIF(current_setting('app.current_tenant',"
            " true), ''::text))::uuid)"
        ),
        None,
    ),
    (
        "comms_integrationoperation",
        "tenant_isolation",
        "*",
        (
            "(organization_id = (NULLIF(current_setting('app.current_tenant',"
            " true), ''::text))::uuid)"
        ),
        None,
    ),
    (
        "comms_internalnote",
        "tenant_isolation",
        "*",
        (
            "(organization_id = (NULLIF(current_setting('app.current_tenant',"
            " true), ''::text))::uuid)"
        ),
        None,
    ),
    (
        "comms_message",
        "tenant_isolation",
        "*",
        (
            "(organization_id = (NULLIF(current_setting('app.current_tenant',"
            " true), ''::text))::uuid)"
        ),
        None,
    ),
    (
        "comms_messagemeter",
        "tenant_isolation",
        "*",
        (
            "(organization_id = (NULLIF(current_setting('app.current_tenant',"
            " true), ''::text))::uuid)"
        ),
        None,
    ),
    (
        "comms_participant",
        "tenant_isolation",
        "*",
        (
            "(organization_id = (NULLIF(current_setting('app.current_tenant',"
            " true), ''::text))::uuid)"
        ),
        None,
    ),
    (
        "comms_provideraccount",
        "tenant_isolation",
        "*",
        (
            "(organization_id = (NULLIF(current_setting('app.current_tenant',"
            " true), ''::text))::uuid)"
        ),
        None,
    ),
    (
        "comms_quickreply",
        "tenant_isolation",
        "*",
        (
            "(organization_id = (NULLIF(current_setting('app.current_tenant',"
            " true), ''::text))::uuid)"
        ),
        None,
    ),
    (
        "comms_suggestedreply",
        "tenant_isolation",
        "*",
        (
            "(organization_id = (NULLIF(current_setting('app.current_tenant',"
            " true), ''::text))::uuid)"
        ),
        None,
    ),
    (
        "comms_templateversion",
        "tenant_isolation",
        "*",
        (
            "(organization_id = (NULLIF(current_setting('app.current_tenant',"
            " true), ''::text))::uuid)"
        ),
        None,
    ),
    (
        "consent_aiusedisclosure",
        "consent_ai_disclosure_insert",
        "a",
        None,
        (
            "has_permission('clinical.write'::text, clinic_id, ( SELECT en.id"
            "\n   FROM (ehr_encounter e\n     JOIN intake_patientclinicenrollme"
            "nt en ON (((en.organization_id = e.organization_id) AND (en.clin"
            "ic_id = e.clinic_id) AND (en.patient_id = e.patient_id))))\n  WHE"
            "RE (e.id = consent_aiusedisclosure.encounter_id)))"
        ),
    ),
    (
        "consent_aiusedisclosure",
        "consent_ai_disclosure_read",
        "r",
        (
            "(has_permission('demographics.read'::text, clinic_id, NULL::uuid"
            ") OR has_permission('configuration.organization'::text, clinic_i"
            "d, NULL::uuid) OR (EXISTS ( SELECT 1\n   FROM consent_session() s"
            "(session_id, organization_id, clinic_id, patient_id, enrollment_"
            "id)\n  WHERE ((consent_aiusedisclosure.patient_id = s.patient_id)"
            " AND (consent_aiusedisclosure.clinic_id = s.clinic_id)))))"
        ),
        None,
    ),
    (
        "consent_aiusedisclosure",
        "consent_alias_read",
        "r",
        (
            "(EXISTS ( SELECT 1\n   FROM (consent_session() s(session_id, orga"
            "nization_id, clinic_id, patient_id, enrollment_id)\n     CROSS JO"
            "IN LATERAL patient_alias_set(s.patient_id) lineage(patient_id, s"
            "urvivor_patient_id, unified_at))\n  WHERE ((s.organization_id = c"
            "onsent_aiusedisclosure.organization_id) AND (s.clinic_id = conse"
            "nt_aiusedisclosure.clinic_id) AND (consent_aiusedisclosure.patie"
            "nt_id = lineage.patient_id))))"
        ),
        None,
    ),
    (
        "consent_aiusedisclosure",
        "delegate_confidentiality",
        "*",
        "(NOT patient_is_delegate())",
        None,
    ),
    (
        "consent_consentacceptance",
        "consent_acceptance_insert",
        "a",
        None,
        (
            "(enrollment_id = ( SELECT s.enrollment_id\n   FROM consent_sessio"
            "n() s(session_id, organization_id, clinic_id, patient_id, enroll"
            "ment_id)))"
        ),
    ),
    (
        "consent_consentacceptance",
        "consent_acceptance_read",
        "r",
        (
            "(has_permission('demographics.read'::text, clinic_id, NULL::uuid"
            ") OR has_permission('configuration.organization'::text, clinic_i"
            "d, NULL::uuid) OR (enrollment_id = ( SELECT s.enrollment_id\n   F"
            "ROM consent_session() s(session_id, organization_id, clinic_id, "
            "patient_id, enrollment_id))))"
        ),
        None,
    ),
    (
        "consent_consentacceptance",
        "consent_alias_read",
        "r",
        (
            "(EXISTS ( SELECT 1\n   FROM (consent_session() s(session_id, orga"
            "nization_id, clinic_id, patient_id, enrollment_id)\n     CROSS JO"
            "IN LATERAL patient_alias_set(s.patient_id) lineage(patient_id, s"
            "urvivor_patient_id, unified_at))\n  WHERE ((s.organization_id = c"
            "onsent_consentacceptance.organization_id) AND (s.clinic_id = con"
            "sent_consentacceptance.clinic_id) AND (consent_consentacceptance"
            ".patient_id = lineage.patient_id))))"
        ),
        None,
    ),
    (
        "consent_consentacceptance",
        "delegate_purpose",
        "*",
        (
            "((NOT patient_is_delegate()) OR delegate_purpose_allowed((( SELE"
            "CT t.purpose\n   FROM consent_consenttext t\n  WHERE (t.id = conse"
            "nt_consentacceptance.text_id)))::text))"
        ),
        None,
    ),
    (
        "consent_consentrevocation",
        "consent_alias_read",
        "r",
        (
            "(EXISTS ( SELECT 1\n   FROM ((consent_consentacceptance a\n     JO"
            "IN consent_session() s(session_id, organization_id, clinic_id, p"
            "atient_id, enrollment_id) ON (((s.organization_id = a.organizati"
            "on_id) AND (s.clinic_id = a.clinic_id))))\n     CROSS JOIN LATERA"
            "L patient_alias_set(s.patient_id) lineage(patient_id, survivor_p"
            "atient_id, unified_at))\n  WHERE ((a.id = consent_consentrevocati"
            "on.acceptance_id) AND (a.organization_id = consent_consentrevoca"
            "tion.organization_id) AND (a.clinic_id = consent_consentrevocati"
            "on.clinic_id) AND (a.patient_id = lineage.patient_id))))"
        ),
        None,
    ),
    (
        "consent_consentrevocation",
        "consent_revocation_insert",
        "a",
        None,
        (
            "(EXISTS ( SELECT 1\n   FROM consent_consentacceptance a\n  WHERE ("
            "(a.id = consent_consentrevocation.acceptance_id) AND (a.enrollme"
            "nt_id = ( SELECT s.enrollment_id\n           FROM consent_session"
            "() s(session_id, organization_id, clinic_id, patient_id, enrollm"
            "ent_id))))))"
        ),
    ),
    (
        "consent_consentrevocation",
        "consent_revocation_read",
        "r",
        (
            "(has_permission('demographics.read'::text, clinic_id, NULL::uuid"
            ") OR has_permission('configuration.organization'::text, clinic_i"
            "d, NULL::uuid) OR (EXISTS ( SELECT 1\n   FROM consent_consentacce"
            "ptance a\n  WHERE ((a.id = consent_consentrevocation.acceptance_i"
            "d) AND (a.enrollment_id = ( SELECT s.enrollment_id\n           FR"
            "OM consent_session() s(session_id, organization_id, clinic_id, p"
            "atient_id, enrollment_id)))))))"
        ),
        None,
    ),
    (
        "consent_consentrevocation",
        "delegate_purpose",
        "*",
        (
            "((NOT patient_is_delegate()) OR (EXISTS ( SELECT 1\n   FROM (cons"
            "ent_consentacceptance a\n     JOIN consent_consenttext t ON ((t.i"
            "d = a.text_id)))\n  WHERE ((a.id = consent_consentrevocation.acce"
            "ptance_id) AND delegate_purpose_allowed((t.purpose)::text)))))"
        ),
        None,
    ),
    (
        "consent_consenttext",
        "consent_text_insert",
        "a",
        None,
        (
            "(has_permission('configuration.clinic'::text, clinic_id, NULL::u"
            "uid) OR has_permission('configuration.organization'::text, clini"
            "c_id, NULL::uuid))"
        ),
    ),
    (
        "consent_consenttext",
        "consent_text_read",
        "r",
        (
            "(has_permission('demographics.read'::text, clinic_id, NULL::uuid"
            ") OR has_permission('configuration.clinic'::text, clinic_id, NUL"
            "L::uuid) OR has_permission('configuration.organization'::text, c"
            "linic_id, NULL::uuid) OR (clinic_id = ( SELECT s.clinic_id\n   FR"
            "OM consent_session() s(session_id, organization_id, clinic_id, p"
            "atient_id, enrollment_id))))"
        ),
        None,
    ),
    (
        "consent_consenttext",
        "delegate_purpose",
        "*",
        ("((NOT patient_is_delegate()) OR delegate_purpose_allowed((purpose)::text))"),
        None,
    ),
    (
        "consent_noticeversion",
        "consent_notice_insert",
        "a",
        None,
        (
            "(has_permission('configuration.clinic'::text, clinic_id, NULL::u"
            "uid) OR has_permission('configuration.organization'::text, clini"
            "c_id, NULL::uuid))"
        ),
    ),
    (
        "consent_noticeversion",
        "consent_notice_read",
        "r",
        (
            "(has_permission('demographics.read'::text, clinic_id, NULL::uuid"
            ") OR has_permission('configuration.clinic'::text, clinic_id, NUL"
            "L::uuid) OR has_permission('configuration.organization'::text, c"
            "linic_id, NULL::uuid) OR (clinic_id = ( SELECT s.clinic_id\n   FR"
            "OM consent_session() s(session_id, organization_id, clinic_id, p"
            "atient_id, enrollment_id))))"
        ),
        None,
    ),
    (
        "consent_participantacknowledgment",
        "consent_participant_insert",
        "a",
        None,
        (
            "has_permission('clinical.write'::text, clinic_id, ( SELECT en.id"
            "\n   FROM (ehr_encounter e\n     JOIN intake_patientclinicenrollme"
            "nt en ON (((en.organization_id = e.organization_id) AND (en.clin"
            "ic_id = e.clinic_id) AND (en.patient_id = e.patient_id))))\n  WHE"
            "RE (e.id = consent_participantacknowledgment.session_id)))"
        ),
    ),
    (
        "consent_participantacknowledgment",
        "consent_participant_read",
        "r",
        (
            "(has_permission('demographics.read'::text, clinic_id, NULL::uuid"
            ") OR has_permission('configuration.organization'::text, clinic_i"
            "d, NULL::uuid))"
        ),
        None,
    ),
    (
        "consent_refusalrecord",
        "consent_alias_read",
        "r",
        (
            "(EXISTS ( SELECT 1\n   FROM (consent_session() s(session_id, orga"
            "nization_id, clinic_id, patient_id, enrollment_id)\n     CROSS JO"
            "IN LATERAL patient_alias_set(s.patient_id) lineage(patient_id, s"
            "urvivor_patient_id, unified_at))\n  WHERE ((s.organization_id = c"
            "onsent_refusalrecord.organization_id) AND (s.clinic_id = consent"
            "_refusalrecord.clinic_id) AND (consent_refusalrecord.patient_id "
            "= lineage.patient_id))))"
        ),
        None,
    ),
    (
        "consent_refusalrecord",
        "consent_refusal_insert",
        "a",
        None,
        (
            "(enrollment_id = ( SELECT s.enrollment_id\n   FROM consent_sessio"
            "n() s(session_id, organization_id, clinic_id, patient_id, enroll"
            "ment_id)))"
        ),
    ),
    (
        "consent_refusalrecord",
        "consent_refusal_read",
        "r",
        (
            "(has_permission('demographics.read'::text, clinic_id, NULL::uuid"
            ") OR has_permission('configuration.organization'::text, clinic_i"
            "d, NULL::uuid) OR (enrollment_id = ( SELECT s.enrollment_id\n   F"
            "ROM consent_session() s(session_id, organization_id, clinic_id, "
            "patient_id, enrollment_id))))"
        ),
        None,
    ),
    (
        "consent_refusalrecord",
        "delegate_purpose",
        "*",
        (
            "((NOT patient_is_delegate()) OR delegate_purpose_allowed((( SELE"
            "CT t.purpose\n   FROM consent_consenttext t\n  WHERE (t.id = conse"
            "nt_refusalrecord.text_id)))::text))"
        ),
        None,
    ),
    (
        "crm_campaign",
        "crm_staff_insert",
        "a",
        None,
        "crm_manager(clinic_id)",
    ),
    (
        "crm_campaign",
        "crm_staff_read",
        "r",
        "crm_manager(clinic_id)",
        None,
    ),
    (
        "crm_campaign",
        "crm_staff_update",
        "w",
        "crm_manager(clinic_id)",
        None,
    ),
    (
        "crm_campaignsend",
        "crm_staff_insert",
        "a",
        None,
        "crm_manager(clinic_id)",
    ),
    (
        "crm_campaignsend",
        "crm_staff_read",
        "r",
        "crm_manager(clinic_id)",
        None,
    ),
    (
        "crm_lead",
        "crm_staff_insert",
        "a",
        None,
        "crm_manager(clinic_id)",
    ),
    (
        "crm_lead",
        "crm_staff_read",
        "r",
        "crm_manager(clinic_id)",
        None,
    ),
    (
        "crm_lead",
        "crm_staff_update",
        "w",
        "crm_manager(clinic_id)",
        None,
    ),
    (
        "crm_marketingpreference",
        "crm_patient_insert",
        "a",
        None,
        (
            "(enrollment_id = ( SELECT s.enrollment_id\n   FROM consent_sessio"
            "n() s(session_id, organization_id, clinic_id, patient_id, enroll"
            "ment_id)))"
        ),
    ),
    (
        "crm_marketingpreference",
        "crm_patient_read",
        "r",
        (
            "(enrollment_id = ( SELECT s.enrollment_id\n   FROM consent_sessio"
            "n() s(session_id, organization_id, clinic_id, patient_id, enroll"
            "ment_id)))"
        ),
        None,
    ),
    (
        "crm_marketingpreference",
        "crm_staff_read",
        "r",
        "crm_manager(clinic_id)",
        None,
    ),
    (
        "crm_publicbookingprofile",
        "crm_staff_insert",
        "a",
        None,
        "crm_manager(clinic_id)",
    ),
    (
        "crm_publicbookingprofile",
        "crm_staff_read",
        "r",
        "crm_manager(clinic_id)",
        None,
    ),
    (
        "crm_publicbookingprofile",
        "crm_staff_update",
        "w",
        "crm_manager(clinic_id)",
        None,
    ),
    (
        "crm_referralsource",
        "crm_staff_insert",
        "a",
        None,
        "crm_manager(clinic_id)",
    ),
    (
        "crm_referralsource",
        "crm_staff_read",
        "r",
        "crm_manager(clinic_id)",
        None,
    ),
    (
        "crm_satisfactionsurvey",
        "crm_patient_insert",
        "a",
        None,
        (
            "(clinic_id = ( SELECT s.clinic_id\n   FROM patient_booking_scope("
            ") s(session_id, organization_id, clinic_id, patient_id, enrollme"
            "nt_id, clinic_name, timezone)))"
        ),
    ),
    (
        "crm_satisfactionsurvey",
        "crm_patient_read",
        "r",
        (
            "(EXISTS ( SELECT 1\n   FROM (scheduling_appointment a\n     JOIN p"
            "atient_booking_scope() s(session_id, organization_id, clinic_id,"
            " patient_id, enrollment_id, clinic_name, timezone) ON ((s.patien"
            "t_id = a.patient_id)))\n  WHERE ((a.id = crm_satisfactionsurvey.a"
            "ppointment_id) AND (a.clinic_id = s.clinic_id))))"
        ),
        None,
    ),
    (
        "crm_satisfactionsurvey",
        "crm_staff_read",
        "r",
        "crm_manager(clinic_id)",
        None,
    ),
    (
        "ehr_addendumsavereceipt",
        "addendum_receipt_insert",
        "a",
        None,
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (EXISTS ( SELECT 1\n   FROM ehr_en"
            "counteraddendum a\n  WHERE ((a.id = ehr_addendumsavereceipt.adden"
            "dum_id) AND ((a.state)::text = 'draft'::text)))))"
        ),
    ),
    (
        "ehr_addendumsavereceipt",
        "addendum_receipt_read",
        "r",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (EXISTS ( SELECT 1\n   FROM ehr_en"
            "counteraddendum a\n  WHERE ((a.id = ehr_addendumsavereceipt.adden"
            "dum_id) AND ((a.state)::text = 'draft'::text)))))"
        ),
        None,
    ),
    (
        "ehr_allergy",
        "history_insert",
        "a",
        None,
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (EXISTS ( SELECT 1\n   FROM ehr_hi"
            "storyassessment h\n  WHERE ((h.id = ehr_allergy.assessment_id) AN"
            "D ehr_assigned(h.encounter_id) AND (h.author_id = (NULLIF(curren"
            "t_setting('app.current_user_id', true), ''::text))::uuid)))))"
        ),
    ),
    (
        "ehr_allergy",
        "history_read",
        "r",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (EXISTS ( SELECT 1\n   FROM ehr_hi"
            "storyassessment h\n  WHERE ((h.id = ehr_allergy.assessment_id) AN"
            "D (h.organization_id = ehr_allergy.organization_id)))))"
        ),
        None,
    ),
    (
        "ehr_clinicalattachment",
        "attachment_insert",
        "a",
        None,
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (uploader_id = (NULLIF(current_se"
            "tting('app.current_user_id', true), ''::text))::uuid) AND ehr_as"
            "signed(encounter_id))"
        ),
    ),
    (
        "ehr_clinicalattachment",
        "attachment_read",
        "r",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (ehr_assigned(encounter_id) OR (("
            "(state)::text = 'available'::text) AND ehr_care(encounter_id))))"
        ),
        None,
    ),
    (
        "ehr_clinicalattachment",
        "attachment_scan",
        "w",
        ("(((state)::text = 'quarantined'::text) AND ehr_assigned(encounter_id))"),
        (
            "(((state)::text = ANY (ARRAY[('quarantined'::character varying):"
            ":text, ('available'::character varying)::text, ('rejected'::char"
            "acter varying)::text])) AND ehr_assigned(encounter_id))"
        ),
    ),
    (
        "ehr_clinicaldocument",
        "clinical_insert",
        "a",
        None,
        "ehr_assigned(encounter_id)",
    ),
    (
        "ehr_clinicaldocument",
        "clinical_read",
        "r",
        (
            "(EXISTS ( SELECT 1\n   FROM ehr_encounter e\n  WHERE ((e.id = ehr_"
            "clinicaldocument.encounter_id) AND (e.organization_id = ehr_clin"
            "icaldocument.organization_id))))"
        ),
        None,
    ),
    (
        "ehr_clinicaldocumentversion",
        "clinical_insert",
        "a",
        None,
        (
            "((author_id = (NULLIF(current_setting('app.current_user_id', tru"
            "e), ''::text))::uuid) AND (EXISTS ( SELECT 1\n   FROM ehr_clinica"
            "ldocument d\n  WHERE ((d.id = ehr_clinicaldocumentversion.documen"
            "t_id) AND ehr_assigned(d.encounter_id)))))"
        ),
    ),
    (
        "ehr_clinicaldocumentversion",
        "clinical_read",
        "r",
        (
            "((((state)::text <> 'discarded'::text) AND (EXISTS ( SELECT 1\n  "
            " FROM ehr_clinicaldocument d\n  WHERE ((d.id = ehr_clinicaldocume"
            "ntversion.document_id) AND (d.organization_id = ehr_clinicaldocu"
            "mentversion.organization_id) AND ((ehr_assigned(d.encounter_id) "
            "AND (((ehr_clinicaldocumentversion.state)::text <> 'draft'::text"
            ") OR (ehr_clinicaldocumentversion.author_id = (NULLIF(current_se"
            "tting('app.current_user_id', true), ''::text))::uuid))) OR (((eh"
            "r_clinicaldocumentversion.state)::text = ANY (ARRAY[('finalized'"
            "::character varying)::text, ('superseded'::character varying)::t"
            "ext])) AND ehr_care(d.encounter_id))))))) OR (((state)::text = '"
            "discarded'::text) AND (author_id = (NULLIF(current_setting('app."
            "current_user_id', true), ''::text))::uuid) AND (EXISTS ( SELECT "
            "1\n   FROM ehr_clinicaldocument d\n  WHERE ((d.id = ehr_clinicaldo"
            "cumentversion.document_id) AND ehr_assigned(d.encounter_id))))))"
        ),
        None,
    ),
    (
        "ehr_clinicaldocumentversion",
        "clinical_update",
        "w",
        (
            "((EXISTS ( SELECT 1\n   FROM ehr_clinicaldocument d\n  WHERE ((d.i"
            "d = ehr_clinicaldocumentversion.document_id) AND ehr_assigned(d."
            "encounter_id)))) AND ((((state)::text = 'draft'::text) AND (auth"
            "or_id = (NULLIF(current_setting('app.current_user_id', true), ''"
            "::text))::uuid)) OR ((state)::text = 'finalized'::text)))"
        ),
        (
            "((EXISTS ( SELECT 1\n   FROM ehr_clinicaldocument d\n  WHERE ((d.i"
            "d = ehr_clinicaldocumentversion.document_id) AND ehr_assigned(d."
            "encounter_id)))) AND ((((state)::text = ANY (ARRAY[('draft'::cha"
            "racter varying)::text, ('discarded'::character varying)::text]))"
            " AND (author_id = (NULLIF(current_setting('app.current_user_id',"
            " true), ''::text))::uuid)) OR ((state)::text = ANY (ARRAY[('fina"
            "lized'::character varying)::text, ('superseded'::character varyi"
            "ng)::text]))))"
        ),
    ),
    (
        "ehr_clinicaldocumentversion",
        "restricted_note_visibility",
        "*",
        "ehr_note_visible(document_id)",
        None,
    ),
    (
        "ehr_copiednoteblock",
        "copy_insert",
        "a",
        None,
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (EXISTS ( SELECT 1\n   FROM ehr_cl"
            "inicaldocumentversion v\n  WHERE ((v.id = ehr_copiednoteblock.tar"
            "get_id) AND (v.author_id = (NULLIF(current_setting('app.current_"
            "user_id', true), ''::text))::uuid) AND ehr_note_author(v.documen"
            "t_id)))) AND (EXISTS ( SELECT 1\n   FROM ehr_clinicaldocumentvers"
            "ion v\n  WHERE ((v.id = ehr_copiednoteblock.source_id) AND ((v.st"
            "ate)::text = ANY (ARRAY[('finalized'::character varying)::text, "
            "('superseded'::character varying)::text]))))))"
        ),
    ),
    (
        "ehr_copiednoteblock",
        "copy_read",
        "r",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (EXISTS ( SELECT 1\n   FROM ehr_cl"
            "inicaldocumentversion v\n  WHERE (v.id = ehr_copiednoteblock.targ"
            "et_id))))"
        ),
        None,
    ),
    (
        "ehr_copiednoteblock",
        "copy_review",
        "w",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (EXISTS ( SELECT 1\n   FROM ehr_cl"
            "inicaldocumentversion v\n  WHERE ((v.id = ehr_copiednoteblock.tar"
            "get_id) AND ((v.state)::text = 'draft'::text) AND (v.author_id ="
            " (NULLIF(current_setting('app.current_user_id', true), ''::text)"
            ")::uuid) AND ehr_note_author(v.document_id)))))"
        ),
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (reviewed_by_id = (NULLIF(current"
            "_setting('app.current_user_id', true), ''::text))::uuid))"
        ),
    ),
    (
        "ehr_drafteditstate",
        "draft_state_author",
        "*",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (author_id = (NULLIF(current_sett"
            "ing('app.current_user_id', true), ''::text))::uuid) AND (EXISTS "
            "( SELECT 1\n   FROM (ehr_clinicaldocumentversion v\n     JOIN ehr_"
            "clinicaldocument d ON ((d.id = v.document_id)))\n  WHERE ((v.id ="
            " ehr_drafteditstate.version_id) AND ((v.state)::text = 'draft'::"
            "text) AND (v.author_id = (NULLIF(current_setting('app.current_us"
            "er_id', true), ''::text))::uuid) AND ehr_assigned(d.encounter_id"
            ")))))"
        ),
        None,
    ),
    (
        "ehr_draftsavereceipt",
        "draft_receipt_insert",
        "a",
        None,
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (EXISTS ( SELECT 1\n   FROM (ehr_c"
            "linicaldocumentversion v\n     JOIN ehr_clinicaldocument d ON ((d"
            ".id = v.document_id)))\n  WHERE ((v.id = ehr_draftsavereceipt.ver"
            "sion_id) AND ((v.state)::text = 'draft'::text) AND (v.author_id "
            "= (NULLIF(current_setting('app.current_user_id', true), ''::text"
            "))::uuid) AND ehr_assigned(d.encounter_id)))))"
        ),
    ),
    (
        "ehr_draftsavereceipt",
        "draft_receipt_read",
        "r",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (EXISTS ( SELECT 1\n   FROM (ehr_c"
            "linicaldocumentversion v\n     JOIN ehr_clinicaldocument d ON ((d"
            ".id = v.document_id)))\n  WHERE ((v.id = ehr_draftsavereceipt.ver"
            "sion_id) AND ((v.state)::text = 'draft'::text) AND (v.author_id "
            "= (NULLIF(current_setting('app.current_user_id', true), ''::text"
            "))::uuid) AND ehr_assigned(d.encounter_id)))))"
        ),
        None,
    ),
    (
        "ehr_encounter",
        "clinical_insert",
        "a",
        None,
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (physician_id = (NULLIF(current_s"
            "etting('app.current_user_id', true), ''::text))::uuid) AND quest"
            "ionnaire_staff(clinic_id, ARRAY['physician'::text]) AND ((appoin"
            "tment_id IS NOT NULL) OR has_permission('encounter.open_unschedu"
            "led'::text, clinic_id, NULL::uuid)))"
        ),
    ),
    (
        "ehr_encounter",
        "clinical_lock",
        "w",
        "ehr_assigned(id)",
        None,
    ),
    (
        "ehr_encounter",
        "clinical_read",
        "r",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND questionnaire_staff(clinic_id, AR"
            "RAY['physician'::text, 'receptionist'::text, 'owner'::text, 'cli"
            "nic_admin'::text]))"
        ),
        None,
    ),
    (
        "ehr_encounteraddendum",
        "addendum_insert",
        "a",
        None,
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (author_id = (NULLIF(current_sett"
            "ing('app.current_user_id', true), ''::text))::uuid) AND has_perm"
            "ission('clinical.write'::text, clinic_id, ( SELECT p.id\n   FROM "
            "intake_patientclinicenrollment p\n  WHERE ((p.clinic_id = ehr_enc"
            "ounteraddendum.clinic_id) AND (p.patient_id = ehr_encounteradden"
            "dum.patient_id) AND (p.organization_id = ehr_encounteraddendum.o"
            "rganization_id)))) AND (NOT ehr_assigned(encounter_id)))"
        ),
    ),
    (
        "ehr_encounteraddendum",
        "addendum_read",
        "r",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (author_id = (NULLIF(current_sett"
            "ing('app.current_user_id', true), ''::text))::uuid) AND has_perm"
            "ission('clinical.read'::text, clinic_id, ( SELECT p.id\n   FROM i"
            "ntake_patientclinicenrollment p\n  WHERE ((p.clinic_id = ehr_enco"
            "unteraddendum.clinic_id) AND (p.patient_id = ehr_encounteraddend"
            "um.patient_id) AND (p.organization_id = ehr_encounteraddendum.or"
            "ganization_id)))))"
        ),
        None,
    ),
    (
        "ehr_encounteraddendum",
        "addendum_write",
        "w",
        (
            "(((state)::text = 'draft'::text) AND (author_id = (NULLIF(curren"
            "t_setting('app.current_user_id', true), ''::text))::uuid) AND ha"
            "s_permission('clinical.write'::text, clinic_id, ( SELECT p.id\n  "
            " FROM intake_patientclinicenrollment p\n  WHERE ((p.clinic_id = e"
            "hr_encounteraddendum.clinic_id) AND (p.patient_id = ehr_encounte"
            "raddendum.patient_id) AND (p.organization_id = ehr_encounteradde"
            "ndum.organization_id)))))"
        ),
        None,
    ),
    (
        "ehr_encounterintakereference",
        "clinical_insert",
        "a",
        None,
        "ehr_assigned(encounter_id)",
    ),
    (
        "ehr_encounterintakereference",
        "clinical_read",
        "r",
        "ehr_assigned(encounter_id)",
        None,
    ),
    (
        "ehr_episode",
        "episode_close",
        "w",
        (
            "(((state)::text = 'open'::text) AND has_permission('clinical.wri"
            "te'::text, clinic_id, ( SELECT p.id\n   FROM intake_patientclinic"
            "enrollment p\n  WHERE ((p.clinic_id = ehr_episode.clinic_id) AND "
            "(p.patient_id = ehr_episode.patient_id) AND (p.organization_id ="
            " ehr_episode.organization_id)))))"
        ),
        (
            "((closed_by_id = (NULLIF(current_setting('app.current_user_id', "
            "true), ''::text))::uuid) AND has_permission('clinical.write'::te"
            "xt, clinic_id, ( SELECT p.id\n   FROM intake_patientclinicenrollm"
            "ent p\n  WHERE ((p.clinic_id = ehr_episode.clinic_id) AND (p.pati"
            "ent_id = ehr_episode.patient_id) AND (p.organization_id = ehr_ep"
            "isode.organization_id)))))"
        ),
    ),
    (
        "ehr_episode",
        "episode_insert",
        "a",
        None,
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (opened_by_id = (NULLIF(current_s"
            "etting('app.current_user_id', true), ''::text))::uuid) AND has_p"
            "ermission('clinical.write'::text, clinic_id, ( SELECT p.id\n   FR"
            "OM intake_patientclinicenrollment p\n  WHERE ((p.clinic_id = ehr_"
            "episode.clinic_id) AND (p.patient_id = ehr_episode.patient_id) A"
            "ND (p.organization_id = ehr_episode.organization_id)))))"
        ),
    ),
    (
        "ehr_episode",
        "episode_read",
        "r",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND has_permission('clinical.read'::t"
            "ext, clinic_id, ( SELECT p.id\n   FROM intake_patientclinicenroll"
            "ment p\n  WHERE ((p.clinic_id = ehr_episode.clinic_id) AND (p.pat"
            "ient_id = ehr_episode.patient_id) AND (p.organization_id = ehr_e"
            "pisode.organization_id)))))"
        ),
        None,
    ),
    (
        "ehr_episodeencounter",
        "episode_member_insert",
        "a",
        None,
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (linked_by_id = (NULLIF(current_s"
            "etting('app.current_user_id', true), ''::text))::uuid) AND ehr_a"
            "ssigned(encounter_id) AND (EXISTS ( SELECT 1\n   FROM ehr_episode"
            " ep\n  WHERE ((ep.id = ehr_episodeencounter.episode_id) AND ((ep."
            "state)::text = 'open'::text) AND has_permission('clinical.write'"
            "::text, ep.clinic_id, ( SELECT p.id\n           FROM intake_patie"
            "ntclinicenrollment p\n          WHERE ((p.clinic_id = ep.clinic_i"
            "d) AND (p.patient_id = ep.patient_id) AND (p.organization_id = e"
            "p.organization_id))))))))"
        ),
    ),
    (
        "ehr_episodeencounter",
        "episode_member_read",
        "r",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (EXISTS ( SELECT 1\n   FROM ehr_ep"
            "isode ep\n  WHERE (ep.id = ehr_episodeencounter.episode_id))))"
        ),
        None,
    ),
    (
        "ehr_historyassessment",
        "history_insert",
        "a",
        None,
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (ehr_assigned(encounter_id) AND ("
            "author_id = (NULLIF(current_setting('app.current_user_id', true)"
            ", ''::text))::uuid)))"
        ),
    ),
    (
        "ehr_historyassessment",
        "history_read",
        "r",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND ehr_history_care(encounter_id))"
        ),
        None,
    ),
    (
        "ehr_medicationstatement",
        "longitudinal_confirm",
        "w",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND ehr_patient_permission('clinical."
            "write'::text, clinic_id, patient_id))"
        ),
        None,
    ),
    (
        "ehr_medicationstatement",
        "longitudinal_insert",
        "a",
        None,
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (performer_id = (NULLIF(current_s"
            "etting('app.current_user_id', true), ''::text))::uuid) AND ehr_p"
            "atient_permission('clinical.write'::text, clinic_id, patient_id)"
            ")"
        ),
    ),
    (
        "ehr_medicationstatement",
        "longitudinal_read",
        "r",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND ehr_patient_permission('clinical."
            "read'::text, clinic_id, patient_id))"
        ),
        None,
    ),
    (
        "ehr_noteaccessgrant",
        "note_grant_insert",
        "a",
        None,
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (granted_by_id = (NULLIF(current_"
            "setting('app.current_user_id', true), ''::text))::uuid) AND (EXI"
            "STS ( SELECT 1\n   FROM ehr_restrictednote n\n  WHERE ((n.id = ehr"
            "_noteaccessgrant.note_id) AND ehr_note_author(n.document_id)))))"
        ),
    ),
    (
        "ehr_noteaccessgrant",
        "note_grant_read",
        "r",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND ((grantee_id = (NULLIF(current_se"
            "tting('app.current_user_id', true), ''::text))::uuid) OR (grante"
            "d_by_id = (NULLIF(current_setting('app.current_user_id', true), "
            "''::text))::uuid)) AND (EXISTS ( SELECT 1\n   FROM ehr_restricted"
            "note n\n  WHERE (n.id = ehr_noteaccessgrant.note_id))))"
        ),
        None,
    ),
    (
        "ehr_noteaccessgrant",
        "note_grant_revoke",
        "w",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (granted_by_id = (NULLIF(current_"
            "setting('app.current_user_id', true), ''::text))::uuid) AND (EXI"
            "STS ( SELECT 1\n   FROM ehr_restrictednote n\n  WHERE ((n.id = ehr"
            "_noteaccessgrant.note_id) AND ehr_note_author(n.document_id)))))"
        ),
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (granted_by_id = (NULLIF(current_"
            "setting('app.current_user_id', true), ''::text))::uuid))"
        ),
    ),
    (
        "ehr_observation",
        "longitudinal_confirm",
        "w",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND ehr_patient_permission('clinical."
            "write'::text, clinic_id, patient_id))"
        ),
        None,
    ),
    (
        "ehr_observation",
        "longitudinal_insert",
        "a",
        None,
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (performer_id = (NULLIF(current_s"
            "etting('app.current_user_id', true), ''::text))::uuid) AND ehr_p"
            "atient_permission('clinical.write'::text, clinic_id, patient_id)"
            ")"
        ),
    ),
    (
        "ehr_observation",
        "longitudinal_read",
        "r",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (ehr_patient_permission('clinical"
            ".read'::text, clinic_id, patient_id) OR ehr_patient_permission('"
            "observation.write'::text, clinic_id, patient_id)))"
        ),
        None,
    ),
    (
        "ehr_observation",
        "observation_nursing_insert",
        "a",
        None,
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (performer_id = (NULLIF(current_s"
            "etting('app.current_user_id', true), ''::text))::uuid) AND ehr_p"
            "atient_permission('observation.write'::text, clinic_id, patient_"
            "id))"
        ),
    ),
    (
        "ehr_problem",
        "history_insert",
        "a",
        None,
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (EXISTS ( SELECT 1\n   FROM ehr_hi"
            "storyassessment h\n  WHERE ((h.id = ehr_problem.assessment_id) AN"
            "D ehr_assigned(h.encounter_id) AND (h.author_id = (NULLIF(curren"
            "t_setting('app.current_user_id', true), ''::text))::uuid)))))"
        ),
    ),
    (
        "ehr_problem",
        "history_read",
        "r",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (EXISTS ( SELECT 1\n   FROM ehr_hi"
            "storyassessment h\n  WHERE ((h.id = ehr_problem.assessment_id) AN"
            "D (h.organization_id = ehr_problem.organization_id)))))"
        ),
        None,
    ),
    (
        "ehr_restrictednote",
        "note_restriction_insert",
        "a",
        None,
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (restricted_by_id = (NULLIF(curre"
            "nt_setting('app.current_user_id', true), ''::text))::uuid) AND e"
            "hr_note_author(document_id))"
        ),
    ),
    (
        "ehr_restrictednote",
        "note_restriction_read",
        "r",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND ehr_note_visible(document_id))"
        ),
        None,
    ),
    (
        "ehr_specialtyentry",
        "specialty_insert",
        "a",
        None,
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (recorded_by_id = (NULLIF(current"
            "_setting('app.current_user_id', true), ''::text))::uuid) AND ehr"
            "_assigned(encounter_id) AND (EXISTS ( SELECT 1\n   FROM ehr_encou"
            "nter e\n  WHERE ((e.id = ehr_specialtyentry.encounter_id) AND ehr"
            "_patient_permission('clinical.write'::text, e.clinic_id, e.patie"
            "nt_id)))))"
        ),
    ),
    (
        "ehr_specialtyentry",
        "specialty_read",
        "r",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (ehr_assigned(encounter_id) OR eh"
            "r_care(encounter_id)) AND (EXISTS ( SELECT 1\n   FROM ehr_encount"
            "er e\n  WHERE ((e.id = ehr_specialtyentry.encounter_id) AND ehr_p"
            "atient_permission('clinical.read'::text, e.clinic_id, e.patient_"
            "id)))))"
        ),
        None,
    ),
    (
        "ehr_specialtypack",
        "pack_insert",
        "a",
        None,
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (clinical_owner_id = (NULLIF(curr"
            "ent_setting('app.current_user_id', true), ''::text))::uuid) AND "
            "questionnaire_staff(clinic_id, ARRAY['owner'::text, 'clinic_admi"
            "n'::text]))"
        ),
    ),
    (
        "ehr_specialtypack",
        "pack_read",
        "r",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND questionnaire_staff(clinic_id, AR"
            "RAY['owner'::text, 'clinic_admin'::text, 'physician'::text]))"
        ),
        None,
    ),
    (
        "ehr_specialtypack",
        "pack_retire",
        "w",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND questionnaire_staff(clinic_id, AR"
            "RAY['owner'::text, 'clinic_admin'::text]))"
        ),
        None,
    ),
    (
        "ehr_specialtytemplate",
        "clinical_insert",
        "a",
        None,
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND questionnaire_staff(clinic_id, AR"
            "RAY['clinic_admin'::text, 'owner'::text, 'physician'::text]))"
        ),
    ),
    (
        "ehr_specialtytemplate",
        "clinical_read",
        "r",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND questionnaire_staff(clinic_id, AR"
            "RAY['physician'::text, 'clinic_admin'::text, 'owner'::text]))"
        ),
        None,
    ),
    (
        "ehr_timelineprojection",
        "timeline_read",
        "r",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND ehr_timeline_visible((kind)::text"
            ", source_id, clinic_id, patient_id))"
        ),
        None,
    ),
    (
        "identity_authenticatorreset",
        "manager",
        "*",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (has_permission('staff.clinic'::t"
            "ext, clinic_id, NULL::uuid) OR has_permission('staff.organizatio"
            "n'::text, clinic_id, NULL::uuid)))"
        ),
        None,
    ),
    (
        "identity_authenticatorreset",
        "notified_user",
        "r",
        (
            "((user_id = (NULLIF(current_setting('app.current_user_id', true)"
            ", ''::text))::uuid) AND (NULLIF(current_setting('app.current_pat"
            "ient_session', true), ''::text) IS NULL) AND (NULLIF(current_set"
            "ting('app.current_principal', true), ''::text) IS NULL))"
        ),
        None,
    ),
    (
        "identity_authenticatorretirement",
        "own_user",
        "*",
        (
            "((user_id = (NULLIF(current_setting('app.current_user_id', true)"
            ", ''::text))::uuid) AND (NULLIF(current_setting('app.current_pat"
            "ient_session', true), ''::text) IS NULL) AND (NULLIF(current_set"
            "ting('app.current_principal', true), ''::text) IS NULL))"
        ),
        None,
    ),
    (
        "identity_careteammembership",
        "scope_read",
        "r",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (has_permission('staff.clinic'::t"
            "ext, clinic_id, NULL::uuid) OR has_permission('staff.organizatio"
            "n'::text, clinic_id, NULL::uuid) OR ((user_id = (NULLIF(current_"
            "setting('app.current_user_id', true), ''::text))::uuid) AND ques"
            "tionnaire_staff(clinic_id, ARRAY[(role)::text]))))"
        ),
        None,
    ),
    (
        "identity_clinic",
        "tenant_isolation",
        "*",
        (
            "(organization_id = (NULLIF(current_setting('app.current_tenant',"
            " true), ''::text))::uuid)"
        ),
        None,
    ),
    (
        "identity_clinicconfiguration",
        "configuration_insert",
        "a",
        None,
        (
            "(questionnaire_staff(clinic_id, ARRAY['owner'::text, 'clinic_adm"
            "in'::text]) OR has_permission('configuration.organization'::text"
            ", clinic_id, NULL::uuid))"
        ),
    ),
    (
        "identity_clinicconfiguration",
        "configuration_read",
        "r",
        (
            "(questionnaire_staff(clinic_id, ARRAY['owner'::text, 'clinic_adm"
            "in'::text, 'physician'::text, 'receptionist'::text]) OR has_perm"
            "ission('configuration.organization'::text, clinic_id, NULL::uuid"
            "))"
        ),
        None,
    ),
    (
        "identity_onboardingprogress",
        "manager",
        "*",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (has_permission('staff.clinic'::t"
            "ext, clinic_id, NULL::uuid) OR has_permission('staff.organizatio"
            "n'::text, clinic_id, NULL::uuid)))"
        ),
        None,
    ),
    (
        "identity_organization",
        "active_organization_runtime",
        "r",
        "((status)::text = 'active'::text)",
        None,
    ),
    (
        "identity_organization",
        "tenant_isolation",
        "*",
        (
            "(id = (NULLIF(current_setting('app.current_tenant', true), ''::t"
            "ext))::uuid)"
        ),
        None,
    ),
    (
        "identity_passkey",
        "passkey_owner_only",
        "*",
        (
            "((user_id = (NULLIF(current_setting('app.current_user_id', true)"
            ", ''::text))::uuid) AND (NULLIF(current_setting('app.current_pri"
            "ncipal', true), ''::text) IS NULL) AND (NULLIF(current_setting('"
            "app.current_patient_session', true), ''::text) IS NULL) AND user"
            "_has_org(organization_id))"
        ),
        None,
    ),
    (
        "identity_passkey",
        "tenant_isolation",
        "*",
        (
            "(organization_id = (NULLIF(current_setting('app.current_tenant',"
            " true), ''::text))::uuid)"
        ),
        None,
    ),
    (
        "identity_patientdelegategrant",
        "delegate_read",
        "r",
        (
            "(delegate_staff(clinic_id) OR delegate_grantor(clinic_id, repres"
            "ented_enrollment_id) OR delegate_grantor(clinic_id, delegate_enr"
            "ollment_id))"
        ),
        None,
    ),
    (
        "identity_physicianevidence",
        "physician_evidence_assigned",
        "*",
        (
            "(EXISTS ( SELECT 1\n   FROM (identity_physicianprofile p\n     JOI"
            "N ehr_encounter e ON ((e.id = identity_physicianevidence.encount"
            "er_id)))\n  WHERE ((p.id = identity_physicianevidence.profile_id)"
            " AND (p.organization_id = e.organization_id) AND (p.user_id = e."
            "physician_id) AND ((p.jurisdiction)::text = (( SELECT c.crm_uf\n "
            "          FROM identity_clinic c\n          WHERE (c.id = e.clini"
            "c_id)))::text) AND ehr_assigned(e.id) AND (EXISTS ( SELECT 1\n   "
            "        FROM identity_userclinicrole r\n          WHERE ((r.user_"
            "id = p.user_id) AND (r.organization_id = p.organization_id) AND "
            "(r.clinic_id = e.clinic_id) AND ((r.role)::text = 'physician'::t"
            "ext) AND (r.revoked_at IS NULL)))))))"
        ),
        None,
    ),
    (
        "identity_physicianprofile",
        "physician_self",
        "*",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (user_id = (NULLIF(current_settin"
            "g('app.current_user_id', true), ''::text))::uuid) AND (EXISTS ( "
            "SELECT 1\n   FROM identity_userclinicrole r\n  WHERE ((r.user_id ="
            " identity_physicianprofile.user_id) AND (r.organization_id = ide"
            "ntity_physicianprofile.organization_id) AND (r.revoked_at IS NUL"
            "L) AND ((r.role)::text = 'physician'::text)))))"
        ),
        None,
    ),
    (
        "identity_professionalregistration",
        "scope_read",
        "r",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (has_permission('staff.clinic'::t"
            "ext, clinic_id, NULL::uuid) OR has_permission('staff.organizatio"
            "n'::text, clinic_id, NULL::uuid) OR ((user_id = (NULLIF(current_"
            "setting('app.current_user_id', true), ''::text))::uuid) AND ques"
            "tionnaire_staff(clinic_id, ARRAY[(role)::text]))))"
        ),
        None,
    ),
    (
        "identity_recoverybatch",
        "own_user",
        "*",
        (
            "((user_id = (NULLIF(current_setting('app.current_user_id', true)"
            ", ''::text))::uuid) AND (NULLIF(current_setting('app.current_pat"
            "ient_session', true), ''::text) IS NULL) AND (NULLIF(current_set"
            "ting('app.current_principal', true), ''::text) IS NULL))"
        ),
        None,
    ),
    (
        "identity_recoverycode",
        "own_user",
        "*",
        (
            "((EXISTS ( SELECT 1\n   FROM identity_recoverybatch batch\n  WHERE"
            " ((batch.id = identity_recoverycode.batch_id) AND (batch.user_id"
            " = (NULLIF(current_setting('app.current_user_id', true), ''::tex"
            "t))::uuid)))) AND (NULLIF(current_setting('app.current_patient_s"
            "ession', true), ''::text) IS NULL) AND (NULLIF(current_setting('"
            "app.current_principal', true), ''::text) IS NULL))"
        ),
        None,
    ),
    (
        "identity_rolegrant",
        "scope_read",
        "r",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (has_permission('staff.clinic'::t"
            "ext, clinic_id, NULL::uuid) OR has_permission('staff.organizatio"
            "n'::text, clinic_id, NULL::uuid) OR questionnaire_staff(clinic_i"
            "d, ARRAY[(role)::text])))"
        ),
        None,
    ),
    (
        "identity_savedview",
        "savedview_owner_only",
        "*",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (user_id = (NULLIF(current_settin"
            "g('app.current_user_id', true), ''::text))::uuid) AND (EXISTS ( "
            "SELECT 1\n   FROM identity_userclinicrole role_row\n  WHERE ((role"
            "_row.user_id = identity_savedview.user_id) AND (role_row.clinic_"
            "id = identity_savedview.clinic_id) AND (role_row.organization_id"
            " = identity_savedview.organization_id) AND (role_row.revoked_at "
            "IS NULL)))))"
        ),
        None,
    ),
    (
        "identity_sessionrecord",
        "own_user",
        "*",
        (
            "((user_id = (NULLIF(current_setting('app.current_user_id', true)"
            ", ''::text))::uuid) AND (NULLIF(current_setting('app.current_pat"
            "ient_session', true), ''::text) IS NULL) AND (NULLIF(current_set"
            "ting('app.current_principal', true), ''::text) IS NULL))"
        ),
        None,
    ),
    (
        "identity_staffinvitation",
        "manager",
        "*",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (has_permission('staff.clinic'::t"
            "ext, clinic_id, NULL::uuid) OR has_permission('staff.organizatio"
            "n'::text, clinic_id, NULL::uuid)))"
        ),
        None,
    ),
    (
        "identity_userclinicrole",
        "tenant_isolation",
        "*",
        (
            "(organization_id = (NULLIF(current_setting('app.current_tenant',"
            " true), ''::text))::uuid)"
        ),
        None,
    ),
    (
        "identity_userpreference",
        "userpreference_owner_only",
        "*",
        (
            "(user_id = (NULLIF(current_setting('app.current_user_id', true),"
            " ''::text))::uuid)"
        ),
        None,
    ),
    (
        "insurance_appeal",
        "insurance_scope",
        "*",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (has_permission('tiss.read'::text"
            ", clinic_id, NULL::uuid) OR has_permission('tiss.clinical_approv"
            "e'::text, clinic_id, NULL::uuid)))"
        ),
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND has_permission('tiss.manage'::tex"
            "t, clinic_id, NULL::uuid))"
        ),
    ),
    (
        "insurance_batch",
        "insurance_scope",
        "*",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND has_permission('tiss.read'::text,"
            " clinic_id, NULL::uuid))"
        ),
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND has_permission('tiss.manage'::tex"
            "t, clinic_id, NULL::uuid))"
        ),
    ),
    (
        "insurance_batchguide",
        "insurance_scope",
        "*",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND has_permission('tiss.read'::text,"
            " clinic_id, NULL::uuid))"
        ),
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND has_permission('tiss.manage'::tex"
            "t, clinic_id, NULL::uuid))"
        ),
    ),
    (
        "insurance_clinicalapproval",
        "insurance_scope",
        "*",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (has_permission('tiss.read'::text"
            ", clinic_id, NULL::uuid) OR has_permission('tiss.clinical_approv"
            "e'::text, clinic_id, NULL::uuid)))"
        ),
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND has_permission('tiss.clinical_app"
            "rove'::text, clinic_id, NULL::uuid))"
        ),
    ),
    (
        "insurance_commandreceipt",
        "insurance_scope",
        "*",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (has_permission('tiss.read'::text"
            ", clinic_id, NULL::uuid) OR (((kind)::text = 'clinical_approved'"
            "::text) AND has_permission('tiss.clinical_approve'::text, clinic"
            "_id, NULL::uuid))))"
        ),
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND ((has_permission('tiss.manage'::t"
            "ext, clinic_id, NULL::uuid) OR (((kind)::text = 'clinical_approv"
            "ed'::text) AND has_permission('tiss.clinical_approve'::text, cli"
            "nic_id, NULL::uuid))) AND ((kind)::text <> 'coverage'::text)))"
        ),
    ),
    (
        "insurance_glosa",
        "insurance_scope",
        "*",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND has_permission('tiss.read'::text,"
            " clinic_id, NULL::uuid))"
        ),
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND has_permission('tiss.manage'::tex"
            "t, clinic_id, NULL::uuid))"
        ),
    ),
    (
        "insurance_guide",
        "insurance_scope",
        "*",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND has_permission('tiss.read'::text,"
            " clinic_id, NULL::uuid))"
        ),
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND has_permission('tiss.manage'::tex"
            "t, clinic_id, NULL::uuid))"
        ),
    ),
    (
        "insurance_insurancemembership",
        "insurance_scope",
        "*",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND has_permission('tiss.read'::text,"
            " clinic_id, NULL::uuid))"
        ),
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND has_permission('tiss.manage'::tex"
            "t, clinic_id, NULL::uuid))"
        ),
    ),
    (
        "insurance_payer",
        "insurance_scope",
        "*",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND has_permission('tiss.read'::text,"
            " clinic_id, NULL::uuid))"
        ),
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND has_permission('tiss.manage'::tex"
            "t, clinic_id, NULL::uuid))"
        ),
    ),
    (
        "insurance_payercontract",
        "insurance_scope",
        "*",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND has_permission('tiss.read'::text,"
            " clinic_id, NULL::uuid))"
        ),
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND has_permission('tiss.manage'::tex"
            "t, clinic_id, NULL::uuid))"
        ),
    ),
    (
        "insurance_payerrequest",
        "insurance_scope",
        "*",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND has_permission('tiss.read'::text,"
            " clinic_id, NULL::uuid))"
        ),
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND has_permission('tiss.manage'::tex"
            "t, clinic_id, NULL::uuid))"
        ),
    ),
    (
        "insurance_paymentmatch",
        "insurance_scope",
        "*",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND has_permission('tiss.read'::text,"
            " clinic_id, NULL::uuid))"
        ),
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND has_permission('tiss.manage'::tex"
            "t, clinic_id, NULL::uuid))"
        ),
    ),
    (
        "insurance_pricetable",
        "insurance_scope",
        "*",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND has_permission('tiss.read'::text,"
            " clinic_id, NULL::uuid))"
        ),
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND has_permission('tiss.manage'::tex"
            "t, clinic_id, NULL::uuid))"
        ),
    ),
    (
        "insurance_return",
        "insurance_scope",
        "*",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND has_permission('tiss.read'::text,"
            " clinic_id, NULL::uuid))"
        ),
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND has_permission('tiss.manage'::tex"
            "t, clinic_id, NULL::uuid))"
        ),
    ),
    (
        "insurance_submission",
        "insurance_scope",
        "*",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND has_permission('tiss.read'::text,"
            " clinic_id, NULL::uuid))"
        ),
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND has_permission('tiss.manage'::tex"
            "t, clinic_id, NULL::uuid))"
        ),
    ),
    (
        "insurance_tussterm",
        "insurance_scope",
        "*",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND has_permission('tiss.read'::text,"
            " clinic_id, NULL::uuid))"
        ),
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND has_permission('tiss.manage'::tex"
            "t, clinic_id, NULL::uuid))"
        ),
    ),
    (
        "intake_clinicintakepolicy",
        "tenant_isolation",
        "*",
        (
            "(organization_id = (NULLIF(current_setting('app.current_tenant',"
            " true), ''::text))::uuid)"
        ),
        None,
    ),
    (
        "intake_datasubjectrequest",
        "privacy_staff_read",
        "r",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND has_permission('privacy.requests'"
            "::text, clinic_id, NULL::uuid))"
        ),
        None,
    ),
    (
        "intake_demographicscorrection",
        "tenant_isolation",
        "*",
        (
            "(organization_id = (NULLIF(current_setting('app.current_tenant',"
            " true), ''::text))::uuid)"
        ),
        None,
    ),
    (
        "intake_duplicatecandidate",
        "interop_import_visibility",
        "r",
        (
            "(NOT (EXISTS ( SELECT 1\n   FROM interop_hiddenenrollment h\n  WHE"
            "RE (h.enrollment_id = ANY (ARRAY[intake_duplicatecandidate.enrol"
            "lment_a_id, intake_duplicatecandidate.enrollment_b_id])))))"
        ),
        None,
    ),
    (
        "intake_duplicatecandidate",
        "tenant_isolation",
        "*",
        (
            "(organization_id = (NULLIF(current_setting('app.current_tenant',"
            " true), ''::text))::uuid)"
        ),
        None,
    ),
    (
        "intake_emergencycontact",
        "tenant_isolation",
        "*",
        (
            "(organization_id = (NULLIF(current_setting('app.current_tenant',"
            " true), ''::text))::uuid)"
        ),
        None,
    ),
    (
        "intake_insurancemembership",
        "tenant_isolation",
        "*",
        (
            "(organization_id = (NULLIF(current_setting('app.current_tenant',"
            " true), ''::text))::uuid)"
        ),
        None,
    ),
    (
        "intake_mergedecision",
        "tenant_isolation",
        "*",
        (
            "(organization_id = (NULLIF(current_setting('app.current_tenant',"
            " true), ''::text))::uuid)"
        ),
        None,
    ),
    (
        "intake_patient",
        "tenant_isolation",
        "*",
        (
            "(organization_id = (NULLIF(current_setting('app.current_tenant',"
            " true), ''::text))::uuid)"
        ),
        None,
    ),
    (
        "intake_patientaccessgrant",
        "tenant_isolation",
        "*",
        (
            "(organization_id = (NULLIF(current_setting('app.current_tenant',"
            " true), ''::text))::uuid)"
        ),
        None,
    ),
    (
        "intake_patientaddress",
        "tenant_isolation",
        "*",
        (
            "(organization_id = (NULLIF(current_setting('app.current_tenant',"
            " true), ''::text))::uuid)"
        ),
        None,
    ),
    (
        "intake_patientalias",
        "tenant_isolation",
        "*",
        (
            "(organization_id = (NULLIF(current_setting('app.current_tenant',"
            " true), ''::text))::uuid)"
        ),
        None,
    ),
    (
        "intake_patientchannelpreference",
        "tenant_isolation",
        "*",
        (
            "(organization_id = (NULLIF(current_setting('app.current_tenant',"
            " true), ''::text))::uuid)"
        ),
        None,
    ),
    (
        "intake_patientclinicenrollment",
        "interop_import_visibility",
        "r",
        (
            "(NOT (EXISTS ( SELECT 1\n   FROM interop_hiddenenrollment h\n  WHE"
            "RE (h.enrollment_id = intake_patientclinicenrollment.id))))"
        ),
        None,
    ),
    (
        "intake_patientclinicenrollment",
        "tenant_isolation",
        "*",
        (
            "(organization_id = (NULLIF(current_setting('app.current_tenant',"
            " true), ''::text))::uuid)"
        ),
        None,
    ),
    (
        "intake_patientcontact",
        "tenant_isolation",
        "*",
        (
            "(organization_id = (NULLIF(current_setting('app.current_tenant',"
            " true), ''::text))::uuid)"
        ),
        None,
    ),
    (
        "intake_patientcontactevent",
        "tenant_isolation",
        "*",
        (
            "(organization_id = (NULLIF(current_setting('app.current_tenant',"
            " true), ''::text))::uuid)"
        ),
        None,
    ),
    (
        "intake_patientdemographics",
        "tenant_isolation",
        "*",
        (
            "(organization_id = (NULLIF(current_setting('app.current_tenant',"
            " true), ''::text))::uuid)"
        ),
        None,
    ),
    (
        "intake_patientduplicateindex",
        "tenant_isolation",
        "*",
        (
            "(organization_id = (NULLIF(current_setting('app.current_tenant',"
            " true), ''::text))::uuid)"
        ),
        None,
    ),
    (
        "intake_patientidentifier",
        "tenant_isolation",
        "*",
        (
            "(organization_id = (NULLIF(current_setting('app.current_tenant',"
            " true), ''::text))::uuid)"
        ),
        None,
    ),
    (
        "intake_patientmergetransfer",
        "tenant_isolation",
        "*",
        (
            "(organization_id = (NULLIF(current_setting('app.current_tenant',"
            " true), ''::text))::uuid)"
        ),
        None,
    ),
    (
        "intake_patientsession",
        "tenant_isolation",
        "*",
        (
            "(organization_id = (NULLIF(current_setting('app.current_tenant',"
            " true), ''::text))::uuid)"
        ),
        None,
    ),
    (
        "intake_questionnaireevent",
        "event_read",
        "r",
        "questionnaire_staff(clinic_id, ARRAY['physician'::text])",
        None,
    ),
    (
        "intake_questionnaireresponse",
        "delegate_confidentiality",
        "*",
        "((NOT patient_is_delegate()) OR (NOT restricted_from_delegates))",
        None,
    ),
    (
        "intake_questionnaireresponse",
        "response_assign",
        "a",
        None,
        "questionnaire_staff(clinic_id, ARRAY['physician'::text])",
    ),
    (
        "intake_questionnaireresponse",
        "response_read",
        "r",
        (
            "(questionnaire_staff(clinic_id, ARRAY['physician'::text]) OR (en"
            "rollment_id = questionnaire_patient_enrollment()))"
        ),
        None,
    ),
    (
        "intake_questionnaireresponse",
        "response_write",
        "w",
        (
            "(questionnaire_staff(clinic_id, ARRAY['physician'::text]) OR (en"
            "rollment_id = questionnaire_patient_enrollment()))"
        ),
        None,
    ),
    (
        "intake_questionnairetemplate",
        "template_read",
        "r",
        (
            "(questionnaire_staff(clinic_id, ARRAY['owner'::text, 'clinic_adm"
            "in'::text, 'physician'::text]) OR (EXISTS ( SELECT 1\n   FROM int"
            "ake_questionnaireresponse r\n  WHERE ((r.template_id = intake_que"
            "stionnairetemplate.id) AND (r.enrollment_id = questionnaire_pati"
            "ent_enrollment())))))"
        ),
        None,
    ),
    (
        "intake_questionnairetemplate",
        "template_write",
        "a",
        None,
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND questionnaire_staff(clinic_id, AR"
            "RAY['owner'::text, 'clinic_admin'::text, 'physician'::text]))"
        ),
    ),
    (
        "interop_apiclient",
        "interop_manage_read",
        "r",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND ( SELECT interop_manager((NULLIF("
            "current_setting('app.current_tenant', true), ''::text))::uuid) A"
            "S interop_manager))"
        ),
        None,
    ),
    (
        "interop_apiwritereceipt",
        "interop_manage_read",
        "r",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND ( SELECT interop_manager((NULLIF("
            "current_setting('app.current_tenant', true), ''::text))::uuid) A"
            "S interop_manager))"
        ),
        None,
    ),
    (
        "interop_hiddenenrollment",
        "tenant_isolation",
        "*",
        (
            "(organization_id = (NULLIF(current_setting('app.current_tenant',"
            " true), ''::text))::uuid)"
        ),
        None,
    ),
    (
        "interop_importbatch",
        "interop_insert",
        "a",
        None,
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND interop_allowed(clinic_id))"
        ),
    ),
    (
        "interop_importbatch",
        "interop_read",
        "r",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND interop_allowed(clinic_id))"
        ),
        None,
    ),
    (
        "interop_importbatch",
        "interop_update",
        "w",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND interop_allowed(clinic_id))"
        ),
        None,
    ),
    (
        "interop_importedrecord",
        "interop_insert",
        "a",
        None,
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND interop_allowed(clinic_id))"
        ),
    ),
    (
        "interop_importedrecord",
        "interop_read",
        "r",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (interop_allowed(clinic_id) OR in"
            "terop_record_visible(job_id, clinic_id, enrollment_id)))"
        ),
        None,
    ),
    (
        "interop_importfile",
        "interop_insert",
        "a",
        None,
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND interop_allowed(clinic_id))"
        ),
    ),
    (
        "interop_importfile",
        "interop_read",
        "r",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (interop_allowed(clinic_id) OR (E"
            "XISTS ( SELECT 1\n   FROM interop_importedrecord r\n  WHERE ((r.fi"
            "le_id = interop_importfile.id) AND interop_record_visible(r.job_"
            "id, r.clinic_id, r.enrollment_id))))))"
        ),
        None,
    ),
    (
        "interop_importjob",
        "interop_insert",
        "a",
        None,
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND interop_allowed(clinic_id))"
        ),
    ),
    (
        "interop_importjob",
        "interop_read",
        "r",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND interop_allowed(clinic_id))"
        ),
        None,
    ),
    (
        "interop_importmapping",
        "interop_insert",
        "a",
        None,
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND interop_allowed(clinic_id))"
        ),
    ),
    (
        "interop_importmapping",
        "interop_read",
        "r",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND interop_allowed(clinic_id))"
        ),
        None,
    ),
    (
        "interop_importtransition",
        "interop_insert",
        "a",
        None,
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND interop_allowed(clinic_id))"
        ),
    ),
    (
        "interop_importtransition",
        "interop_read",
        "r",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND interop_allowed(clinic_id))"
        ),
        None,
    ),
    (
        "interop_organizationexport",
        "interop_export_update",
        "w",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND ( SELECT interop_manager((NULLIF("
            "current_setting('app.current_tenant', true), ''::text))::uuid) A"
            "S interop_manager))"
        ),
        None,
    ),
    (
        "interop_organizationexport",
        "interop_manage_read",
        "r",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND ( SELECT interop_manager((NULLIF("
            "current_setting('app.current_tenant', true), ''::text))::uuid) A"
            "S interop_manager))"
        ),
        None,
    ),
    (
        "interop_organizationexportpart",
        "interop_manage_read",
        "r",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND ( SELECT interop_manager((NULLIF("
            "current_setting('app.current_tenant', true), ''::text))::uuid) A"
            "S interop_manager))"
        ),
        None,
    ),
    (
        "interop_organizationexportpart",
        "interop_part_insert",
        "a",
        None,
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND ( SELECT interop_manager((NULLIF("
            "current_setting('app.current_tenant', true), ''::text))::uuid) A"
            "S interop_manager))"
        ),
    ),
    (
        "interop_webhookattempt",
        "interop_manage_read",
        "r",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND ( SELECT interop_manager((NULLIF("
            "current_setting('app.current_tenant', true), ''::text))::uuid) A"
            "S interop_manager))"
        ),
        None,
    ),
    (
        "interop_webhookdelivery",
        "interop_manage_read",
        "r",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND ( SELECT interop_manager((NULLIF("
            "current_setting('app.current_tenant', true), ''::text))::uuid) A"
            "S interop_manager))"
        ),
        None,
    ),
    (
        "interop_webhooksubscription",
        "interop_manage_read",
        "r",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND ( SELECT interop_manager((NULLIF("
            "current_setting('app.current_tenant', true), ''::text))::uuid) A"
            "S interop_manager))"
        ),
        None,
    ),
    (
        "inventory_batch",
        "inventory_insert",
        "a",
        None,
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (NULLIF(current_setting('app.curr"
            "ent_principal', true), ''::text) IS NULL) AND has_permission('in"
            "ventory.manage'::text, clinic_id, NULL::uuid))"
        ),
    ),
    (
        "inventory_batch",
        "inventory_read",
        "r",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (NULLIF(current_setting('app.curr"
            "ent_principal', true), ''::text) IS NULL) AND (has_permission('i"
            "nventory.read'::text, clinic_id, NULL::uuid) OR (EXISTS ( SELECT"
            " 1\n   FROM inventory_location l\n  WHERE (inventory_batch_allowed"
            "(inventory_batch.id, l.clinic_id) AND (l.organization_id = inven"
            "tory_batch.organization_id))))))"
        ),
        None,
    ),
    (
        "inventory_calibration",
        "inventory_insert",
        "a",
        None,
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (NULLIF(current_setting('app.curr"
            "ent_principal', true), ''::text) IS NULL) AND has_permission('in"
            "ventory.manage'::text, clinic_id, NULL::uuid))"
        ),
    ),
    (
        "inventory_calibration",
        "inventory_read",
        "r",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (NULLIF(current_setting('app.curr"
            "ent_principal', true), ''::text) IS NULL) AND has_permission('in"
            "ventory.read'::text, clinic_id, NULL::uuid))"
        ),
        None,
    ),
    (
        "inventory_commandreceipt",
        "inventory_insert",
        "a",
        None,
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (NULLIF(current_setting('app.curr"
            "ent_principal', true), ''::text) IS NULL) AND has_permission(\nCA"
            "SE\n    WHEN ((operation)::text = ANY (ARRAY[('use'::character va"
            "rying)::text, ('return_stock'::character varying)::text, ('creat"
            "e_purchase_request'::character varying)::text, ('propose_repleni"
            "shment'::character varying)::text])) THEN 'inventory.use'::text\n"
            "    ELSE 'inventory.manage'::text\nEND, clinic_id, NULL::uuid))"
        ),
    ),
    (
        "inventory_commandreceipt",
        "inventory_read",
        "r",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (NULLIF(current_setting('app.curr"
            "ent_principal', true), ''::text) IS NULL) AND has_permission('in"
            "ventory.read'::text, clinic_id, NULL::uuid))"
        ),
        None,
    ),
    (
        "inventory_equipment",
        "inventory_insert",
        "a",
        None,
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (NULLIF(current_setting('app.curr"
            "ent_principal', true), ''::text) IS NULL) AND has_permission('in"
            "ventory.manage'::text, clinic_id, NULL::uuid))"
        ),
    ),
    (
        "inventory_equipment",
        "inventory_read",
        "r",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (NULLIF(current_setting('app.curr"
            "ent_principal', true), ''::text) IS NULL) AND has_permission('in"
            "ventory.read'::text, clinic_id, NULL::uuid))"
        ),
        None,
    ),
    (
        "inventory_item",
        "inventory_insert",
        "a",
        None,
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (NULLIF(current_setting('app.curr"
            "ent_principal', true), ''::text) IS NULL) AND has_permission('in"
            "ventory.manage'::text, clinic_id, NULL::uuid))"
        ),
    ),
    (
        "inventory_item",
        "inventory_read",
        "r",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (NULLIF(current_setting('app.curr"
            "ent_principal', true), ''::text) IS NULL) AND has_permission('in"
            "ventory.read'::text, clinic_id, NULL::uuid))"
        ),
        None,
    ),
    (
        "inventory_location",
        "inventory_insert",
        "a",
        None,
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (NULLIF(current_setting('app.curr"
            "ent_principal', true), ''::text) IS NULL) AND has_permission('in"
            "ventory.manage'::text, clinic_id, NULL::uuid))"
        ),
    ),
    (
        "inventory_location",
        "inventory_read",
        "r",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (NULLIF(current_setting('app.curr"
            "ent_principal', true), ''::text) IS NULL) AND has_permission('in"
            "ventory.read'::text, clinic_id, NULL::uuid))"
        ),
        None,
    ),
    (
        "inventory_maintenance",
        "inventory_insert",
        "a",
        None,
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (NULLIF(current_setting('app.curr"
            "ent_principal', true), ''::text) IS NULL) AND has_permission('in"
            "ventory.manage'::text, clinic_id, NULL::uuid))"
        ),
    ),
    (
        "inventory_maintenance",
        "inventory_read",
        "r",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (NULLIF(current_setting('app.curr"
            "ent_principal', true), ''::text) IS NULL) AND has_permission('in"
            "ventory.read'::text, clinic_id, NULL::uuid))"
        ),
        None,
    ),
    (
        "inventory_minimumstock",
        "inventory_insert",
        "a",
        None,
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (NULLIF(current_setting('app.curr"
            "ent_principal', true), ''::text) IS NULL) AND has_permission('in"
            "ventory.manage'::text, clinic_id, NULL::uuid))"
        ),
    ),
    (
        "inventory_minimumstock",
        "inventory_read",
        "r",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (NULLIF(current_setting('app.curr"
            "ent_principal', true), ''::text) IS NULL) AND has_permission('in"
            "ventory.read'::text, clinic_id, NULL::uuid))"
        ),
        None,
    ),
    (
        "inventory_purchasebudget",
        "inventory_insert",
        "a",
        None,
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (NULLIF(current_setting('app.curr"
            "ent_principal', true), ''::text) IS NULL) AND has_permission('in"
            "ventory.manage'::text, clinic_id, NULL::uuid))"
        ),
    ),
    (
        "inventory_purchasebudget",
        "inventory_read",
        "r",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (NULLIF(current_setting('app.curr"
            "ent_principal', true), ''::text) IS NULL) AND has_permission('in"
            "ventory.read'::text, clinic_id, NULL::uuid))"
        ),
        None,
    ),
    (
        "inventory_purchaserequest",
        "inventory_insert",
        "a",
        None,
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (NULLIF(current_setting('app.curr"
            "ent_principal', true), ''::text) IS NULL) AND has_permission('in"
            "ventory.use'::text, clinic_id, NULL::uuid))"
        ),
    ),
    (
        "inventory_purchaserequest",
        "inventory_read",
        "r",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (NULLIF(current_setting('app.curr"
            "ent_principal', true), ''::text) IS NULL) AND has_permission('in"
            "ventory.read'::text, clinic_id, NULL::uuid))"
        ),
        None,
    ),
    (
        "inventory_purchaserequest",
        "inventory_update",
        "w",
        (
            "((NULLIF(current_setting('app.current_principal', true), ''::tex"
            "t) IS NULL) AND has_permission('inventory.manage'::text, clinic_"
            "id, NULL::uuid) AND (organization_id = (NULLIF(current_setting('"
            "app.current_tenant', true), ''::text))::uuid))"
        ),
        (
            "((NULLIF(current_setting('app.current_principal', true), ''::tex"
            "t) IS NULL) AND has_permission('inventory.manage'::text, clinic_"
            "id, NULL::uuid))"
        ),
    ),
    (
        "inventory_stockmovement",
        "inventory_insert",
        "a",
        None,
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (NULLIF(current_setting('app.curr"
            "ent_principal', true), ''::text) IS NULL) AND has_permission(\nCA"
            "SE\n    WHEN ((kind)::text = ANY (ARRAY[('use'::character varying"
            ")::text, ('return'::character varying)::text])) THEN 'inventory."
            "use'::text\n    ELSE 'inventory.manage'::text\nEND, clinic_id, NUL"
            "L::uuid))"
        ),
    ),
    (
        "inventory_stockmovement",
        "inventory_read",
        "r",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (NULLIF(current_setting('app.curr"
            "ent_principal', true), ''::text) IS NULL) AND (has_permission('i"
            "nventory.read'::text, clinic_id, NULL::uuid) OR (EXISTS ( SELECT"
            " 1\n   FROM inventory_location l\n  WHERE ((l.id = inventory_stock"
            "movement.destination_location_id) AND has_permission('inventory."
            "read'::text, l.clinic_id, NULL::uuid))))))"
        ),
        None,
    ),
    (
        "inventory_supplier",
        "inventory_insert",
        "a",
        None,
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (NULLIF(current_setting('app.curr"
            "ent_principal', true), ''::text) IS NULL) AND has_permission('in"
            "ventory.manage'::text, clinic_id, NULL::uuid))"
        ),
    ),
    (
        "inventory_supplier",
        "inventory_read",
        "r",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (NULLIF(current_setting('app.curr"
            "ent_principal', true), ''::text) IS NULL) AND has_permission('in"
            "ventory.read'::text, clinic_id, NULL::uuid))"
        ),
        None,
    ),
    (
        "orders_labaccount",
        "orders_read",
        "r",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND has_permission('demographics.read"
            "'::text, clinic_id, NULL::uuid))"
        ),
        None,
    ),
    (
        "orders_order",
        "orders_insert",
        "a",
        None,
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND has_permission('order.place'::tex"
            "t, clinic_id, enrollment_id))"
        ),
    ),
    (
        "orders_order",
        "orders_read",
        "r",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (has_permission('order.route'::te"
            "xt, clinic_id, NULL::uuid) OR has_permission('result.read'::text"
            ", clinic_id, enrollment_id) OR has_permission('order.place'::tex"
            "t, clinic_id, enrollment_id)))"
        ),
        None,
    ),
    (
        "orders_orderevent",
        "orders_insert",
        "a",
        None,
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (EXISTS ( SELECT 1\n   FROM orders"
            "_order o\n  WHERE (o.id = orders_orderevent.order_id))))"
        ),
    ),
    (
        "orders_orderevent",
        "orders_read",
        "r",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (EXISTS ( SELECT 1\n   FROM orders"
            "_order o\n  WHERE (o.id = orders_orderevent.order_id))))"
        ),
        None,
    ),
    (
        "orders_orderitem",
        "orders_insert",
        "a",
        None,
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (EXISTS ( SELECT 1\n   FROM orders"
            "_order o\n  WHERE (o.id = orders_orderitem.order_id))))"
        ),
    ),
    (
        "orders_orderitem",
        "orders_read",
        "r",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (EXISTS ( SELECT 1\n   FROM orders"
            "_order o\n  WHERE (o.id = orders_orderitem.order_id))))"
        ),
        None,
    ),
    (
        "orders_result",
        "orders_insert",
        "a",
        None,
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND ((orders_lab_account(clinic_id) I"
            "S NOT NULL) OR has_permission('demographics.read'::text, clinic_"
            "id, NULL::uuid)))"
        ),
    ),
    (
        "orders_result",
        "orders_read",
        "r",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (EXISTS ( SELECT 1\n   FROM orders"
            "_resultversion v\n  WHERE ((v.result_id = orders_result.id) AND o"
            "rders_can_read_version(v.id)))))"
        ),
        None,
    ),
    (
        "orders_resultacknowledgment",
        "orders_insert",
        "a",
        None,
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND orders_can_read_version(version_i"
            "d))"
        ),
    ),
    (
        "orders_resultacknowledgment",
        "orders_read",
        "r",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND orders_can_read_version(version_i"
            "d))"
        ),
        None,
    ),
    (
        "orders_resultmatch",
        "orders_read",
        "r",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND orders_can_read_version(version_i"
            "d))"
        ),
        None,
    ),
    (
        "orders_resultobservation",
        "orders_insert",
        "a",
        None,
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND ((orders_lab_account(clinic_id) I"
            "S NOT NULL) OR (EXISTS ( SELECT 1\n   FROM orders_resultversion v"
            "\n  WHERE ((v.id = orders_resultobservation.version_id) AND order"
            "s_ingest_allowed(v.clinic_id, v.proposed_order_id))))))"
        ),
    ),
    (
        "orders_resultobservation",
        "orders_read",
        "r",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (EXISTS ( SELECT 1\n   FROM (order"
            "s_resultmatch m\n     JOIN orders_order o ON ((o.id = m.order_id)"
            "))\n  WHERE ((m.version_id = orders_resultobservation.version_id)"
            " AND has_permission('result.read'::text, o.clinic_id, o.enrollme"
            "nt_id)))))"
        ),
        None,
    ),
    (
        "orders_resultquarantine",
        "orders_read",
        "r",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND orders_can_read_version(version_i"
            "d))"
        ),
        None,
    ),
    (
        "orders_resultrejection",
        "orders_insert",
        "a",
        None,
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND has_permission('order.route'::tex"
            "t, clinic_id, NULL::uuid))"
        ),
    ),
    (
        "orders_resultrejection",
        "orders_read",
        "r",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND orders_can_read_version(version_i"
            "d))"
        ),
        None,
    ),
    (
        "orders_resultrelease",
        "orders_insert",
        "a",
        None,
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND orders_can_read_version(version_i"
            "d))"
        ),
    ),
    (
        "orders_resultrelease",
        "orders_read",
        "r",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND orders_can_read_version(version_i"
            "d))"
        ),
        None,
    ),
    (
        "orders_resultversion",
        "orders_insert",
        "a",
        None,
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND orders_ingest_allowed(clinic_id, "
            "proposed_order_id))"
        ),
    ),
    (
        "orders_resultversion",
        "orders_read",
        "r",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND orders_can_read_version(id))"
        ),
        None,
    ),
    (
        "orders_resultworkitem",
        "orders_read",
        "r",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND orders_can_read_version(version_i"
            "d))"
        ),
        None,
    ),
    (
        "otp_static_staticdevice",
        "own_user",
        "*",
        (
            "(user_id = (NULLIF(current_setting('app.current_user_id', true),"
            " ''::text))::uuid)"
        ),
        (
            "((user_id = (NULLIF(current_setting('app.current_user_id', true)"
            ", ''::text))::uuid) AND (NULLIF(current_setting('app.current_pri"
            "ncipal', true), ''::text) IS NULL) AND (NULLIF(current_setting('"
            "app.current_patient_session', true), ''::text) IS NULL))"
        ),
    ),
    (
        "otp_totp_totpdevice",
        "identity_totp_retirement",
        "*",
        (
            "(NOT (EXISTS ( SELECT 1\n   FROM identity_authenticatorretirement"
            " retirement\n  WHERE (retirement.device_id = otp_totp_totpdevice."
            "id))))"
        ),
        None,
    ),
    (
        "otp_totp_totpdevice",
        "otp_totp_device_user_isolation",
        "*",
        (
            "(user_id = (NULLIF(current_setting('app.current_user_id', true),"
            " ''::text))::uuid)"
        ),
        None,
    ),
    (
        "prescription_documentdraftcontent",
        "content_insert",
        "a",
        None,
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (EXISTS ( SELECT 1\n   FROM prescr"
            "iption_prescriptiondraft d\n  WHERE ((d.id = prescription_documen"
            "tdraftcontent.draft_id) AND ((d.state)::text = 'draft'::text) AN"
            "D ((d.kind)::text <> 'prescription'::text) AND (d.version = pres"
            "cription_documentdraftcontent.version)))))"
        ),
    ),
    (
        "prescription_documentdraftcontent",
        "content_read",
        "r",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (EXISTS ( SELECT 1\n   FROM prescr"
            "iption_prescriptiondraft d\n  WHERE ((d.id = prescription_documen"
            "tdraftcontent.draft_id) AND ((d.state)::text = 'draft'::text))))"
            ")"
        ),
        None,
    ),
    (
        "prescription_medicationfavorite",
        "favorite_insert",
        "a",
        None,
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (issuer_id = (NULLIF(current_sett"
            "ing('app.current_user_id', true), ''::text))::uuid) AND question"
            "naire_staff(clinic_id, ARRAY['physician'::text]))"
        ),
    ),
    (
        "prescription_medicationfavorite",
        "favorite_read",
        "r",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (issuer_id = (NULLIF(current_sett"
            "ing('app.current_user_id', true), ''::text))::uuid) AND question"
            "naire_staff(clinic_id, ARRAY['physician'::text]))"
        ),
        None,
    ),
    (
        "prescription_medicationfavorite",
        "favorite_update",
        "w",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (issuer_id = (NULLIF(current_sett"
            "ing('app.current_user_id', true), ''::text))::uuid) AND question"
            "naire_staff(clinic_id, ARRAY['physician'::text]))"
        ),
        None,
    ),
    (
        "prescription_prescriptiondocument",
        "document_insert",
        "a",
        None,
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (issuer_id = (NULLIF(current_sett"
            "ing('app.current_user_id', true), ''::text))::uuid) AND ehr_assi"
            "gned(encounter_id))"
        ),
    ),
    (
        "prescription_prescriptiondocument",
        "document_read",
        "r",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND ehr_care(encounter_id))"
        ),
        None,
    ),
    (
        "prescription_prescriptiondocumentrelease",
        "delegate_confidentiality",
        "*",
        "((NOT patient_is_delegate()) OR (NOT restricted_from_delegates))",
        None,
    ),
    (
        "prescription_prescriptiondocumentrelease",
        "release_insert",
        "a",
        None,
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (issuer_id = (NULLIF(current_sett"
            "ing('app.current_user_id', true), ''::text))::uuid) AND (release"
            "d_by_id = (NULLIF(current_setting('app.current_user_id', true), "
            "''::text))::uuid) AND ehr_assigned(encounter_id))"
        ),
    ),
    (
        "prescription_prescriptiondocumentrelease",
        "release_read",
        "r",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND ehr_care(encounter_id))"
        ),
        None,
    ),
    (
        "prescription_prescriptiondocumentrelease",
        "release_revoke",
        "w",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (issuer_id = (NULLIF(current_sett"
            "ing('app.current_user_id', true), ''::text))::uuid) AND ehr_assi"
            "gned(encounter_id))"
        ),
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (issuer_id = (NULLIF(current_sett"
            "ing('app.current_user_id', true), ''::text))::uuid) AND (revoked"
            "_by_id = (NULLIF(current_setting('app.current_user_id', true), '"
            "'::text))::uuid) AND ehr_assigned(encounter_id))"
        ),
    ),
    (
        "prescription_prescriptiondocumentrevocation",
        "revocation_insert",
        "a",
        None,
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (issuer_id = (NULLIF(current_sett"
            "ing('app.current_user_id', true), ''::text))::uuid) AND (revoked"
            "_by_id = (NULLIF(current_setting('app.current_user_id', true), '"
            "'::text))::uuid) AND ehr_assigned(encounter_id))"
        ),
    ),
    (
        "prescription_prescriptiondocumentrevocation",
        "revocation_read",
        "r",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND ehr_care(encounter_id))"
        ),
        None,
    ),
    (
        "prescription_prescriptiondraft",
        "draft_insert",
        "a",
        None,
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (issuer_id = (NULLIF(current_sett"
            "ing('app.current_user_id', true), ''::text))::uuid) AND ehr_assi"
            "gned(encounter_id) AND (((kind)::text = 'prescription'::text) OR"
            " (EXISTS ( SELECT 1\n   FROM intake_patientclinicenrollment enrol"
            "lment\n  WHERE ((enrollment.patient_id = prescription_prescriptio"
            "ndraft.patient_id) AND (enrollment.clinic_id = prescription_pres"
            "criptiondraft.clinic_id) AND has_permission('prescription.prepar"
            "e'::text, enrollment.clinic_id, enrollment.id))))))"
        ),
    ),
    (
        "prescription_prescriptiondraft",
        "draft_read",
        "r",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (issuer_id = (NULLIF(current_sett"
            "ing('app.current_user_id', true), ''::text))::uuid) AND ehr_assi"
            "gned(encounter_id) AND (((kind)::text = 'prescription'::text) OR"
            " (EXISTS ( SELECT 1\n   FROM intake_patientclinicenrollment enrol"
            "lment\n  WHERE ((enrollment.patient_id = prescription_prescriptio"
            "ndraft.patient_id) AND (enrollment.clinic_id = prescription_pres"
            "criptiondraft.clinic_id) AND has_permission('prescription.prepar"
            "e'::text, enrollment.clinic_id, enrollment.id))))))"
        ),
        None,
    ),
    (
        "prescription_prescriptiondraft",
        "draft_update",
        "w",
        (
            "(((state)::text = 'draft'::text) AND (organization_id = (NULLIF("
            "current_setting('app.current_tenant', true), ''::text))::uuid) A"
            "ND (issuer_id = (NULLIF(current_setting('app.current_user_id', t"
            "rue), ''::text))::uuid) AND ehr_assigned(encounter_id))"
        ),
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (issuer_id = (NULLIF(current_sett"
            "ing('app.current_user_id', true), ''::text))::uuid) AND ehr_assi"
            "gned(encounter_id))"
        ),
    ),
    (
        "prescription_prescriptionissuance",
        "issuer_insert",
        "a",
        None,
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (issuer_id = (NULLIF(current_sett"
            "ing('app.current_user_id', true), ''::text))::uuid) AND (EXISTS "
            "( SELECT 1\n   FROM prescription_prescriptiondocument d\n  WHERE ("
            "(d.id = prescription_prescriptionissuance.document_id) AND ehr_a"
            "ssigned(d.encounter_id)))))"
        ),
    ),
    (
        "prescription_prescriptionissuance",
        "issuer_read",
        "r",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (issuer_id = (NULLIF(current_sett"
            "ing('app.current_user_id', true), ''::text))::uuid) AND (EXISTS "
            "( SELECT 1\n   FROM identity_userclinicrole r\n  WHERE ((r.clinic_"
            "id = prescription_prescriptionissuance.clinic_id) AND (r.user_id"
            " = prescription_prescriptionissuance.issuer_id) AND ((r.role)::t"
            "ext = 'physician'::text) AND (r.revoked_at IS NULL)))))"
        ),
        None,
    ),
    (
        "prescription_prescriptionitem",
        "item_insert",
        "a",
        None,
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (EXISTS ( SELECT 1\n   FROM prescr"
            "iption_prescriptiondraft d\n  WHERE ((d.id = prescription_prescri"
            "ptionitem.draft_id) AND ((d.state)::text = 'draft'::text) AND (d"
            ".version = prescription_prescriptionitem.version)))))"
        ),
    ),
    (
        "prescription_prescriptionitem",
        "item_read",
        "r",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (EXISTS ( SELECT 1\n   FROM prescr"
            "iption_prescriptiondraft d\n  WHERE ((d.id = prescription_prescri"
            "ptionitem.draft_id) AND ((d.state)::text = 'draft'::text)))))"
        ),
        None,
    ),
    (
        "prescription_signaturecallback",
        "signature_callback_insert",
        "a",
        None,
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (EXISTS ( SELECT 1\n   FROM prescr"
            "iption_signatureoperation o\n  WHERE ((o.id = prescription_signat"
            "urecallback.operation_id) AND (o.issuer_id = (NULLIF(current_set"
            "ting('app.current_user_id', true), ''::text))::uuid) AND ehr_ass"
            "igned(o.encounter_id)))))"
        ),
    ),
    (
        "prescription_signaturecallback",
        "signature_callback_read",
        "r",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (EXISTS ( SELECT 1\n   FROM prescr"
            "iption_signatureoperation o\n  WHERE (o.id = prescription_signatu"
            "recallback.operation_id))))"
        ),
        None,
    ),
    (
        "prescription_signatureoperation",
        "signature_insert",
        "a",
        None,
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (issuer_id = (NULLIF(current_sett"
            "ing('app.current_user_id', true), ''::text))::uuid) AND ehr_assi"
            "gned(encounter_id))"
        ),
    ),
    (
        "prescription_signatureoperation",
        "signature_read",
        "r",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (issuer_id = (NULLIF(current_sett"
            "ing('app.current_user_id', true), ''::text))::uuid) AND ehr_assi"
            "gned(encounter_id))"
        ),
        None,
    ),
    (
        "prescription_signatureoperation",
        "signature_update",
        "w",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (issuer_id = (NULLIF(current_sett"
            "ing('app.current_user_id', true), ''::text))::uuid) AND ehr_assi"
            "gned(encounter_id))"
        ),
        None,
    ),
    (
        "prescription_sncrnumberallocation",
        "issuer_read",
        "r",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (issuer_id = (NULLIF(current_sett"
            "ing('app.current_user_id', true), ''::text))::uuid) AND (EXISTS "
            "( SELECT 1\n   FROM identity_userclinicrole r\n  WHERE ((r.clinic_"
            "id = prescription_sncrnumberallocation.clinic_id) AND (r.user_id"
            " = prescription_sncrnumberallocation.issuer_id) AND ((r.role)::t"
            "ext = 'physician'::text) AND (r.revoked_at IS NULL)))))"
        ),
        None,
    ),
    (
        "retention_artifactpurge",
        "purge_read",
        "r",
        "retention_purge_visible(id)",
        None,
    ),
    (
        "retention_legalhold",
        "hold_insert",
        "a",
        None,
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (placed_by_id = (NULLIF(current_s"
            "etting('app.current_user_id', true), ''::text))::uuid) AND quest"
            "ionnaire_staff(clinic_id, ARRAY['owner'::text, 'clinic_admin'::t"
            "ext]))"
        ),
    ),
    (
        "retention_legalhold",
        "hold_read",
        "r",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND questionnaire_staff(clinic_id, AR"
            "RAY['physician'::text, 'receptionist'::text, 'owner'::text, 'cli"
            "nic_admin'::text]))"
        ),
        None,
    ),
    (
        "retention_legalhold",
        "hold_release",
        "w",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND questionnaire_staff(clinic_id, AR"
            "RAY['owner'::text, 'clinic_admin'::text]))"
        ),
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (released_at IS NOT NULL) AND que"
            "stionnaire_staff(clinic_id, ARRAY['owner'::text, 'clinic_admin':"
            ":text]))"
        ),
    ),
    (
        "retention_recordexport",
        "export_insert_patient",
        "a",
        None,
        (
            "(((kind)::text = 'patient'::text) AND (requested_by_id IS NULL) "
            "AND (EXISTS ( SELECT 1\n   FROM retention_records_session() s(ses"
            "sion_id, organization_id, clinic_id, patient_id)\n  WHERE ((s.ses"
            "sion_id = retention_recordexport.patient_session_id) AND (s.orga"
            "nization_id = retention_recordexport.organization_id) AND (s.cli"
            "nic_id = retention_recordexport.clinic_id) AND (s.patient_id = r"
            "etention_recordexport.patient_id)))))"
        ),
    ),
    (
        "retention_recordexport",
        "export_insert_staff",
        "a",
        None,
        (
            "(((kind)::text = 'staff'::text) AND (organization_id = (NULLIF(c"
            "urrent_setting('app.current_tenant', true), ''::text))::uuid) AN"
            "D (requested_by_id = (NULLIF(current_setting('app.current_user_i"
            "d', true), ''::text))::uuid) AND retention_care(clinic_id, patie"
            "nt_id))"
        ),
    ),
    (
        "retention_recordexport",
        "export_read",
        "r",
        (
            "(((organization_id = (NULLIF(current_setting('app.current_tenant"
            "', true), ''::text))::uuid) AND questionnaire_staff(clinic_id, A"
            "RRAY['physician'::text, 'receptionist'::text, 'owner'::text, 'cl"
            "inic_admin'::text])) OR (EXISTS ( SELECT 1\n   FROM retention_rec"
            "ords_session() s(session_id, organization_id, clinic_id, patient"
            "_id)\n  WHERE ((s.organization_id = retention_recordexport.organi"
            "zation_id) AND (s.clinic_id = retention_recordexport.clinic_id) "
            "AND (s.patient_id = retention_recordexport.patient_id)))))"
        ),
        None,
    ),
    (
        "retention_recordrelease",
        "delegate_confidentiality",
        "*",
        "((NOT patient_is_delegate()) OR (NOT restricted_from_delegates))",
        None,
    ),
    (
        "retention_recordrelease",
        "release_insert",
        "a",
        None,
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (released_by_id = (NULLIF(current"
            "_setting('app.current_user_id', true), ''::text))::uuid) AND (EX"
            "ISTS ( SELECT 1\n   FROM (ehr_clinicaldocumentversion v\n     JOIN"
            " ehr_clinicaldocument d ON ((d.id = v.document_id)))\n  WHERE ((v"
            ".id = retention_recordrelease.version_id) AND ehr_assigned(d.enc"
            "ounter_id)))))"
        ),
    ),
    (
        "retention_recordrelease",
        "release_read",
        "r",
        (
            "(((organization_id = (NULLIF(current_setting('app.current_tenant"
            "', true), ''::text))::uuid) AND questionnaire_staff(clinic_id, A"
            "RRAY['physician'::text, 'receptionist'::text, 'owner'::text, 'cl"
            "inic_admin'::text])) OR (EXISTS ( SELECT 1\n   FROM retention_rec"
            "ords_session() s(session_id, organization_id, clinic_id, patient"
            "_id)\n  WHERE ((s.organization_id = retention_recordrelease.organ"
            "ization_id) AND (s.clinic_id = retention_recordrelease.clinic_id"
            ") AND (s.patient_id = retention_recordrelease.patient_id)))))"
        ),
        None,
    ),
    (
        "retention_recordrelease",
        "release_revoke",
        "w",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (EXISTS ( SELECT 1\n   FROM (ehr_c"
            "linicaldocumentversion v\n     JOIN ehr_clinicaldocument d ON ((d"
            ".id = v.document_id)))\n  WHERE ((v.id = retention_recordrelease."
            "version_id) AND ehr_assigned(d.encounter_id)))))"
        ),
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (revoked_at IS NOT NULL) AND (EXI"
            "STS ( SELECT 1\n   FROM (ehr_clinicaldocumentversion v\n     JOIN "
            "ehr_clinicaldocument d ON ((d.id = v.document_id)))\n  WHERE ((v."
            "id = retention_recordrelease.version_id) AND ehr_assigned(d.enco"
            "unter_id)))))"
        ),
    ),
    (
        "retention_retentionpolicy",
        "policy_insert",
        "a",
        None,
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (proposed_by_id = (NULLIF(current"
            "_setting('app.current_user_id', true), ''::text))::uuid) AND que"
            "stionnaire_staff(clinic_id, ARRAY['owner'::text, 'clinic_admin':"
            ":text]))"
        ),
    ),
    (
        "retention_retentionpolicy",
        "policy_read",
        "r",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND questionnaire_staff(clinic_id, AR"
            "RAY['physician'::text, 'receptionist'::text, 'owner'::text, 'cli"
            "nic_admin'::text]))"
        ),
        None,
    ),
    (
        "retention_retentionpolicy",
        "policy_update",
        "w",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND questionnaire_staff(clinic_id, AR"
            "RAY['owner'::text, 'clinic_admin'::text]))"
        ),
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND ((state)::text = ANY (ARRAY[('app"
            "roved'::character varying)::text, ('retired'::character varying)"
            "::text])) AND questionnaire_staff(clinic_id, ARRAY['owner'::text"
            ", 'clinic_admin'::text]))"
        ),
    ),
    (
        "scheduling_absence",
        "tenant_isolation",
        "*",
        (
            "(organization_id = (NULLIF(current_setting('app.current_tenant',"
            " true), ''::text))::uuid)"
        ),
        None,
    ),
    (
        "scheduling_appointment",
        "patient_booking_insert",
        "a",
        None,
        (
            "(EXISTS ( SELECT 1\n   FROM patient_booking_scope() s(session_id,"
            " organization_id, clinic_id, patient_id, enrollment_id, clinic_n"
            "ame, timezone)\n  WHERE ((s.organization_id = scheduling_appointm"
            "ent.organization_id) AND (s.clinic_id = scheduling_appointment.c"
            "linic_id) AND (s.patient_id = scheduling_appointment.patient_id)"
            ")))"
        ),
    ),
    (
        "scheduling_appointment",
        "patient_booking_read",
        "r",
        (
            "(EXISTS ( SELECT 1\n   FROM patient_booking_scope() s(session_id,"
            " organization_id, clinic_id, patient_id, enrollment_id, clinic_n"
            "ame, timezone)\n  WHERE ((s.organization_id = scheduling_appointm"
            "ent.organization_id) AND (s.clinic_id = scheduling_appointment.c"
            "linic_id) AND (s.patient_id = scheduling_appointment.patient_id)"
            ")))"
        ),
        None,
    ),
    (
        "scheduling_appointment",
        "patient_booking_update",
        "w",
        (
            "(EXISTS ( SELECT 1\n   FROM patient_booking_scope() s(session_id,"
            " organization_id, clinic_id, patient_id, enrollment_id, clinic_n"
            "ame, timezone)\n  WHERE ((s.organization_id = scheduling_appointm"
            "ent.organization_id) AND (s.clinic_id = scheduling_appointment.c"
            "linic_id) AND (s.patient_id = scheduling_appointment.patient_id)"
            ")))"
        ),
        None,
    ),
    (
        "scheduling_appointment",
        "tenant_isolation",
        "*",
        (
            "(organization_id = (NULLIF(current_setting('app.current_tenant',"
            " true), ''::text))::uuid)"
        ),
        None,
    ),
    (
        "scheduling_appointmentresource",
        "tenant_isolation",
        "*",
        (
            "(organization_id = (NULLIF(current_setting('app.current_tenant',"
            " true), ''::text))::uuid)"
        ),
        None,
    ),
    (
        "scheduling_appointmentseries",
        "tenant_isolation",
        "*",
        (
            "(organization_id = (NULLIF(current_setting('app.current_tenant',"
            " true), ''::text))::uuid)"
        ),
        None,
    ),
    (
        "scheduling_appointmenttransition",
        "patient_booking_read",
        "r",
        (
            "(EXISTS ( SELECT 1\n   FROM (patient_booking_scope() s(session_id"
            ", organization_id, clinic_id, patient_id, enrollment_id, clinic_"
            "name, timezone)\n     JOIN scheduling_appointment a ON (((a.patie"
            "nt_id = s.patient_id) AND (a.clinic_id = s.clinic_id) AND (a.org"
            "anization_id = s.organization_id))))\n  WHERE (a.id = scheduling_"
            "appointmenttransition.appointment_id)))"
        ),
        None,
    ),
    (
        "scheduling_appointmenttransition",
        "tenant_isolation",
        "*",
        (
            "(organization_id = (NULLIF(current_setting('app.current_tenant',"
            " true), ''::text))::uuid)"
        ),
        None,
    ),
    (
        "scheduling_availabilityblock",
        "patient_booking_read",
        "r",
        (
            "(EXISTS ( SELECT 1\n   FROM patient_booking_scope() s(session_id,"
            " organization_id, clinic_id, patient_id, enrollment_id, clinic_n"
            "ame, timezone)\n  WHERE ((s.organization_id = scheduling_availabi"
            "lityblock.organization_id) AND (s.clinic_id = scheduling_availab"
            "ilityblock.clinic_id))))"
        ),
        None,
    ),
    (
        "scheduling_availabilityblock",
        "tenant_isolation",
        "*",
        (
            "(organization_id = (NULLIF(current_setting('app.current_tenant',"
            " true), ''::text))::uuid)"
        ),
        None,
    ),
    (
        "scheduling_availabilitytemplate",
        "patient_booking_read",
        "r",
        (
            "(EXISTS ( SELECT 1\n   FROM patient_booking_scope() s(session_id,"
            " organization_id, clinic_id, patient_id, enrollment_id, clinic_n"
            "ame, timezone)\n  WHERE ((s.organization_id = scheduling_availabi"
            "litytemplate.organization_id) AND (s.clinic_id = scheduling_avai"
            "labilitytemplate.clinic_id))))"
        ),
        None,
    ),
    (
        "scheduling_availabilitytemplate",
        "tenant_isolation",
        "*",
        (
            "(organization_id = (NULLIF(current_setting('app.current_tenant',"
            " true), ''::text))::uuid)"
        ),
        None,
    ),
    (
        "scheduling_cliniciandelay",
        "reception_scope",
        "*",
        "has_permission('reception.queue'::text, clinic_id, NULL::uuid)",
        None,
    ),
    (
        "scheduling_cliniciandelay",
        "tenant_isolation",
        "*",
        (
            "(organization_id = (NULLIF(current_setting('app.current_tenant',"
            " true), ''::text))::uuid)"
        ),
        None,
    ),
    (
        "scheduling_displaydevice",
        "reception_scope",
        "*",
        (
            "(has_permission('reception.queue'::text, clinic_id, NULL::uuid) "
            "AND has_permission('appointment.move'::text, clinic_id, NULL::uu"
            "id))"
        ),
        None,
    ),
    (
        "scheduling_displaydevice",
        "tenant_isolation",
        "*",
        (
            "(organization_id = (NULLIF(current_setting('app.current_tenant',"
            " true), ''::text))::uuid)"
        ),
        None,
    ),
    (
        "scheduling_holiday",
        "tenant_isolation",
        "*",
        (
            "(organization_id = (NULLIF(current_setting('app.current_tenant',"
            " true), ''::text))::uuid)"
        ),
        None,
    ),
    (
        "scheduling_patientbookingevent",
        "tenant_isolation",
        "r",
        (
            "(organization_id = (NULLIF(current_setting('app.current_tenant',"
            " true), ''::text))::uuid)"
        ),
        None,
    ),
    (
        "scheduling_queueentry",
        "reception_scope",
        "*",
        (
            "(has_permission('reception.queue'::text, clinic_id, NULL::uuid) "
            "OR has_permission('appointment.move'::text, clinic_id, NULL::uui"
            "d) OR (has_permission('appointment.move_own'::text, clinic_id, N"
            "ULL::uuid) AND (EXISTS ( SELECT 1\n   FROM scheduling_appointment"
            " a\n  WHERE ((a.id = scheduling_queueentry.appointment_id) AND (a"
            ".clinic_id = scheduling_queueentry.clinic_id) AND (a.organizatio"
            "n_id = scheduling_queueentry.organization_id) AND (a.practitione"
            "r_id = (NULLIF(current_setting('app.current_user_id', true), '':"
            ":text))::uuid))))))"
        ),
        None,
    ),
    (
        "scheduling_queueentry",
        "tenant_isolation",
        "*",
        (
            "(organization_id = (NULLIF(current_setting('app.current_tenant',"
            " true), ''::text))::uuid)"
        ),
        None,
    ),
    (
        "scheduling_resource",
        "patient_booking_read",
        "r",
        (
            "(EXISTS ( SELECT 1\n   FROM patient_booking_scope() s(session_id,"
            " organization_id, clinic_id, patient_id, enrollment_id, clinic_n"
            "ame, timezone)\n  WHERE ((s.organization_id = scheduling_resource"
            ".organization_id) AND (s.clinic_id = scheduling_resource.clinic_"
            "id))))"
        ),
        None,
    ),
    (
        "scheduling_resource",
        "tenant_isolation",
        "*",
        (
            "(organization_id = (NULLIF(current_setting('app.current_tenant',"
            " true), ''::text))::uuid)"
        ),
        None,
    ),
    (
        "scheduling_roomstatus",
        "reception_scope",
        "*",
        "has_permission('reception.queue'::text, clinic_id, NULL::uuid)",
        None,
    ),
    (
        "scheduling_roomstatus",
        "tenant_isolation",
        "*",
        (
            "(organization_id = (NULLIF(current_setting('app.current_tenant',"
            " true), ''::text))::uuid)"
        ),
        None,
    ),
    (
        "scheduling_seriesexception",
        "tenant_isolation",
        "*",
        (
            "(organization_id = (NULLIF(current_setting('app.current_tenant',"
            " true), ''::text))::uuid)"
        ),
        None,
    ),
    (
        "scheduling_servicetype",
        "patient_booking_read",
        "r",
        (
            "(EXISTS ( SELECT 1\n   FROM patient_booking_scope() s(session_id,"
            " organization_id, clinic_id, patient_id, enrollment_id, clinic_n"
            "ame, timezone)\n  WHERE ((s.organization_id = scheduling_servicet"
            "ype.organization_id) AND (s.clinic_id = scheduling_servicetype.c"
            "linic_id))))"
        ),
        None,
    ),
    (
        "scheduling_servicetype",
        "tenant_isolation",
        "*",
        (
            "(organization_id = (NULLIF(current_setting('app.current_tenant',"
            " true), ''::text))::uuid)"
        ),
        None,
    ),
    (
        "scheduling_waitlistentry",
        "waitlist_insert",
        "a",
        None,
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND waitlist_staff(clinic_id))"
        ),
    ),
    (
        "scheduling_waitlistentry",
        "waitlist_merge_insert",
        "a",
        None,
        "intake_merge_mutation_allowed(clinic_id, patient_id)",
    ),
    (
        "scheduling_waitlistentry",
        "waitlist_merge_read",
        "r",
        "intake_merge_mutation_allowed(clinic_id, patient_id)",
        None,
    ),
    (
        "scheduling_waitlistentry",
        "waitlist_merge_update",
        "w",
        "intake_merge_mutation_allowed(clinic_id, patient_id)",
        None,
    ),
    (
        "scheduling_waitlistentry",
        "waitlist_read",
        "r",
        (
            "(((organization_id = (NULLIF(current_setting('app.current_tenant"
            "', true), ''::text))::uuid) AND waitlist_staff(clinic_id)) OR (E"
            "XISTS ( SELECT 1\n   FROM patient_booking_scope() s(session_id, o"
            "rganization_id, clinic_id, patient_id, enrollment_id, clinic_nam"
            "e, timezone)\n  WHERE ((s.organization_id = scheduling_waitlisten"
            "try.organization_id) AND (s.clinic_id = scheduling_waitlistentry"
            ".clinic_id) AND (s.enrollment_id = scheduling_waitlistentry.enro"
            "llment_id)))))"
        ),
        None,
    ),
    (
        "scheduling_waitlistentry",
        "waitlist_update",
        "w",
        (
            "(((organization_id = (NULLIF(current_setting('app.current_tenant"
            "', true), ''::text))::uuid) AND waitlist_staff(clinic_id)) OR (E"
            "XISTS ( SELECT 1\n   FROM patient_booking_scope() s(session_id, o"
            "rganization_id, clinic_id, patient_id, enrollment_id, clinic_nam"
            "e, timezone)\n  WHERE ((s.organization_id = scheduling_waitlisten"
            "try.organization_id) AND (s.clinic_id = scheduling_waitlistentry"
            ".clinic_id) AND (s.enrollment_id = scheduling_waitlistentry.enro"
            "llment_id)))))"
        ),
        None,
    ),
    (
        "scheduling_waitlistoffer",
        "waitlist_insert",
        "a",
        None,
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND waitlist_staff(clinic_id))"
        ),
    ),
    (
        "scheduling_waitlistoffer",
        "waitlist_merge_read",
        "r",
        (
            "(EXISTS ( SELECT 1\n   FROM scheduling_waitlistentry e\n  WHERE (("
            "e.id = scheduling_waitlistoffer.entry_id) AND intake_merge_mutat"
            "ion_allowed(e.clinic_id, e.patient_id))))"
        ),
        None,
    ),
    (
        "scheduling_waitlistoffer",
        "waitlist_merge_update",
        "w",
        (
            "(EXISTS ( SELECT 1\n   FROM scheduling_waitlistentry e\n  WHERE (("
            "e.id = scheduling_waitlistoffer.entry_id) AND intake_merge_mutat"
            "ion_allowed(e.clinic_id, e.patient_id))))"
        ),
        None,
    ),
    (
        "scheduling_waitlistoffer",
        "waitlist_read",
        "r",
        (
            "(((organization_id = (NULLIF(current_setting('app.current_tenant"
            "', true), ''::text))::uuid) AND waitlist_staff(clinic_id)) OR (E"
            "XISTS ( SELECT 1\n   FROM patient_booking_scope() s(session_id, o"
            "rganization_id, clinic_id, patient_id, enrollment_id, clinic_nam"
            "e, timezone)\n  WHERE ((s.organization_id = scheduling_waitlistof"
            "fer.organization_id) AND (s.clinic_id = scheduling_waitlistoffer"
            ".clinic_id) AND (EXISTS ( SELECT 1\n           FROM scheduling_wa"
            "itlistentry e\n          WHERE ((e.id = scheduling_waitlistoffer."
            "entry_id) AND (e.enrollment_id = s.enrollment_id))))))))"
        ),
        None,
    ),
    (
        "scheduling_waitlistoffer",
        "waitlist_update",
        "w",
        (
            "(((organization_id = (NULLIF(current_setting('app.current_tenant"
            "', true), ''::text))::uuid) AND waitlist_staff(clinic_id)) OR (E"
            "XISTS ( SELECT 1\n   FROM patient_booking_scope() s(session_id, o"
            "rganization_id, clinic_id, patient_id, enrollment_id, clinic_nam"
            "e, timezone)\n  WHERE ((s.organization_id = scheduling_waitlistof"
            "fer.organization_id) AND (s.clinic_id = scheduling_waitlistoffer"
            ".clinic_id) AND (EXISTS ( SELECT 1\n           FROM scheduling_wa"
            "itlistentry e\n          WHERE ((e.id = scheduling_waitlistoffer."
            "entry_id) AND (e.enrollment_id = s.enrollment_id))))))))"
        ),
        None,
    ),
    (
        "scribe_aidraft",
        "draft_insert",
        "a",
        None,
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND scribe_note_author(version_id))"
        ),
    ),
    (
        "scribe_aidraft",
        "draft_read",
        "r",
        "scribe_note_author(version_id)",
        None,
    ),
    (
        "scribe_audiochunk",
        "capture_insert",
        "a",
        None,
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND scribe_assigned(session_id))"
        ),
    ),
    (
        "scribe_audiochunk",
        "capture_read",
        "r",
        "scribe_assigned(session_id)",
        None,
    ),
    (
        "scribe_audiochunk",
        "capture_update",
        "w",
        "scribe_assigned(session_id)",
        None,
    ),
    (
        "scribe_captureepoch",
        "capture_insert",
        "a",
        None,
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND scribe_assigned(session_id))"
        ),
    ),
    (
        "scribe_captureepoch",
        "capture_read",
        "r",
        "scribe_assigned(session_id)",
        None,
    ),
    (
        "scribe_captureepoch",
        "capture_update",
        "w",
        "scribe_assigned(session_id)",
        None,
    ),
    (
        "scribe_chunktranscription",
        "transcript_insert",
        "a",
        None,
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND scribe_assigned(session_id))"
        ),
    ),
    (
        "scribe_chunktranscription",
        "transcript_read",
        "r",
        "scribe_assigned(session_id)",
        None,
    ),
    (
        "scribe_draftactionproposal",
        "draft_insert",
        "a",
        None,
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND scribe_note_author(version_id))"
        ),
    ),
    (
        "scribe_draftactionproposal",
        "draft_read",
        "r",
        "scribe_note_author(version_id)",
        None,
    ),
    (
        "scribe_draftsectionsuggestion",
        "draft_insert",
        "a",
        None,
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND scribe_note_author(version_id))"
        ),
    ),
    (
        "scribe_draftsectionsuggestion",
        "draft_read",
        "r",
        "scribe_note_author(version_id)",
        None,
    ),
    (
        "scribe_recordingcommand",
        "capture_insert",
        "a",
        None,
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND scribe_assigned(session_id))"
        ),
    ),
    (
        "scribe_recordingcommand",
        "capture_read",
        "r",
        "scribe_assigned(session_id)",
        None,
    ),
    (
        "scribe_recordingsession",
        "capture_insert",
        "a",
        None,
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (physician_id = (NULLIF(current_s"
            "etting('app.current_user_id', true), ''::text))::uuid) AND ehr_a"
            "ssigned(encounter_id))"
        ),
    ),
    (
        "scribe_recordingsession",
        "capture_read",
        "r",
        "scribe_assigned(id)",
        None,
    ),
    (
        "scribe_recordingsession",
        "capture_update",
        "w",
        "scribe_assigned(id)",
        None,
    ),
    (
        "scribe_reviewdecision",
        "draft_insert",
        "a",
        None,
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND scribe_note_author(version_id))"
        ),
    ),
    (
        "scribe_reviewdecision",
        "draft_read",
        "r",
        "scribe_note_visible(version_id)",
        None,
    ),
    (
        "scribe_transcriptsegment",
        "transcript_insert",
        "a",
        None,
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND scribe_assigned(session_id))"
        ),
    ),
    (
        "scribe_transcriptsegment",
        "transcript_read",
        "r",
        "scribe_assigned(session_id)",
        None,
    ),
    (
        "scribe_transcriptversion",
        "transcript_insert",
        "a",
        None,
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND scribe_assigned(session_id))"
        ),
    ),
    (
        "scribe_transcriptversion",
        "transcript_read",
        "r",
        "scribe_assigned(session_id)",
        None,
    ),
    (
        "subscriptions_entitlement",
        "subscription_read",
        "r",
        "true",
        None,
    ),
    (
        "subscriptions_plan",
        "subscription_read",
        "r",
        "true",
        None,
    ),
    (
        "subscriptions_seat",
        "subscription_read",
        "r",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND user_has_org(organization_id))"
        ),
        None,
    ),
    (
        "subscriptions_subscription",
        "subscription_read",
        "r",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND user_has_org(organization_id))"
        ),
        None,
    ),
    (
        "subscriptions_subscriptionevent",
        "subscription_read",
        "r",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND user_has_org(organization_id))"
        ),
        None,
    ),
    (
        "subscriptions_usageevent",
        "subscription_read",
        "r",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND user_has_org(organization_id))"
        ),
        None,
    ),
    (
        "subscriptions_usagerollup",
        "subscription_read",
        "r",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND user_has_org(organization_id))"
        ),
        None,
    ),
    (
        "teleconsult_teleconsultcredential",
        "teleconsult_credential_insert",
        "a",
        None,
        (
            "((((role)::text = 'physician'::text) AND teleconsult_assigned(se"
            "ssion_id)) OR (((role)::text = 'patient'::text) AND teleconsult_"
            "patient_match(session_id)))"
        ),
    ),
    (
        "teleconsult_teleconsultcredential",
        "teleconsult_credential_read",
        "r",
        (
            "(teleconsult_assigned(session_id) OR (((role)::text = 'patient':"
            ":text) AND teleconsult_patient_match(session_id)))"
        ),
        None,
    ),
    (
        "teleconsult_teleconsultcredential",
        "teleconsult_credential_update",
        "w",
        (
            "(teleconsult_assigned(session_id) OR (((role)::text = 'patient':"
            ":text) AND teleconsult_patient_match(session_id)))"
        ),
        None,
    ),
    (
        "teleconsult_teleconsultdevicecheck",
        "teleconsult_device_insert",
        "a",
        None,
        (
            "((((role)::text = 'physician'::text) AND teleconsult_clinician(s"
            "ession_id)) OR (((role)::text = 'patient'::text) AND teleconsult"
            "_patient_match(session_id)))"
        ),
    ),
    (
        "teleconsult_teleconsultdevicecheck",
        "teleconsult_device_read",
        "r",
        (
            "(teleconsult_clinician(session_id) OR (((role)::text = 'patient'"
            "::text) AND teleconsult_patient_match(session_id)))"
        ),
        None,
    ),
    (
        "teleconsult_teleconsultevent",
        "teleconsult_event_insert",
        "a",
        None,
        ("(teleconsult_assigned(session_id) OR teleconsult_patient_match(session_id))"),
    ),
    (
        "teleconsult_teleconsultevent",
        "teleconsult_event_read",
        "r",
        (
            "(((organization_id = (NULLIF(current_setting('app.current_tenant"
            "', true), ''::text))::uuid) AND questionnaire_staff(( SELECT s.c"
            "linic_id\n   FROM teleconsult_teleconsultsession s\n  WHERE (s.id "
            "= teleconsult_teleconsultevent.session_id)), ARRAY['physician'::"
            "text, 'receptionist'::text, 'owner'::text, 'clinic_admin'::text]"
            ")) OR teleconsult_patient_match(session_id))"
        ),
        None,
    ),
    (
        "teleconsult_teleconsultroom",
        "teleconsult_room_capture_update",
        "w",
        "teleconsult_assigned(session_id)",
        None,
    ),
    (
        "teleconsult_teleconsultroom",
        "teleconsult_room_insert",
        "a",
        None,
        "teleconsult_assigned(session_id)",
    ),
    (
        "teleconsult_teleconsultroom",
        "teleconsult_room_read",
        "r",
        (
            "(((organization_id = (NULLIF(current_setting('app.current_tenant"
            "', true), ''::text))::uuid) AND questionnaire_staff(( SELECT s.c"
            "linic_id\n   FROM teleconsult_teleconsultsession s\n  WHERE (s.id "
            "= teleconsult_teleconsultroom.session_id)), ARRAY['physician'::t"
            "ext, 'receptionist'::text, 'owner'::text, 'clinic_admin'::text])"
            ") OR teleconsult_patient_match(session_id))"
        ),
        None,
    ),
    (
        "teleconsult_teleconsultsession",
        "teleconsult_session_insert",
        "a",
        None,
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (physician_id = (NULLIF(current_s"
            "etting('app.current_user_id', true), ''::text))::uuid) AND quest"
            "ionnaire_staff(clinic_id, ARRAY['physician'::text]))"
        ),
    ),
    (
        "teleconsult_teleconsultsession",
        "teleconsult_session_read",
        "r",
        (
            "(((organization_id = (NULLIF(current_setting('app.current_tenant"
            "', true), ''::text))::uuid) AND questionnaire_staff(clinic_id, A"
            "RRAY['physician'::text, 'receptionist'::text, 'owner'::text, 'cl"
            "inic_admin'::text])) OR teleconsult_patient_match(id))"
        ),
        None,
    ),
    (
        "teleconsult_teleconsultsession",
        "teleconsult_session_update",
        "w",
        "teleconsult_assigned(id)",
        None,
    ),
    (
        "tenancy_tenantdatakey",
        "tenant_isolation",
        "*",
        (
            "(organization_id = (NULLIF(current_setting('app.current_tenant',"
            " true), ''::text))::uuid)"
        ),
        None,
    ),
    (
        "tenancy_tenantprobe",
        "tenant_isolation",
        "*",
        (
            "(organization_id = (NULLIF(current_setting('app.current_tenant',"
            " true), ''::text))::uuid)"
        ),
        None,
    ),
    (
        "workflows_careaction",
        "care_insert",
        "a",
        None,
        (
            "(organization_id = (NULLIF(current_setting('app.current_tenant',"
            " true), ''::text))::uuid)"
        ),
    ),
    (
        "workflows_careaction",
        "care_read",
        "r",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND ((((kind)::text <> 'patient_instr"
            "uction'::text) AND has_permission('tasks.view'::text, clinic_id,"
            " NULL::uuid) AND ((created_by_id = (NULLIF(current_setting('app."
            "current_user_id', true), ''::text))::uuid) OR has_permission('ta"
            "sks.reassign'::text, clinic_id, NULL::uuid) OR workflows_owned(c"
            "linic_id, owner_user_id, (owner_role)::text) OR (EXISTS ( SELECT"
            " 1\n   FROM workflows_careplan p\n  WHERE ((p.id = workflows_carea"
            "ction.plan_id) AND (p.clinician_id = (NULLIF(current_setting('ap"
            "p.current_user_id', true), ''::text))::uuid)))))) OR (EXISTS ( S"
            "ELECT 1\n   FROM workflows_careplan p\n  WHERE ((p.id = workflows_"
            "careaction.plan_id) AND has_permission('clinical.read'::text, p."
            "clinic_id, p.enrollment_id))))))"
        ),
        None,
    ),
    (
        "workflows_careaction",
        "care_update",
        "w",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND ((((kind)::text <> 'patient_instr"
            "uction'::text) AND has_permission('tasks.view'::text, clinic_id,"
            " NULL::uuid) AND ((created_by_id = (NULLIF(current_setting('app."
            "current_user_id', true), ''::text))::uuid) OR has_permission('ta"
            "sks.reassign'::text, clinic_id, NULL::uuid) OR workflows_owned(c"
            "linic_id, owner_user_id, (owner_role)::text) OR (EXISTS ( SELECT"
            " 1\n   FROM workflows_careplan p\n  WHERE ((p.id = workflows_carea"
            "ction.plan_id) AND (p.clinician_id = (NULLIF(current_setting('ap"
            "p.current_user_id', true), ''::text))::uuid)))))) OR (EXISTS ( S"
            "ELECT 1\n   FROM workflows_careplan p\n  WHERE ((p.id = workflows_"
            "careaction.plan_id) AND has_permission('clinical.read'::text, p."
            "clinic_id, p.enrollment_id))))))"
        ),
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND has_permission('tasks.view'::text"
            ", clinic_id, NULL::uuid))"
        ),
    ),
    (
        "workflows_careplan",
        "care_insert",
        "a",
        None,
        (
            "(organization_id = (NULLIF(current_setting('app.current_tenant',"
            " true), ''::text))::uuid)"
        ),
    ),
    (
        "workflows_careplan",
        "care_read",
        "r",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND ((has_permission('tasks.view'::te"
            "xt, clinic_id, NULL::uuid) AND ((clinician_id = (NULLIF(current_"
            "setting('app.current_user_id', true), ''::text))::uuid) OR has_p"
            "ermission('tasks.reassign'::text, clinic_id, NULL::uuid))) OR ha"
            "s_permission('clinical.read'::text, clinic_id, enrollment_id)))"
        ),
        None,
    ),
    (
        "workflows_careplan",
        "care_update",
        "w",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND ((has_permission('tasks.view'::te"
            "xt, clinic_id, NULL::uuid) AND ((clinician_id = (NULLIF(current_"
            "setting('app.current_user_id', true), ''::text))::uuid) OR has_p"
            "ermission('tasks.reassign'::text, clinic_id, NULL::uuid))) OR ha"
            "s_permission('clinical.read'::text, clinic_id, enrollment_id)))"
        ),
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND has_permission('tasks.view'::text"
            ", clinic_id, NULL::uuid))"
        ),
    ),
    (
        "workflows_carerefusal",
        "care_read",
        "r",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND has_permission('tasks.view'::text"
            ", clinic_id, NULL::uuid) AND has_permission('tasks.reassign'::te"
            "xt, clinic_id, NULL::uuid))"
        ),
        None,
    ),
    (
        "workflows_proassignment",
        "care_insert",
        "a",
        None,
        (
            "(organization_id = (NULLIF(current_setting('app.current_tenant',"
            " true), ''::text))::uuid)"
        ),
    ),
    (
        "workflows_proassignment",
        "care_read",
        "r",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (has_permission('clinical.read'::"
            "text, clinic_id, enrollment_id) OR has_permission('observation.w"
            "rite'::text, clinic_id, enrollment_id)))"
        ),
        None,
    ),
    (
        "workflows_proassignment",
        "care_update",
        "w",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (has_permission('clinical.read'::"
            "text, clinic_id, enrollment_id) OR has_permission('observation.w"
            "rite'::text, clinic_id, enrollment_id)))"
        ),
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND has_permission('tasks.view'::text"
            ", clinic_id, NULL::uuid))"
        ),
    ),
    (
        "workflows_prooccurrence",
        "care_insert",
        "a",
        None,
        (
            "(organization_id = (NULLIF(current_setting('app.current_tenant',"
            " true), ''::text))::uuid)"
        ),
    ),
    (
        "workflows_prooccurrence",
        "care_read",
        "r",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (EXISTS ( SELECT 1\n   FROM workfl"
            "ows_proassignment a\n  WHERE (a.id = workflows_prooccurrence.assi"
            "gnment_id))))"
        ),
        None,
    ),
    (
        "workflows_prooccurrence",
        "care_update",
        "w",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (EXISTS ( SELECT 1\n   FROM workfl"
            "ows_proassignment a\n  WHERE (a.id = workflows_prooccurrence.assi"
            "gnment_id))))"
        ),
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND has_permission('tasks.view'::text"
            ", clinic_id, NULL::uuid))"
        ),
    ),
    (
        "workflows_recall",
        "care_insert",
        "a",
        None,
        (
            "(organization_id = (NULLIF(current_setting('app.current_tenant',"
            " true), ''::text))::uuid)"
        ),
    ),
    (
        "workflows_recall",
        "care_read",
        "r",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (has_permission('tasks.view'::tex"
            "t, clinic_id, NULL::uuid) AND ((created_by_id = (NULLIF(current_"
            "setting('app.current_user_id', true), ''::text))::uuid) OR has_p"
            "ermission('tasks.reassign'::text, clinic_id, NULL::uuid))))"
        ),
        None,
    ),
    (
        "workflows_recall",
        "care_update",
        "w",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (has_permission('tasks.view'::tex"
            "t, clinic_id, NULL::uuid) AND ((created_by_id = (NULLIF(current_"
            "setting('app.current_user_id', true), ''::text))::uuid) OR has_p"
            "ermission('tasks.reassign'::text, clinic_id, NULL::uuid))))"
        ),
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND has_permission('tasks.view'::text"
            ", clinic_id, NULL::uuid))"
        ),
    ),
    (
        "workflows_referral",
        "care_insert",
        "a",
        None,
        (
            "(organization_id = (NULLIF(current_setting('app.current_tenant',"
            " true), ''::text))::uuid)"
        ),
    ),
    (
        "workflows_referral",
        "care_read",
        "r",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (((target_label IS NULL) AND has_"
            "permission('tasks.view'::text, clinic_id, NULL::uuid) AND ((crea"
            "ted_by_id = (NULLIF(current_setting('app.current_user_id', true)"
            ", ''::text))::uuid) OR has_permission('tasks.reassign'::text, cl"
            "inic_id, NULL::uuid))) OR (EXISTS ( SELECT 1\n   FROM workflows_c"
            "areplan p\n  WHERE ((p.id = workflows_referral.plan_id) AND has_p"
            "ermission('clinical.read'::text, p.clinic_id, p.enrollment_id)))"
            ") OR (EXISTS ( SELECT 1\n   FROM workflows_careaction a\n  WHERE ("
            "(a.id = workflows_referral.action_id) AND workflows_owned(a.clin"
            "ic_id, a.owner_user_id, (a.owner_role)::text))))))"
        ),
        None,
    ),
    (
        "workflows_referral",
        "care_update",
        "w",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (((target_label IS NULL) AND has_"
            "permission('tasks.view'::text, clinic_id, NULL::uuid) AND ((crea"
            "ted_by_id = (NULLIF(current_setting('app.current_user_id', true)"
            ", ''::text))::uuid) OR has_permission('tasks.reassign'::text, cl"
            "inic_id, NULL::uuid))) OR (EXISTS ( SELECT 1\n   FROM workflows_c"
            "areplan p\n  WHERE ((p.id = workflows_referral.plan_id) AND has_p"
            "ermission('clinical.read'::text, p.clinic_id, p.enrollment_id)))"
            ") OR (EXISTS ( SELECT 1\n   FROM workflows_careaction a\n  WHERE ("
            "(a.id = workflows_referral.action_id) AND workflows_owned(a.clin"
            "ic_id, a.owner_user_id, (a.owner_role)::text))))))"
        ),
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND has_permission('tasks.view'::text"
            ", clinic_id, NULL::uuid))"
        ),
    ),
    (
        "workflows_releasedelivery",
        "coordination_insert",
        "a",
        None,
        (
            "(organization_id = (NULLIF(current_setting('app.current_tenant',"
            " true), ''::text))::uuid)"
        ),
    ),
    (
        "workflows_releasedelivery",
        "coordination_read",
        "r",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND has_permission('result.release'::"
            "text, clinic_id, enrollment_id))"
        ),
        None,
    ),
    (
        "workflows_releasenoticetemplate",
        "coordination_insert",
        "a",
        None,
        (
            "(organization_id = (NULLIF(current_setting('app.current_tenant',"
            " true), ''::text))::uuid)"
        ),
    ),
    (
        "workflows_releasenoticetemplate",
        "coordination_read",
        "r",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND has_permission('configuration.pro"
            "pose'::text, clinic_id, NULL::uuid))"
        ),
        None,
    ),
    (
        "workflows_task",
        "workflow_insert",
        "a",
        None,
        (
            "(organization_id = (NULLIF(current_setting('app.current_tenant',"
            " true), ''::text))::uuid)"
        ),
    ),
    (
        "workflows_task",
        "workflow_read",
        "r",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (has_permission('tasks.view'::tex"
            "t, clinic_id, NULL::uuid) AND ((((state)::text = 'open'::text) A"
            "ND (created_by_id = (NULLIF(current_setting('app.current_user_id"
            "', true), ''::text))::uuid)) OR workflows_owned(clinic_id, owner"
            "_user_id, (owner_role)::text) OR has_permission('tasks.reassign'"
            "::text, clinic_id, NULL::uuid))))"
        ),
        None,
    ),
    (
        "workflows_task",
        "workflow_update",
        "w",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (has_permission('tasks.view'::tex"
            "t, clinic_id, NULL::uuid) AND ((((state)::text = 'open'::text) A"
            "ND (created_by_id = (NULLIF(current_setting('app.current_user_id"
            "', true), ''::text))::uuid)) OR workflows_owned(clinic_id, owner"
            "_user_id, (owner_role)::text) OR has_permission('tasks.reassign'"
            "::text, clinic_id, NULL::uuid))))"
        ),
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND has_permission('tasks.view'::text"
            ", clinic_id, NULL::uuid))"
        ),
    ),
    (
        "workflows_taskcomment",
        "workflow_insert",
        "a",
        None,
        (
            "(organization_id = (NULLIF(current_setting('app.current_tenant',"
            " true), ''::text))::uuid)"
        ),
    ),
    (
        "workflows_taskcomment",
        "workflow_read",
        "r",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (EXISTS ( SELECT 1\n   FROM workfl"
            "ows_task t\n  WHERE (t.id = workflows_taskcomment.task_id))))"
        ),
        None,
    ),
    (
        "workflows_urgentconcern",
        "coordination_read",
        "r",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (workflows_urgent_responsible(cli"
            "nic_id, ( SELECT p.responsible_roles\n   FROM workflows_urgentcov"
            "eragepolicy p\n  WHERE (p.id = workflows_urgentconcern.policy_id)"
            ")) OR has_permission('configuration.clinic'::text, clinic_id, NU"
            "LL::uuid)))"
        ),
        None,
    ),
    (
        "workflows_urgentconcern",
        "coordination_update",
        "w",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (workflows_urgent_responsible(cli"
            "nic_id, ( SELECT p.responsible_roles\n   FROM workflows_urgentcov"
            "eragepolicy p\n  WHERE (p.id = workflows_urgentconcern.policy_id)"
            ")) OR has_permission('configuration.clinic'::text, clinic_id, NU"
            "LL::uuid)))"
        ),
        (
            "(organization_id = (NULLIF(current_setting('app.current_tenant',"
            " true), ''::text))::uuid)"
        ),
    ),
    (
        "workflows_urgentcoveragepolicy",
        "coordination_insert",
        "a",
        None,
        (
            "(organization_id = (NULLIF(current_setting('app.current_tenant',"
            " true), ''::text))::uuid)"
        ),
    ),
    (
        "workflows_urgentcoveragepolicy",
        "coordination_read",
        "r",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (has_permission('demographics.rea"
            "d'::text, clinic_id, NULL::uuid) OR has_permission('configuratio"
            "n.clinic'::text, clinic_id, NULL::uuid)))"
        ),
        None,
    ),
    (
        "workflows_workflowdefinitionversion",
        "workflow_insert",
        "a",
        None,
        (
            "(organization_id = (NULLIF(current_setting('app.current_tenant',"
            " true), ''::text))::uuid)"
        ),
    ),
    (
        "workflows_workflowdefinitionversion",
        "workflow_read",
        "r",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND has_permission('tasks.view'::text"
            ", clinic_id, NULL::uuid))"
        ),
        None,
    ),
    (
        "workflows_workflowdefinitionversion",
        "workflow_update",
        "w",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND has_permission('tasks.view'::text"
            ", clinic_id, NULL::uuid))"
        ),
        None,
    ),
    (
        "workflows_workflowrun",
        "workflow_insert",
        "a",
        None,
        (
            "(organization_id = (NULLIF(current_setting('app.current_tenant',"
            " true), ''::text))::uuid)"
        ),
    ),
    (
        "workflows_workflowrun",
        "workflow_read",
        "r",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (has_permission('tasks.view'::tex"
            "t, clinic_id, NULL::uuid) AND ((started_by_id = (NULLIF(current_"
            "setting('app.current_user_id', true), ''::text))::uuid) OR has_p"
            "ermission('tasks.reassign'::text, clinic_id, NULL::uuid))))"
        ),
        None,
    ),
    (
        "workflows_workflowrun",
        "workflow_update",
        "w",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (has_permission('tasks.view'::tex"
            "t, clinic_id, NULL::uuid) AND ((started_by_id = (NULLIF(current_"
            "setting('app.current_user_id', true), ''::text))::uuid) OR has_p"
            "ermission('tasks.reassign'::text, clinic_id, NULL::uuid))))"
        ),
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND has_permission('tasks.view'::text"
            ", clinic_id, NULL::uuid))"
        ),
    ),
    (
        "workflows_workflowstep",
        "workflow_insert",
        "a",
        None,
        (
            "(organization_id = (NULLIF(current_setting('app.current_tenant',"
            " true), ''::text))::uuid)"
        ),
    ),
    (
        "workflows_workflowstep",
        "workflow_read",
        "r",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (EXISTS ( SELECT 1\n   FROM workfl"
            "ows_workflowrun r\n  WHERE (r.id = workflows_workflowstep.run_id)"
            ")))"
        ),
        None,
    ),
    (
        "workflows_workflowstep",
        "workflow_update",
        "w",
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND (EXISTS ( SELECT 1\n   FROM workfl"
            "ows_workflowrun r\n  WHERE (r.id = workflows_workflowstep.run_id)"
            ")))"
        ),
        (
            "((organization_id = (NULLIF(current_setting('app.current_tenant'"
            ", true), ''::text))::uuid) AND has_permission('tasks.view'::text"
            ", clinic_id, NULL::uuid))"
        ),
    ),
)
