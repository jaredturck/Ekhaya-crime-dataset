import os
import sys
import re
import json
import calendar
from pathlib import Path
from datetime import date, datetime

import django
from django.apps import apps
import pandas as pd
from shapely.geometry import shape
from shapely.validation import make_valid


PROJECT_SETTINGS_MODULE = 'Ekhaya_crime_dataset.settings'
APP_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = APP_DIR.parent
RUNTIME_DIR = PROJECT_ROOT / 'runtime'

CAPE_TOWN_SUBURBS_FILE = 'CoCT_Management.txt'
SAPS_PRECINCTS_FILE = 'SAP_PoliceStations.txt'
SAPS_CRIME_STATS_FILE = '2025-2026_-_4th_Quarter_WEB.xlsx'

CAPE_TOWN_SUBURBS_SOURCE_URL = 'https://gis.westerncape.gov.za/server2/rest/services/SpatialDataWarehouse/CoCT_Management_Boundaries/MapServer/23/query?where=1%3D1&outFields=*&returnGeometry=true&outSR=4326&f=geojson'
SAPS_PRECINCTS_SOURCE_URL = 'https://gis.westerncape.gov.za/server2/rest/services/SpatialDataWarehouse/SAPS_PoliceStations/MapServer/3/query?where=1%3D1&outFields=*&returnGeometry=true&outSR=4326&f=geojson'
SAPS_CRIME_STATS_SOURCE_URL = 'https://www.saps.gov.za/services/crimestats.php'

EXCEL_SHEET_NAME = 'RAW Data'
EXCEL_HEADER_ROW = 2
IMPORT_PROVINCE = 'Western Cape'
AREA_TYPE = 'suburb'
MUNICIPALITY = 'City of Cape Town'
MIN_OVERLAP_PERCENT = 0.0
BULK_CREATE_BATCH_SIZE = 1000

MONTHS = {
    'january': 1,
    'february': 2,
    'march': 3,
    'april': 4,
    'may': 5,
    'june': 6,
    'july': 7,
    'august': 8,
    'september': 9,
    'october': 10,
    'november': 11,
    'december': 12,
}


def setup_django():
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))

    os.environ.setdefault('DJANGO_SETTINGS_MODULE', PROJECT_SETTINGS_MODULE)

    if not apps.ready:
        django.setup()


def normalize_name(value):
    value = '' if value is None else str(value)
    value = value.lower().replace("'", '')
    value = re.sub(r'[^a-z0-9]+', ' ', value)
    return value.strip()


def clean_text(value):
    if pd.isna(value):
        return ''
    return str(value).strip()


def clean_number(value):
    if pd.isna(value):
        return None
    return float(value)


def clean_int(value):
    if pd.isna(value):
        return None
    return int(value)


def clean_json_value(value):
    if pd.isna(value):
        return None
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if hasattr(value, 'item'):
        return value.item()
    return value


def clean_json_dict(values):
    clean_values = {}
    for key, value in values.items():
        clean_values[str(key)] = clean_json_value(value)
    return clean_values


def model_fields(model):
    fields = set()
    for field in model._meta.fields:
        fields.add(field.name)
    return fields


def model_object(model, values):
    fields = model_fields(model)
    clean_values = {}
    for key, value in values.items():
        if key in fields:
            clean_values[key] = value
    return model(**clean_values)


def create_model_object(model, values):
    fields = model_fields(model)
    clean_values = {}
    for key, value in values.items():
        if key in fields:
            clean_values[key] = value
    return model.objects.create(**clean_values)


def load_geojson(path):
    with open(path, 'r', encoding='utf-8') as file:
        return json.load(file)


def get_geometry(feature):
    geometry = shape(feature['geometry'])
    if not geometry.is_valid:
        geometry = make_valid(geometry)
    return geometry


def get_period_columns(columns):
    period_columns = []
    for column in columns:
        if not isinstance(column, str):
            continue
        clean_column = re.sub(r'\s+', ' ', column).strip()
        if re.match(r'^[A-Za-z]+ \d{4} to [A-Za-z]+ \d{4}$', clean_column):
            period_columns.append(column)
    return period_columns


def parse_period_name(period_name):
    clean_period_name = re.sub(r'\s+', ' ', str(period_name)).strip()
    match = re.match(r'^([A-Za-z]+) (\d{4}) to ([A-Za-z]+) (\d{4})$', clean_period_name)
    start_month = MONTHS[match.group(1).lower()]
    start_year = int(match.group(2))
    end_month = MONTHS[match.group(3).lower()]
    end_year = int(match.group(4))
    end_day = calendar.monthrange(end_year, end_month)[1]
    return clean_period_name, date(start_year, start_month, 1), date(end_year, end_month, end_day)


def create_warning(import_run, warning_type, message, source_file_name='', source_row=None, raw_values=None):
    from crime_data.models import ImportWarning

    ImportWarning.objects.create(
        import_run=import_run,
        warning_type=warning_type,
        message=message,
        source_file_name=source_file_name,
        source_row=source_row,
        raw_values=raw_values or {},
    )


def create_data_sources(import_run):
    from crime_data.models import DataSource

    suburb_source = DataSource.objects.create(
        name='Cape Town suburb polygons',
        source_file_name=CAPE_TOWN_SUBURBS_FILE,
        source_url=CAPE_TOWN_SUBURBS_SOURCE_URL,
        description='City of Cape Town official planning suburb polygons imported from GeoJSON.',
    )

    precinct_source = DataSource.objects.create(
        name='Western Cape SAPS precinct polygons',
        source_file_name=SAPS_PRECINCTS_FILE,
        source_url=SAPS_PRECINCTS_SOURCE_URL,
        description='Western Cape SAPS police precinct polygons imported from GeoJSON.',
    )

    crime_stats_source = DataSource.objects.create(
        name='SAPS crime statistics Excel',
        source_file_name=SAPS_CRIME_STATS_FILE,
        source_url=SAPS_CRIME_STATS_SOURCE_URL,
        description='SAPS detailed crime statistics Excel workbook.',
    )

    return suburb_source, precinct_source, crime_stats_source


def import_crime_areas(import_run, data_source):
    from crime_data.models import CrimeArea, PlaceAlias

    path = RUNTIME_DIR / CAPE_TOWN_SUBURBS_FILE
    data = load_geojson(path)
    areas = []

    for feature in data['features']:
        properties = feature['properties']
        area_name = clean_text(properties.get('OFC_SBRB_N'))

        if not area_name:
            create_warning(import_run, 'missing_area_name', 'GeoJSON suburb feature has no OFC_SBRB_N value.', CAPE_TOWN_SUBURBS_FILE, raw_values=properties)
            continue

        values = {
            'data_source': data_source,
            'import_run': import_run,
            'area_name': area_name,
            'normalized_name': normalize_name(area_name),
            'area_type': AREA_TYPE,
            'municipality': MUNICIPALITY,
            'province': IMPORT_PROVINCE,
            'source_object_id': clean_text(properties.get('OBJECTID')),
            'source_object_id_1': clean_text(properties.get('OBJECTID_1')),
            'shape_area': clean_number(properties.get('Shape_Area')),
            'shape_length': clean_number(properties.get('Shape_Length')),
            'shape_length_alt': clean_number(properties.get('SHAPE_Leng')),
            'geometry': feature['geometry'],
            'raw_properties': properties,
        }
        areas.append(model_object(CrimeArea, values))

    CrimeArea.objects.bulk_create(areas, batch_size=BULK_CREATE_BATCH_SIZE)

    aliases = []
    for area in CrimeArea.objects.filter(import_run=import_run):
        values = {
            'crime_area': area,
            'alias_name': area.area_name,
            'normalized_alias': area.normalized_name,
            'match_type': 'exact',
            'confidence': 'high',
            'reviewed': True,
            'notes': 'Automatically created from official area name during import.',
        }
        aliases.append(model_object(PlaceAlias, values))

    PlaceAlias.objects.bulk_create(aliases, batch_size=BULK_CREATE_BATCH_SIZE)
    return CrimeArea.objects.filter(import_run=import_run).count()


def import_saps_precincts(import_run, data_source):
    from crime_data.models import SapsPrecinct

    path = RUNTIME_DIR / SAPS_PRECINCTS_FILE
    data = load_geojson(path)
    precincts = []

    for feature in data['features']:
        properties = feature['properties']
        precinct_name = clean_text(properties.get('PolicePrec'))

        if not precinct_name:
            create_warning(import_run, 'missing_precinct_name', 'GeoJSON SAPS precinct feature has no PolicePrec value.', SAPS_PRECINCTS_FILE, raw_values=properties)
            continue

        values = {
            'data_source': data_source,
            'import_run': import_run,
            'precinct_name': precinct_name,
            'normalized_name': normalize_name(precinct_name),
            'component_name': clean_text(properties.get('COMPNT_NM')),
            'province': IMPORT_PROVINCE,
            'source_object_id': clean_text(properties.get('OBJECTID')),
            'source_create_date': clean_text(properties.get('CREATE_DT')),
            'shape_area': clean_number(properties.get('Shape_Area')),
            'shape_length': clean_number(properties.get('Shape_Length')),
            'geometry': feature['geometry'],
            'raw_properties': properties,
        }
        precincts.append(model_object(SapsPrecinct, values))

    SapsPrecinct.objects.bulk_create(precincts, batch_size=BULK_CREATE_BATCH_SIZE)
    return SapsPrecinct.objects.filter(import_run=import_run).count()


def build_area_precinct_matches(import_run):
    from crime_data.models import CrimeArea, SapsPrecinct, AreaPrecinctMatch

    area_records = []
    precinct_records = []

    for area in CrimeArea.objects.filter(import_run=import_run):
        area_records.append((area, get_geometry({'geometry': area.geometry})))

    for precinct in SapsPrecinct.objects.filter(import_run=import_run):
        precinct_records.append((precinct, get_geometry({'geometry': precinct.geometry})))

    matches = []

    for area, area_geometry in area_records:
        area_size = area_geometry.area
        area_matches = []

        for precinct, precinct_geometry in precinct_records:
            if not area_geometry.intersects(precinct_geometry):
                continue

            overlap = area_geometry.intersection(precinct_geometry)
            overlap_area = overlap.area

            if overlap_area <= 0:
                continue

            overlap_percent = (overlap_area / area_size) * 100

            if overlap_percent < MIN_OVERLAP_PERCENT:
                continue

            area_matches.append({
                'crime_area': area,
                'saps_precinct': precinct,
                'overlap_area': overlap_area,
                'overlap_percent': overlap_percent,
            })

        if not area_matches:
            create_warning(import_run, 'area_without_precinct_match', f'No SAPS precinct polygon overlap found for {area.area_name}.', CAPE_TOWN_SUBURBS_FILE, raw_values={'area_name': area.area_name})
            continue

        primary_match = max(area_matches, key=lambda item: item['overlap_percent'])

        for area_match in area_matches:
            values = {
                'import_run': import_run,
                'crime_area': area_match['crime_area'],
                'saps_precinct': area_match['saps_precinct'],
                'overlap_area': area_match['overlap_area'],
                'overlap_percent': area_match['overlap_percent'],
                'is_primary': area_match is primary_match,
                'match_method': 'geometry_overlap',
                'notes': '',
            }
            matches.append(model_object(AreaPrecinctMatch, values))

    AreaPrecinctMatch.objects.bulk_create(matches, batch_size=BULK_CREATE_BATCH_SIZE)
    return AreaPrecinctMatch.objects.filter(import_run=import_run).count()


def precinct_lookup(import_run):
    from crime_data.models import SapsPrecinct

    lookup = {}

    for precinct in SapsPrecinct.objects.filter(import_run=import_run):
        lookup[precinct.normalized_name] = precinct
        component_name = normalize_name(precinct.component_name)
        if component_name:
            lookup[component_name] = precinct

    return lookup


def import_saps_crime_metrics(import_run, data_source):
    from crime_data.models import SapsCrimeMetric

    path = RUNTIME_DIR / SAPS_CRIME_STATS_FILE
    df = pd.read_excel(path, sheet_name=EXCEL_SHEET_NAME, header=EXCEL_HEADER_ROW)
    df = df[df['Comp level'].astype(str).str.strip().str.lower() == 'station']
    df = df[df['Province'].astype(str).str.strip().str.lower() == IMPORT_PROVINCE.lower()]

    period_columns = get_period_columns(df.columns)
    lookup = precinct_lookup(import_run)
    metrics = []

    for index, row in df.iterrows():
        source_row = int(index) + EXCEL_HEADER_ROW + 2
        station_name = clean_text(row.get('Station'))
        normalized_station_name = normalize_name(station_name)
        precinct = lookup.get(normalized_station_name)
        raw_values = clean_json_dict(row.to_dict())

        if not precinct:
            create_warning(
                import_run,
                'unmatched_excel_station',
                f'No SAPS precinct polygon matched Excel station {station_name}.',
                SAPS_CRIME_STATS_FILE,
                source_row=source_row,
                raw_values=raw_values,
            )

        for period_column in period_columns:
            incidents = clean_int(row.get(period_column))

            if incidents is None:
                create_warning(
                    import_run,
                    'missing_incident_value',
                    f'Missing incident value for {station_name} / {clean_text(row.get("Crime_Category"))} / {period_column}.',
                    SAPS_CRIME_STATS_FILE,
                    source_row=source_row,
                    raw_values=raw_values,
                )
                continue

            period_name, period_start, period_end = parse_period_name(period_column)

            values = {
                'data_source': data_source,
                'import_run': import_run,
                'saps_precinct': precinct,
                'comp_level': clean_text(row.get('Comp level')),
                'station_name': station_name,
                'normalized_station_name': normalized_station_name,
                'station_crime_category': clean_text(row.get('Station Crime_Category')),
                'district': clean_text(row.get('District')),
                'province': clean_text(row.get('Province')),
                'crime_category': clean_text(row.get('Crime_Category')),
                'crime_code': clean_text(row.get('Code')),
                'crime_group': clean_text(row.get('Count offence group')),
                'count_direction': clean_text(row.get('Count direction')),
                'period_name': period_name,
                'period_type': 'quarter_total',
                'period_start': period_start,
                'period_end': period_end,
                'incidents': incidents,
                'crime_category_national_placement': clean_text(row.get('Crime_Category National contribution\nplacement')),
                'crime_category_provincial_placement': clean_text(row.get('Crime_Category Provincial contribution\nplacement')),
                'national_contribution_placement': clean_text(row.get('National contribution\nplacement')),
                'national_count_diff_placement': clean_text(row.get('National count diff\nplacement')),
                'provincial_contribution_placement': clean_text(row.get('Provincial contribution\nplacement')),
                'provincial_count_diff_placement': clean_text(row.get('Provincial count diff\nplacement')),
                'source_sheet': EXCEL_SHEET_NAME,
                'source_row': source_row,
                'source_column': period_name,
                'source_no': clean_text(row.get('No')),
                'raw_values': raw_values,
            }
            metrics.append(model_object(SapsCrimeMetric, values))

            if len(metrics) >= BULK_CREATE_BATCH_SIZE:
                SapsCrimeMetric.objects.bulk_create(metrics, batch_size=BULK_CREATE_BATCH_SIZE)
                metrics = []

    if metrics:
        SapsCrimeMetric.objects.bulk_create(metrics, batch_size=BULK_CREATE_BATCH_SIZE)

    return SapsCrimeMetric.objects.filter(import_run=import_run).count()


def import_crime_data():
    setup_django()

    from django.utils import timezone
    from crime_data.models import CrimeImportRun, ImportWarning

    import_run = CrimeImportRun.objects.create(
        status='running',
        notes='Importing Cape Town suburbs, Western Cape SAPS precincts, and SAPS crime statistics.',
    )

    suburb_source, precinct_source, crime_stats_source = create_data_sources(import_run)

    suburb_count = import_crime_areas(import_run, suburb_source)
    precinct_count = import_saps_precincts(import_run, precinct_source)
    area_precinct_match_count = build_area_precinct_matches(import_run)
    crime_metric_count = import_saps_crime_metrics(import_run, crime_stats_source)
    warning_count = ImportWarning.objects.filter(import_run=import_run).count()

    import_run.status = 'completed'
    import_run.suburb_count = suburb_count
    import_run.precinct_count = precinct_count
    import_run.area_precinct_match_count = area_precinct_match_count
    import_run.crime_metric_count = crime_metric_count
    import_run.warning_count = warning_count
    import_run.completed_at = timezone.now()
    import_run.save()

    print(f'Import completed: {import_run}')
    print(f'Suburbs: {suburb_count}')
    print(f'SAPS precincts: {precinct_count}')
    print(f'Area precinct matches: {area_precinct_match_count}')
    print(f'Crime metrics: {crime_metric_count}')
    print(f'Warnings: {warning_count}')


if __name__ == '__main__':
    import_crime_data()
