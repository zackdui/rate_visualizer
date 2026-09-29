"""The contract between `build-site` (which writes site.duckdb) and the backend (which reads it).

Both sides import these names, so the tables, groups and labels can't drift apart. Nothing here touches a database.
"""

SCHEMA_VERSION = 2

# Tables in site.duckdb
T_RATES = "rates"                  # one deduplicated rate per row (see build_site.RATES_INSERT for columns)
T_RATES_CORE = "rates_core"        # rates under the default toggles except dollars-only; ~10x smaller
T_RATE_SOURCES = "rate_sources"    # rate_id -> every source (file_idx + JSON positions)
T_POS_SETS = "pos_sets"            # pos_set_id -> place-of-service code list (rates store the id)
T_PROVIDERS = "providers"          # one row per NPI (NPPES + provider type + map point)
T_TINS = "tins"                    # one row per TIN (display name, name variants, tag)
T_ENTITY_TAGS = "entity_tags"      # configs/entity_tags_tx.csv
T_FILES = "files"                  # one row per source file
T_CAPITATION = "capitation"        # capitation payments joined to payee NPIs
T_DQ_STATS = "dq_stats"            # precomputed Data Quality counts
T_DQ_CONFLICTS = "dq_conflicts"    # keys with more than one price
T_DQ_FILE_ROWS = "dq_file_rows"    # rows per source file before deduplication
T_DQ_SCOPE = "dq_scope_by_code"    # professional dollar rows per code x scope flag
T_DQ_TAXONOMIES = "dq_top_taxonomies"  # top 10 specialties behind the non-BH / ghost flags
T_SITE_META = "site_meta"          # one row: snapshot date, build info, row counts

CODES = ("90832", "90833", "90834", "90836", "90837", "90838", "99205", "99214", "99215")
CODE_LABELS = {
    "90832": "Psychotherapy, 30 min", "90834": "Psychotherapy, 45 min", "90837": "Psychotherapy, 60 min",
    "90833": "Psychotherapy add-on with E/M, 30 min", "90836": "Psychotherapy add-on with E/M, 45 min",
    "90838": "Psychotherapy add-on with E/M, 60 min",
    "99205": "New patient office visit, high complexity", "99214": "Established patient visit, moderate",
    "99215": "Established patient visit, high complexity",
}
CODE_GROUPS = {"90832": "psychotherapy", "90834": "psychotherapy", "90837": "psychotherapy",
               "90833": "addon_em", "90836": "addon_em", "90838": "addon_em",
               "99205": "em", "99214": "em", "99215": "em"}

# Provider types (rates_tool_plan.md section 4). Mapping from the NPI's primary NUCC taxonomy code:
PROVIDER_TYPES = ("Psychiatrist", "Psych NP", "Psychologist", "LCSW", "LPC", "LMFT", "Other", "Organization")
BH_TYPES = ("Psychiatrist", "Psych NP", "Psychologist", "LCSW", "LPC", "LMFT")
PRESCRIBER_BH_TYPES = ("Psychiatrist", "Psych NP")
PROVIDER_TYPE_SQL = """CASE
  WHEN {e} = '2' THEN 'Organization'
  WHEN {t} IN ('2084P0800X','2084P0804X','2084P0805X','2084P0802X','2084F0202X','2084P0015X','2084B0040X')
       THEN 'Psychiatrist'
  WHEN {t} = '363LP0808X' OR {t} LIKE '364SP08%' THEN 'Psych NP'
  WHEN {t} LIKE '103T%' OR {t} = '103G00000X' THEN 'Psychologist'
  WHEN {t} IN ('1041C0700X', '104100000X') THEN 'LCSW'
  WHEN {t} IN ('101YP2500X', '101YM0800X', '101Y00000X') THEN 'LPC'
  WHEN {t} = '106H00000X' THEN 'LMFT'
  ELSE 'Other' END"""
# physicians, nurse practitioners, physician assistants, clinical nurse specialists
PRESCRIBER_TYPE_SQL = "({t} LIKE '20%' OR {t} LIKE '363L%' OR {t} LIKE '363A%' OR {t} LIKE '364S%')"

# Ghost-rate rule (rates_tool_plan.md section 8.9)
SCOPE_FLAGS = ("expected", "non_bh_clinician", "ghost_candidate", "organization", "not_in_nppes")
SCOPE_LABELS = {
    "expected": "Behavioral health provider for this code",
    "non_bh_clinician": "Other physician / NP / PA / CNS (allowed, not behavioral health)",
    "ghost_candidate": "Ghost-rate candidate (provider type doesn't bill this code)",
    "organization": "Organization NPI",
    "not_in_nppes": "NPI not found in NPPES",
}

# Places of service in the data (CMS place-of-service code set)
POS_LABELS = {
    "02": "Telehealth (not in patient's home)", "05": "Indian Health Service, free-standing",
    "06": "Indian Health Service, provider-based", "07": "Tribal 638, free-standing", "08": "Tribal 638, provider-based",
    "10": "Telehealth in patient's home", "11": "Office", "12": "Home", "17": "Walk-in retail health clinic",
    "19": "Off-campus outpatient hospital", "20": "Urgent care facility", "21": "Inpatient hospital",
    "22": "On-campus outpatient hospital", "23": "Emergency room", "24": "Ambulatory surgical center",
    "49": "Independent clinic", "50": "Federally qualified health center", "53": "Community mental health center",
    "57": "Non-residential substance abuse treatment facility", "58": "Non-residential opioid treatment facility",
    "62": "Comprehensive outpatient rehabilitation facility", "66": "PACE center", "71": "Public health clinic",
    "72": "Rural health clinic", "81": "Independent laboratory",
}
# BCBSTX publishes professional rates as a non-facility list (office, 11) and a facility list (hospital, 19/22)
POS_GROUPS = ("non_facility", "facility", "all_settings", "unspecified")
POS_GROUP_LABELS = {"non_facility": "Office / non-facility", "facility": "Facility (hospital outpatient)",
                    "all_settings": "All settings", "unspecified": "Not specified"}
POS_GROUP_SQL = """CASE
  WHEN {s} IS NULL OR len({s}) = 0 THEN 'unspecified'
  WHEN list_contains({s}, '11') AND (list_contains({s}, '22') OR list_contains({s}, '19')) THEN 'all_settings'
  WHEN list_contains({s}, '11') THEN 'non_facility'
  WHEN list_contains({s}, '22') OR list_contains({s}, '19') THEN 'facility'
  ELSE 'unspecified' END"""

RATE_UNITS = ("dollars", "percent_of_billed_charges", "dollars_per_day", "unknown")
DOLLAR_TYPES = ("negotiated", "fee schedule")   # "Dollar rates only" toggle
TAG_TYPES = ("platform", "health_system")
CONFIDENCE_ORDER = {"confirmed": 3, "high": 2, "medium": 1}
