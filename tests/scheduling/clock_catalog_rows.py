"""Whole-row catalog normalisation; exclusions are physical/statistics only."""

# New PostgreSQL columns are included automatically. Do not turn this into a
# list of semantic columns: the omission plants specifically forbid that.
PHYSICAL_COLUMNS = {
    "pg_class": {
        "relfilenode": "Physical file identity changes on rewrites and TRUNCATE.",
        "reltablespace": "Storage placement does not change SQL expression semantics.",
        "relpages": "ANALYZE/VACUUM update the planner page estimate.",
        "reltuples": "ANALYZE/VACUUM update the planner row estimate.",
        "relallvisible": "VACUUM maintains the visibility-map page count.",
        "relfrozenxid": "VACUUM FREEZE advances the transaction freeze horizon.",
        "relminmxid": "VACUUM advances the multixact freeze horizon.",
        "relhasindex": "Lazy hint; pg_index contains the actual indexes.",
        "relhassubclass": "Lazy hint cleared by ANALYZE; pg_inherits has the edges.",
        "relhasrules": "Lazy rule-presence hint; pg_rewrite contains the actual rules.",
        "relhastriggers": "Lazy hint; pg_trigger contains actual triggers.",
        "relnatts": "Dropped-column slots; live pg_attribute rows define schema.",
        "relrewrite": "Transient physical rewrite relation identity.",
    },
    "pg_attribute": {
        "attstattarget": "Statistics collection target, not column semantics.",
        "attcacheoff": "Physical tuple-offset cache, not column semantics.",
        "attstorage": "Physical TOAST storage strategy; logical values are unchanged.",
        "attcompression": "Physical TOAST compression; logical values are unchanged.",
    },
}
