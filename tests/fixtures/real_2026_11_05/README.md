# Real data fixture: strike day with replacement coaches

Extract of the SNCF open data feeds of 5 November 2026, a national strike day
(timetable version 2026-11-04, realtime feeds fetched at 12:21 Paris time):
the trains and coaches between Toulouse Matabiau, Saint-Sulpice and
Rabastens - Couffouleux, their realtime updates and the alerts about them.
Many trains of the line were cancelled and replaced by coaches (`Car TER`,
trip ids `OCESN<number>R…`). Made with `tests/fixtures/make_real_fixtures.py`.

Source: SNCF Voyageurs, "Réseau SNCF TGV, Intercités et TER",
<https://transport.data.gouv.fr/datasets/horaires-sncf>.
Licence: Open Database License (ODbL) 1.0,
<https://opendatacommons.org/licenses/odbl/1-0/>.
