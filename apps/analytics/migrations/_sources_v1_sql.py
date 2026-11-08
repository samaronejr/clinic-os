"""Approved metadata-only sources; financial facts come only from the journal."""

SOURCES_SQL = """
CREATE VIEW clinic_app.analytics_metricfact WITH (security_barrier=true) AS
SELECT f.* FROM (
 SELECT a.organization_id,a.clinic_id,'no_show'::text AS metric_id,a.id AS record_id,
 a.start_at AS occurred_at,a.end_at AS ended_at,a.status::text AS state,
 CASE WHEN a.status='no_show' THEN 1 ELSE 0 END::numeric AS numerator,
 CASE WHEN a.status IN ('completed','no_show') THEN 1 ELSE 0 END::numeric AS
 denominator,
 CASE WHEN a.status IN ('scheduled','arrived','in_progress') THEN 1 ELSE 0 END::bigint
 AS missing
 FROM clinic_app.scheduling_appointment a
 WHERE a.status NOT IN ('cancelled','expired','held','requested')
 UNION ALL
 SELECT a.organization_id,a.clinic_id,'utilization',a.id,a.start_at,a.end_at,a.status,
 extract(epoch FROM a.end_at-a.start_at),0,0
 FROM clinic_app.scheduling_appointment a
 WHERE a.status IN ('scheduled','arrived','in_progress','completed','no_show')
 UNION ALL
 SELECT a.organization_id,a.clinic_id,'utilization',a.id,a.start_at,a.end_at,
 'available',
 0,extract(epoch FROM a.end_at-a.start_at),0
 FROM clinic_app.scheduling_availabilityblock a
 WHERE a.practitioner_id IS NOT NULL AND a.retired_at IS NULL
 UNION ALL
 SELECT q.organization_id,q.clinic_id,'waiting_time',q.id,q.created_at,q.called_at,
 q.state,
 coalesce(extract(epoch FROM q.called_at-q.created_at),0),
 CASE WHEN q.called_at IS NOT NULL THEN 1 ELSE 0 END,
 CASE WHEN q.called_at IS NULL THEN 1 ELSE 0 END
 FROM clinic_app.scheduling_queueentry q
 UNION ALL
 SELECT t.organization_id,t.clinic_id,'task_turnaround',t.id,t.created_at,t.updated_at,
 t.state,
 CASE WHEN t.state='done' THEN extract(epoch FROM t.updated_at-t.created_at) ELSE 0 END,
 CASE WHEN t.state='done' THEN 1 ELSE 0 END,
 CASE WHEN t.state NOT IN ('done','cancelled') THEN 1 ELSE 0 END
 FROM clinic_app.workflows_task t WHERE t.state<>'cancelled'
 UNION ALL
 SELECT t.organization_id,t.clinic_id,'follow_up_overdue',t.id,t.due_at,t.due_at,
 t.state,
 1,1,0 FROM clinic_app.workflows_task t
 WHERE t.kind='follow_up' AND t.state NOT IN ('done','cancelled')
 UNION ALL
 SELECT v.organization_id,v.clinic_id,'result_ack_latency',v.id,v.created_at,
 a.created_at,
 CASE WHEN a.id IS NULL THEN 'unacknowledged' ELSE 'acknowledged' END,
 coalesce(extract(epoch FROM a.created_at-v.created_at),0),
 CASE WHEN a.id IS NULL THEN 0 ELSE 1 END,
 CASE WHEN a.id IS NULL THEN 1 ELSE 0 END
 FROM clinic_app.orders_resultversion v
 JOIN clinic_app.orders_resultmatch m ON m.version_id=v.id
 LEFT JOIN clinic_app.orders_resultacknowledgment a ON a.version_id=v.id
 UNION ALL
 SELECT d.organization_id,d.clinic_id,'ai_correction_time',r.id,r.created_at,
 r.created_at,
 r.action,extract(epoch FROM r.created_at-s.created_at),1,0
 FROM clinic_app.scribe_reviewdecision r
 JOIN clinic_app.scribe_draftsectionsuggestion s ON s.id=r.suggestion_id
 JOIN clinic_app.scribe_aidraft d ON d.id=s.draft_id
 WHERE r.action='modified'
 UNION ALL
 SELECT b.organization_id,b.clinic_id,k.metric_id,l.id,e.posted_at,e.posted_at,
 e.source_kind,
 CASE WHEN l.side='debit' THEN l.amount_minor ELSE -l.amount_minor END::numeric
 * CASE WHEN k.metric_id='payout_liabilities' THEN -1 ELSE 1 END,1,0
 FROM clinic_app.billing_journalline l
 JOIN clinic_app.billing_journalentry e ON e.id=l.entry_id AND e.state='posted'
 JOIN clinic_app.billing_journalbook b ON b.id=e.book_id
 JOIN clinic_app.billing_account a ON a.id=l.account_id AND a.book_id=b.id
 CROSS JOIN (VALUES ('receivables'),('verified_collections'),('net_collections'),
 ('payout_liabilities')) k(metric_id)
 WHERE (k.metric_id='receivables' AND a.code IN ('patient_ar','insurer_ar'))
 OR (k.metric_id='payout_liabilities' AND a.code IN ('practitioner_payables',
 'payout_in_transit'))
 OR (k.metric_id IN ('verified_collections','net_collections')
 AND a.code IN ('bank','petty_cash','psp_clearing')
 AND (e.source_kind='collection'
 OR (k.metric_id='net_collections' AND e.source_kind='refund_confirmed')))
) f WHERE f.organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid
AND clinic_app.analytics_allowed(f.clinic_id,f.metric_id);

CREATE VIEW clinic_app.analytics_usagefact WITH (security_barrier=true) AS
SELECT DISTINCT r.organization_id,r.clinic_id,r.user_id,e.occurred_at_utc AS occurred_at
FROM clinic_app.identity_userclinicrole r
JOIN clinic_app.identity_user u ON u.id=r.user_id AND u.is_active
LEFT JOIN clinic_app.audit_event_tenant e ON e.organization_id=r.organization_id
AND e.actor_user_id=r.user_id AND e.payload->>'clinic_id'=r.clinic_id::text
AND (e.event_type LIKE 'scheduling.%' OR e.event_type LIKE 'ehr.%'
 OR e.event_type LIKE 'workflows.task.%' OR e.event_type LIKE 'billing.%')
WHERE r.revoked_at IS NULL
AND r.organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid
AND clinic_app.analytics_allowed(r.clinic_id,'activation');
"""
