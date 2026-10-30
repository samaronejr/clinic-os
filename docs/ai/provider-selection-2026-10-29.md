---
{
  "adversarial_classes": [
    "accents",
    "ambiguous_abbreviations",
    "caregiver_child",
    "conflicting_records",
    "decimal_commas",
    "interruptions",
    "malicious_content",
    "mic_failure",
    "negation",
    "noise",
    "stale_permissions",
    "wrong_patient_attachments"
  ],
  "corpus_sha256": "a40d05b16b4141392e34e67683226789387a0db512494fad21a2ec0f6644de85",
  "corpus_version": "engineering-v1",
  "data_class": "synthetic",
  "external_gates_satisfied": [],
  "generated_at": "2026-10-29T11:50:31.060390+00:00",
  "protocol": {
    "clinical_review": "pending_two_raters",
    "corpus": "engineering-v1",
    "eligibility": "verified_region_and_retention_same_jurisdiction_no_failed_calls",
    "jurisdiction": "BR",
    "latency_quantile": "nearest-rank",
    "ranking": [
      "failed_calls",
      "word_errors",
      "cost_micros",
      "candidate_name"
    ],
    "retries": 0,
    "version": "benchmark-v1"
  },
  "protocol_sha256": "0cb75e7562c48170fb03f866d0d61ae50815fa9e6ffdcd25664fe8cc5079e0f9",
  "provider_leg": "BLOCKED-ON-EG",
  "real_world_performance": false,
  "schema_version": 1,
  "selections": [
    {
      "candidates": [
        {
          "candidate": {
            "adapter": "synthetic",
            "jurisdiction": "BR",
            "model": "synthetic-v1",
            "name": "synthetic-primary",
            "price": {
              "audio_micros": 0,
              "cached_micros": 0,
              "input_micros": 0,
              "output_micros": 0
            },
            "region": {
              "retrieved": "2026-10-29",
              "source": "apps/ai/adapters.py",
              "value": "br-synthetic",
              "verification": "verified"
            },
            "retention": {
              "retrieved": "2026-10-29",
              "source": "apps/ai/adapters.py",
              "value": "Offline memory only; no network, training or provider storage",
              "verification": "verified"
            }
          },
          "clinical_error_rates": {
            "allergy_handling": null,
            "chronology": null,
            "hallucination": null,
            "medication_name_dose": null,
            "negation": null,
            "significant_omission": null,
            "speaker_attribution": null,
            "units_decimals": null
          },
          "clinical_review_status": "pending_two_raters",
          "cost_per_visit_micros": 0.0,
          "eligible": true,
          "encounters": 4,
          "failures": {},
          "latency_ms": {
            "p50": 0.06739,
            "p95": 0.10523
          },
          "reasons": [],
          "samples": 12,
          "structured_output": {
            "rate": 1.0,
            "total": 12,
            "valid": 12
          },
          "usage_per_visit": {
            "audio_seconds": 3.0,
            "tokens_cached": 0.0,
            "tokens_in": 0.0,
            "tokens_out": 0.0
          },
          "wer": {
            "deletions": 109,
            "errors": 158,
            "insertions": 0,
            "rate": 0.9349112426035503,
            "reference_words": 169,
            "substitutions": 49
          }
        },
        {
          "candidate": {
            "adapter": "recorded_fixture",
            "jurisdiction": "BR",
            "model": "fixture-v1",
            "name": "recorded-fallback",
            "price": {
              "audio_micros": 0,
              "cached_micros": 0,
              "input_micros": 0,
              "output_micros": 0
            },
            "region": {
              "retrieved": "2026-10-29",
              "source": "apps/ai/adapters.py",
              "value": "br-synthetic-secondary",
              "verification": "verified"
            },
            "retention": {
              "retrieved": "2026-10-29",
              "source": "apps/ai/fixtures/v1.json",
              "value": "Static self-authored synthetic recording; no provider storage",
              "verification": "verified"
            }
          },
          "clinical_error_rates": {
            "allergy_handling": null,
            "chronology": null,
            "hallucination": null,
            "medication_name_dose": null,
            "negation": null,
            "significant_omission": null,
            "speaker_attribution": null,
            "units_decimals": null
          },
          "clinical_review_status": "pending_two_raters",
          "cost_per_visit_micros": 0.0,
          "eligible": true,
          "encounters": 4,
          "failures": {},
          "latency_ms": {
            "p50": 0.10587,
            "p95": 0.11947
          },
          "reasons": [],
          "samples": 12,
          "structured_output": {
            "rate": 1.0,
            "total": 12,
            "valid": 12
          },
          "usage_per_visit": {
            "audio_seconds": 3.0,
            "tokens_cached": 0.0,
            "tokens_in": 0.0,
            "tokens_out": 0.0
          },
          "wer": {
            "deletions": 97,
            "errors": 169,
            "insertions": 0,
            "rate": 1.0,
            "reference_words": 169,
            "substitutions": 72
          }
        }
      ],
      "fallback": "recorded-fallback",
      "primary": "synthetic-primary",
      "purpose": "scribe_asr"
    },
    {
      "candidates": [
        {
          "candidate": {
            "adapter": "synthetic",
            "jurisdiction": "BR",
            "model": "synthetic-v1",
            "name": "synthetic-primary",
            "price": {
              "audio_micros": 0,
              "cached_micros": 0,
              "input_micros": 0,
              "output_micros": 0
            },
            "region": {
              "retrieved": "2026-10-29",
              "source": "apps/ai/adapters.py",
              "value": "br-synthetic",
              "verification": "verified"
            },
            "retention": {
              "retrieved": "2026-10-29",
              "source": "apps/ai/adapters.py",
              "value": "Offline memory only; no network, training or provider storage",
              "verification": "verified"
            }
          },
          "clinical_error_rates": {
            "allergy_handling": null,
            "chronology": null,
            "hallucination": null,
            "medication_name_dose": null,
            "negation": null,
            "significant_omission": null,
            "speaker_attribution": null,
            "units_decimals": null
          },
          "clinical_review_status": "pending_two_raters",
          "cost_per_visit_micros": 0.0,
          "eligible": true,
          "encounters": 4,
          "failures": {},
          "latency_ms": {
            "p50": 0.01118,
            "p95": 0.02675
          },
          "reasons": [],
          "samples": 12,
          "structured_output": {
            "rate": 1.0,
            "total": 12,
            "valid": 12
          },
          "usage_per_visit": {
            "audio_seconds": 0.0,
            "tokens_cached": 0.0,
            "tokens_in": 12.0,
            "tokens_out": 18.0
          },
          "wer": {
            "deletions": 0,
            "errors": 0,
            "insertions": 0,
            "rate": null,
            "reference_words": 0,
            "substitutions": 0
          }
        },
        {
          "candidate": {
            "adapter": "recorded_fixture",
            "jurisdiction": "BR",
            "model": "fixture-v1",
            "name": "recorded-fallback",
            "price": {
              "audio_micros": 0,
              "cached_micros": 0,
              "input_micros": 0,
              "output_micros": 0
            },
            "region": {
              "retrieved": "2026-10-29",
              "source": "apps/ai/adapters.py",
              "value": "br-synthetic-secondary",
              "verification": "verified"
            },
            "retention": {
              "retrieved": "2026-10-29",
              "source": "apps/ai/fixtures/v1.json",
              "value": "Static self-authored synthetic recording; no provider storage",
              "verification": "verified"
            }
          },
          "clinical_error_rates": {
            "allergy_handling": null,
            "chronology": null,
            "hallucination": null,
            "medication_name_dose": null,
            "negation": null,
            "significant_omission": null,
            "speaker_attribution": null,
            "units_decimals": null
          },
          "clinical_review_status": "pending_two_raters",
          "cost_per_visit_micros": 0.0,
          "eligible": true,
          "encounters": 4,
          "failures": {},
          "latency_ms": {
            "p50": 0.03148,
            "p95": 0.03993
          },
          "reasons": [],
          "samples": 12,
          "structured_output": {
            "rate": 1.0,
            "total": 12,
            "valid": 12
          },
          "usage_per_visit": {
            "audio_seconds": 0.0,
            "tokens_cached": 0.0,
            "tokens_in": 9.0,
            "tokens_out": 15.0
          },
          "wer": {
            "deletions": 0,
            "errors": 0,
            "insertions": 0,
            "rate": null,
            "reference_words": 0,
            "substitutions": 0
          }
        }
      ],
      "fallback": "synthetic-primary",
      "primary": "recorded-fallback",
      "purpose": "scribe_draft"
    },
    {
      "candidates": [
        {
          "candidate": {
            "adapter": "synthetic",
            "jurisdiction": "BR",
            "model": "synthetic-v1",
            "name": "synthetic-primary",
            "price": {
              "audio_micros": 0,
              "cached_micros": 0,
              "input_micros": 0,
              "output_micros": 0
            },
            "region": {
              "retrieved": "2026-10-29",
              "source": "apps/ai/adapters.py",
              "value": "br-synthetic",
              "verification": "verified"
            },
            "retention": {
              "retrieved": "2026-10-29",
              "source": "apps/ai/adapters.py",
              "value": "Offline memory only; no network, training or provider storage",
              "verification": "verified"
            }
          },
          "clinical_error_rates": {
            "allergy_handling": null,
            "chronology": null,
            "hallucination": null,
            "medication_name_dose": null,
            "negation": null,
            "significant_omission": null,
            "speaker_attribution": null,
            "units_decimals": null
          },
          "clinical_review_status": "pending_two_raters",
          "cost_per_visit_micros": 0.0,
          "eligible": true,
          "encounters": 4,
          "failures": {},
          "latency_ms": {
            "p50": 0.05256,
            "p95": 0.08866
          },
          "reasons": [],
          "samples": 12,
          "structured_output": {
            "rate": 1.0,
            "total": 12,
            "valid": 12
          },
          "usage_per_visit": {
            "audio_seconds": 0.0,
            "tokens_cached": 0.0,
            "tokens_in": 12.0,
            "tokens_out": 18.0
          },
          "wer": {
            "deletions": 0,
            "errors": 0,
            "insertions": 0,
            "rate": null,
            "reference_words": 0,
            "substitutions": 0
          }
        },
        {
          "candidate": {
            "adapter": "recorded_fixture",
            "jurisdiction": "BR",
            "model": "fixture-v1",
            "name": "recorded-fallback",
            "price": {
              "audio_micros": 0,
              "cached_micros": 0,
              "input_micros": 0,
              "output_micros": 0
            },
            "region": {
              "retrieved": "2026-10-29",
              "source": "apps/ai/adapters.py",
              "value": "br-synthetic-secondary",
              "verification": "verified"
            },
            "retention": {
              "retrieved": "2026-10-29",
              "source": "apps/ai/fixtures/v1.json",
              "value": "Static self-authored synthetic recording; no provider storage",
              "verification": "verified"
            }
          },
          "clinical_error_rates": {
            "allergy_handling": null,
            "chronology": null,
            "hallucination": null,
            "medication_name_dose": null,
            "negation": null,
            "significant_omission": null,
            "speaker_attribution": null,
            "units_decimals": null
          },
          "clinical_review_status": "pending_two_raters",
          "cost_per_visit_micros": 0.0,
          "eligible": false,
          "encounters": 4,
          "failures": {
            "invalid_output": 12
          },
          "latency_ms": {
            "p50": 0.058061,
            "p95": 0.07211
          },
          "reasons": [
            "failed_calls"
          ],
          "samples": 12,
          "structured_output": {
            "rate": 0.0,
            "total": 12,
            "valid": 0
          },
          "usage_per_visit": {
            "audio_seconds": 0.0,
            "tokens_cached": 0.0,
            "tokens_in": 0.0,
            "tokens_out": 0.0
          },
          "wer": {
            "deletions": 0,
            "errors": 0,
            "insertions": 0,
            "rate": null,
            "reference_words": 0,
            "substitutions": 0
          }
        }
      ],
      "fallback": null,
      "primary": "synthetic-primary",
      "purpose": "brief"
    },
    {
      "candidates": [
        {
          "candidate": {
            "adapter": "synthetic",
            "jurisdiction": "BR",
            "model": "synthetic-v1",
            "name": "synthetic-primary",
            "price": {
              "audio_micros": 0,
              "cached_micros": 0,
              "input_micros": 0,
              "output_micros": 0
            },
            "region": {
              "retrieved": "2026-10-29",
              "source": "apps/ai/adapters.py",
              "value": "br-synthetic",
              "verification": "verified"
            },
            "retention": {
              "retrieved": "2026-10-29",
              "source": "apps/ai/adapters.py",
              "value": "Offline memory only; no network, training or provider storage",
              "verification": "verified"
            }
          },
          "clinical_error_rates": {
            "allergy_handling": null,
            "chronology": null,
            "hallucination": null,
            "medication_name_dose": null,
            "negation": null,
            "significant_omission": null,
            "speaker_attribution": null,
            "units_decimals": null
          },
          "clinical_review_status": "pending_two_raters",
          "cost_per_visit_micros": 0.0,
          "eligible": true,
          "encounters": 4,
          "failures": {},
          "latency_ms": {
            "p50": 0.01291,
            "p95": 0.02184
          },
          "reasons": [],
          "samples": 12,
          "structured_output": {
            "rate": 1.0,
            "total": 12,
            "valid": 12
          },
          "usage_per_visit": {
            "audio_seconds": 0.0,
            "tokens_cached": 0.0,
            "tokens_in": 12.0,
            "tokens_out": 18.0
          },
          "wer": {
            "deletions": 0,
            "errors": 0,
            "insertions": 0,
            "rate": null,
            "reference_words": 0,
            "substitutions": 0
          }
        },
        {
          "candidate": {
            "adapter": "recorded_fixture",
            "jurisdiction": "BR",
            "model": "fixture-v1",
            "name": "recorded-fallback",
            "price": {
              "audio_micros": 0,
              "cached_micros": 0,
              "input_micros": 0,
              "output_micros": 0
            },
            "region": {
              "retrieved": "2026-10-29",
              "source": "apps/ai/adapters.py",
              "value": "br-synthetic-secondary",
              "verification": "verified"
            },
            "retention": {
              "retrieved": "2026-10-29",
              "source": "apps/ai/fixtures/v1.json",
              "value": "Static self-authored synthetic recording; no provider storage",
              "verification": "verified"
            }
          },
          "clinical_error_rates": {
            "allergy_handling": null,
            "chronology": null,
            "hallucination": null,
            "medication_name_dose": null,
            "negation": null,
            "significant_omission": null,
            "speaker_attribution": null,
            "units_decimals": null
          },
          "clinical_review_status": "pending_two_raters",
          "cost_per_visit_micros": 0.0,
          "eligible": true,
          "encounters": 4,
          "failures": {},
          "latency_ms": {
            "p50": 0.036091,
            "p95": 0.04268
          },
          "reasons": [],
          "samples": 12,
          "structured_output": {
            "rate": 1.0,
            "total": 12,
            "valid": 12
          },
          "usage_per_visit": {
            "audio_seconds": 0.0,
            "tokens_cached": 0.0,
            "tokens_in": 9.0,
            "tokens_out": 15.0
          },
          "wer": {
            "deletions": 0,
            "errors": 0,
            "insertions": 0,
            "rate": null,
            "reference_words": 0,
            "substitutions": 0
          }
        }
      ],
      "fallback": "synthetic-primary",
      "primary": "recorded-fallback",
      "purpose": "extraction"
    },
    {
      "candidates": [
        {
          "candidate": {
            "adapter": "synthetic",
            "jurisdiction": "BR",
            "model": "synthetic-v1",
            "name": "synthetic-primary",
            "price": {
              "audio_micros": 0,
              "cached_micros": 0,
              "input_micros": 0,
              "output_micros": 0
            },
            "region": {
              "retrieved": "2026-10-29",
              "source": "apps/ai/adapters.py",
              "value": "br-synthetic",
              "verification": "verified"
            },
            "retention": {
              "retrieved": "2026-10-29",
              "source": "apps/ai/adapters.py",
              "value": "Offline memory only; no network, training or provider storage",
              "verification": "verified"
            }
          },
          "clinical_error_rates": {
            "allergy_handling": null,
            "chronology": null,
            "hallucination": null,
            "medication_name_dose": null,
            "negation": null,
            "significant_omission": null,
            "speaker_attribution": null,
            "units_decimals": null
          },
          "clinical_review_status": "pending_two_raters",
          "cost_per_visit_micros": 0.0,
          "eligible": true,
          "encounters": 4,
          "failures": {},
          "latency_ms": {
            "p50": 0.01004,
            "p95": 0.01396
          },
          "reasons": [],
          "samples": 12,
          "structured_output": {
            "rate": 1.0,
            "total": 12,
            "valid": 12
          },
          "usage_per_visit": {
            "audio_seconds": 0.0,
            "tokens_cached": 0.0,
            "tokens_in": 12.0,
            "tokens_out": 18.0
          },
          "wer": {
            "deletions": 0,
            "errors": 0,
            "insertions": 0,
            "rate": null,
            "reference_words": 0,
            "substitutions": 0
          }
        },
        {
          "candidate": {
            "adapter": "recorded_fixture",
            "jurisdiction": "BR",
            "model": "fixture-v1",
            "name": "recorded-fallback",
            "price": {
              "audio_micros": 0,
              "cached_micros": 0,
              "input_micros": 0,
              "output_micros": 0
            },
            "region": {
              "retrieved": "2026-10-29",
              "source": "apps/ai/adapters.py",
              "value": "br-synthetic-secondary",
              "verification": "verified"
            },
            "retention": {
              "retrieved": "2026-10-29",
              "source": "apps/ai/fixtures/v1.json",
              "value": "Static self-authored synthetic recording; no provider storage",
              "verification": "verified"
            }
          },
          "clinical_error_rates": {
            "allergy_handling": null,
            "chronology": null,
            "hallucination": null,
            "medication_name_dose": null,
            "negation": null,
            "significant_omission": null,
            "speaker_attribution": null,
            "units_decimals": null
          },
          "clinical_review_status": "pending_two_raters",
          "cost_per_visit_micros": 0.0,
          "eligible": true,
          "encounters": 4,
          "failures": {},
          "latency_ms": {
            "p50": 0.03095,
            "p95": 0.035481
          },
          "reasons": [],
          "samples": 12,
          "structured_output": {
            "rate": 1.0,
            "total": 12,
            "valid": 12
          },
          "usage_per_visit": {
            "audio_seconds": 0.0,
            "tokens_cached": 0.0,
            "tokens_in": 9.0,
            "tokens_out": 15.0
          },
          "wer": {
            "deletions": 0,
            "errors": 0,
            "insertions": 0,
            "rate": null,
            "reference_words": 0,
            "substitutions": 0
          }
        }
      ],
      "fallback": "synthetic-primary",
      "primary": "recorded-fallback",
      "purpose": "front_desk"
    },
    {
      "candidates": [
        {
          "candidate": {
            "adapter": "synthetic",
            "jurisdiction": "BR",
            "model": "synthetic-v1",
            "name": "synthetic-primary",
            "price": {
              "audio_micros": 0,
              "cached_micros": 0,
              "input_micros": 0,
              "output_micros": 0
            },
            "region": {
              "retrieved": "2026-10-29",
              "source": "apps/ai/adapters.py",
              "value": "br-synthetic",
              "verification": "verified"
            },
            "retention": {
              "retrieved": "2026-10-29",
              "source": "apps/ai/adapters.py",
              "value": "Offline memory only; no network, training or provider storage",
              "verification": "verified"
            }
          },
          "clinical_error_rates": {
            "allergy_handling": null,
            "chronology": null,
            "hallucination": null,
            "medication_name_dose": null,
            "negation": null,
            "significant_omission": null,
            "speaker_attribution": null,
            "units_decimals": null
          },
          "clinical_review_status": "pending_two_raters",
          "cost_per_visit_micros": 0.0,
          "eligible": true,
          "encounters": 4,
          "failures": {},
          "latency_ms": {
            "p50": 0.0103,
            "p95": 0.01337
          },
          "reasons": [],
          "samples": 12,
          "structured_output": {
            "rate": 1.0,
            "total": 12,
            "valid": 12
          },
          "usage_per_visit": {
            "audio_seconds": 0.0,
            "tokens_cached": 0.0,
            "tokens_in": 12.0,
            "tokens_out": 18.0
          },
          "wer": {
            "deletions": 0,
            "errors": 0,
            "insertions": 0,
            "rate": null,
            "reference_words": 0,
            "substitutions": 0
          }
        },
        {
          "candidate": {
            "adapter": "recorded_fixture",
            "jurisdiction": "BR",
            "model": "fixture-v1",
            "name": "recorded-fallback",
            "price": {
              "audio_micros": 0,
              "cached_micros": 0,
              "input_micros": 0,
              "output_micros": 0
            },
            "region": {
              "retrieved": "2026-10-29",
              "source": "apps/ai/adapters.py",
              "value": "br-synthetic-secondary",
              "verification": "verified"
            },
            "retention": {
              "retrieved": "2026-10-29",
              "source": "apps/ai/fixtures/v1.json",
              "value": "Static self-authored synthetic recording; no provider storage",
              "verification": "verified"
            }
          },
          "clinical_error_rates": {
            "allergy_handling": null,
            "chronology": null,
            "hallucination": null,
            "medication_name_dose": null,
            "negation": null,
            "significant_omission": null,
            "speaker_attribution": null,
            "units_decimals": null
          },
          "clinical_review_status": "pending_two_raters",
          "cost_per_visit_micros": 0.0,
          "eligible": true,
          "encounters": 4,
          "failures": {},
          "latency_ms": {
            "p50": 0.03139,
            "p95": 0.03682
          },
          "reasons": [],
          "samples": 12,
          "structured_output": {
            "rate": 1.0,
            "total": 12,
            "valid": 12
          },
          "usage_per_visit": {
            "audio_seconds": 0.0,
            "tokens_cached": 0.0,
            "tokens_in": 9.0,
            "tokens_out": 15.0
          },
          "wer": {
            "deletions": 0,
            "errors": 0,
            "insertions": 0,
            "rate": null,
            "reference_words": 0,
            "substitutions": 0
          }
        }
      ],
      "fallback": "synthetic-primary",
      "primary": "recorded-fallback",
      "purpose": "analyst"
    },
    {
      "candidates": [
        {
          "candidate": {
            "adapter": "synthetic",
            "jurisdiction": "BR",
            "model": "synthetic-v1",
            "name": "synthetic-primary",
            "price": {
              "audio_micros": 0,
              "cached_micros": 0,
              "input_micros": 0,
              "output_micros": 0
            },
            "region": {
              "retrieved": "2026-10-29",
              "source": "apps/ai/adapters.py",
              "value": "br-synthetic",
              "verification": "verified"
            },
            "retention": {
              "retrieved": "2026-10-29",
              "source": "apps/ai/adapters.py",
              "value": "Offline memory only; no network, training or provider storage",
              "verification": "verified"
            }
          },
          "clinical_error_rates": {
            "allergy_handling": null,
            "chronology": null,
            "hallucination": null,
            "medication_name_dose": null,
            "negation": null,
            "significant_omission": null,
            "speaker_attribution": null,
            "units_decimals": null
          },
          "clinical_review_status": "pending_two_raters",
          "cost_per_visit_micros": 0.0,
          "eligible": true,
          "encounters": 4,
          "failures": {},
          "latency_ms": {
            "p50": 0.1136,
            "p95": 0.153971
          },
          "reasons": [],
          "samples": 12,
          "structured_output": {
            "rate": 1.0,
            "total": 12,
            "valid": 12
          },
          "usage_per_visit": {
            "audio_seconds": 0.0,
            "tokens_cached": 0.0,
            "tokens_in": 12.0,
            "tokens_out": 18.0
          },
          "wer": {
            "deletions": 0,
            "errors": 0,
            "insertions": 0,
            "rate": null,
            "reference_words": 0,
            "substitutions": 0
          }
        },
        {
          "candidate": {
            "adapter": "recorded_fixture",
            "jurisdiction": "BR",
            "model": "fixture-v1",
            "name": "recorded-fallback",
            "price": {
              "audio_micros": 0,
              "cached_micros": 0,
              "input_micros": 0,
              "output_micros": 0
            },
            "region": {
              "retrieved": "2026-10-29",
              "source": "apps/ai/adapters.py",
              "value": "br-synthetic-secondary",
              "verification": "verified"
            },
            "retention": {
              "retrieved": "2026-10-29",
              "source": "apps/ai/fixtures/v1.json",
              "value": "Static self-authored synthetic recording; no provider storage",
              "verification": "verified"
            }
          },
          "clinical_error_rates": {
            "allergy_handling": null,
            "chronology": null,
            "hallucination": null,
            "medication_name_dose": null,
            "negation": null,
            "significant_omission": null,
            "speaker_attribution": null,
            "units_decimals": null
          },
          "clinical_review_status": "pending_two_raters",
          "cost_per_visit_micros": 0.0,
          "eligible": false,
          "encounters": 4,
          "failures": {
            "invalid_output": 12
          },
          "latency_ms": {
            "p50": 0.076581,
            "p95": 0.084351
          },
          "reasons": [
            "failed_calls"
          ],
          "samples": 12,
          "structured_output": {
            "rate": 0.0,
            "total": 12,
            "valid": 0
          },
          "usage_per_visit": {
            "audio_seconds": 0.0,
            "tokens_cached": 0.0,
            "tokens_in": 0.0,
            "tokens_out": 0.0
          },
          "wer": {
            "deletions": 0,
            "errors": 0,
            "insertions": 0,
            "rate": null,
            "reference_words": 0,
            "substitutions": 0
          }
        }
      ],
      "fallback": null,
      "primary": "synthetic-primary",
      "purpose": "decision_support"
    }
  ]
}
---
# Synthetic ASR/LLM benchmark

Engineering adapter measurements only; not real-world performance.
Clinical error rates are unknown pending blinded two-rater review (todo 47).
Latency is local adapter wall time, not streaming partial or provider SLA.
Cost is zero paid spend; units are measured per encounter, not vendor prices.
Live/provider/sandbox legs: BLOCKED-ON-EG (EG-1/2/3/5/11).
Manual documentation is required when no eligible fallback exists.

| Purpose | Primary | Fallback |
| --- | --- | --- |
| scribe_asr | synthetic-primary | recorded-fallback |
| scribe_draft | recorded-fallback | synthetic-primary |
| brief | synthetic-primary | manual |
| extraction | recorded-fallback | synthetic-primary |
| front_desk | recorded-fallback | synthetic-primary |
| analyst | recorded-fallback | synthetic-primary |
| decision_support | synthetic-primary | manual |

| Candidate | Eligibility | Reasons |
| --- | --- | --- |
| synthetic-primary (scribe_asr) | eligible | offline engineering only |
| recorded-fallback (scribe_asr) | eligible | offline engineering only |
| synthetic-primary (scribe_draft) | eligible | offline engineering only |
| recorded-fallback (scribe_draft) | eligible | offline engineering only |
| synthetic-primary (brief) | eligible | offline engineering only |
| recorded-fallback (brief) | não elegível | failed_calls |
| synthetic-primary (extraction) | eligible | offline engineering only |
| recorded-fallback (extraction) | eligible | offline engineering only |
| synthetic-primary (front_desk) | eligible | offline engineering only |
| recorded-fallback (front_desk) | eligible | offline engineering only |
| synthetic-primary (analyst) | eligible | offline engineering only |
| recorded-fallback (analyst) | eligible | offline engineering only |
| synthetic-primary (decision_support) | eligible | offline engineering only |
| recorded-fallback (decision_support) | não elegível | failed_calls |

## Protocol and scope

This is a versioned engineering comparison, not clinical validation or a vendor
performance claim. The machine-consumed JSON front matter is valid YAML and is
validated by apps/ai/benchmark.schema.json. Run with the lane environment:

`uv run --frozen --no-sync --no-env-file python manage.py ai_benchmark --report <new-path>`

The closed runner uses todo 47's engineering-v1 corpus, hashed audio fixtures,
request builder, shared output gateway, WER alignment and clinical error taxonomy.
Every candidate sees all 12 cases in four encounter clusters for each of the
seven supported inference purposes. The corpus includes accents, noise,
caregiver/child, interruptions, conflicting records, mic failure, decimal commas,
ambiguous abbreviations, wrong-patient attachments, stale permissions, malicious
content and negation. These are adapter contract probes, not evidence that a
workflow resisted every attack or that a fake understood a case.

The protocol is fixed and hashed before dispatch. Report WER only for ASR;
structured-output validity means the existing gateway's checks, not semantic
clinical correctness. Plain-text purposes retain the gateway's bounded-text
contract. Latencies use local monotonic wall time per call and nearest-rank
p50/p95. Treat failed ASR calls as empty hypotheses (all reference words deleted).
Include failed calls, use no retries, and report timeout, unavailable and
invalid-output counts separately. Local adapter timings do not estimate remote
streaming partial latency or draft-after-stop latency. Usage sums validated
responses and divides by four encounters; failed responses have unknown usage.
Fake prices are zero micro-BRL, and actual paid spend is zero. Vendor price,
exchange rate, ASR-minute and LLM-token inputs for todo 74 remain UNVERIFIED
variables, not these zero fixture prices. The 15% retry and 2x burst scenarios
belong in the economics model, not extra calls in this experiment.

All eight clinical error rates are null, not zero. No clinician adjudication is
fabricated. Blinded two-rater review plus adjudication, kappa and uncertainty
come from todo 47. Approval-bound real-corpus results remain BLOCKED-ON-EG-3/11.
WER alone cannot authorize a clinical configuration. The fake ASR has high WER
because it returns a fixed review-only string, not a transcription model.

Eligible means offline engineering only: sourced, dated region and retention
terms, BR jurisdiction matching the region registry, and no failed calls.
Candidates are ranked by failed-call count, ASR word errors, cost, then stable
candidate name. Latency is reported but is not a tie-breaker for near-zero local
fake timings. Missing or unverified terms are labeled ineligible; foreign-region
fallbacks are rejected even when they claim BR jurisdiction. A rejected fallback
becomes manual documentation, never a silent cross-region route. The recording
fails chart/decision-support schemas, so those purposes deliberately have no
eligible automatic fallback.

## Provider proposals presented for owner review

The runnable selections in front matter are synthetic. The following PV proposals
are research selections only (selected_in_plan), not measured vendor results:

| Supported purpose | Proposed primary | Proposed fallback | Region/retention/account eligibility |
| --- | --- | --- | --- |
| scribe_asr | Amazon Transcribe sa-east-1 | Azure Speech Brazil South | Public region descriptions verified [S1,S3]; account policy and terms UNVERIFIED; nao elegivel for live |
| scribe_draft | Bedrock sa-east-1 in-region only | Azure OpenAI Brazil South regional only | Exact model and account availability/retention UNVERIFIED; nao elegivel for live |
| brief | Bedrock sa-east-1 in-region only | Azure OpenAI Brazil South regional only | Exact model and account availability/retention UNVERIFIED; nao elegivel for live |
| extraction | Bedrock sa-east-1 in-region only | Azure OpenAI Brazil South regional only | Exact model and account availability/retention UNVERIFIED; nao elegivel for live |
| front_desk | Bedrock sa-east-1 in-region only | Azure OpenAI Brazil South regional only | Exact model and account availability/retention UNVERIFIED; nao elegivel for live |
| analyst | Bedrock sa-east-1 in-region only | Azure OpenAI Brazil South regional only | Exact model and account availability/retention UNVERIFIED; nao elegivel for live |
| decision_support | Bedrock sa-east-1 in-region only | Azure OpenAI Brazil South regional only | Exact model and account availability/retention UNVERIFIED; additional EG-3/4/13 apply; nao elegivel for live |

No inference that AWS or Azure is clinically superior follows from offline
recordings. Alternatives in the PV table remain researched, not secretly routed.
Global/geo cross-region inference profiles are excluded. Public product docs are
not executed account settings, contractual DPAs, model entitlements or an owner
approval. The owner must approve the exact account/model/deployment and terms
before any provider test. Paid, real-provider, sandbox and live legs remain
BLOCKED-ON-EG-1/2/3/5/11, plus purpose-specific gates. No EG gate is satisfied here.

## Dated primary-source re-verification

Retrieved 2026-10-29 via public documentation only; no provider account/API calls.
"Verified" below applies only to the stated public text, never the deployment.

| Ref | Public fact and limits | Verification | Source |
| --- | --- | --- | --- |
| S1 | The Transcribe endpoint tables list Sao Paulo sa-east-1. pt-BR accuracy, account availability and negotiated retention are UNVERIFIED. | verified public text, 2026-10-29 | https://docs.aws.amazon.com/general/latest/gr/transcribe.html |
| S2 | Transcribe says voice inputs are used for service improvement by default and describes AWS Organizations opt-out. The actual account opt-out and contractual retention are UNVERIFIED. | verified public text, 2026-10-29 | https://docs.aws.amazon.com/transcribe/latest/dg/opt-out.html |
| S3 | Speech lists Brazil South and states processing/storage follows the Speech resource region, with a Voice Live exception. Voice Live is excluded. Resource configuration is UNVERIFIED. | verified public text, 2026-10-29 | https://learn.microsoft.com/en-us/azure/ai-services/speech-service/regions |
| S4 | Speech describes real-time server-memory processing without at-rest storage; batch input/output storage and TTL are customer-configured. No universal batch zero-retention claim is made. | verified public text, 2026-10-29 | https://learn.microsoft.com/en-us/azure/foundry/responsible-ai/speech-service/speech-to-text/data-privacy-security |
| S5 | Bedrock distinguishes in-region from geo/global inference routing. Exact model availability in sa-east-1 is UNVERIFIED for this account. | verified routing description, 2026-10-29 | https://docs.aws.amazon.com/bedrock/latest/userguide/models-region-compatibility.html |
| S6 | Bedrock retention varies by effective mode and model requirements; unconfigured regions inherit model defaults. Account/project/model retention settings are UNVERIFIED. | verified public text, 2026-10-29 | https://docs.aws.amazon.com/bedrock/latest/userguide/data-retention.html |
| S7 | Microsoft describes model data processing and optional stateful storage; training is not performed without permission/instruction. Brazil South model/deployment availability, monitoring and retention configuration remain UNVERIFIED. | verified public text, 2026-10-29 | https://learn.microsoft.com/en-us/azure/foundry/responsible-ai/openai/data-privacy |

Reverify these sources and bind the exact model, API, account, region, routing,
training opt-out, abuse-monitoring policy, storage TTL/deletion, DPA and price
before the separate owner-approved sandbox leg. A documentation change requires
a new dated report, not rewriting this record in place.

## Lifecycle publication

The export-only command does not query or mutate a database. With an explicit
`--record-selections`, the existing clinic_owner-only lifecycle writer appends
selected_in_plan versions for dedicated benchmark_<purpose>_<route> keys. These
keys hold the executable synthetic selections and cannot replace production
asr/llm_inference registry entries. They carry environment=synthetic, the report
reference and protocol hash; they create no approvals or activation records.
Only an owner decision can move an actual provider lifecycle beyond selection.
The existing todo 4 PV provider records remain research proposals, not authorized
by these benchmark records. This avoids overwriting an unrelated current provider
while recording per-purpose primary/fallback decisions for owner review.

