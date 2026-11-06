The recorded PSP workflows ran against the task's private PostgreSQL as
`clinic_app`. Every runner asserted `MemoryMax=12884901888` and
`MemorySwapMax=0`. No real payment provider or patient data was used.

1. A recorded PIX instruction remained pending and created no receipt. An
   independently verified settlement posted once through the journal. Repeated
   application and statement import produced one receipt and one collection.
   The final balances were bank 9900, clearing 0, PSP fees 100 and practitioner
   payables -3000 minor units. Evidence: `pix.json`,
   `takeover/manual-qa.log` and `takeover/manual-qa.junit.xml`.
2. The provider fixture recorded its effect, then timed out. The outbox stayed
   `in_progress` with `unknown` evidence. Recovery queried the original key and
   completed with one effect and one send attempt. Evidence: `timeout.json`.
3. A correctly signed refund callback was independently verified. Its replay
   returned `duplicate`; repeated Finance application produced one refund and
   one linked repasse reversal. The refund was 5000 and the repasse reversal was
   1500 minor units. Evidence: `refund.json`,
   `takeover/manual-qa-refund.log` and its JUnit receipt.
4. A forged raw body was refused before the stored-scope resolver ran. The
   authenticated callback's tenant claim was ignored. A foreign organization
   saw no rows in all five PSP tables; runtime DELETE/TRUNCATE and privileged
   DELETE were refused. The synthetic clinical sentinel reached none of audit,
   outbox or logs. Evidence: `adversarial.json`.

The three main probes passed, and the separate refund replay probe passed.
Their runner and source are retained in `takeover/qa_runner.py` and
`takeover/manual_qa.py`.

The required Redis/PostgreSQL broker gate separately killed the prefork worker
after its recorded effect. Redelivery reconciled the original key with one
send attempt. It passed with `CLINIC_BROKER_GATE=required`; the owned Redis
container was removed in the runner's `finally` block. Evidence:
`takeover/broker-r3-psp-broker.log`, `takeover/psp-broker.junit.xml` and
`takeover/broker-r3-remove-private-broker.log`.

The reader/different-writer regression failed twice against the original
schema, then passed against the amended migration with the same SQLSTATE and
message for known and unknown intents. The same green run covered corrected
same-version statements, newer authoritative reversals and lookup-only recovery
after capability rotation. Evidence: `takeover/red-evidence-authority.log` and
`takeover/green-review.log`.

Visual browser qualification is `deferred-to-host-quiet-batch` under the root's
instruction. The refused canonical attempt remains in
`takeover/canonical-billing-chromium.log` and `browser/`. Full native R is also
reserved for the root's exclusive qualification window. External provider legs
remain `BLOCKED-ON-EG-1/EG-5`.
