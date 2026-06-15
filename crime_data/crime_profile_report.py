import os
import sys
import json
import statistics
import threading
from pathlib import Path

import django
from django.apps import apps
from django.db.models import Max, Sum
from shapely.geometry import shape
from shapely.ops import unary_union
from shapely.validation import make_valid


PROJECT_SETTINGS_MODULE = 'Ekhaya_crime_dataset.settings'
APP_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = APP_DIR.parent

DEFAULT_TEST_LOCATION = 'Khayelitsha'
PERIOD_TYPE = 'quarter_total'
MIN_NEARBY_PRECINCTS = 5
MAX_NEARBY_PRECINCTS = 12
LOW_COUNT_TREND_THRESHOLD = 5

QWEN_MODEL_ID = 'Qwen/Qwen3-8B'
QWEN_MAX_NEW_TOKENS = 900
QWEN_RUNTIME_PROFILE = 'crime_report'


OVERALL_CATEGORIES = [
    '17 Community reported serious Crime',
]

DIRECT_PERSONAL_CATEGORIES = [
    'Contact crime (Crimes against the person)',
]

DIRECT_PERSONAL_SUPPORTING_CATEGORIES = [
    'Sexual offences',
]

RESIDENTIAL_SECURITY_CATEGORIES = [
    'Burglary at residential premises',
    'Robbery at residential premises',
]

RESIDENTIAL_SECURITY_SUPPORTING_CATEGORIES = [
    'Malicious damage to property',
]

VEHICLE_SECURITY_CATEGORIES = [
    'Theft of motor vehicle and motorcycle',
    'Theft out of or from motor vehicle',
    'Carjacking',
    'Truck hijacking',
]

PUBLIC_ROBBERY_CATEGORIES = [
    'Common robbery',
    'Robbery with aggravating circumstances',
    'Robbery at non-residential premises',
]

CORE_BASKETS = {
    'overall_safety': OVERALL_CATEGORIES,
    'direct_personal_safety': DIRECT_PERSONAL_CATEGORIES,
    'residential_security': RESIDENTIAL_SECURITY_CATEGORIES,
    'vehicle_security': VEHICLE_SECURITY_CATEGORIES,
    'public_robbery_signal': PUBLIC_ROBBERY_CATEGORIES,
}

BASKET_VALUE_CACHE = {}
GEOMETRY_CACHE = {}


def setup_django():
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.append(str(PROJECT_ROOT))
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', PROJECT_SETTINGS_MODULE)
    if not apps.ready:
        django.setup()


def normalize_name(value):
    import re

    value = '' if value is None else str(value)
    value = value.lower().replace("'", '')
    value = re.sub(r'[^a-z0-9]+', ' ', value)
    return value.strip()


def safe_round(value, digits=2):
    if value is None:
        return None
    return round(float(value), digits)


def percentage_change(latest_value, old_value):
    if old_value is None or old_value == 0:
        return None
    return ((latest_value - old_value) / old_value) * 100


def median_value(values):
    clean_values = [value for value in values if value is not None]
    if not clean_values:
        return None
    return statistics.median(clean_values)


def ratio_to_median(value, values):
    median = median_value(values)
    if median is None or median == 0:
        return None
    return value / median


def percentile_rank(value, values):
    clean_values = [item for item in values if item is not None]
    if value is None or not clean_values:
        return None

    less_count = 0
    equal_count = 0
    for item in clean_values:
        if item < value:
            less_count += 1
        elif item == value:
            equal_count += 1

    return ((less_count + (equal_count * 0.5)) / len(clean_values)) * 100


def concern_position_from_percentile(percentile):
    if percentile is None:
        return 'unknown'
    if percentile <= 20:
        return 'very_low_reported_concern'
    if percentile <= 40:
        return 'lower_reported_concern'
    if percentile <= 60:
        return 'typical_reported_concern'
    if percentile <= 80:
        return 'elevated_reported_concern'
    return 'high_reported_concern'


def concern_score_from_position(position):
    scores = {
        'very_low_reported_concern': 0,
        'lower_reported_concern': 1,
        'typical_reported_concern': 2,
        'elevated_reported_concern': 3,
        'high_reported_concern': 4,
        'unknown': None,
    }
    return scores.get(position)


def position_from_concern_score(score):
    if score is None:
        return 'unknown'
    if score <= 0.75:
        return 'very_low_reported_concern'
    if score <= 1.5:
        return 'lower_reported_concern'
    if score <= 2.5:
        return 'typical_reported_concern'
    if score <= 3.25:
        return 'elevated_reported_concern'
    return 'high_reported_concern'


def compare_target_to_nearby(target_value, nearby_values):
    percentile = percentile_rank(target_value, nearby_values)
    if percentile is None:
        return 'unknown'
    if percentile <= 25:
        return 'better_than_nearby_areas'
    if percentile <= 45:
        return 'somewhat_better_than_nearby_areas'
    if percentile <= 55:
        return 'similar_to_nearby_areas'
    if percentile <= 75:
        return 'somewhat_worse_than_nearby_areas'
    return 'worse_than_nearby_areas'


def latest_completed_import_run():
    from crime_data.models import CrimeImportRun

    return CrimeImportRun.objects.filter(status='completed').order_by('-completed_at', '-id').first()


def latest_period_start(import_run):
    from crime_data.models import SapsCrimeMetric

    value = SapsCrimeMetric.objects.filter(
        import_run=import_run,
        period_type=PERIOD_TYPE,
    ).aggregate(Max('period_start'))['period_start__max']
    return value


def period_name_for_start(import_run, period_start):
    from crime_data.models import SapsCrimeMetric

    metric = SapsCrimeMetric.objects.filter(
        import_run=import_run,
        period_type=PERIOD_TYPE,
        period_start=period_start,
    ).first()

    if metric:
        return metric.period_name
    return ''


def all_period_starts(import_run):
    from crime_data.models import SapsCrimeMetric

    values = SapsCrimeMetric.objects.filter(
        import_run=import_run,
        period_type=PERIOD_TYPE,
    ).values_list('period_start', flat=True).distinct().order_by('period_start')
    return list(values)


def find_crime_area(location, import_run):
    from crime_data.models import CrimeArea, PlaceAlias

    normalized_location = normalize_name(location)

    alias = PlaceAlias.objects.filter(
        crime_area__import_run=import_run,
        normalized_alias=normalized_location,
    ).select_related('crime_area').first()

    if alias:
        return alias.crime_area

    return CrimeArea.objects.filter(
        import_run=import_run,
        normalized_name=normalized_location,
    ).first()


def area_precinct_matches(crime_area, import_run):
    from crime_data.models import AreaPrecinctMatch

    return list(AreaPrecinctMatch.objects.filter(
        import_run=import_run,
        crime_area=crime_area,
    ).select_related('saps_precinct').order_by('-overlap_percent'))


def precinct_weights_from_matches(matches):
    total_overlap = 0
    for match in matches:
        total_overlap += match.overlap_percent

    if total_overlap == 0:
        return []

    precinct_weights = []
    for match in matches:
        weight = match.overlap_percent / total_overlap
        precinct_weights.append({
            'precinct': match.saps_precinct,
            'weight': weight,
            'overlap_percent': match.overlap_percent,
            'is_primary': match.is_primary,
        })

    return precinct_weights


def precinct_geometry(precinct):
    cache_key = precinct.id
    if cache_key in GEOMETRY_CACHE:
        return GEOMETRY_CACHE[cache_key]

    geometry = shape(precinct.geometry)
    if not geometry.is_valid:
        geometry = make_valid(geometry)

    GEOMETRY_CACHE[cache_key] = geometry
    return geometry


def target_geometry_from_weights(precinct_weights):
    geometries = []
    for item in precinct_weights:
        geometries.append(precinct_geometry(item['precinct']))
    if not geometries:
        return None
    return unary_union(geometries)


def all_precincts_for_run(import_run):
    from crime_data.models import SapsPrecinct

    return list(SapsPrecinct.objects.filter(import_run=import_run).order_by('precinct_name'))


def cape_town_linked_precincts(import_run):
    from crime_data.models import AreaPrecinctMatch, SapsPrecinct

    precinct_ids = AreaPrecinctMatch.objects.filter(import_run=import_run).values_list('saps_precinct_id', flat=True).distinct()
    return list(SapsPrecinct.objects.filter(id__in=precinct_ids).order_by('precinct_name'))


def nearby_precincts(import_run, precinct_weights):
    target_precinct_ids = [item['precinct'].id for item in precinct_weights]
    target_geometry = target_geometry_from_weights(precinct_weights)

    if target_geometry is None:
        return []

    adjacent = []
    distance_candidates = []
    target_centroid = target_geometry.centroid

    for precinct in all_precincts_for_run(import_run):
        if precinct.id in target_precinct_ids:
            continue

        geometry = precinct_geometry(precinct)

        if geometry.touches(target_geometry) or geometry.intersects(target_geometry):
            adjacent.append(precinct)

        distance = geometry.centroid.distance(target_centroid)
        distance_candidates.append((distance, precinct))

    if len(adjacent) >= MIN_NEARBY_PRECINCTS:
        return adjacent[:MAX_NEARBY_PRECINCTS]

    nearby = list(adjacent)
    known_ids = set([precinct.id for precinct in nearby])
    distance_candidates.sort(key=lambda item: item[0])

    for distance, precinct in distance_candidates:
        if precinct.id in known_ids:
            continue
        nearby.append(precinct)
        known_ids.add(precinct.id)
        if len(nearby) >= MIN_NEARBY_PRECINCTS:
            break

    return nearby[:MAX_NEARBY_PRECINCTS]


def basket_value_for_precinct(import_run, precinct, categories, period_start):
    from crime_data.models import SapsCrimeMetric

    cache_key = (
        import_run.id,
        precinct.id,
        tuple(categories),
        str(period_start),
    )

    if cache_key in BASKET_VALUE_CACHE:
        return BASKET_VALUE_CACHE[cache_key]

    result = SapsCrimeMetric.objects.filter(
        import_run=import_run,
        saps_precinct=precinct,
        period_type=PERIOD_TYPE,
        period_start=period_start,
        crime_category__in=categories,
    ).aggregate(Sum('incidents'))['incidents__sum']

    if result is None:
        result = 0

    BASKET_VALUE_CACHE[cache_key] = result
    return result


def basket_row_count_for_precinct(import_run, precinct, categories, period_start):
    from crime_data.models import SapsCrimeMetric

    return SapsCrimeMetric.objects.filter(
        import_run=import_run,
        saps_precinct=precinct,
        period_type=PERIOD_TYPE,
        period_start=period_start,
        crime_category__in=categories,
    ).count()


def weighted_basket_value(import_run, precinct_weights, categories, period_start):
    total = 0
    row_count = 0

    for item in precinct_weights:
        precinct = item['precinct']
        weight = item['weight']
        value = basket_value_for_precinct(import_run, precinct, categories, period_start)
        rows = basket_row_count_for_precinct(import_run, precinct, categories, period_start)
        total += value * weight
        row_count += rows

    return total, row_count


def basket_values_for_precincts(import_run, precincts, categories, period_start):
    values = []
    for precinct in precincts:
        value = basket_value_for_precinct(import_run, precinct, categories, period_start)
        values.append(value)
    return values


def basic_basket_analysis(import_run, precinct_weights, categories, latest_period_start_value, nearby, cape_town_precincts, province_precincts):
    target_value, row_count = weighted_basket_value(import_run, precinct_weights, categories, latest_period_start_value)
    cape_town_values = basket_values_for_precincts(import_run, cape_town_precincts, categories, latest_period_start_value)
    province_values = basket_values_for_precincts(import_run, province_precincts, categories, latest_period_start_value)
    nearby_values = basket_values_for_precincts(import_run, nearby, categories, latest_period_start_value)

    cape_town_percentile = percentile_rank(target_value, cape_town_values)
    province_percentile = percentile_rank(target_value, province_values)
    nearby_percentile = percentile_rank(target_value, nearby_values)

    return {
        'categories_used': list(categories),
        'target_value': safe_round(target_value),
        'metric_row_count': row_count,
        'cape_town_percentile': safe_round(cape_town_percentile),
        'western_cape_percentile': safe_round(province_percentile),
        'nearby_percentile': safe_round(nearby_percentile),
        'cape_town_median': safe_round(median_value(cape_town_values)),
        'western_cape_median': safe_round(median_value(province_values)),
        'nearby_median': safe_round(median_value(nearby_values)),
        'ratio_to_cape_town_median': safe_round(ratio_to_median(target_value, cape_town_values)),
        'ratio_to_nearby_median': safe_round(ratio_to_median(target_value, nearby_values)),
        'position': concern_position_from_percentile(cape_town_percentile),
        'nearby_position': compare_target_to_nearby(target_value, nearby_values),
        'comparison_sizes': {
            'cape_town_precincts': len(cape_town_values),
            'western_cape_precincts': len(province_values),
            'nearby_precincts': len(nearby_values),
        },
    }


def supporting_value(import_run, precinct_weights, categories, period_start):
    value, row_count = weighted_basket_value(import_run, precinct_weights, categories, period_start)
    return {
        'categories_used': list(categories),
        'target_value': safe_round(value),
        'metric_row_count': row_count,
    }


def composite_position(weighted_positions):
    total_weight = 0
    total_score = 0
    components = []

    for name, position, weight in weighted_positions:
        score = concern_score_from_position(position)
        components.append({
            'category': name,
            'position': position,
            'weight': weight,
            'score': score,
        })
        if score is None:
            continue
        total_score += score * weight
        total_weight += weight

    if total_weight == 0:
        return 'unknown', None, components

    score = total_score / total_weight
    return position_from_concern_score(score), score, components


def trend_for_basket(import_run, precinct_weights, categories, period_starts):
    values = []
    for period_start in period_starts:
        value, row_count = weighted_basket_value(import_run, precinct_weights, categories, period_start)
        values.append({
            'period_start': str(period_start),
            'value': safe_round(value),
            'metric_row_count': row_count,
        })

    if len(values) < 2:
        return {
            'categories_used': list(categories),
            'values_by_period': values,
            'trend_position': 'insufficient_periods',
        }

    latest = values[-1]['value']
    previous = values[-2]['value']
    previous_values = [item['value'] for item in values[:-1] if item['value'] is not None]
    average_previous = sum(previous_values) / len(previous_values) if previous_values else None

    change_previous = percentage_change(latest, previous)
    change_average = percentage_change(latest, average_previous)

    if average_previous is None or average_previous < LOW_COUNT_TREND_THRESHOLD:
        trend_position = 'insufficient_or_noisy_signal'
    elif change_average <= -15:
        trend_position = 'improving'
    elif change_average >= 15:
        trend_position = 'worsening'
    else:
        trend_position = 'stable'

    return {
        'categories_used': list(categories),
        'values_by_period': values,
        'latest_value': latest,
        'previous_value': previous,
        'average_previous_value': safe_round(average_previous),
        'change_from_previous_percent': safe_round(change_previous),
        'change_from_previous_average_percent': safe_round(change_average),
        'trend_position': trend_position,
    }


def confidence_level_from_components(geographic_confidence, metric_confidence, comparison_confidence, trend_confidence):
    values = [geographic_confidence, metric_confidence, comparison_confidence, trend_confidence]
    if 'low' in values:
        return 'low'
    if 'medium' in values:
        return 'medium'
    return 'high'


def geographic_confidence(matches):
    if not matches:
        return 'low'

    primary_overlap = matches[0].overlap_percent
    total_overlap = sum([match.overlap_percent for match in matches])

    if primary_overlap >= 85 and total_overlap >= 95:
        return 'high'
    if primary_overlap >= 60 and total_overlap >= 85:
        return 'medium'
    return 'low'


def metric_coverage_confidence(report_sections):
    required_names = [
        'overall_safety',
        'direct_personal_safety',
        'residential_security',
        'vehicle_security',
        'public_movement_safety',
    ]

    missing_count = 0
    for name in required_names:
        section = report_sections.get(name, {})
        if section.get('metric_row_count', 1) == 0:
            missing_count += 1

    if missing_count == 0:
        return 'high'
    if missing_count <= 2:
        return 'medium'
    return 'low'


def comparison_confidence(nearby_count, cape_town_count):
    if nearby_count >= MIN_NEARBY_PRECINCTS and cape_town_count >= 30:
        return 'high'
    if nearby_count >= 3 and cape_town_count >= 15:
        return 'medium'
    return 'low'


def trend_confidence(period_count):
    if period_count >= 4:
        return 'high'
    if period_count >= 2:
        return 'medium'
    return 'low'


def build_overall_safety(report_context):
    return basic_basket_analysis(
        report_context['import_run'],
        report_context['precinct_weights'],
        OVERALL_CATEGORIES,
        report_context['latest_period_start'],
        report_context['nearby_precincts'],
        report_context['cape_town_precincts'],
        report_context['province_precincts'],
    )


def build_direct_personal_safety(report_context):
    section = basic_basket_analysis(
        report_context['import_run'],
        report_context['precinct_weights'],
        DIRECT_PERSONAL_CATEGORIES,
        report_context['latest_period_start'],
        report_context['nearby_precincts'],
        report_context['cape_town_precincts'],
        report_context['province_precincts'],
    )
    section['supporting_indicators'] = {
        'sexual_offences': supporting_value(
            report_context['import_run'],
            report_context['precinct_weights'],
            DIRECT_PERSONAL_SUPPORTING_CATEGORIES,
            report_context['latest_period_start'],
        )
    }
    return section


def build_residential_security(report_context):
    section = basic_basket_analysis(
        report_context['import_run'],
        report_context['precinct_weights'],
        RESIDENTIAL_SECURITY_CATEGORIES,
        report_context['latest_period_start'],
        report_context['nearby_precincts'],
        report_context['cape_town_precincts'],
        report_context['province_precincts'],
    )
    section['supporting_indicators'] = {
        'malicious_damage_to_property': supporting_value(
            report_context['import_run'],
            report_context['precinct_weights'],
            RESIDENTIAL_SECURITY_SUPPORTING_CATEGORIES,
            report_context['latest_period_start'],
        )
    }
    return section


def build_vehicle_security(report_context):
    return basic_basket_analysis(
        report_context['import_run'],
        report_context['precinct_weights'],
        VEHICLE_SECURITY_CATEGORIES,
        report_context['latest_period_start'],
        report_context['nearby_precincts'],
        report_context['cape_town_precincts'],
        report_context['province_precincts'],
    )


def build_public_movement_safety(report_context, report_sections):
    public_robbery = basic_basket_analysis(
        report_context['import_run'],
        report_context['precinct_weights'],
        PUBLIC_ROBBERY_CATEGORIES,
        report_context['latest_period_start'],
        report_context['nearby_precincts'],
        report_context['cape_town_precincts'],
        report_context['province_precincts'],
    )

    direct_personal = report_sections.get('direct_personal_safety', {})
    vehicle = report_sections.get('vehicle_security', {})

    position, score, components = composite_position([
        ('public_robbery_signal', public_robbery.get('position'), 0.50),
        ('direct_personal_safety', direct_personal.get('position'), 0.35),
        ('vehicle_security', vehicle.get('position'), 0.15),
    ])

    public_robbery['position'] = position
    public_robbery['composite_score'] = safe_round(score)
    public_robbery['composite_components'] = components
    public_robbery['public_robbery_signal'] = {
        'position': concern_position_from_percentile(public_robbery.get('cape_town_percentile')),
        'target_value': public_robbery.get('target_value'),
        'nearby_position': public_robbery.get('nearby_position'),
        'categories_used': list(PUBLIC_ROBBERY_CATEGORIES),
    }
    return public_robbery


def build_solo_living_safety(report_sections):
    position, score, components = composite_position([
        ('direct_personal_safety', report_sections.get('direct_personal_safety', {}).get('position'), 0.35),
        ('residential_security', report_sections.get('residential_security', {}).get('position'), 0.30),
        ('public_movement_safety', report_sections.get('public_movement_safety', {}).get('position'), 0.25),
        ('vehicle_security', report_sections.get('vehicle_security', {}).get('position'), 0.10),
    ])

    return {
        'position': position,
        'composite_score': safe_round(score),
        'composite_components': components,
        'interpretation_basis': 'Composite assessment from direct personal safety, residential security, public movement safety, and vehicle security.',
    }


def build_family_safety(report_sections):
    position, score, components = composite_position([
        ('overall_safety', report_sections.get('overall_safety', {}).get('position'), 0.30),
        ('direct_personal_safety', report_sections.get('direct_personal_safety', {}).get('position'), 0.30),
        ('residential_security', report_sections.get('residential_security', {}).get('position'), 0.25),
        ('public_movement_safety', report_sections.get('public_movement_safety', {}).get('position'), 0.15),
    ])

    return {
        'position': position,
        'composite_score': safe_round(score),
        'composite_components': components,
        'interpretation_basis': 'Composite assessment from overall safety, direct personal safety, residential security, and public movement safety.',
    }


def build_nearby_safety_comparison(report_context, report_sections):
    comparisons = {}
    for name, categories in CORE_BASKETS.items():
        target_value, row_count = weighted_basket_value(
            report_context['import_run'],
            report_context['precinct_weights'],
            categories,
            report_context['latest_period_start'],
        )
        nearby_values = basket_values_for_precincts(
            report_context['import_run'],
            report_context['nearby_precincts'],
            categories,
            report_context['latest_period_start'],
        )
        comparisons[name] = {
            'categories_used': list(categories),
            'target_value': safe_round(target_value),
            'nearby_median': safe_round(median_value(nearby_values)),
            'nearby_percentile': safe_round(percentile_rank(target_value, nearby_values)),
            'ratio_to_nearby_median': safe_round(ratio_to_median(target_value, nearby_values)),
            'nearby_position': compare_target_to_nearby(target_value, nearby_values),
            'metric_row_count': row_count,
        }

    nearby_positions = [item['nearby_position'] for item in comparisons.values()]
    better_count = nearby_positions.count('better_than_nearby_areas') + nearby_positions.count('somewhat_better_than_nearby_areas')
    worse_count = nearby_positions.count('worse_than_nearby_areas') + nearby_positions.count('somewhat_worse_than_nearby_areas')

    if better_count > worse_count:
        overall_nearby_position = 'better_than_nearby_areas'
    elif worse_count > better_count:
        overall_nearby_position = 'worse_than_nearby_areas'
    else:
        overall_nearby_position = 'mixed_or_similar_to_nearby_areas'

    return {
        'position': overall_nearby_position,
        'nearby_precincts_used': [precinct.precinct_name for precinct in report_context['nearby_precincts']],
        'nearby_precinct_count': len(report_context['nearby_precincts']),
        'category_comparisons': comparisons,
    }


def build_safety_trend(report_context):
    sections = {}
    for name, categories in CORE_BASKETS.items():
        sections[name] = trend_for_basket(
            report_context['import_run'],
            report_context['precinct_weights'],
            categories,
            report_context['period_starts'],
        )

    trend_positions = [section.get('trend_position') for section in sections.values()]
    improving_count = trend_positions.count('improving')
    worsening_count = trend_positions.count('worsening')

    if improving_count > worsening_count:
        overall_trend_position = 'improving'
    elif worsening_count > improving_count:
        overall_trend_position = 'worsening'
    elif 'stable' in trend_positions:
        overall_trend_position = 'stable_or_mixed'
    else:
        overall_trend_position = 'insufficient_or_noisy_signal'

    return {
        'position': overall_trend_position,
        'periods_used': [str(period_start) for period_start in report_context['period_starts']],
        'category_trends': sections,
    }


def build_data_confidence_and_coverage(report_context, report_sections):
    matches = report_context['matches']
    primary_overlap = matches[0].overlap_percent if matches else None
    total_overlap = sum([match.overlap_percent for match in matches]) if matches else 0

    geo_confidence = geographic_confidence(matches)
    metric_confidence = metric_coverage_confidence(report_sections)
    compare_confidence = comparison_confidence(
        len(report_context['nearby_precincts']),
        len(report_context['cape_town_precincts']),
    )
    trend_conf = trend_confidence(len(report_context['period_starts']))
    overall_confidence = confidence_level_from_components(
        geo_confidence,
        metric_confidence,
        compare_confidence,
        trend_conf,
    )

    if overall_confidence == 'high':
        language_guidance = 'High confidence.'
    elif overall_confidence == 'medium':
        language_guidance = 'Medium confidence.'
    else:
        language_guidance = 'Low confidence.'

    return {
        'overall_report_confidence': overall_confidence,
        'language_guidance': language_guidance,
        'geographic_confidence': geo_confidence,
        'metric_coverage_confidence': metric_confidence,
        'comparison_confidence': compare_confidence,
        'trend_confidence': trend_conf,
        'primary_overlap_percent': safe_round(primary_overlap),
        'total_overlap_percent': safe_round(total_overlap),
        'linked_precinct_count': len(matches),
        'linked_precincts': [
            {
                'precinct_name': match.saps_precinct.precinct_name,
                'overlap_percent': safe_round(match.overlap_percent),
                'is_primary': match.is_primary,
            }
            for match in matches
        ],
        'nearby_precinct_count': len(report_context['nearby_precincts']),
        'cape_town_baseline_precinct_count': len(report_context['cape_town_precincts']),
        'western_cape_baseline_precinct_count': len(report_context['province_precincts']),
    }


def blank_report(location, message):
    return {
        'location': location,
        'status': 'not_available',
        'message': message,
        'sections': {
            'overall_safety': {},
            'direct_personal_safety': {},
            'residential_security': {},
            'vehicle_security': {},
            'public_movement_safety': {},
            'solo_living_safety': {},
            'family_safety': {},
            'nearby_safety_comparison': {},
            'safety_trend': {},
            'data_confidence_and_coverage': {},
        },
    }


def get_crime_report(location):
    setup_django()

    import_run = latest_completed_import_run()
    if not import_run:
        return blank_report(location, 'No completed crime import run found.')

    crime_area = find_crime_area(location, import_run)
    if not crime_area:
        return blank_report(location, 'No matching CrimeArea or PlaceAlias found for this location.')

    matches = area_precinct_matches(crime_area, import_run)
    if not matches:
        return blank_report(location, 'No AreaPrecinctMatch rows found for this location.')

    precinct_weights = precinct_weights_from_matches(matches)
    if not precinct_weights:
        return blank_report(location, 'Precinct matches exist, but overlap weights could not be calculated.')

    latest_start = latest_period_start(import_run)
    if not latest_start:
        return blank_report(location, 'No SAPS crime metrics with a latest period were found.')

    period_starts = all_period_starts(import_run)
    nearby = nearby_precincts(import_run, precinct_weights)
    cape_town_precincts = cape_town_linked_precincts(import_run)
    province_precincts = all_precincts_for_run(import_run)

    report_context = {
        'import_run': import_run,
        'crime_area': crime_area,
        'matches': matches,
        'precinct_weights': precinct_weights,
        'latest_period_start': latest_start,
        'latest_period_name': period_name_for_start(import_run, latest_start),
        'period_starts': period_starts,
        'nearby_precincts': nearby,
        'cape_town_precincts': cape_town_precincts,
        'province_precincts': province_precincts,
    }

    sections = {}
    sections['overall_safety'] = build_overall_safety(report_context)
    sections['direct_personal_safety'] = build_direct_personal_safety(report_context)
    sections['residential_security'] = build_residential_security(report_context)
    sections['vehicle_security'] = build_vehicle_security(report_context)
    sections['public_movement_safety'] = build_public_movement_safety(report_context, sections)
    sections['solo_living_safety'] = build_solo_living_safety(sections)
    sections['family_safety'] = build_family_safety(sections)
    sections['nearby_safety_comparison'] = build_nearby_safety_comparison(report_context, sections)
    sections['safety_trend'] = build_safety_trend(report_context)
    sections['data_confidence_and_coverage'] = build_data_confidence_and_coverage(report_context, sections)

    return {
        'location': crime_area.area_name,
        'status': 'available',
        'import_run_id': import_run.id,
        'period': report_context['latest_period_name'],
        'period_start': str(latest_start),
        'sections': sections,
    }



SECTION_ORDER = [
    ('OVERALL_SAFETY', 'overall_safety'),
    ('DIRECT_PERSONAL_SAFETY', 'direct_personal_safety'),
    ('RESIDENTIAL_SECURITY', 'residential_security'),
    ('VEHICLE_SECURITY', 'vehicle_security'),
    ('PUBLIC_MOVEMENT_SAFETY', 'public_movement_safety'),
    ('SOLO_LIVING_SAFETY', 'solo_living_safety'),
    ('FAMILY_SAFETY', 'family_safety'),
    ('NEARBY_SAFETY_COMPARISON', 'nearby_safety_comparison'),
    ('SAFETY_TREND', 'safety_trend'),
    ('DATA_CONFIDENCE_AND_COVERAGE', 'data_confidence_and_coverage'),
]

CATEGORY_DISPLAY_NAMES = {
    'overall_safety': 'Overall safety',
    'direct_personal_safety': 'Direct personal safety',
    'residential_security': 'Residential security',
    'vehicle_security': 'Vehicle security',
    'public_movement_safety': 'Public movement safety',
    'public_robbery_signal': 'Public-facing robbery signal',
    'solo_living_safety': 'Solo-living safety',
    'family_safety': 'Family safety',
}

POSITION_DISPLAY_NAMES = {
    'very_low_reported_concern': 'very low reported concern',
    'lower_reported_concern': 'lower reported concern',
    'typical_reported_concern': 'typical reported concern',
    'elevated_reported_concern': 'elevated reported concern',
    'high_reported_concern': 'high reported concern',
    'unknown': '',
}

NEARBY_POSITION_DISPLAY_NAMES = {
    'better_than_nearby_areas': 'better than nearby comparison areas',
    'somewhat_better_than_nearby_areas': 'somewhat better than nearby comparison areas',
    'similar_to_nearby_areas': 'similar to nearby comparison areas',
    'somewhat_worse_than_nearby_areas': 'somewhat worse than nearby comparison areas',
    'worse_than_nearby_areas': 'worse than nearby comparison areas',
    'mixed_or_similar_to_nearby_areas': 'mixed or similar to nearby comparison areas',
    'unknown': '',
}

TREND_DISPLAY_NAMES = {
    'improving': 'improving',
    'stable': 'stable',
    'worsening': 'worsening',
    'stable_or_mixed': 'stable or mixed',
    'insufficient_or_noisy_signal': 'insufficient or noisy signal',
    'insufficient_periods': 'insufficient comparison periods',
}


# RAG report rendering
# --------------------
# get_crime_report() returns a structured internal Python dictionary.
# format_crime_report_text() turns that dictionary into a clean evidence document.
# This text is intended to be passed to the language model as context, not as a
# list of task instructions. The system prompt should handle the actual label task.


def display_position(position):
    return POSITION_DISPLAY_NAMES.get(position, str(position).replace('_', ' '))


def display_nearby_position(position):
    return NEARBY_POSITION_DISPLAY_NAMES.get(position, str(position).replace('_', ' '))


def display_trend(position):
    return TREND_DISPLAY_NAMES.get(position, str(position).replace('_', ' '))


def display_category_name(name):
    return CATEGORY_DISPLAY_NAMES.get(name, str(name).replace('_', ' '))


def add_subsection(lines, title, body_lines):
    clean_lines = []
    for line in body_lines:
        if line is None:
            continue
        line = str(line).strip()
        if line:
            clean_lines.append(line)

    if not clean_lines:
        return

    lines.append(f'{title}:')
    for line in clean_lines:
        lines.append(f'- {line}')
    lines.append('')


def categories_lines(section):
    categories = section.get('categories_used') or []
    return [str(category) for category in categories]


def area_result_lines(section):
    lines = []
    position = section.get('position')
    target_value = section.get('target_value')

    if position:
        lines.append(f'Reported concern level: {display_position(position)}.')
    if target_value is not None:
        lines.append(f'Weighted reported incident estimate for the area: {target_value}.')

    return lines


def comparison_lines(section):
    lines = []
    cape_town_percentile = section.get('cape_town_percentile')
    ratio_to_cape_town = section.get('ratio_to_cape_town_median')
    western_cape_percentile = section.get('western_cape_percentile')
    nearby_position = section.get('nearby_position')
    nearby_percentile = section.get('nearby_percentile')
    ratio_to_nearby = section.get('ratio_to_nearby_median')

    if cape_town_percentile is not None:
        lines.append(f'Cape Town-linked precinct comparison: {cape_town_percentile}th percentile.')
    if ratio_to_cape_town is not None:
        lines.append(f'Cape Town-linked median ratio: {ratio_to_cape_town}.')
    if western_cape_percentile is not None:
        lines.append(f'Western Cape precinct comparison: {western_cape_percentile}th percentile.')
    if nearby_position:
        lines.append(f'Nearby comparison precinct result: {display_nearby_position(nearby_position)}.')
    if nearby_percentile is not None:
        lines.append(f'Nearby comparison percentile: {nearby_percentile}th percentile.')
    if ratio_to_nearby is not None:
        lines.append(f'Nearby median ratio: {ratio_to_nearby}.')

    return lines


def supporting_indicator_lines(section):
    indicators = section.get('supporting_indicators') or {}
    lines = []

    for name, values in indicators.items():
        if name == 'sexual_offences':
            display_name = 'Sexual offences'
        elif name == 'malicious_damage_to_property':
            display_name = 'Damage to property'
        else:
            display_name = display_category_name(name)

        categories = values.get('categories_used') or []
        target_value = values.get('target_value')
        category_text = ', '.join(categories)

        if category_text and target_value is not None:
            lines.append(f'{display_name}: {category_text}; weighted reported incident estimate: {target_value}.')
        elif category_text:
            lines.append(f'{display_name}: {category_text}.')
        elif target_value is not None:
            lines.append(f'{display_name}: weighted reported incident estimate: {target_value}.')

    return lines


def component_lines(section):
    components = section.get('composite_components') or []
    lines = []

    for item in components:
        name = display_category_name(item.get('category'))
        position = display_position(item.get('position'))
        weight = item.get('weight')
        if name and position and weight is not None:
            lines.append(f'{name}: {position}; assessment weight: {int(weight * 100)}%.')
        elif name and position:
            lines.append(f'{name}: {position}.')

    return lines


def strongest_and_weakest_component_lines(section):
    components = section.get('composite_components') or []
    strongest = []
    weakest = []

    for item in components:
        score = item.get('score')
        name = display_category_name(item.get('category'))
        position = display_position(item.get('position'))
        if score is None or not name or not position:
            continue
        line = f'{name}: {position}.'
        if score <= 1:
            strongest.append(line)
        elif score >= 3:
            weakest.append(line)

    return strongest, weakest


def format_standard_category_section(lines, section):
    add_subsection(lines, 'Main finding', area_result_lines(section))
    add_subsection(lines, 'Crime categories used', categories_lines(section))
    add_subsection(lines, 'Comparison evidence', comparison_lines(section))
    add_subsection(lines, 'Supporting evidence', supporting_indicator_lines(section))


def format_public_movement_section(lines, section):
    add_subsection(lines, 'Main finding', area_result_lines(section))
    add_subsection(lines, 'Crime categories used', categories_lines(section))
    add_subsection(lines, 'Composite evidence', component_lines(section))
    add_subsection(lines, 'Comparison evidence', comparison_lines(section))

    public_robbery_signal = section.get('public_robbery_signal') or {}
    public_signal_lines = []
    if public_robbery_signal:
        position = display_position(public_robbery_signal.get('position'))
        target_value = public_robbery_signal.get('target_value')
        nearby_position = display_nearby_position(public_robbery_signal.get('nearby_position'))
        categories = ', '.join(public_robbery_signal.get('categories_used') or [])
        if position:
            public_signal_lines.append(f'Public-facing robbery signal: {position}.')
        if target_value is not None:
            public_signal_lines.append(f'Weighted reported incident estimate for public-facing robbery categories: {target_value}.')
        if nearby_position:
            public_signal_lines.append(f'Nearby comparison precinct result for public-facing robbery categories: {nearby_position}.')
        if categories:
            public_signal_lines.append(f'Public-facing robbery categories used: {categories}.')
    add_subsection(lines, 'Public-facing robbery evidence', public_signal_lines)


def format_composite_section(lines, section):
    add_subsection(lines, 'Main finding', area_result_lines(section))
    add_subsection(lines, 'Composite evidence', component_lines(section))

    strongest, weakest = strongest_and_weakest_component_lines(section)
    add_subsection(lines, 'Strongest supporting signals', strongest)
    add_subsection(lines, 'Weakest supporting signals', weakest)

    basis = section.get('interpretation_basis')
    add_subsection(lines, 'Assessment basis', [basis])


def format_nearby_comparison_section(lines, section):
    position = section.get('position')
    main_lines = []
    if position:
        main_lines.append(f'Overall nearby comparison: {display_nearby_position(position)}.')
    add_subsection(lines, 'Main finding', main_lines)

    nearby_precincts = section.get('nearby_precincts_used') or []
    add_subsection(lines, 'Nearby comparison precincts', nearby_precincts)

    comparisons = section.get('category_comparisons') or {}
    better_lines = []
    similar_lines = []
    worse_lines = []

    for name, item in comparisons.items():
        display_name = display_category_name(name)
        nearby_position = item.get('nearby_position')
        ratio = item.get('ratio_to_nearby_median')
        percentile = item.get('nearby_percentile')
        line_parts = [f'{display_name}: {display_nearby_position(nearby_position)}']
        if percentile is not None:
            line_parts.append(f'{percentile}th nearby percentile')
        if ratio is not None:
            line_parts.append(f'nearby median ratio {ratio}')
        line = '; '.join(line_parts) + '.'

        if nearby_position in ['better_than_nearby_areas', 'somewhat_better_than_nearby_areas']:
            better_lines.append(line)
        elif nearby_position in ['worse_than_nearby_areas', 'somewhat_worse_than_nearby_areas']:
            worse_lines.append(line)
        else:
            similar_lines.append(line)

    add_subsection(lines, 'Better-than-nearby signals', better_lines)
    add_subsection(lines, 'Similar-to-nearby signals', similar_lines)
    add_subsection(lines, 'Worse-than-nearby signals', worse_lines)


def trend_line_for_item(name, item):
    trend = display_trend(item.get('trend_position'))
    latest_value = item.get('latest_value')
    previous_average = item.get('average_previous_value')
    change_average = item.get('change_from_previous_average_percent')

    parts = [f'{display_category_name(name)}: {trend}']
    if change_average is not None:
        parts.append(f'{change_average}% versus the previous-period average')
    if latest_value is not None and previous_average is not None:
        parts.append(f'latest value {latest_value}, previous average {previous_average}')
    return '; '.join(parts) + '.'


def format_safety_trend_section(lines, section):
    position = section.get('position')
    main_lines = []
    if position:
        main_lines.append(f'Overall trend: {display_trend(position)}.')
    add_subsection(lines, 'Main finding', main_lines)

    periods = section.get('periods_used') or []
    add_subsection(lines, 'Periods compared', periods)

    trends = section.get('category_trends') or {}
    improving_lines = []
    stable_lines = []
    worsening_lines = []

    for name, item in trends.items():
        trend = item.get('trend_position')
        line = trend_line_for_item(name, item)
        if trend == 'improving':
            improving_lines.append(line)
        elif trend == 'worsening':
            worsening_lines.append(line)
        else:
            stable_lines.append(line)

    add_subsection(lines, 'Improving signals', improving_lines)
    add_subsection(lines, 'Stable or mixed signals', stable_lines)
    add_subsection(lines, 'Worsening signals', worsening_lines)


def format_confidence_section(lines, section):
    main_lines = []
    confidence = section.get('overall_report_confidence')
    if confidence:
        main_lines.append(f'Overall confidence: {confidence}.')
    add_subsection(lines, 'Main finding', main_lines)

    geographic_lines = []
    geographic_confidence = section.get('geographic_confidence')
    primary_overlap = section.get('primary_overlap_percent')
    total_overlap = section.get('total_overlap_percent')
    linked_count = section.get('linked_precinct_count')
    linked_precincts = section.get('linked_precincts') or []

    if geographic_confidence:
        geographic_lines.append(f'Geographic match confidence: {geographic_confidence}.')
    if primary_overlap is not None:
        geographic_lines.append(f'Primary precinct overlap: {primary_overlap}%.')
    if total_overlap is not None:
        geographic_lines.append(f'Total matched precinct overlap: {total_overlap}%.')
    if linked_count is not None:
        geographic_lines.append(f'Linked precinct count: {linked_count}.')
    for item in linked_precincts:
        precinct_name = item.get('precinct_name')
        overlap = item.get('overlap_percent')
        if precinct_name and overlap is not None:
            geographic_lines.append(f'{precinct_name}: {overlap}% overlap.')
    add_subsection(lines, 'Geographic match evidence', geographic_lines)

    coverage_lines = []
    metric_confidence = section.get('metric_coverage_confidence')
    if metric_confidence:
        coverage_lines.append(f'Crime data coverage confidence: {metric_confidence}.')
    add_subsection(lines, 'Crime data coverage', coverage_lines)

    comparison_lines = []
    comparison_confidence = section.get('comparison_confidence')
    nearby_count = section.get('nearby_precinct_count')
    cape_town_count = section.get('cape_town_baseline_precinct_count')
    western_cape_count = section.get('western_cape_baseline_precinct_count')
    if comparison_confidence:
        comparison_lines.append(f'Comparison confidence: {comparison_confidence}.')
    if nearby_count is not None:
        comparison_lines.append(f'Nearby comparison precinct count: {nearby_count}.')
    if cape_town_count is not None:
        comparison_lines.append(f'Cape Town-linked baseline precinct count: {cape_town_count}.')
    if western_cape_count is not None:
        comparison_lines.append(f'Western Cape baseline precinct count: {western_cape_count}.')
    add_subsection(lines, 'Comparison coverage', comparison_lines)

    trend_lines = []
    trend_confidence_value = section.get('trend_confidence')
    if trend_confidence_value:
        trend_lines.append(f'Trend confidence: {trend_confidence_value}.')
    add_subsection(lines, 'Trend coverage', trend_lines)


def format_section_for_llm(lines, heading, section_key, section):
    lines.append(f'[{heading}]')

    if not section:
        lines.append('')
        return

    if section_key in ['overall_safety', 'direct_personal_safety', 'residential_security', 'vehicle_security']:
        format_standard_category_section(lines, section)
    elif section_key == 'public_movement_safety':
        format_public_movement_section(lines, section)
    elif section_key in ['solo_living_safety', 'family_safety']:
        format_composite_section(lines, section)
    elif section_key == 'nearby_safety_comparison':
        format_nearby_comparison_section(lines, section)
    elif section_key == 'safety_trend':
        format_safety_trend_section(lines, section)
    elif section_key == 'data_confidence_and_coverage':
        format_confidence_section(lines, section)

    if lines and lines[-1] != '':
        lines.append('')


def add_report_context(lines, report):
    location = report.get('location')
    status = report.get('status')
    period = report.get('period')
    period_start = report.get('period_start')

    if location:
        lines.append(f'Area being assessed: {location}')
    if period:
        lines.append(f'Crime period used: {period}')
    elif period_start:
        lines.append(f'Crime period start: {period_start}')
    if status and status != 'available':
        lines.append(f'Report status: {status}')

    lines.append('')
    lines.append('Interpretation notes:')
    lines.append('- Percentiles compare reported incidents against other precincts. Lower percentiles mean fewer reported incidents in the comparison group.')
    lines.append('- Median ratios compare reported incidents against the median of the comparison group. Lower ratios mean fewer reported incidents than the comparison median.')
    lines.append('- Area values are weighted from linked SAPS precincts using geographic overlap percentages.')
    lines.append('')


def format_crime_report_text(report):
    lines = []
    add_report_context(lines, report)

    sections = report.get('sections', {})
    for heading, section_key in SECTION_ORDER:
        format_section_for_llm(lines, heading, section_key, sections.get(section_key, {}))

    return '\n'.join(lines).strip() + '\n'



CRIME_LABEL_SYSTEM_PROMPT = (
    'You are given a crime report about one area.\n',
    'Rewrite the report into a rich, searchable place safety profile.\n',
    'Keep the same section headings and order.\n',
    'Under each heading, write compact full sentences that synthesize the evidence in that section.\n',
    'Use the report evidence only.\n',
    'Return only the headings and profile text.\n'
)

_qwen_crime_label_model = None
_qwen_crime_label_model_lock = threading.Lock()


class QwenCrimeLabelModel:
    """Load Qwen3 8B for crime-label generation."""

    def __init__(self):
        from transformers import AutoTokenizer, AutoModelForCausalLM, BitsAndBytesConfig
        import torch

        self.torch = torch
        self.tokenizer = AutoTokenizer.from_pretrained(QWEN_MODEL_ID)
        self.tokenizer.padding_side = 'left'

        model_kwargs = {
            'dtype': 'auto',
            'device_map': self.get_device_map(),
        }

        if torch.cuda.is_available():
            model_kwargs['quantization_config'] = BitsAndBytesConfig(load_in_8bit=True)

        self.model = AutoModelForCausalLM.from_pretrained(
            QWEN_MODEL_ID,
            **model_kwargs,
        )
        self.model.eval()

        if self.tokenizer.pad_token_id is None:
            self.tokenizer.pad_token = self.tokenizer.eos_token

        self.model.config.pad_token_id = self.tokenizer.pad_token_id
        self.generation_lock = threading.RLock()
        self.print_loaded_device()

    def get_device_map(self):
        if self.torch.cuda.is_available():
            return {'': 'cuda:0'}
        return {'': 'cpu'}

    def get_input_device(self):
        device_map = getattr(self.model, 'hf_device_map', None)

        if isinstance(device_map, dict):
            devices = {
                str(device)
                for device in device_map.values()
                if str(device) not in ('disk', '')
            }
            if len(devices) == 1:
                return self.torch.device(next(iter(devices)))

        model_device = getattr(self.model, 'device', None)
        if model_device is not None:
            return model_device

        return next(self.model.parameters()).device

    def print_loaded_device(self):
        device = self.get_input_device()
        print(f'[qwen] loaded {QWEN_MODEL_ID} for crime labels on {device}')

    def build_chat_prompt(self, system_prompt, crime_report_text):
        messages = [
            {'role': 'system', 'content': str(system_prompt or '').strip()},
            {'role': 'user', 'content': str(crime_report_text or '').strip()},
        ]
        return self.tokenizer.apply_chat_template(
            messages,
            tokenize=False,
            add_generation_prompt=True,
            enable_thinking=False,
        )

    def generate_labels(self, crime_report_text, max_new_tokens=QWEN_MAX_NEW_TOKENS):
        prompt = self.build_chat_prompt(CRIME_LABEL_SYSTEM_PROMPT, crime_report_text)
        inputs = self.tokenizer([prompt], padding=True, return_tensors='pt')
        inputs = inputs.to(self.get_input_device())
        input_length = inputs['input_ids'].shape[1]

        with self.generation_lock:
            with self.torch.no_grad():
                outputs = self.model.generate(
                    **inputs,
                    max_new_tokens=max_new_tokens,
                    do_sample=False,
                    pad_token_id=self.tokenizer.pad_token_id,
                )

        answer_ids = outputs[0][input_length:]
        answer = self.tokenizer.decode(
            answer_ids,
            skip_special_tokens=True,
            clean_up_tokenization_spaces=False,
        )
        return clean_model_output(answer)


def clean_model_output(text):
    text = str(text or '').strip()

    if text.startswith('<think>') and '</think>' in text:
        text = text.split('</think>', 1)[1].strip()

    return text


def get_qwen_crime_label_model():
    global _qwen_crime_label_model

    if _qwen_crime_label_model is None:
        with _qwen_crime_label_model_lock:
            if _qwen_crime_label_model is None:
                _qwen_crime_label_model = QwenCrimeLabelModel()

    return _qwen_crime_label_model


def generate_crime_labels_from_report(crime_report_text, max_new_tokens=QWEN_MAX_NEW_TOKENS):
    model = get_qwen_crime_label_model()
    return model.generate_labels(crime_report_text, max_new_tokens=max_new_tokens)


def print_crime_report(location):
    report = get_crime_report(location)
    print(format_crime_report_text(report))


def print_crime_report_and_labels(location):
    report = get_crime_report(location)
    crime_report_text = format_crime_report_text(report)

    print('===== CRIME REPORT RAG DOCUMENT =====')
    print(crime_report_text)

    if report.get('status') != 'available':
        print('===== QWEN LABEL OUTPUT =====')
        print('')
        return

    labels = generate_crime_labels_from_report(crime_report_text)

    print('===== QWEN LABEL OUTPUT =====')
    print(labels)
    print('')


if __name__ == '__main__':
    print_crime_report_and_labels(DEFAULT_TEST_LOCATION)
