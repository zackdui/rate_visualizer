# Entity matching: platforms and health systems

How every platform and health system in `rates_tool_plan.md` §10 was matched to billing TINs in the BCBSTX data
(index 2026-08-20), and why. The result is the seed file **`configs/entity_tags_tx.csv`** (250 rows: 12 platform
TINs, 238 health-system TINs; each TIN has exactly one tag), which `build-site` will load as the `entity_tags` table.

## 1. Why simple name matching isn't enough
- **The TIN business name in BCBSTX files is unreliable.** The same TIN often shows an individual clinician's name in
  some files and the organisation's name in others (e.g. Talkiatry's TIN shows "MATTHEW KOLLER K" in 6 files, and
  THR's physician group shows "HARRIS METHODIST FORT WORTH MEDICAL FOUNDATION").
- **Organisations contract under legal names that differ from their brand.** Headway's Texas group is "New York
  Medical Behavioral Health Services, P.C."; Talkiatry's entities are "MCCD … Psychiatry Services"; Rula's is "Mental
  Health Specialty Group PA"; Methodist Health System's physician group is legally "MedHealth".
- **Brand words are shared.** Three unrelated Texas systems use "Methodist"; "Baptist" covers Tenet's San Antonio
  system and unrelated hospitals; "Baylor College of Medicine" is not Baylor Scott & White; many individuals are named
  Alma.

## 2. Evidence sources
For every TIN in the data, a name inventory was built from three sources (`match_method` in the seed file):

| `match_method` part | Source | What it is |
|---|---|---|
| `bcbstx_tin_name` | BCBSTX `provider_groups.tin.business_name` | The name BCBSTX attaches to the TIN (any of its variants) |
| `nppes_org_name` | NPPES legal name of each **Type 2 (organisation) NPI** listed in the TIN's provider groups | The organisation's registered legal name |
| `nppes_org_dba` | NPPES "other organization name" file (`othername_pfile`) for those NPIs | The organisation's registered "doing business as" names |
| `published_list` | The organisation's own published list of group name, NPI and TIN | Used for Headway |

The strongest link is an organisation's **own organisation NPI appearing inside a TIN's provider groups**: BCBSTX
lists the NPIs that bill under a TIN, and an organisation NPI registered as "Talkiatry" billing under a TIN ties that
TIN to Talkiatry. NPPES names were only accepted from organisations whose practice address is in Texas (except for the
national platforms).

## 3. Confidence rules
Applied automatically, then reviewed by hand for every TIN with 40+ NPIs (all review decisions are in the seed file's
`evidence` column):

| Confidence | Rule |
|---|---|
| `confirmed` | The organisation's own published list, or its own organisation NPI with its brand registered as the NPPES DBA (platforms) |
| `high` | The BCBSTX TIN name matches; or ≥ 50% of the organisation NPIs inside the TIN carry the system name; or the TIN has ≤ 3 organisation NPIs and they match |
| `medium` | Fewer than half of the TIN's organisation NPIs carry the name; or the entity is a facility/joint-venture type (surgery centre, imaging, urgent care, rehab, specialty heart/spine hospital), which is often jointly owned; or a legacy name |
| excluded | Reviewed and rejected (section 6) |

`relationship` column: `platform_entity`, `affiliated_location` (a branded location with its own TIN), `system`, or
`affiliate_jv`. Suggested default in the site: count `confirmed` + `high` as the entity; show `medium` with a marker.

## 4. Platforms
| Platform | TIN | NPIs | Confidence | How it was matched and why |
|---|---|---|---|---|
| **Headway** | **83-2675429** | 15,838 | confirmed | Headway's help center lists its Texas group as "New York Medical Behavioral Health Services, P.C.", NPI 1235600834, TIN 832675429 ([source](https://help.headway.co/hc/en-us/articles/21556524888596-Calling-your-insurance-company)). The TIN appears in 6 files with that name, and NPI 1235600834 is in its groups |
| **Talkiatry** | **88-2977235** | 176 | confirmed | Its groups contain NPI 1659003457, legal name "MCCD FL PSYCHIATRY SERVICES PA", NPPES DBA "Talkiatry". BCBSTX names the TIN "MCCD FL PSYCHIATRY SERVICES PA" in one file and "MATTHEW KOLLER K" in six |
| **Talkiatry** | **84-3213629** | 34 | confirmed | Its groups contain NPI 1033750823, "MCCD PSYCHIATRY SERVICES PLLC" (Talkiatry's parent entity, New York), NPPES DBA "Talkiatry". No Texas-specific Talkiatry entity exists in NPPES |
| Grow Therapy | 85-2938829 | 3,577 | high | TIN name "GROW HEALTHCARE GROUP PA" and its organisation NPI 1245845932 (Grow Therapy's clinical entity, Miami) in its groups |
| SonderMind | 47-5025949 | 2,622 | high | TIN name and organisation NPI 1760854442 "SONDERMIND PROVIDER NETWORK LLC" |
| SonderMind | 99-4151402 | 3 | high | "SONDERMIND PC", NPI 1003646977 |
| Rula | 86-2493019 | 2,592 | high | TIN name "MENTAL HEALTH SPECIALTY GROUP PA"; its groups contain NPI 1801472634 with NPPES DBA "Rula Health" |
| LifeStance Health | 26-4621796 | 929 | high | Its groups contain NPI 1659567238, NPPES DBA "LifeStance Health". BCBSTX shows individuals' names for this TIN |
| Thriveworks | 26-3447487 | 479 | high | Its groups contain NPI 1215181300, NPPES DBA "Thriveworks" |
| Thriveworks | 47-1744442 | 27 | medium | **Affiliated location.** "ROBERSON COUNSELING CENTER, PLLC", 7701 N Lamar Blvd, Austin, NPPES DBA "Thriveworks North Central Austin". A Thriveworks-branded location with its **own TIN and contract**, so it's tagged Thriveworks with `relationship = affiliated_location`. Its rates overlap the main Thriveworks TIN, but those values are BCBSTX standard fee-schedule steps shared by many practices, so that isn't evidence either way |
| Brightside | 83-2104126 | 488 | high | Its groups contain NPI 1801355482 "BRIGHTSIDE MEDICAL, P.C." |
| Cerebral | 83-4662816 | 190 | high | Its groups contain NPI 1992355879 "CEREBRAL MEDICAL GROUP, A PROFESSIONAL CORPORATION" |
| Alma | – | – | not found | 16 Alma-named NPPES organisations exist, but none appears in any TIN's groups. Alma clinicians most likely bill under their own TINs, which can't be told apart from other independent practices |

All platforms have professional dollar rates for all 9 codes.

## 5. Health systems
Patterns (Texas NPPES organisations or BCBSTX TIN names; exclusions in brackets):

| System | Names matched |
|---|---|
| Baylor Scott & White | "Baylor Scott & White", "Scott & White", "HealthTexas Provider Network", "Baylor University Medical Center", "Baylor All Saints", "Baylor Medical/Regional Medical Center", "Baylor Heart", "Baylor Institute for Rehab" [not "Baylor College"] |
| Texas Health Resources | "Texas Health Resources/Physicians Group/Presbyterian/Harris Methodist/Arlington Memorial/Hospital/Huguley/Heart & Vascular" |
| Memorial Hermann | "Memorial Hermann" |
| Houston Methodist | "Houston Methodist", "The Methodist Hospital", Houston-area "Methodist" campuses [not San Antonio/Dallas] |
| Methodist Health System (Dallas) | "Methodist Health System", "Methodist Dallas/Medical Group/Charlton/Richardson/Mansfield/Celina/Midlothian/Southlake" [not San Antonio/Houston] |
| CHRISTUS Health | "CHRISTUS", "Trinity Clinic" |
| Ascension Seton | "Ascension Seton/Texas/Medical Group", "Seton Healthcare/Family/Medical Center/…", "Dell Seton", "Dell Children's" |
| HCA Healthcare | "Medical City", "HCA Houston", "HCA", "St. David's", "Methodist Healthcare System (San Antonio)", San Antonio "Methodist Hospital" campuses, "Methodist Physician Practices" [not "Methodist Healthcare Ministries"] |
| Tenet | "Tenet", "Baptist Health System", "Baptist Medical Center", "North Central/Northeast/St. Luke's/Mission Trail/Resolute Baptist", "VHS San Antonio" [not "Baptist Hospitals of Southeast Texas"] |
| Covenant Health | "Covenant Health/Medical Center/Medical Group/Hospital/Children's/Specialty/High Plains/Levelland/Plainview" (Texas only) |

Main TINs (40+ NPIs; the seed file has all 238):

| System | TIN | NPIs | Confidence | Evidence / reasoning |
|---|---|---|---|---|
| Baylor Scott & White | 74-2958277 | 3,309 | high | All 10 organisation NPIs are Scott & White Clinic entities |
| Baylor Scott & White | 75-2536818 | 3,040 | high | 14 of 15 organisation NPIs are BSW (Baylor University Medical Center, HealthTexas Provider Network, One Medical with BSW) |
| Baylor Scott & White | 74-2967081 | 188 | high | BSW Medical Center - Hillcrest (BCBSTX name "Hillcrest Physician Services") |
| Baylor Scott & White | 74-1166904 | 164 | high | Scott & White Memorial Hospital / BSW Medical Center - Temple |
| Baylor Scott & White | 46-4007700, 74-1595711 | 76, 43 | high | BSW Llano / Marble Falls; BSW Taylor |
| Baylor Scott & White | 26-0845489, 75-2885038 | 231, 172 | medium | "BSW Urgent Care Plus" joint venture (legal "UCP Physicians of Central Texas"; also DBA NextCare) |
| Baylor Scott & White | 75-2254247 | 135 | medium | Mixed TIN: HeartPlace cardiology plus BSW All Saints |
| Baylor Scott & White | 41-2101361, 75-2951355 | 95, 66 | medium | BSW Heart Hospital; BSW Texas Spine & Joint (jointly owned specialty hospitals) |
| Texas Health Resources | 75-2613493 | 1,602 | high | Texas Health Physicians Group (legal "Harris Methodist Fort Worth Medical Foundation") |
| Memorial Hermann | 20-4923281 | 1,130 | high | Memorial Hermann Medical Group |
| Memorial Hermann | 84-4504483 | 413 | high | Memorial Hermann Hospital Based Physician Group |
| Memorial Hermann | 76-0385980 | 116 | high | Memorial Hermann Neighborhood Health Centers + MHS Physicians of Texas |
| Memorial Hermann | 88-1580452 | 216 | medium | Memorial Hermann-GoHealth Urgent Care (joint venture) |
| Houston Methodist | 33-2241086 | 1,691 | high | Houston Methodist Physician Organization / Specialty Physicians |
| Methodist Health System (Dallas) | 75-2896138 | 506 | high | Legal "MedHealth", NPPES DBA "Methodist Medical Group" (Dallas) |
| CHRISTUS Health | 75-2616977 | 2,089 | high | CHRISTUS Trinity Clinic (8 of 10 organisation NPIs) |
| CHRISTUS Health | 75-0818167, 75-1976930 | 429, 257 | high | Legal "Mother Frances Hospital (Tyler / Jacksonville)", NPPES DBA "CHRISTUS Mother Frances Hospital" |
| CHRISTUS Health | 75-0974351, 75-2796815 | 113, 92 | high | CHRISTUS Good Shepherd; CHRISTUS St. Michael |
| Ascension Seton | 26-4562522 | 1,291 | high (reviewed) | Seton Family of Doctors + Seton/UT Southwestern University Physicians; the other organisations are practices it absorbed |
| Ascension Seton | 74-2800601 | 588 | high (reviewed) | Dell Children's Medical Group (+ absorbed pediatric practices) |
| Ascension Seton | 74-1109643, 26-4562712 | 191, 142 | high | Ascension Seton hospitals/clinics; Seton Family of Doctors |
| HCA Healthcare | 26-2400924 | 303 | high | Methodist Physician Practices, PLLC (HCA's Methodist Healthcare, San Antonio) |
| HCA Healthcare | 74-2082653, 99-0675124, 26-3778482 | 228, 103, 87 | high | St. David's Heart & Vascular; HCA Houston OB Hospitalist; Medical City OB-GYN |
| HCA Healthcare | 38-3830353 | 92 | high (reviewed) | St. David's Ortho, Neuro and Rehab, PLLC is a physician group, not a rehab facility |
| HCA Healthcare | 62-1682198, 74-2730328 | 58, 46 | high | Medical City Dallas; Methodist Healthcare System of San Antonio |
| HCA Healthcare | 38-4007312 | 198 | medium | St. David's CareNow Urgent Care |
| Covenant Health | 75-2743883 | 500 | high (reviewed) | Covenant Medical Group, Covenant's main physician group (it also runs urgent care) |
| Covenant Health | 75-2426010 | 43 | high | Covenant Hospital Plainview |
| Tenet | (6 TINs, ≤ 8 NPIs each) | – | high/medium | Tenet Hospitals Limited (El Paso), VHS San Antonio (Baptist Health System), VHS San Antonio Imaging, Nacogdoches Medical Center; Valley Baptist (Harlingen/Brownsville, VHS) at medium as a Tenet-majority joint venture |

Rate coverage (confirmed + high TINs, professional dollar rates): every system has rates for all 9 codes except
Tenet (8 codes, 7 NPIs), because Tenet's Texas presence here is hospitals, not employed physician groups. Covenant has
only 45 NPIs with professional rates for these codes.

## 6. Exclusions and reassignments (reviewed)
| Candidate | TIN | Decision | Reason |
|---|---|---|---|
| CHRISTUS | 76-0459500 | excluded | TIN is **UT Physicians** (UTHealth Houston faculty practice, 3,325 NPIs); one stray "CHRISTUS St Joseph Hospital" organisation NPI in its groups |
| Tenet | 75-2668018 | excluded | TIN is **Texas Tech University HSC El Paso** (701 NPIs); "Tenet Hospitals Limited" is 1 of 21 organisations in its groups |
| Ascension Seton | 74-3001674 | excluded | TIN is **Lone Star Circle of Care**, an independent FQHC; one "Dell Children's-Circle of Care" DBA |
| BSW and CHRISTUS | 75-2661960 | excluded | "PCA-Primary Care Associates": a multi-organisation network TIN (1 of 8 organisations each) |
| Tenet | 46-5515662 | excluded | First Baptist Medical Center (Dallas) is not Tenet |
| Tenet | 41-2092141 | excluded | Surgery centre TIN also named "CHRISTUS Santa Rosa Physicians"; ownership unclear |
| Texas Health Resources | 87-3003947 | excluded | Pure Health transitional care is a tenant at Texas Health Presbyterian, not THR |
| CHRISTUS | 82-2231824 | excluded | Former CHRISTUS Dubuis LTAC, now LHC Group ("LHCG CXXI LLC") |
| Covenant | 45-4788430 | excluded | 1 NPI; TIN name "DFW Practice Consultants"; not Covenant Health (Lubbock) |
| Tenet → **BSW** | 74-1161944 | BSW only | Hillcrest Baptist Medical Center (Waco) is now Baylor Scott & White - Hillcrest (the TIN also matched BSW directly) |
| Tenet → **BSW** | 82-4052186 | BSW only | BCBSTX still names it "TENET FRISCO LTD" / "Centennial Medical Center"; its NPPES legal name is now "Baylor Scott & White Medical Center - Centennial" |
| CHRISTUS → **Houston Methodist** | 46-4389870, 46-4402004 | Houston Methodist only | BCBSTX still uses the old names "CHRISTUS St. John" / "CHRISTUS St. Catherine"; both hospitals are now Houston Methodist (Clear Lake, Continuing Care) |
| HCA → **Houston Methodist** | 76-0545192 | reassigned | "Methodist Healthcare System" here is Houston Methodist Sugar Land/Willowbrook/West |

Rule for legacy names: the organisation's **current NPPES registration wins** over an old BCBSTX name, and a TIN
gets only one tag. Other legacy names accepted: "Methodist Hospital Levelland" and "Methodist Children's Hospital" (Lubbock) are now
**Covenant**.

## 7. Caveats and upkeep
- Tags are **TIN-level**. A clinician employed by a system but billing under another TIN isn't tagged.
- System ownership and joint ventures change; re-check yearly, and re-run the matching each month to catch new TINs
  (platforms add state entities).
- The research used the 2026-08-20 BCBSTX index and the September 2026 NPPES file. NPI counts in the seed file are
  from that month.
- Everything above can be re-derived from `rates.duckdb` plus the NPPES zip. A future `tag-entities` command should
  regenerate candidates and diff them against `configs/entity_tags_tx.csv` for review.
